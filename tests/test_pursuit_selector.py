"""
Phase 1 unit tests for the Pursuit selector and store.

These tests must pass before Phase 2 (heartbeat cutover) is allowed.
"""

from __future__ import annotations

import time

import pytest

from repryntt.core.pursuit import (
    Pursuit,
    PursuitStore,
    select_pursuit,
    score_pursuit,
)


# ── Fakes ──────────────────────────────────────────────────


class FakeValueCompass:
    """Mimics ValueCompass.get_budget_status()."""

    def __init__(self, duty_pct: float, growth_pct: float, exploration_pct: float):
        self._duty = duty_pct
        self._growth = growth_pct
        self._explore = exploration_pct

    def get_budget_status(self):
        return {
            "duty_pct": self._duty,
            "growth_pct": self._growth,
            "exploration_pct": self._explore,
            "duty_target": 0.70,
            "growth_target": 0.20,
            "exploration_target": 0.10,
        }


# ── Selector tests ─────────────────────────────────────────


def test_locked_operator_pursuit_always_wins():
    locked = Pursuit(
        goal="operator order", deliverable="ship it",
        source="operator", character="duty", locked=True, priority=1.0,
    )
    explore = Pursuit(
        goal="study cancer", deliverable="notes.md",
        source="interest", character="exploration",
    )
    vc = FakeValueCompass(duty_pct=0.95, growth_pct=0.05, exploration_pct=0.0)
    pick = select_pursuit([locked, explore], vc)
    assert pick.pursuit is locked
    assert "operator-locked" in pick.rationale


def test_exploration_deficit_forces_interest_pick():
    """The scenario from today's logs: 14D/6G/0E → exploration must win."""
    duty = Pursuit(goal="duty thing", deliverable="x", source="self", character="duty")
    explore = Pursuit(
        goal="cancer research", deliverable="brief.md",
        source="interest", character="exploration",
    )
    # 70% duty, 30% growth, 0% exploration → exploration deficit highest
    vc = FakeValueCompass(duty_pct=0.70, growth_pct=0.30, exploration_pct=0.0)
    pick = select_pursuit([duty, explore], vc)
    assert pick.pursuit is explore
    assert pick.deficit["exploration"] > pick.deficit["duty"]


def test_no_active_pursuits_returns_none():
    pick = select_pursuit([], FakeValueCompass(0.7, 0.2, 0.1))
    assert pick.pursuit is None
    assert "no active" in pick.rationale


def test_abandoned_pursuit_is_not_selected():
    p = Pursuit(goal="dead loop", deliverable="x", source="follow_up", character="growth")
    p.abandon("zombie")
    pick = select_pursuit([p], FakeValueCompass(0.0, 0.0, 0.0))
    assert pick.pursuit is None


def test_completed_pursuit_is_not_selected():
    p = Pursuit(goal="done", deliverable="x", source="self", character="duty")
    p.complete("shipped")
    pick = select_pursuit([p], FakeValueCompass(0.0, 0.0, 0.0))
    assert pick.pursuit is None


def test_staleness_breaks_ties_for_neglected_pursuit():
    fresh = Pursuit(
        goal="fresh", deliverable="x", source="interest", character="exploration",
    )
    stale = Pursuit(
        goal="stale", deliverable="x", source="interest", character="exploration",
    )
    stale.last_touched = time.time() - 7 * 24 * 3600  # 7 days old
    vc = FakeValueCompass(0.7, 0.2, 0.1)  # equal deficit
    pick = select_pursuit([fresh, stale], vc)
    assert pick.pursuit is stale


def test_priority_boosts_score():
    low = Pursuit(goal="low", deliverable="x", source="self", character="duty", priority=0.0)
    high = Pursuit(goal="high", deliverable="x", source="self", character="duty", priority=2.0)
    vc = FakeValueCompass(0.7, 0.2, 0.1)
    pick = select_pursuit([low, high], vc)
    assert pick.pursuit is high


def test_score_pursuit_uses_character_deficit():
    p = Pursuit(goal="x", deliverable="y", source="self", character="exploration")
    high_explore = {"duty": -0.1, "growth": 0.0, "exploration": 0.5}
    low_explore = {"duty": 0.5, "growth": 0.0, "exploration": -0.1}
    assert score_pursuit(p, high_explore) > score_pursuit(p, low_explore)


# ── Lifecycle tests ────────────────────────────────────────


def test_abandon_marks_inactive_with_reason():
    p = Pursuit(goal="x", deliverable="y", source="self", character="duty")
    assert p.active
    p.abandon("dead end")
    assert not p.active
    assert p.abandoned
    assert p.abandoned_reason == "dead end"


def test_observe_records_step_without_completing():
    p = Pursuit(goal="x", deliverable="y", source="self", character="duty")
    p.observe("watched the queue, nothing actionable")
    assert p.active
    assert p.steps_done == 1
    assert p.history[0].action == "observe"


def test_complete_marks_inactive():
    p = Pursuit(goal="x", deliverable="y", source="self", character="duty")
    p.complete("shipped")
    assert p.completed
    assert not p.active


# ── Store tests ────────────────────────────────────────────


def test_store_roundtrip(tmp_path):
    path = tmp_path / "pursuits.json"
    store = PursuitStore(path)
    p = Pursuit(
        goal="study quantum loop gravity",
        deliverable="notes/qlg.md",
        source="interest",
        character="exploration",
        topic="physics",
    )
    store.upsert(p)

    store2 = PursuitStore(path)  # reload from disk
    loaded = store2.get(p.id)
    assert loaded is not None
    assert loaded.goal == p.goal
    assert loaded.character == "exploration"
    assert loaded.topic == "physics"


def test_store_active_filters_completed_and_abandoned(tmp_path):
    store = PursuitStore(tmp_path / "pursuits.json")
    a = Pursuit(goal="a", deliverable="x", source="self", character="duty")
    b = Pursuit(goal="b", deliverable="x", source="self", character="duty")
    c = Pursuit(goal="c", deliverable="x", source="self", character="duty")
    b.complete()
    c.abandon("nope")
    store.upsert(a, persist=False)
    store.upsert(b, persist=False)
    store.upsert(c)
    actives = store.active()
    assert len(actives) == 1
    assert actives[0].id == a.id


def test_store_find_active_by_topic(tmp_path):
    store = PursuitStore(tmp_path / "pursuits.json")
    p = Pursuit(
        goal="cancer immunotherapy", deliverable="brief.md",
        source="interest", character="exploration", topic="cancer",
    )
    store.upsert(p)
    found = store.find_active_by_topic("cancer")
    assert found is not None
    assert found.id == p.id
    # case-insensitive
    found2 = store.find_active_by_topic("CANCER")
    assert found2 is not None
