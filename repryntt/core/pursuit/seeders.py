"""
Pursuit seeders — convert bootstrap files into standing Pursuits.

Phase 3 of the Unified Autonomy Refactor:
- Parse INTERESTS.md and ensure one `source=interest, character=exploration`
  Pursuit per topic.
- Refresh weekly (each Pursuit's `last_seeded` timestamp gates re-creation).
- One-time abandonment of the zombie auto_followup chain that hijacked the
  heartbeat loop (the "INNER MONOLOGUE — SELF-DIRECTED HEARTBEAT PLANNING"
  generic chain).

Idempotent: safe to call on every boot.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import List, Optional, Tuple

from .model import Pursuit
from .store import PursuitStore

logger = logging.getLogger(__name__)

# A Pursuit older than this is eligible for refresh (sub-questions may have changed).
REFRESH_INTERVAL_SECONDS = 7 * 24 * 3600  # 1 week

# Markers that identify a zombie auto_followup chain (Phase 3.3).
ZOMBIE_GOAL_MARKERS = (
    "INNER MONOLOGUE — SELF-DIRECTED HEARTBEAT PLANNING",
    "self-directed heartbeat planning",
)


# ── INTERESTS.md parsing ──────────────────────────────────────────────


def parse_interests_md(text: str) -> List[dict]:
    """
    Parse INTERESTS.md into a list of topics.

    Format expected:
        ## Tier 1 ...
        ### Topic Name
        - sub-question 1
        - sub-question 2

    Returns: [{topic, tier, sub_questions: [...]}, ...]
    """
    topics: List[dict] = []
    current_tier = "tier_unknown"
    current_topic: Optional[dict] = None

    tier_re = re.compile(r"^##\s+Tier\s+(\d+)", re.IGNORECASE)
    topic_re = re.compile(r"^###\s+(.+?)\s*$")
    bullet_re = re.compile(r"^\s*[-*]\s+(.+?)\s*$")

    for line in text.splitlines():
        m = tier_re.match(line)
        if m:
            if current_topic:
                topics.append(current_topic)
            current_tier = f"tier_{m.group(1)}"
            current_topic = None
            continue

        m = topic_re.match(line)
        if m:
            if current_topic:
                topics.append(current_topic)
            current_topic = {
                "topic": m.group(1).strip(),
                "tier": current_tier,
                "sub_questions": [],
            }
            continue

        if current_topic is not None:
            m = bullet_re.match(line)
            if m:
                current_topic["sub_questions"].append(m.group(1).strip())

    if current_topic:
        topics.append(current_topic)

    # Filter out non-topic headers ("How to Use This File", etc.)
    return [t for t in topics if t["sub_questions"]]


# ── Pursuit seeding ───────────────────────────────────────────────────


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:48]


def _topic_to_pursuit(topic: dict) -> Pursuit:
    """Build a standing exploration Pursuit from a parsed INTERESTS topic."""
    name = topic["topic"]
    sub = topic.get("sub_questions") or []
    # The deliverable is concrete: pick one sub-question, produce a written
    # analysis. This satisfies the Phase 4.4 mirror-loop guard which rejects
    # "plan X" goals.
    deliverable = (
        f"agent_workspaces/jarvis/memory/explorations/{_slugify(name)}_<date>.md "
        f"— a written exploration (>=500 words) that picks one sub-question "
        f"and answers it with research, analysis, or working code."
    )
    goal = f"Explore {name}: pick one sub-question and go deep"
    phase_guide = [
        f"Pick the sub-question from INTERESTS.md that most pulls you today",
        f"Research it (web_search / scrape_web_page / mesh_search) — gather sources",
        f"Synthesize: write your own analysis with cited evidence",
        f"Save the artifact under memory/explorations/ and append_daily_memory",
    ]
    p = Pursuit(
        goal=goal,
        deliverable=deliverable,
        source="interest",
        character="exploration",
        phase_guide=phase_guide,
        target_steps=4,
        topic=name,
    )
    # Stamp seeding metadata into state for refresh logic.
    p.state["last_seeded"] = time.time()
    p.state["sub_questions"] = sub
    p.state["tier"] = topic.get("tier", "tier_unknown")
    return p


def seed_interest_pursuits(
    store: PursuitStore,
    interests_md_path: Path | str,
    refresh_interval_seconds: float = REFRESH_INTERVAL_SECONDS,
) -> Tuple[int, int]:
    """
    Ensure one standing exploration Pursuit per topic in INTERESTS.md.

    Returns: (created_count, refreshed_count).
    """
    path = Path(interests_md_path)
    if not path.exists():
        logger.info(f"seed_interest_pursuits: {path} not found — skipping")
        return (0, 0)

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"seed_interest_pursuits: cannot read {path}: {e}")
        return (0, 0)

    topics = parse_interests_md(text)
    if not topics:
        logger.info("seed_interest_pursuits: no topics parsed — skipping")
        return (0, 0)

    created = 0
    refreshed = 0
    now = time.time()

    for topic in topics:
        existing = store.find_active_by_topic(topic["topic"])
        if existing is None:
            new_p = _topic_to_pursuit(topic)
            store.upsert(new_p, persist=False)
            created += 1
            continue

        last_seeded = float(existing.state.get("last_seeded", 0) or 0)
        if (now - last_seeded) > refresh_interval_seconds:
            existing.state["last_seeded"] = now
            existing.state["sub_questions"] = topic.get("sub_questions") or []
            existing.state["tier"] = topic.get("tier", existing.state.get("tier"))
            existing.touch()
            store.upsert(existing, persist=False)
            refreshed += 1

    if created or refreshed:
        store.save()
        logger.info(
            f"🌱 seed_interest_pursuits: created={created} refreshed={refreshed} "
            f"total_topics={len(topics)}"
        )
    return (created, refreshed)


# ── Phase 3.3: zombie chain abandonment ──────────────────────────────


def abandon_zombie_chain(reasoning_chain_path: Path | str) -> Optional[str]:
    """
    One-time abandonment of the generic auto_followup zombie chain.

    Returns the abandoned chain's goal text if it was killed, else None.
    Safe to call repeatedly — only acts on chains that match the markers
    AND have status=="active". Writes a sibling .abandoned.json snapshot
    so nothing is lost.
    """
    path = Path(reasoning_chain_path)
    if not path.exists():
        return None
    try:
        with open(path, "r") as f:
            chain = json.load(f)
    except Exception as e:
        logger.debug(f"abandon_zombie_chain: cannot read {path}: {e}")
        return None
    if not isinstance(chain, dict):
        return None
    if chain.get("status") != "active":
        return None
    goal = (chain.get("goal") or chain.get("topic") or "").strip()
    if not any(marker.lower() in goal.lower() for marker in ZOMBIE_GOAL_MARKERS):
        return None

    snapshot = path.with_suffix(".abandoned.json")
    try:
        with open(snapshot, "w") as f:
            json.dump(chain, f, indent=2, default=str)
    except Exception as e:
        logger.debug(f"abandon_zombie_chain: snapshot failed: {e}")

    chain["status"] = "completed"
    chain["outcome"] = "abandoned"
    chain["outcome_detail"] = (
        "zombie auto_followup, no deliverable — abandoned by Phase 3 seeder"
    )
    chain["abandoned_at"] = time.time()
    try:
        with open(path, "w") as f:
            json.dump(chain, f, indent=2, default=str)
        # Remove the active file so next load returns None.
        os.remove(path)
    except Exception as e:
        logger.warning(f"abandon_zombie_chain: rewrite failed: {e}")
        return None

    logger.warning(f"🪦 Zombie chain abandoned: {goal[:120]}")
    return goal
