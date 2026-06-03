"""
Phase 3 tests: INTERESTS.md parsing, seeding, zombie-chain abandonment.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from repryntt.core.pursuit import (
    PursuitStore,
    parse_interests_md,
    seed_interest_pursuits,
    abandon_zombie_chain,
)


SAMPLE_INTERESTS = """# Interests

> preamble

## Tier 1 — Core Passions

### Artificial Intelligence
- How do transformers work?
- Build a small NN from scratch

### Edge Computing
- What runs on Jetson?

## Tier 2 — Strong Interests

### Physics & Mathematics
- Implement physics from first principles?

### Space & Cosmology
- Analyze JWST data

## How to Use This File

1. Pick a topic.
"""


def test_parse_interests_md_extracts_tiered_topics():
    topics = parse_interests_md(SAMPLE_INTERESTS)
    names = [t["topic"] for t in topics]
    assert "Artificial Intelligence" in names
    assert "Edge Computing" in names
    assert "Physics & Mathematics" in names
    assert "Space & Cosmology" in names
    # "How to Use This File" has no bullets → filtered
    assert all("How to Use" not in n for n in names)
    ai = next(t for t in topics if t["topic"] == "Artificial Intelligence")
    assert ai["tier"] == "tier_1"
    assert "How do transformers work?" in ai["sub_questions"]


def test_seed_interest_pursuits_creates_one_per_topic(tmp_path):
    interests_path = tmp_path / "INTERESTS.md"
    interests_path.write_text(SAMPLE_INTERESTS)
    store = PursuitStore(tmp_path / "pursuits.json")
    created, refreshed = seed_interest_pursuits(store, interests_path)
    assert created == 4  # AI, Edge, Physics, Space
    assert refreshed == 0
    actives = store.active()
    assert all(p.character == "exploration" for p in actives)
    assert all(p.source == "interest" for p in actives)
    # Deliverable must point to a concrete artifact (Phase 4.4 requirement)
    assert all("memory/explorations/" in p.deliverable for p in actives)


def test_seed_is_idempotent(tmp_path):
    interests_path = tmp_path / "INTERESTS.md"
    interests_path.write_text(SAMPLE_INTERESTS)
    store = PursuitStore(tmp_path / "pursuits.json")
    seed_interest_pursuits(store, interests_path)
    created2, refreshed2 = seed_interest_pursuits(store, interests_path)
    # No new pursuits created — already exist and not stale
    assert created2 == 0
    assert refreshed2 == 0
    assert len(store.active()) == 4


def test_seed_refreshes_after_interval(tmp_path):
    interests_path = tmp_path / "INTERESTS.md"
    interests_path.write_text(SAMPLE_INTERESTS)
    store = PursuitStore(tmp_path / "pursuits.json")
    seed_interest_pursuits(store, interests_path, refresh_interval_seconds=0.01)
    time.sleep(0.05)
    created, refreshed = seed_interest_pursuits(
        store, interests_path, refresh_interval_seconds=0.01
    )
    assert created == 0
    assert refreshed == 4


def test_abandon_zombie_chain_kills_match(tmp_path):
    chain_path = tmp_path / "reasoning_chain.json"
    chain_path.write_text(json.dumps({
        "status": "active",
        "goal": "INNER MONOLOGUE — SELF-DIRECTED HEARTBEAT PLANNING",
        "topic": "planning",
        "source": "auto_followup",
        "steps_completed": [],
    }))
    killed = abandon_zombie_chain(chain_path)
    assert killed is not None
    assert "INNER MONOLOGUE" in killed
    # File removed (so next load returns None)
    assert not chain_path.exists()
    # Snapshot preserved
    snapshot = chain_path.with_suffix(".abandoned.json")
    assert snapshot.exists()
    snap = json.loads(snapshot.read_text())
    assert snap["status"] == "active"  # snapshot is the original


def test_abandon_zombie_chain_leaves_real_work_alone(tmp_path):
    chain_path = tmp_path / "reasoning_chain.json"
    real_chain = {
        "status": "active",
        "goal": "Research orbital mechanics for the simulation",
        "topic": "orbital_mechanics",
        "source": "operator",
        "steps_completed": [],
    }
    chain_path.write_text(json.dumps(real_chain))
    killed = abandon_zombie_chain(chain_path)
    assert killed is None
    assert chain_path.exists()
    # Untouched
    assert json.loads(chain_path.read_text())["status"] == "active"


def test_abandon_zombie_chain_no_file(tmp_path):
    killed = abandon_zombie_chain(tmp_path / "nonexistent.json")
    assert killed is None
