"""
repryntt.core.frameworks.tools — Agent-facing tools.

Surface a minimal, composable tool set the agent can call to list, spawn,
observe, advance, and evolve frameworks. These are the four tools
requested plus two execution helpers (``framework_tick``, ``framework_update``)
without which the agent has no way to drive an instance.

Register with the tool registry in
``repryntt/tools/registry.py::register_native_tools``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from repryntt.core.frameworks.registry import get_registry
from repryntt.core.frameworks.runtime import get_runtime
from repryntt.core.frameworks.schema import Framework, FrameworkState


# ── List ─────────────────────────────────────────────────────────────────

def framework_list() -> Dict[str, Any]:
    """Return every known framework spec, with metrics."""
    registry = get_registry()
    out: List[Dict[str, Any]] = []
    for fw in registry.all():
        out.append({
            "id": fw.id,
            "label": fw.label,
            "description": fw.description,
            "states": [s.name for s in fw.states],
            "version": fw.version,
            "runs": fw.runs,
            "wins": fw.wins,
            "losses": fw.losses,
            "win_rate": round(fw.win_rate, 3),
            "match_keywords": fw.match_keywords,
            "lineage": fw.lineage,
        })
    out.sort(key=lambda d: d["label"].lower())
    return {"ok": True, "frameworks": out, "count": len(out)}


# ── Spawn ────────────────────────────────────────────────────────────────

def framework_spawn(framework_id: str, goal: str, *,
                    target: str = "", spawned_by: str = "agent") -> Dict[str, Any]:
    """Start a new running instance of ``framework_id`` with ``goal``."""
    runtime = get_runtime()
    try:
        inst = runtime.spawn(framework_id, goal=goal, target=target, spawned_by=spawned_by)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "instance_id": inst.id,
        "framework_id": inst.framework_id,
        "current_state": inst.current_state,
        "guidance": runtime.guidance_for(inst.id),
    }


# ── Status ───────────────────────────────────────────────────────────────

def framework_instance_status(instance_id: str = "") -> Dict[str, Any]:
    """Return status of one Layer 3 instance, or all active instances if no id given."""
    runtime = get_runtime()
    if instance_id:
        inst = runtime.load(instance_id)
        if inst is None:
            return {"ok": False, "error": f"unknown instance: {instance_id}"}
        return {
            "ok": True,
            "instance": inst.to_dict(),
            "guidance": runtime.guidance_for(instance_id),
        }
    actives = runtime.active_instances()
    return {
        "ok": True,
        "active_count": len(actives),
        "instances": [
            {
                "id": i.id,
                "framework_id": i.framework_id,
                "goal": i.goal,
                "target": i.target,
                "state": i.current_state,
                "heartbeats_in_state": i.heartbeats_in_state,
                "updated": i.updated,
            }
            for i in actives
        ],
    }


# ── Tick / Update (drive an instance forward) ────────────────────────────

def framework_instance_update(instance_id: str, working_state: Dict[str, Any]) -> Dict[str, Any]:
    """Merge ``working_state`` into a Layer 3 instance without ticking.

    Use this to accumulate artefacts across several actions before asking
    the runtime to evaluate the gate.
    """
    runtime = get_runtime()
    inst = runtime.load(instance_id)
    if inst is None:
        return {"ok": False, "error": f"unknown instance: {instance_id}"}
    if not isinstance(working_state, dict):
        return {"ok": False, "error": "working_state must be a dict"}
    inst.working_state.update(working_state)
    runtime._save_instance(inst)
    return {
        "ok": True,
        "instance_id": inst.id,
        "working_state_keys": list(inst.working_state.keys()),
    }


def framework_tick(instance_id: str,
                   working_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Advance one heartbeat — evaluate gate, advance/hold/escalate.

    Optionally merges ``working_state`` before evaluating.
    """
    runtime = get_runtime()
    result = runtime.tick(instance_id, working_state_update=working_state)
    if result.get("ok"):
        result["guidance"] = runtime.guidance_for(instance_id)
    return result


def framework_score(instance_id: str, score: int, notes: str = "") -> Dict[str, Any]:
    """Score an instance (1-5) and close it, updating spec stats."""
    return get_runtime().score_and_close(instance_id, score=score, notes=notes)


# ── Propose (evolution) ──────────────────────────────────────────────────

def framework_propose_mutation(base_id: str, new_id: str,
                               spec_patch: Dict[str, Any]) -> Dict[str, Any]:
    """Fork ``base_id`` into a new framework ``new_id`` applying ``spec_patch``.

    ``spec_patch`` is a shallow-merge over the base spec dict. Allowed top-level
    keys: ``label``, ``description``, ``match_keywords``, ``tags``,
    ``success_criteria``, ``states`` (full replacement). The new spec
    automatically records ``base_id`` in its lineage.

    Example::

        framework_propose_mutation(
            base_id="deep_research",
            new_id="deep_research_fast",
            spec_patch={
                "label": "Deep Research (Fast)",
                "states": [... shortened state list ...],
            },
        )
    """
    registry = get_registry()
    allowed = {"label", "description", "match_keywords", "tags",
               "success_criteria", "states"}
    unknown = set(spec_patch) - allowed
    if unknown:
        return {
            "ok": False,
            "error": f"unknown patch keys: {sorted(unknown)} (allowed: {sorted(allowed)})",
        }

    # Validate states shape if present
    if "states" in spec_patch:
        if not isinstance(spec_patch["states"], list) or not spec_patch["states"]:
            return {"ok": False, "error": "states must be a non-empty list"}
        for i, raw in enumerate(spec_patch["states"]):
            if not isinstance(raw, dict) or "name" not in raw or "guidance" not in raw:
                return {
                    "ok": False,
                    "error": f"state[{i}] must be a dict with at least name and guidance",
                }

    def _mutate(fw: Framework) -> Framework:
        for k, v in spec_patch.items():
            if k == "states":
                fw.states = [FrameworkState.from_dict(s) for s in v]
                # Fill label if missing
                for s in fw.states:
                    if not s.label:
                        s.label = s.name.replace("_", " ").title()
            elif hasattr(fw, k):
                setattr(fw, k, v)
        return fw

    try:
        child = registry.fork(base_id, new_id, _mutate)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "new_framework_id": child.id,
        "lineage": child.lineage,
        "states": [s.name for s in child.states],
        "version": child.version,
    }


__all__ = [
    "framework_list",
    "framework_spawn",
    "framework_instance_status",
    "framework_instance_update",
    "framework_tick",
    "framework_score",
    "framework_propose_mutation",
]
