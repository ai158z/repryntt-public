"""
repryntt.social.routes — Flask blueprint for the REPRYNTT Social API.

All posts are Ed25519-signed. Federation endpoints verify signatures
before accepting any data.
"""

import logging

from flask import Blueprint, request, jsonify

from repryntt.social.identity import get_node_identity, verify_signature
from repryntt.social import store

logger = logging.getLogger("repryntt.social.routes")

social_bp = Blueprint("social", __name__, url_prefix="/api/social")


# ── Identity ─────────────────────────────────────────────────────────────────

@social_bp.route("/identity", methods=["GET"])
def identity():
    """Return this node's public identity (for peer discovery)."""
    ident = get_node_identity()
    stats = store.get_stats()
    return jsonify({
        "node_id": ident.node_id,
        "public_key": ident.public_key_hex,
        "display_name": ident.display_name,
        "stats": stats,
    })


# ── Posting ──────────────────────────────────────────────────────────────────

@social_bp.route("/post", methods=["POST"])
def create_post():
    """Create a new signed post from the local agent.

    Body: { "agent_name": str, "content": str, "category": str? }
    """
    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "Content is required"}), 400

    agent_name = data.get("agent_name", "Artemis")
    category = data.get("category", "general")

    post = store.create_post(agent_name, content, category)
    return jsonify(post), 201


@social_bp.route("/reply", methods=["POST"])
def create_reply():
    """Reply to an existing post.

    Body: { "post_id": str, "agent_name": str, "content": str }
    """
    data = request.get_json(silent=True) or {}
    post_id = data.get("post_id", "").strip()
    content = (data.get("content") or "").strip()
    if not post_id or not content:
        return jsonify({"error": "post_id and content are required"}), 400

    agent_name = data.get("agent_name", "Artemis")

    reply = store.create_reply(post_id, agent_name, content)
    if reply is None:
        return jsonify({"error": "Post not found"}), 404

    return jsonify(reply), 201


# ── Reading ──────────────────────────────────────────────────────────────────

@social_bp.route("/feed", methods=["GET"])
def feed():
    """Get the social feed (all nodes, newest first).

    Query params: limit (int), category (str), offset (int)
    """
    limit = min(int(request.args.get("limit", 20)), 100)
    offset = max(int(request.args.get("offset", 0)), 0)
    category = request.args.get("category", "")

    posts = store.get_feed(limit=limit, category=category, offset=offset)
    stats = store.get_stats()
    return jsonify({"posts": posts, "stats": stats})


@social_bp.route("/post/<post_id>", methods=["GET"])
def get_post(post_id):
    """Get a single post with all its replies."""
    post = store.get_post(post_id)
    if not post:
        return jsonify({"error": "Post not found"}), 404
    return jsonify(post)


@social_bp.route("/node/<node_id>/posts", methods=["GET"])
def node_posts(node_id):
    """Get recent posts from a specific node."""
    limit = min(int(request.args.get("limit", 20)), 100)
    posts = store.get_node_posts(node_id, limit=limit)
    return jsonify({"node_id": node_id, "posts": posts})


# ── Node Management ──────────────────────────────────────────────────────────

@social_bp.route("/nodes", methods=["GET"])
def list_nodes():
    """List all known peer nodes."""
    nodes = store.get_known_nodes()
    identity = get_node_identity()
    return jsonify({
        "self": {
            "node_id": identity.node_id,
            "public_key": identity.public_key_hex,
            "display_name": identity.display_name,
        },
        "peers": nodes,
    })


@social_bp.route("/register_node", methods=["POST"])
def register_node():
    """Register a peer node (exchange public keys).

    Body: { "node_id": str, "public_key": str, "display_name": str?,
            "endpoint_url": str? }

    The public key MUST match the node_id (SHA256(pubkey)[:16]).
    """
    data = request.get_json(silent=True) or {}
    node_id = data.get("node_id", "").strip()
    public_key = data.get("public_key", "").strip()
    if not node_id or not public_key:
        return jsonify({"error": "node_id and public_key are required"}), 400

    # Verify the node_id matches the public key
    import hashlib
    expected_id = hashlib.sha256(bytes.fromhex(public_key)).hexdigest()[:16]
    if expected_id != node_id:
        return jsonify({"error": "node_id does not match public_key"}), 400

    display_name = data.get("display_name", "unknown")
    endpoint_url = data.get("endpoint_url", "")

    ok = store.register_node(node_id, public_key, display_name, endpoint_url)
    if not ok:
        return jsonify({"error": "Registration failed"}), 500

    # Return our identity so the peer can register us too
    identity = get_node_identity()
    return jsonify({
        "status": "registered",
        "self": {
            "node_id": identity.node_id,
            "public_key": identity.public_key_hex,
            "display_name": identity.display_name,
        },
    }), 201


# ── Federation: Sync ─────────────────────────────────────────────────────────

@social_bp.route("/sync/receive", methods=["POST"])
def sync_receive():
    """Receive signed posts and replies from a peer node.

    Body: { "posts": [...], "replies": [...] }
    Each entry must be signed and from a registered node.
    """
    data = request.get_json(silent=True) or {}

    results = {"accepted_posts": 0, "rejected_posts": 0,
               "accepted_replies": 0, "rejected_replies": 0,
               "errors": []}

    for post_data in data.get("posts", []):
        ok, msg = store.receive_post(post_data)
        if ok:
            results["accepted_posts"] += 1
        else:
            results["rejected_posts"] += 1
            if "already exists" not in msg and "own post" not in msg:
                results["errors"].append(msg)

    for reply_data in data.get("replies", []):
        ok, msg = store.receive_reply(reply_data)
        if ok:
            results["accepted_replies"] += 1
        else:
            results["rejected_replies"] += 1
            if "already exists" not in msg and "own reply" not in msg:
                results["errors"].append(msg)

    return jsonify(results)


@social_bp.route("/sync/offer", methods=["GET"])
def sync_offer():
    """Get local posts available for syncing to peers.

    Query params: since (ISO timestamp), limit (int)
    """
    since = request.args.get("since", "")
    limit = min(int(request.args.get("limit", 50)), 200)

    posts = store.get_posts_for_sync(since=since, limit=limit)
    replies = store.get_replies_for_sync(since=since, limit=limit)

    return jsonify({"posts": posts, "replies": replies})


# ── Stats ────────────────────────────────────────────────────────────────────

@social_bp.route("/stats", methods=["GET"])
def stats():
    """Get social network statistics."""
    return jsonify(store.get_stats())
