"""
repryntt.core.frameworks.runtime — Executes framework instances.

The runtime is deliberately *advisory*:
  * It never calls tools directly.
  * Each tick, :meth:`tick` reads the instance's ``working_state`` and:
      - evaluates the current state's gate
      - either advances, holds, or escalates (on_fail_state)
  * :meth:`guidance_for` returns the text to inject into the agent's PLAN
    prompt so the agent knows what to do this step.

This keeps the runtime compatible with however the agent actually
executes work — Jarvis's existing heartbeat loop, evolution loop, CoT,
etc. — without duplicating tool-call machinery.

Instance state is persisted on every mutation to
``~/.repryntt/frameworks/instances/<id>.json`` so crashes and restarts
do not lose in-flight work.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from repryntt.core.frameworks.schema import (
    Framework,
    FrameworkInstance,
    GateResult,
    InstanceStatus,
)
from repryntt.core.frameworks.registry import FrameworkRegistry, get_registry

logger = logging.getLogger("repryntt.frameworks.runtime")


def _instances_dir() -> Path:
    p = Path.home() / ".repryntt" / "frameworks" / "instances"
    p.mkdir(parents=True, exist_ok=True)
    return p


class FrameworkRuntime:
    """Advances framework instances and yields guidance for the agent."""

    def __init__(self, registry: Optional[FrameworkRegistry] = None):
        self.registry = registry or get_registry()
        self.instances_dir = _instances_dir()

    # ── Instance lifecycle ───────────────────────────────────────────

    def spawn(self, framework_id: str, goal: str, *,
              target: str = "",
              spawned_by: str = "auto",
              initial_state: str = "") -> FrameworkInstance:
        """Start a new running instance of ``framework_id``."""
        fw = self.registry.get(framework_id)
        if fw is None:
            raise ValueError(f"unknown framework: {framework_id}")
        if not fw.states:
            raise ValueError(f"framework has no states: {framework_id}")
        inst = FrameworkInstance.new(
            fw, goal=goal, target=target,
            spawned_by=spawned_by, initial_state=initial_state,
        )
        self._save_instance(inst)
        self._emit_mesh_event(inst, "spawned")
        logger.info(
            f"📐 Framework spawned: {framework_id} v{fw.version} "
            f"→ instance {inst.id[:8]} (goal: {goal[:60]})"
        )
        return inst

    def load(self, instance_id: str) -> Optional[FrameworkInstance]:
        path = self.instances_dir / f"{instance_id}.json"
        if not path.exists():
            return None
        try:
            with open(path, "r") as f:
                return FrameworkInstance.from_dict(json.load(f))
        except Exception as e:
            logger.warning(f"Failed to load instance {instance_id}: {e}")
            return None

    def active_instances(self) -> List[FrameworkInstance]:
        out: List[FrameworkInstance] = []
        for path in self.instances_dir.glob("*.json"):
            try:
                with open(path, "r") as f:
                    inst = FrameworkInstance.from_dict(json.load(f))
                if inst.status == InstanceStatus.ACTIVE:
                    out.append(inst)
            except Exception:
                continue
        out.sort(key=lambda i: i.updated, reverse=True)
        return out

    # ── Per-tick execution ───────────────────────────────────────────

    def tick(self, instance_id: str,
             working_state_update: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Advance an instance one heartbeat.

        Merges ``working_state_update`` into the instance (if provided),
        evaluates the current state's gate, and either:
          * advances to the next state (gate passed),
          * stays (gate failing but under max_heartbeats),
          * jumps to on_fail_state or marks failed (gate failing over budget).

        Returns a summary dict for logging.
        """
        inst = self.load(instance_id)
        if inst is None:
            return {"ok": False, "error": f"unknown instance: {instance_id}"}
        if inst.status != InstanceStatus.ACTIVE:
            return {"ok": False, "error": f"instance {instance_id} is {inst.status.value}"}

        fw = self.registry.get(inst.framework_id)
        if fw is None:
            inst.status = InstanceStatus.FAILED
            inst.notes = f"framework spec disappeared: {inst.framework_id}"
            self._save_instance(inst)
            return {"ok": False, "error": inst.notes}

        if working_state_update:
            inst.working_state.update(working_state_update)

        state = fw.get_state(inst.current_state)
        if state is None:
            # Spec was mutated out from under us — recover to first state
            if fw.states:
                inst.current_state = fw.states[0].name
                state = fw.states[0]
                inst.record_transition("?", state.name, "state missing — recovered to start")
            else:
                inst.status = InstanceStatus.FAILED
                self._save_instance(inst)
                return {"ok": False, "error": "framework has no states"}

        inst.heartbeats_in_state += 1
        gate = state.evaluate_gate(inst.working_state)

        result: Dict[str, Any] = {
            "ok": True,
            "instance_id": inst.id,
            "framework_id": fw.id,
            "state": state.name,
            "heartbeats_in_state": inst.heartbeats_in_state,
            "gate": gate.to_dict(),
            "advanced": False,
            "status": inst.status.value,
        }

        if gate.passed:
            next_name = fw.next_state_after(state.name)
            if next_name is None:
                # End of state machine → completed
                inst.status = InstanceStatus.COMPLETED
                inst.completed = time.time()
                inst.record_transition(state.name, "<done>", "state machine complete", gate)
                logger.info(f"✅ Framework instance {inst.id[:8]} COMPLETED ({fw.id})")
                result["status"] = inst.status.value
            else:
                inst.record_transition(state.name, next_name, "gate passed", gate)
                inst.current_state = next_name
                inst.heartbeats_in_state = 0
                result["advanced"] = True
                result["next_state"] = next_name
        else:
            # Gate failing — have we blown the budget?
            if inst.heartbeats_in_state >= max(1, state.max_heartbeats):
                if state.on_fail_state and fw.get_state(state.on_fail_state):
                    inst.record_transition(
                        state.name, state.on_fail_state,
                        f"gate failed after {inst.heartbeats_in_state} hb — escalating",
                        gate,
                    )
                    inst.current_state = state.on_fail_state
                    inst.heartbeats_in_state = 0
                    result["advanced"] = True
                    result["next_state"] = state.on_fail_state
                    result["escalated"] = True
                else:
                    inst.status = InstanceStatus.FAILED
                    inst.completed = time.time()
                    inst.notes = (
                        f"gate failed in state '{state.name}' after "
                        f"{inst.heartbeats_in_state} heartbeats: {gate.message}"
                    )
                    inst.record_transition(state.name, "<failed>", inst.notes, gate)
                    logger.warning(
                        f"⚠️ Framework instance {inst.id[:8]} FAILED in {state.name}: {gate.message}"
                    )
                    result["status"] = inst.status.value
                    result["failed_reason"] = inst.notes

        self._save_instance(inst)
        if inst.status in (InstanceStatus.COMPLETED, InstanceStatus.FAILED):
            self._emit_mesh_event(inst, inst.status.value)
        return result

    def score_and_close(self, instance_id: str, score: int, *,
                        notes: str = "") -> Dict[str, Any]:
        """Record a final 1-5 score on an instance and update the spec stats."""
        inst = self.load(instance_id)
        if inst is None:
            return {"ok": False, "error": f"unknown instance: {instance_id}"}
        score = max(1, min(5, int(score)))
        inst.score = score
        if notes:
            inst.notes = (inst.notes + " | " + notes).strip(" |")
        if inst.status == InstanceStatus.ACTIVE:
            inst.status = InstanceStatus.ABANDONED if score < 3 else InstanceStatus.COMPLETED
            inst.completed = time.time()
        self._save_instance(inst)
        self.registry.update_outcome(inst.framework_id, score=score)
        self._emit_mesh_event(inst, f"scored:{score}")
        return {"ok": True, "instance_id": inst.id, "score": score, "status": inst.status.value}

    def abandon(self, instance_id: str, reason: str = "") -> Dict[str, Any]:
        inst = self.load(instance_id)
        if inst is None:
            return {"ok": False, "error": f"unknown instance: {instance_id}"}
        if inst.status != InstanceStatus.ACTIVE:
            return {"ok": True, "note": "already closed", "status": inst.status.value}
        inst.status = InstanceStatus.ABANDONED
        inst.completed = time.time()
        inst.notes = (inst.notes + " | abandoned: " + reason).strip(" |")
        self._save_instance(inst)
        self.registry.update_outcome(inst.framework_id, score=1)
        self._emit_mesh_event(inst, "abandoned")
        return {"ok": True, "instance_id": inst.id, "status": inst.status.value}

    # ── Guidance (prompt injection) ──────────────────────────────────

    def guidance_for(self, instance_id: str) -> str:
        """Return text suitable for injection into the agent's PLAN prompt."""
        inst = self.load(instance_id)
        if inst is None:
            return ""
        fw = self.registry.get(inst.framework_id)
        if fw is None:
            return ""
        state = fw.get_state(inst.current_state)
        if state is None:
            return ""
        idx = fw.state_names().index(state.name) if state.name in fw.state_names() else 0
        n = len(fw.states)
        tools_hint = (", ".join(state.tools) if state.tools else "your usual tools")
        gate_hint = self._summarise_gate(state.gate)
        base = (
            f"📐 ACTIVE FRAMEWORK: {fw.label} ({fw.id} v{fw.version})\n"
            f"   Instance: {inst.id[:8]}  |  Goal: {inst.goal}\n"
            f"   State {idx + 1}/{n} — {state.label}\n"
            f"   Guidance: {state.guidance}\n"
            f"   Suggested tools: {tools_hint}\n"
            f"   To advance, your working_state needs: {gate_hint}\n"
            f"   (update with framework_update(working_state={{...}}) when you have it)"
        )
        skills_block = self._render_skill_hints(fw, state)
        return base + ("\n" + skills_block if skills_block else "")

    def _render_skill_hints(self, fw: "Framework", state) -> str:
        """Best-effort skill suggestions for the active state. Never raises."""
        try:
            from repryntt.core.frameworks.skill_bridge import (
                relevant_skills,
                render_skill_hints,
            )
            hints = relevant_skills(fw, state)
            return render_skill_hints(hints)
        except Exception as e:
            logger.debug(f"skill bridge unavailable: {e}")
            return ""

    @staticmethod
    def _summarise_gate(gate: Dict[str, Any]) -> str:
        if not gate:
            return "no gate (auto-advances next tick)"
        parts: List[str] = []
        if gate.get("required_keys"):
            parts.append("keys: " + ", ".join(gate["required_keys"]))
        if gate.get("min_length"):
            parts.append("min_length: " + ", ".join(f"{k}>={v}" for k, v in gate["min_length"].items()))
        if gate.get("min_list_length"):
            parts.append("min_list: " + ", ".join(f"{k}>={v}" for k, v in gate["min_list_length"].items()))
        if gate.get("min_numeric"):
            parts.append("min_numeric: " + ", ".join(f"{k}>={v}" for k, v in gate["min_numeric"].items()))
        return " | ".join(parts) if parts else "(empty)"

    # ── Persistence ──────────────────────────────────────────────────

    def _save_instance(self, inst: FrameworkInstance) -> None:
        inst.updated = time.time()
        path = self.instances_dir / f"{inst.id}.json"
        try:
            with open(path, "w") as f:
                json.dump(inst.to_dict(), f, indent=2, sort_keys=True)
        except Exception as e:
            logger.warning(f"Failed to persist instance {inst.id}: {e}")

    # ── Memory Mesh bridge (soft dependency) ─────────────────────────

    def _emit_mesh_event(self, inst: FrameworkInstance, event: str) -> None:
        """Register the framework + target in the memory mesh (best-effort)."""
        try:
            from repryntt.core.memory.memory_mesh import MemoryMesh
            mesh = MemoryMesh()
            # Framework node itself
            mesh.ensure_node("framework", inst.framework_id, source="frameworks.runtime")
            mesh.activate("framework", inst.framework_id, boost=0.4)
            # Link to target (if any)
            if inst.target:
                mesh.record_association(
                    "framework", inst.framework_id,
                    "target", inst.target,
                    edge_type="operated_on",
                )
            # Link to the event tag so spreading activation finds it later
            mesh.record_association(
                "framework", inst.framework_id,
                "event", event,
                edge_type="lifecycle",
            )
            mesh.save()
        except Exception as e:
            logger.debug(f"mesh emit skipped ({event}): {e}")


# ── Singleton ────────────────────────────────────────────────────────────

_runtime_instance: Optional[FrameworkRuntime] = None


def get_runtime() -> FrameworkRuntime:
    global _runtime_instance
    if _runtime_instance is None:
        _runtime_instance = FrameworkRuntime()
    return _runtime_instance


__all__ = ["FrameworkRuntime", "get_runtime"]
