"""Tests for repryntt.core.frameworks evolution + skill bridge."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from repryntt.core.frameworks.evolution import (
    EvolutionLoop,
    FailureProfile,
    MutationProposal,
)
from repryntt.core.frameworks.registry import FrameworkRegistry
from repryntt.core.frameworks.runtime import FrameworkRuntime
from repryntt.core.frameworks.schema import (
    Framework,
    FrameworkInstance,
    FrameworkState,
    InstanceStatus,
)
from repryntt.core.frameworks.skill_bridge import (
    SkillHint,
    relevant_skills,
    render_skill_hints,
)


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_registry(tmp_path: Path) -> FrameworkRegistry:
    """A fresh, empty registry rooted at a tmp dir."""
    store = tmp_path / "frameworks"
    store.mkdir()
    return FrameworkRegistry(store_dir=store)


@pytest.fixture
def tmp_instances_dir(tmp_path: Path) -> Path:
    p = tmp_path / "instances"
    p.mkdir()
    return p


def _make_framework(fid: str = "diag_v1", *, runs=10, wins=2, losses=8) -> Framework:
    return Framework(
        id=fid,
        label="Diagnose v1",
        description="Find and explain a bug.",
        states=[
            FrameworkState(name="gather", label="Gather", guidance="collect facts",
                           tools=["read_file", "grep"],
                           gate={"required_keys": ["facts"]}, max_heartbeats=2),
            FrameworkState(name="hypothesize", label="Hypothesize",
                           guidance="form a hypothesis",
                           gate={"min_length": {"hypothesis": 40}}, max_heartbeats=2),
        ],
        match_keywords=["bug", "diagnose"],
        tags=["debug"],
        runs=runs, wins=wins, losses=losses,
    )


def _seed_failed_instance(dir_: Path, fw: Framework, *, stuck_in: str,
                          heartbeats: int = 3) -> None:
    inst = FrameworkInstance.new(fw, goal="x", spawned_by="test")
    inst.current_state = stuck_in
    inst.heartbeats_in_state = heartbeats
    inst.status = InstanceStatus.FAILED
    inst.completed = time.time()
    inst.transitions = [
        {"at": time.time(), "from": stuck_in, "to": "<failed>",
         "reason": "gate failed", "gate": None}
    ]
    (dir_ / f"{inst.id}.json").write_text(json.dumps(inst.to_dict()))


# ── Skill bridge ─────────────────────────────────────────────────────────

class _FakeSkill:
    def __init__(self, skill_id, name, description, tags):
        self.skill_id = skill_id
        self.name = name
        self.description = description
        self.tags = tags


class _FakeSkillRegistry:
    def __init__(self, packages, installed_ids=None):
        self._packages = packages
        self._installed = set(installed_ids or [])

    def scan(self):
        return list(self._packages)

    def _get_installed_ids(self):
        return self._installed


def test_skill_bridge_returns_top_matches_by_token_overlap():
    fw = _make_framework()
    state = fw.get_state("gather")
    skills = [
        _FakeSkill("s1", "deep_research", "Conduct deep research and gather facts",
                   ["research", "gather"]),
        _FakeSkill("s2", "trading_mastery", "Trade securities", ["trading"]),
        _FakeSkill("s3", "bug_hunter", "Diagnose and fix bugs",
                   ["debug", "diagnose"]),
    ]
    registry = _FakeSkillRegistry(skills, installed_ids={"s1"})
    hints = relevant_skills(fw, state, registry=registry, limit=3)

    ids = [h.skill_id for h in hints]
    assert "s2" not in ids, "trading skill should not match a debug framework"
    assert "s1" in ids or "s3" in ids
    assert all(isinstance(h, SkillHint) for h in hints)


def test_skill_bridge_empty_when_no_overlap():
    fw = _make_framework()
    state = fw.get_state("gather")
    registry = _FakeSkillRegistry([
        _FakeSkill("x", "cooking", "How to make pasta", ["food"]),
    ])
    assert relevant_skills(fw, state, registry=registry) == []


def test_skill_bridge_render_handles_empty_and_marks_installed():
    assert render_skill_hints([]) == ""
    txt = render_skill_hints([
        SkillHint("a", "alpha", "first skill desc", score=2, installed=True),
        SkillHint("b", "beta", "", score=1, installed=False),
    ])
    assert "✓ alpha" in txt
    assert "· beta" in txt
    assert "first skill desc" in txt


def test_skill_bridge_silent_when_skills_unavailable():
    """If the registry kw is omitted and the import fails, return []."""
    import repryntt.core.frameworks.skill_bridge as sb

    class _Boom:
        def scan(self):
            raise RuntimeError("nope")

    fw = _make_framework()
    state = fw.get_state("gather")
    assert sb.relevant_skills(fw, state, registry=_Boom()) == []


# ── Evolution loop ───────────────────────────────────────────────────────

def test_diagnose_finds_bottleneck_state(tmp_registry, tmp_instances_dir):
    fw = _make_framework()
    tmp_registry.save(fw)
    for _ in range(3):
        _seed_failed_instance(tmp_instances_dir, fw, stuck_in="hypothesize",
                              heartbeats=4)
    _seed_failed_instance(tmp_instances_dir, fw, stuck_in="gather", heartbeats=2)

    loop = EvolutionLoop(registry=tmp_registry, instances_dir=tmp_instances_dir)
    profile = loop.diagnose(tmp_registry.get(fw.id))

    assert profile.bottleneck_state == "hypothesize"
    assert profile.bottleneck_failures == 3
    assert profile.failure_states == {"hypothesize": 3, "gather": 1}


def test_review_forks_and_bumps_heartbeats(tmp_registry, tmp_instances_dir):
    fw = _make_framework(runs=10, wins=2, losses=8)
    tmp_registry.save(fw)
    for _ in range(4):
        _seed_failed_instance(tmp_instances_dir, fw, stuck_in="hypothesize")

    loop = EvolutionLoop(registry=tmp_registry, instances_dir=tmp_instances_dir)
    report = loop.review(min_runs=5, max_win_rate=0.5)

    assert report["evolved"], f"expected an evolved spec, got {report}"
    new_id = report["evolved"][0]["new_framework_id"]
    child = tmp_registry.get(new_id)
    assert child is not None
    assert fw.id in child.lineage
    assert "auto_evolved" in child.tags

    target = child.get_state("hypothesize")
    original = fw.get_state("hypothesize")
    assert target.max_heartbeats == original.max_heartbeats + EvolutionLoop.DEFAULT_HEARTBEAT_BUMP

    # Non-bottleneck state should be untouched
    assert child.get_state("gather").max_heartbeats == fw.get_state("gather").max_heartbeats


def test_review_skips_when_below_min_runs(tmp_registry, tmp_instances_dir):
    fw = _make_framework(runs=2, wins=0, losses=2)
    tmp_registry.save(fw)
    _seed_failed_instance(tmp_instances_dir, fw, stuck_in="hypothesize")

    loop = EvolutionLoop(registry=tmp_registry, instances_dir=tmp_instances_dir)
    report = loop.review(min_runs=5, max_win_rate=0.5)

    assert report["evolved"] == []
    assert report["diagnosed"] == []


def test_review_skips_specs_with_high_win_rate(tmp_registry, tmp_instances_dir):
    fw = _make_framework(runs=10, wins=9, losses=1)
    tmp_registry.save(fw)

    loop = EvolutionLoop(registry=tmp_registry, instances_dir=tmp_instances_dir)
    report = loop.review(min_runs=5, max_win_rate=0.5)
    assert report["evolved"] == []


def test_review_is_idempotent(tmp_registry, tmp_instances_dir):
    fw = _make_framework(runs=10, wins=2, losses=8)
    tmp_registry.save(fw)
    for _ in range(4):
        _seed_failed_instance(tmp_instances_dir, fw, stuck_in="hypothesize")

    loop = EvolutionLoop(registry=tmp_registry, instances_dir=tmp_instances_dir)
    first = loop.review(min_runs=5, max_win_rate=0.5)
    second = loop.review(min_runs=5, max_win_rate=0.5)

    assert len(first["evolved"]) == 1
    assert second["evolved"] == []
    assert any("descendant" in s["reason"] for s in second["skipped"])


def test_dry_run_does_not_create_descendant(tmp_registry, tmp_instances_dir):
    fw = _make_framework(runs=10, wins=2, losses=8)
    tmp_registry.save(fw)
    for _ in range(4):
        _seed_failed_instance(tmp_instances_dir, fw, stuck_in="hypothesize")

    loop = EvolutionLoop(registry=tmp_registry, instances_dir=tmp_instances_dir)
    report = loop.review(min_runs=5, max_win_rate=0.5, dry_run=True)
    assert report["evolved"] and report["evolved"][0]["dry_run"] is True
    # No child was created
    assert all(fw.id not in (other.lineage or [])
               for other in tmp_registry.all())


# ── Runtime integration ──────────────────────────────────────────────────

def test_guidance_for_appends_skill_hints_when_available(
    monkeypatch, tmp_registry, tmp_path
):
    fw = _make_framework()
    tmp_registry.save(fw)

    runtime = FrameworkRuntime(registry=tmp_registry)
    monkeypatch.setattr(runtime, "instances_dir", tmp_path / "inst")
    runtime.instances_dir.mkdir()

    inst = runtime.spawn(fw.id, goal="find the bug")

    # Patch the bridge to return a fixed hint regardless of skill registry
    import repryntt.core.frameworks.skill_bridge as sb
    monkeypatch.setattr(sb, "relevant_skills",
                        lambda *a, **kw: [SkillHint("s1", "bug_hunter",
                                                     "Find bugs fast",
                                                     score=2, installed=True)])
    text = runtime.guidance_for(inst.id)
    assert "Relevant skills" in text
    assert "bug_hunter" in text


def test_guidance_for_unaffected_when_bridge_raises(monkeypatch, tmp_registry,
                                                    tmp_path):
    fw = _make_framework()
    tmp_registry.save(fw)
    runtime = FrameworkRuntime(registry=tmp_registry)
    monkeypatch.setattr(runtime, "instances_dir", tmp_path / "inst")
    runtime.instances_dir.mkdir()
    inst = runtime.spawn(fw.id, goal="x")

    import repryntt.core.frameworks.skill_bridge as sb
    def _boom(*a, **kw):
        raise RuntimeError("simulated failure")
    monkeypatch.setattr(sb, "relevant_skills", _boom)

    # Must still return the base guidance, not raise.
    text = runtime.guidance_for(inst.id)
    assert "ACTIVE FRAMEWORK" in text
    assert "Relevant skills" not in text
