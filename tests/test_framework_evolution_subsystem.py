"""Tests for FrameworkEvolutionSubsystem — throttle, safe mode, directives."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from repryntt.core.consciousness.daemon import ConsciousnessDirective
from repryntt.core.consciousness.framework_evolution_subsystem import (
    FrameworkEvolutionSubsystem,
)
from repryntt.core.frameworks.evolution import EvolutionLoop
from repryntt.core.frameworks.registry import FrameworkRegistry
from repryntt.core.frameworks.schema import (
    Framework,
    FrameworkInstance,
    FrameworkState,
    InstanceStatus,
)


# ── Fixtures ─────────────────────────────────────────────────────────────

def _spec(fid="diag_v1", runs=10, wins=2, losses=8) -> Framework:
    return Framework(
        id=fid, label="Diag", description="",
        states=[
            FrameworkState(name="gather", label="g", guidance="",
                           gate={"required_keys": ["x"]}, max_heartbeats=2),
            FrameworkState(name="hyp", label="h", guidance="",
                           gate={"min_length": {"hypothesis": 40}}, max_heartbeats=2),
        ],
        runs=runs, wins=wins, losses=losses,
    )


def _seed_failed(dir_: Path, fw: Framework, *, stuck_in: str = "hyp") -> None:
    import json
    inst = FrameworkInstance.new(fw, goal="g", spawned_by="t")
    inst.current_state = stuck_in
    inst.heartbeats_in_state = 4
    inst.status = InstanceStatus.FAILED
    inst.completed = time.time()
    inst.transitions = [{"at": time.time(), "from": stuck_in,
                         "to": "<failed>", "reason": "x", "gate": None}]
    (dir_ / f"{inst.id}.json").write_text(json.dumps(inst.to_dict()))


@pytest.fixture
def subsystem(tmp_path):
    fw_dir = tmp_path / "fw"
    fw_dir.mkdir()
    reg = FrameworkRegistry(store_dir=fw_dir)
    fw = _spec()
    reg.save(fw)
    inst_dir = tmp_path / "inst"
    inst_dir.mkdir()
    for _ in range(4):
        _seed_failed(inst_dir, fw)
    loop = EvolutionLoop(registry=reg, instances_dir=inst_dir)
    return FrameworkEvolutionSubsystem(loop=loop, interval_seconds=3600)


def _directive(action: str, **params) -> ConsciousnessDirective:
    return ConsciousnessDirective(
        directive_id=f"test_{action}_{int(time.time()*1000)}",
        target_subsystem="framework_evolution",
        action=action,
        parameters=params,
        priority=5,
        timeout=10.0,
    )


# ── Tests ────────────────────────────────────────────────────────────────

def test_subsystem_name_and_capabilities(subsystem):
    assert subsystem.subsystem_name == "framework_evolution"
    status = subsystem.get_status()
    assert "auto_fork_underperforming_specs" in status.capabilities
    assert status.health_score > 0.5


def test_can_handle_only_supported_directives(subsystem):
    assert subsystem.can_handle_directive(_directive("run_review"))
    assert subsystem.can_handle_directive(_directive("dry_run_review"))
    assert subsystem.can_handle_directive(_directive("get_status"))
    assert subsystem.can_handle_directive(_directive("enter_safe_mode"))
    assert not subsystem.can_handle_directive(_directive("nonsense_action"))


def test_first_run_review_evolves(subsystem):
    resp = subsystem.receive_directive(_directive("run_review"))
    assert resp.success is True
    assert resp.result["evolved"] >= 1
    assert subsystem.total_evolved == resp.result["evolved"]
    assert subsystem.last_run_at > 0


def test_throttle_blocks_second_run_within_interval(subsystem):
    subsystem.receive_directive(_directive("run_review"))
    second = subsystem.receive_directive(_directive("run_review"))
    assert second.success is True
    assert second.result["status"] == "throttled"
    assert second.result["seconds_until_next"] > 0


def test_force_overrides_throttle(subsystem):
    subsystem.receive_directive(_directive("run_review"))
    forced = subsystem.receive_directive(_directive("run_review", force=True))
    assert forced.success is True
    # Forced run returns review-shaped result, not "throttled"
    assert "evolved" in forced.result


def test_dry_run_does_not_advance_throttle(subsystem):
    resp = subsystem.receive_directive(_directive("dry_run_review"))
    assert resp.success is True
    assert resp.result["dry_run"] is True
    assert subsystem.last_run_at == 0  # throttle clock untouched


def test_safe_mode_blocks_run(subsystem):
    subsystem.receive_directive(_directive("enter_safe_mode", reason="test"))
    assert subsystem.safe_mode is True
    blocked = subsystem.receive_directive(_directive("run_review"))
    assert blocked.success is False
    assert blocked.result == "safe_mode_active"


def test_request_attention_only_when_due_and_not_safe(subsystem):
    # Brand-new subsystem with last_run_at=0 → due
    assert subsystem.request_attention(priority=5) is True
    subsystem.receive_directive(_directive("run_review"))
    # Just ran → not due
    assert subsystem.request_attention(priority=5) is False
    # Safe mode → never asks
    subsystem.receive_directive(_directive("enter_safe_mode"))
    subsystem.last_run_at = 0
    assert subsystem.request_attention(priority=5) is False


def test_get_status_directive_returns_health_dict(subsystem):
    resp = subsystem.receive_directive(_directive("get_status"))
    assert resp.success is True
    assert "interval_seconds" in resp.result
    assert "due_now" in resp.result


def test_unknown_action_fails_gracefully(subsystem):
    resp = subsystem.receive_directive(_directive("not_a_real_action"))
    assert resp.success is False
    assert "unknown action" in resp.result
