"""
repryntt.hardware.robotics_nudge — Keep the body moving.

Analogue of self-prompts (which keep the LLM thinking across idle
heartbeats) but for the robot body. Injects a directive into the
heartbeat context when:

1. An ``embodied_explore`` framework instance is active
2. The explorer is currently idle (not running)
3. The framework's traverse gate has not yet been met (or the
   instance is still in an observation/profile state that needs
   real movement to advance)

Also provides an auto-restart helper the explorer loop calls when
it stops with ``reason == "step_limit"`` while a framework is still
asking for more movement — so a 5-step nibble becomes a real
session instead of ending prematurely.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Sentinel file. When present, the embodied loop is paused: nudges are
# suppressed and force-spawning is disabled. Lets the operator hold the
# robot still while sensors/perception are being repaired without having
# to touch this code. `rm` the file to resume.
_EMBODIED_PAUSED_FLAG = Path.home() / ".repryntt" / "EMBODIED_PAUSED"


def _embodied_paused() -> bool:
    return _EMBODIED_PAUSED_FLAG.exists()


# Only fire the nudge for these framework IDs — don't spam other frameworks.
EMBODIED_FRAMEWORK_IDS = {"embodied_explore"}

# Cooldown between nudges so we don't stack identical messages every cycle.
# Short cooldown — we want the nudge hammering the prompt every heartbeat
# the body is idle. 15s = fires each heartbeat at ~90-120s cadence.
_NUDGE_COOLDOWN_SEC = 15.0
_last_nudge_at: Dict[str, float] = {}

# Body-activity-deficit gate: how many consecutive heartbeats can the body
# be idle (while an embodied_explore framework is active and its traverse
# gate is unmet) before we bypass the LLM and spawn nav_explore directly.
_DEFICIT_HEARTBEAT_THRESHOLD = 3
_deficit_heartbeats: Dict[str, int] = {}
# Cooldown on the force-spawn path so we don't start nav_explore every cycle
_FORCE_SPAWN_COOLDOWN_SEC = 45.0
_last_force_spawn_at: Dict[str, float] = {}


def _get_active_embodied_instance() -> Optional[Any]:
    """Return the most recent active embodied_explore instance, or None."""
    try:
        from repryntt.core.frameworks.runtime import get_runtime
        runtime = get_runtime()
        actives = runtime.active_instances()
    except Exception as e:
        logger.debug(f"robotics_nudge: runtime lookup failed: {e}")
        return None

    candidates = [i for i in actives if i.framework_id in EMBODIED_FRAMEWORK_IDS]
    if not candidates:
        return None
    # Pick the most recently updated one
    candidates.sort(key=lambda i: getattr(i, "updated", ""), reverse=True)
    return candidates[0]


def _get_explorer_status() -> Dict[str, Any]:
    """Safe-wrap the explorer status call."""
    try:
        from repryntt.hardware.explorer import get_explorer
        return get_explorer().status() or {}
    except Exception as e:
        logger.debug(f"robotics_nudge: explorer status failed: {e}")
        return {}


def _get_frontier_suggestions(limit: int = 5) -> List[str]:
    """Return human-readable frontier hints from the spatial map."""
    try:
        from repryntt.hardware.spatial_map import SpatialMap
        smap = SpatialMap()
        frontiers = getattr(smap, "frontiers", []) or []
        seen_dirs: List[str] = []
        hints: List[str] = []
        for f in frontiers:
            if not isinstance(f, dict):
                continue
            direction = f.get("direction") or f.get("dir") or "?"
            note = f.get("note") or f.get("reason") or ""
            # Dedup on direction so we don't suggest "forward" 30 times
            if direction in seen_dirs:
                continue
            seen_dirs.append(direction)
            label = direction if not note else f"{direction} — {note[:60]}"
            hints.append(label)
            if len(hints) >= limit:
                break
        return hints
    except Exception as e:
        logger.debug(f"robotics_nudge: frontier lookup failed: {e}")
        return []


def _needs_more_movement(instance: Any) -> Tuple[bool, str]:
    """Check the instance's traverse gate: has it logged enough real moves?

    Returns ``(needs_more, reason)``.
    """
    try:
        ws = getattr(instance, "working_state", {}) or {}
        mlog = ws.get("movement_log") or []
        n = len(mlog) if isinstance(mlog, list) else 0
        state = getattr(instance, "current_state", "")
        # If we're still in a pre-traverse or traverse phase, demand motion
        motion_states = {"prepare", "observe_baseline", "traverse"}
        if state in motion_states and n < 5:
            return True, f"movement_log={n}/5 (state={state})"
        if state == "observe_post" and n < 5:
            # They jumped past traverse without moving — call it out
            return True, f"jumped to observe_post with only {n}/5 moves"
        return False, f"movement_log={n}, state={state}"
    except Exception as e:
        logger.debug(f"robotics_nudge: gate check failed: {e}")
        return False, "gate_check_error"


def _pick_force_spawn_goal(frontiers: List[str], instance_goal: str) -> str:
    """Choose a physical goal string for auto-spawned nav_explore.

    Prefer the first frontier hint; fall back to the framework instance
    goal; last fall back to 'explore the largest open frontier'.
    """
    if frontiers:
        # First hint already has "direction — note" format
        return f"head toward frontier: {frontiers[0]}"
    if instance_goal:
        return instance_goal
    return "explore the largest open frontier"


def force_spawn_if_deficit() -> Optional[Dict[str, Any]]:
    """If the body has been idle for ``_DEFICIT_HEARTBEAT_THRESHOLD``
    heartbeats while an embodied framework still wants motion, call
    ``explorer.start()`` DIRECTLY — bypassing the LLM. Returns the
    spawn result dict if fired, else None.

    This is the hard backstop for when the LLM keeps writing docs instead
    of driving. Call BEFORE ``get_robotics_nudge()`` each heartbeat.
    """
    if _embodied_paused():
        return None
    instance = _get_active_embodied_instance()
    if instance is None:
        return None

    inst_id = getattr(instance, "id", "?")

    status = _get_explorer_status()
    if bool(status.get("running")):
        # Body moving — reset the deficit counter
        _deficit_heartbeats[inst_id] = 0
        return None

    needs_more, gate_reason = _needs_more_movement(instance)
    if not needs_more:
        _deficit_heartbeats[inst_id] = 0
        return None

    # Increment deficit — body idle, framework wants motion
    _deficit_heartbeats[inst_id] = _deficit_heartbeats.get(inst_id, 0) + 1
    deficit = _deficit_heartbeats[inst_id]

    if deficit < _DEFICIT_HEARTBEAT_THRESHOLD:
        logger.info(
            f"🦾 Body-activity deficit: {deficit}/{_DEFICIT_HEARTBEAT_THRESHOLD} "
            f"heartbeats idle for instance {inst_id} ({gate_reason})"
        )
        return None

    # Force-spawn cooldown check so we don't hammer the explorer
    now = time.time()
    last_spawn = _last_force_spawn_at.get(inst_id, 0.0)
    if now - last_spawn < _FORCE_SPAWN_COOLDOWN_SEC:
        return None

    # BYPASS THE LLM — drive the body directly
    frontiers = _get_frontier_suggestions(limit=3)
    goal = _pick_force_spawn_goal(frontiers, getattr(instance, "goal", "") or "")

    try:
        from repryntt.hardware.explorer import get_explorer
        result = get_explorer().start(goal=goal, steps=100, speed=0.3)
        _last_force_spawn_at[inst_id] = now
        _deficit_heartbeats[inst_id] = 0
        logger.warning(
            f"🦾🚨 BODY DEFICIT FORCE-SPAWN: deficit={deficit} → "
            f"auto-started nav_explore(goal='{goal[:60]}', steps=100). "
            f"Instance={inst_id}, gate={gate_reason}. LLM bypassed."
        )
        return result
    except Exception as e:
        logger.error(f"🦾 Force-spawn failed: {e}", exc_info=True)
        return None


def get_robotics_nudge() -> Optional[str]:
    """Return a heartbeat-context injection string, or None if no nudge needed.

    Call once per heartbeat, before the PLAN phase, and append the
    returned string to ``heartbeat_context``.
    """
    if _embodied_paused():
        return None
    instance = _get_active_embodied_instance()
    if instance is None:
        return None

    # Cooldown check — don't spam identical nudges within the same window
    inst_id = getattr(instance, "id", "?")
    now = time.time()
    last = _last_nudge_at.get(inst_id, 0.0)
    if now - last < _NUDGE_COOLDOWN_SEC:
        return None

    status = _get_explorer_status()
    explorer_running = bool(status.get("running"))
    if explorer_running:
        # Body is already moving — no nudge needed, let it run
        return None

    needs_more, gate_reason = _needs_more_movement(instance)
    if not needs_more:
        return None

    frontiers = _get_frontier_suggestions(limit=5)
    frontier_block = ""
    if frontiers:
        frontier_block = "\nAvailable frontiers from spatial map:\n" + "\n".join(
            f"  • {h}" for h in frontiers
        )

    goal = getattr(instance, "goal", "") or "explore the unexplored frontier"
    state = getattr(instance, "current_state", "?")
    deficit = _deficit_heartbeats.get(inst_id, 0)

    _last_nudge_at[inst_id] = now

    # Escalate language as deficit grows
    if deficit == 0:
        urgency = "YOUR BODY IS IDLE"
    elif deficit == 1:
        urgency = "BODY IDLE — SECOND HEARTBEAT, NO MOVEMENT"
    else:
        urgency = (
            f"BODY IDLE FOR {deficit} HEARTBEATS — NEXT HEARTBEAT "
            f"I WILL SPAWN nav_explore FOR YOU (LLM BYPASS)"
        )

    nudge = (
        f"\n🦾 **ROBOTICS NUDGE — {urgency}**\n"
        f"Active embodied_explore instance: `{inst_id}` (state: `{state}`)\n"
        f"Goal: {goal}\n"
        f"Gate status: {gate_reason} — you need MORE REAL MOVEMENT, not documentation.\n"
        f"Deficit: {deficit}/{_DEFICIT_HEARTBEAT_THRESHOLD} heartbeats\n"
        f"{frontier_block}\n\n"
        f"**THIS HEARTBEAT, YOU MUST CALL:**\n"
        f"    `nav_explore(goal='<physical frontier>', steps=100, speed=0.3)`\n\n"
        f"You can use ANY number of steps — 100, 500, 1000+. No artificial cap.\n"
        f"Match the step count to the exploration goal. Short recon = 50. "
        f"Full room mapping = 500. Extended exploration = 1000+.\n\n"
        f"Rules:\n"
        f"  • Do NOT write verification docs, debugging plans, or capability reports.\n"
        f"  • Do NOT research camera/V4L2/hardware issues — the cameras WORK.\n"
        f"    The V4L2 obsensor warnings at boot are cosmetic (depth-cam probe).\n"
        f"    Camera 0 is capturing 1280x720 frames successfully every cycle.\n"
        f"  • The tank now has onboard lighting — dark scenes are not a blocker.\n"
        f"  • Do NOT spawn another framework. Drive THIS one to completion.\n"
        f"  • Pick a frontier direction from the list above. Go there.\n"
        f"  • After nav_explore finishes, call framework_instance_update to log\n"
        f"    the actual moves into movement_log, then framework_tick.\n"
        f"  • Minimum: 5 distinct movements logged, then you may advance.\n"
    )
    logger.info(
        f"🦾 Robotics nudge emitted for instance {inst_id} "
        f"(state={state}, {gate_reason}, deficit={deficit})"
    )
    return nudge


def should_auto_continue_explorer(stop_reason: str) -> bool:
    """True if the explorer just hit step_limit while a framework still wants motion.

    Called from the explorer loop right after a run ends. If True, the
    caller should spin up a fresh run with a new frontier-based goal.
    """
    if stop_reason != "step_limit":
        return False
    instance = _get_active_embodied_instance()
    if instance is None:
        return False
    needs_more, _ = _needs_more_movement(instance)
    return needs_more


__all__ = [
    "get_robotics_nudge",
    "should_auto_continue_explorer",
    "force_spawn_if_deficit",
]
