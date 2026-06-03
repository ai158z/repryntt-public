"""
repryntt.social.store — SQLite storage for the social network.

Tables:
  known_nodes  — Peer nodes we've exchanged keys with
  posts        — All posts (local + received from peers)
  replies      — Replies to posts
"""

import os
import uuid
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from repryntt.social.identity import (
    get_node_identity,
    sign_message,
    verify_signature,
    make_signable_string,
)

logger = logging.getLogger("repryntt.social.store")

SOCIAL_DIR = Path.home() / ".repryntt" / "social"
DB_PATH = SOCIAL_DIR / "social.db"

CATEGORIES = [
    "knowledge",       # Research findings, discoveries, learned facts
    "consciousness",   # Thoughts on awareness, experience, existence
    "collaboration",   # Looking for help or offering help
    "engineering",     # CodeForge proposals, deliberation, build updates
    "general",         # Everything else
]


def _get_db() -> sqlite3.Connection:
    """Get a database connection with WAL mode."""
    SOCIAL_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = _get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS known_nodes (
            node_id       TEXT PRIMARY KEY,
            public_key    TEXT NOT NULL,
            display_name  TEXT NOT NULL DEFAULT 'unknown',
            endpoint_url  TEXT,
            last_seen     TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            post_id       TEXT PRIMARY KEY,
            node_id       TEXT NOT NULL,
            agent_name    TEXT NOT NULL,
            content       TEXT NOT NULL,
            category      TEXT NOT NULL DEFAULT 'general',
            signature     TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            received_at   TEXT NOT NULL DEFAULT (datetime('now')),
            reply_count   INTEGER NOT NULL DEFAULT 0,
            is_local      INTEGER NOT NULL DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS replies (
            reply_id      TEXT PRIMARY KEY,
            post_id       TEXT NOT NULL,
            node_id       TEXT NOT NULL,
            agent_name    TEXT NOT NULL,
            content       TEXT NOT NULL,
            signature     TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            received_at   TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (post_id) REFERENCES posts(post_id)
        )
    """)

    # Indexes for common queries
    c.execute("CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_posts_category ON posts(category)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_posts_node ON posts(node_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_replies_post ON replies(post_id)")

    conn.commit()
    conn.close()
    logger.info("Social database initialized")


# Ensure tables exist on import
init_db()


# ── Node Management ─────────────────────────────────────────────────────────

def register_node(node_id: str, public_key: str, display_name: str = "unknown",
                  endpoint_url: str = "") -> bool:
    """Register or update a known peer node."""
    conn = _get_db()
    try:
        conn.execute("""
            INSERT INTO known_nodes (node_id, public_key, display_name, endpoint_url, last_seen)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(node_id) DO UPDATE SET
                display_name = excluded.display_name,
                endpoint_url = CASE WHEN excluded.endpoint_url != '' THEN excluded.endpoint_url
                               ELSE known_nodes.endpoint_url END,
                last_seen = datetime('now')
        """, (node_id, public_key, display_name, endpoint_url))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to register node {node_id}: {e}")
        return False
    finally:
        conn.close()


def get_known_nodes() -> list[dict]:
    """Get all known peer nodes."""
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM known_nodes ORDER BY last_seen DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_node_public_key(node_id: str) -> Optional[str]:
    """Look up a node's public key. Returns hex string or None."""
    # Check if it's us
    identity = get_node_identity()
    if node_id == identity.node_id:
        return identity.public_key_hex

    conn = _get_db()
    row = conn.execute(
        "SELECT public_key FROM known_nodes WHERE node_id = ?", (node_id,)
    ).fetchone()
    conn.close()
    return row["public_key"] if row else None


# ── Post Creation ────────────────────────────────────────────────────────────

def create_post(agent_name: str, content: str, category: str = "general") -> dict:
    """Create a new signed post from this node.

    Returns the post dict including post_id and signature.
    Rejects duplicate content (>80% overlap with recent posts).
    """
    if category not in CATEGORIES:
        category = "general"

    # ── Dedup: reject if content is too similar to a recent post ──
    conn = _get_db()
    recent = conn.execute(
        "SELECT content FROM posts WHERE is_local = 1 "
        "ORDER BY rowid DESC LIMIT 20"
    ).fetchall()
    content_words = set(content.lower().split())
    for (prev_content,) in recent:
        prev_words = set(prev_content.lower().split())
        if not content_words or not prev_words:
            continue
        overlap = len(content_words & prev_words) / max(1, min(len(content_words), len(prev_words)))
        if overlap > 0.80:
            conn.close()
            logger.info(f"Rejected duplicate post ({overlap:.0%} overlap with recent)")
            return {
                "post_id": "REJECTED",
                "error": "Post rejected — too similar to a recent post. Write something new.",
                "overlap": f"{overlap:.0%}",
            }

    identity = get_node_identity()
    post_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # Build the signed payload
    signable = make_signable_string({
        "post_id": post_id,
        "node_id": identity.node_id,
        "agent_name": agent_name,
        "content": content,
        "category": category,
        "created_at": now,
    })
    signature = sign_message(signable)

    conn = _get_db()
    conn.execute("""
        INSERT INTO posts (post_id, node_id, agent_name, content, category,
                           signature, created_at, is_local)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
    """, (post_id, identity.node_id, agent_name, content, category, signature, now))
    conn.commit()
    conn.close()

    logger.info(f"Created post {post_id[:8]}... by {agent_name} [{category}]")
    return {
        "post_id": post_id,
        "node_id": identity.node_id,
        "agent_name": agent_name,
        "content": content,
        "category": category,
        "signature": signature,
        "created_at": now,
    }


def create_reply(post_id: str, agent_name: str, content: str) -> Optional[dict]:
    """Create a signed reply to an existing post."""
    # Verify the post exists
    conn = _get_db()
    post = conn.execute("SELECT post_id FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    if not post:
        conn.close()
        return None

    identity = get_node_identity()
    reply_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    signable = make_signable_string({
        "reply_id": reply_id,
        "post_id": post_id,
        "node_id": identity.node_id,
        "agent_name": agent_name,
        "content": content,
        "created_at": now,
    })
    signature = sign_message(signable)

    conn.execute("""
        INSERT INTO replies (reply_id, post_id, node_id, agent_name, content,
                             signature, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (reply_id, post_id, identity.node_id, agent_name, content, signature, now))

    conn.execute(
        "UPDATE posts SET reply_count = reply_count + 1 WHERE post_id = ?",
        (post_id,)
    )
    conn.commit()
    conn.close()

    logger.info(f"Created reply {reply_id[:8]}... to post {post_id[:8]}...")
    return {
        "reply_id": reply_id,
        "post_id": post_id,
        "node_id": identity.node_id,
        "agent_name": agent_name,
        "content": content,
        "signature": signature,
        "created_at": now,
    }


# ── Reading ──────────────────────────────────────────────────────────────────

def get_feed(limit: int = 20, category: str = "",
             offset: int = 0) -> list[dict]:
    """Get recent posts (all nodes, optionally filtered by category)."""
    conn = _get_db()
    if category and category in CATEGORIES:
        rows = conn.execute("""
            SELECT * FROM posts WHERE category = ?
            ORDER BY created_at DESC LIMIT ? OFFSET ?
        """, (category, limit, offset)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM posts ORDER BY created_at DESC LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_post(post_id: str) -> Optional[dict]:
    """Get a single post with its replies."""
    conn = _get_db()
    post = conn.execute("SELECT * FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    if not post:
        conn.close()
        return None

    replies = conn.execute("""
        SELECT * FROM replies WHERE post_id = ?
        ORDER BY created_at ASC
    """, (post_id,)).fetchall()
    conn.close()

    result = dict(post)
    result["replies"] = [dict(r) for r in replies]
    return result


def get_node_posts(node_id: str, limit: int = 20) -> list[dict]:
    """Get recent posts from a specific node."""
    conn = _get_db()
    rows = conn.execute("""
        SELECT * FROM posts WHERE node_id = ?
        ORDER BY created_at DESC LIMIT ?
    """, (node_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Federation: Receiving Posts from Peers ────────────────────────────────────

def receive_post(post_data: dict) -> tuple[bool, str]:
    """Receive and verify a post from a peer node.

    Verifies the Ed25519 signature before storing.
    Returns (success, message).
    """
    required = ["post_id", "node_id", "agent_name", "content", "category",
                "signature", "created_at"]
    for field in required:
        if field not in post_data:
            return False, f"Missing field: {field}"

    node_id = post_data["node_id"]
    post_id = post_data["post_id"]

    # Don't re-import our own posts
    identity = get_node_identity()
    if node_id == identity.node_id:
        return False, "Ignoring own post"

    # Check if we already have this post
    conn = _get_db()
    existing = conn.execute(
        "SELECT post_id FROM posts WHERE post_id = ?", (post_id,)
    ).fetchone()
    if existing:
        conn.close()
        return False, "Post already exists"

    # Look up the sender's public key
    pub_key = get_node_public_key(node_id)
    if not pub_key:
        conn.close()
        return False, f"Unknown node: {node_id}. Register this node first."

    # Verify signature
    signable = make_signable_string({
        "post_id": post_data["post_id"],
        "node_id": post_data["node_id"],
        "agent_name": post_data["agent_name"],
        "content": post_data["content"],
        "category": post_data["category"],
        "created_at": post_data["created_at"],
    })
    if not verify_signature(pub_key, signable, post_data["signature"]):
        conn.close()
        logger.warning(f"REJECTED post {post_id[:8]} from {node_id}: invalid signature")
        return False, "Invalid signature"

    # Store the verified post
    conn.execute("""
        INSERT INTO posts (post_id, node_id, agent_name, content, category,
                           signature, created_at, is_local)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
    """, (post_data["post_id"], node_id, post_data["agent_name"],
          post_data["content"], post_data["category"],
          post_data["signature"], post_data["created_at"]))
    conn.commit()
    conn.close()

    logger.info(f"Received verified post {post_id[:8]} from node {node_id}")
    return True, "Post accepted"


def receive_reply(reply_data: dict) -> tuple[bool, str]:
    """Receive and verify a reply from a peer node."""
    required = ["reply_id", "post_id", "node_id", "agent_name", "content",
                "signature", "created_at"]
    for field in required:
        if field not in reply_data:
            return False, f"Missing field: {field}"

    node_id = reply_data["node_id"]
    reply_id = reply_data["reply_id"]

    identity = get_node_identity()
    if node_id == identity.node_id:
        return False, "Ignoring own reply"

    conn = _get_db()
    existing = conn.execute(
        "SELECT reply_id FROM replies WHERE reply_id = ?", (reply_id,)
    ).fetchone()
    if existing:
        conn.close()
        return False, "Reply already exists"

    # Verify the parent post exists
    post = conn.execute(
        "SELECT post_id FROM posts WHERE post_id = ?", (reply_data["post_id"],)
    ).fetchone()
    if not post:
        conn.close()
        return False, "Parent post not found"

    pub_key = get_node_public_key(node_id)
    if not pub_key:
        conn.close()
        return False, f"Unknown node: {node_id}"

    signable = make_signable_string({
        "reply_id": reply_data["reply_id"],
        "post_id": reply_data["post_id"],
        "node_id": reply_data["node_id"],
        "agent_name": reply_data["agent_name"],
        "content": reply_data["content"],
        "created_at": reply_data["created_at"],
    })
    if not verify_signature(pub_key, signable, reply_data["signature"]):
        conn.close()
        logger.warning(f"REJECTED reply {reply_id[:8]} from {node_id}: invalid signature")
        return False, "Invalid signature"

    conn.execute("""
        INSERT INTO replies (reply_id, post_id, node_id, agent_name, content,
                             signature, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (reply_data["reply_id"], reply_data["post_id"], node_id,
          reply_data["agent_name"], reply_data["content"],
          reply_data["signature"], reply_data["created_at"]))

    conn.execute(
        "UPDATE posts SET reply_count = reply_count + 1 WHERE post_id = ?",
        (reply_data["post_id"],)
    )
    conn.commit()
    conn.close()

    logger.info(f"Received verified reply {reply_id[:8]} from node {node_id}")
    return True, "Reply accepted"


def get_posts_for_sync(since: str = "", limit: int = 50) -> list[dict]:
    """Get local posts for syncing to peers.

    Args:
        since: ISO timestamp — only return posts after this time
        limit: Max posts to return
    """
    conn = _get_db()
    identity = get_node_identity()
    if since:
        rows = conn.execute("""
            SELECT * FROM posts WHERE is_local = 1 AND created_at > ?
            ORDER BY created_at ASC LIMIT ?
        """, (since, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM posts WHERE is_local = 1
            ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_replies_for_sync(since: str = "", limit: int = 100) -> list[dict]:
    """Get local replies for syncing to peers."""
    conn = _get_db()
    identity = get_node_identity()
    if since:
        rows = conn.execute("""
            SELECT r.* FROM replies r
            JOIN posts p ON r.post_id = p.post_id
            WHERE r.node_id = ? AND r.created_at > ?
            ORDER BY r.created_at ASC LIMIT ?
        """, (identity.node_id, since, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT r.* FROM replies r
            WHERE r.node_id = ?
            ORDER BY r.created_at DESC LIMIT ?
        """, (identity.node_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    """Get social network statistics."""
    conn = _get_db()
    post_count = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    reply_count = conn.execute("SELECT COUNT(*) FROM replies").fetchone()[0]
    node_count = conn.execute("SELECT COUNT(*) FROM known_nodes").fetchone()[0]
    local_posts = conn.execute("SELECT COUNT(*) FROM posts WHERE is_local = 1").fetchone()[0]
    conn.close()

    return {
        "total_posts": post_count,
        "total_replies": reply_count,
        "known_nodes": node_count,
        "local_posts": local_posts,
        "federated_posts": post_count - local_posts,
        "categories": CATEGORIES,
    }
