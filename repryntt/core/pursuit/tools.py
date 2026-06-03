"""
Pursuit tools — agent-callable verbs for the unified scheduling primitive.

These tools give the agent the right to:
- abandon a Pursuit it judges dead (with a reason)
- observe a heartbeat without committing to action
- record a step with hypothesis/observation/updated_belief (Phase 6)
- list active Pursuits and view selector reasoning

Phase 4 + Phase 6 of the Unified Autonomy Refactor.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from .model import Pursuit, PursuitStep
from .store import PursuitStore
from .selector import select_pursuit

logger = logging.getLogger(__name__)


def _operator_workspace() -> Path:
    return Path.home() / ".repryntt" / "workspace" / "agents" / "operator"


def _store() -> PursuitStore:
    return PursuitStore(_operator_workspace() / "pursuits.json")


# ── Verbs ──────────────────────────────────────────────────────────────


def pursuit_list(active_only: bool = True, **_) -> str:
    """List Pursuits (the unified scheduling primitive: replaces task/chain/exploration).

    Each Pursuit has a goal, a deliverable, a character (duty/growth/exploration),
    and a source (operator/daily_plan/interest/curiosity/follow_up/self).

    Parameters:
        active_only: If True (default), only show active (not completed/abandoned).
    """
    store = _store()
    items = store.active() if active_only else store.all()
    if not items:
        return "No pursuits in pool."
    lines = [f"📋 Pursuits ({len(items)} {'active' if active_only else 'total'}):"]
    for p in items:
        flag = "🔒 " if p.locked else ""
        status = "✅" if p.completed else ("🪦" if p.abandoned else "·")
        lines.append(
            f"  {status} [{p.id}] {flag}{p.goal[:90]}\n"
            f"      char={p.character} src={p.source} topic={p.topic or '—'} "
            f"steps={p.steps_done}/{p.target_steps} stale={int(p.staleness_seconds)}s"
        )
    return "\n".join(lines)


def pursuit_abandon(pursuit_id: str = "", reason: str = "", **_) -> str:
    """Abandon a Pursuit you judge to be a dead end. State WHY.

    This is your right to drop work that has no deliverable, repeats itself,
    or has a premise that was disproven. The reason is recorded for review.

    Parameters:
        pursuit_id: The Pursuit id (e.g. 'p_a1b2c3d4...').
        reason: Required — why you're abandoning. Be specific.
    """
    if not pursuit_id:
        return "ERROR: pursuit_id required."
    if not reason or len(reason.strip()) < 10:
        return ("ERROR: reason required (>=10 chars). State why this pursuit is dead — "
                "premise disproven, no deliverable, looping, etc.")
    store = _store()
    p = store.get(pursuit_id)
    if not p:
        return f"ERROR: pursuit '{pursuit_id}' not found."
    if not p.active:
        return f"NOTE: pursuit '{pursuit_id}' is already inactive (completed={p.completed}, abandoned={p.abandoned})."
    p.abandon(reason.strip())
    store.upsert(p)
    logger.info(f"🪦 Pursuit abandoned by agent: {p.goal[:80]} — reason: {reason[:120]}")
    return f"Abandoned [{p.id}] {p.goal[:80]} — reason recorded."


def pursuit_observe(note: str = "", pursuit_id: str = "", **_) -> str:
    """Record an observe-only heartbeat — you watched, you didn't act.

    Use this when the right move is to read, watch, or wait — not act.
    Forcing action every cycle produces theater. Honest observation is fine.

    Parameters:
        note: What you observed. Required (>=10 chars).
        pursuit_id: Optional — if given, attaches the observation to that
                    Pursuit. If omitted, the most-recent active Pursuit is used.
    """
    if not note or len(note.strip()) < 10:
        return "ERROR: note required (>=10 chars)."
    store = _store()
    target: Optional[Pursuit] = None
    if pursuit_id:
        target = store.get(pursuit_id)
        if target is None:
            return f"ERROR: pursuit '{pursuit_id}' not found."
    else:
        actives = store.active()
        if not actives:
            return "ERROR: no active pursuits — nothing to attach the observation to."
        target = max(actives, key=lambda p: p.last_touched)
    target.observe(note.strip())
    store.upsert(target)
    return f"Observed [{target.id}] {target.goal[:60]} — note recorded."


def pursuit_record_step(
    pursuit_id: str = "",
    hypothesis: str = "",
    action: str = "",
    observation: str = "",
    updated_belief: str = "",
    **_,
) -> str:
    """Record a structured step on a Pursuit: hypothesis → action → observation → updated_belief.

    Phase 6 — memory of WHY, not just WHAT. Every step carries:
    - hypothesis: what you expected
    - action: what you did
    - observation: what actually happened
    - updated_belief: how the result changed your model

    Parameters:
        pursuit_id: Required.
        hypothesis: What you expected before acting.
        action: What you did (tool used, query made, etc.).
        observation: What actually happened.
        updated_belief: What you now believe that you didn't before.
    """
    if not pursuit_id:
        return "ERROR: pursuit_id required."
    if not action:
        return "ERROR: action required."
    store = _store()
    p = store.get(pursuit_id)
    if not p:
        return f"ERROR: pursuit '{pursuit_id}' not found."
    p.append_step(PursuitStep(
        ts=time.time(),
        hypothesis=hypothesis.strip(),
        action=action.strip(),
        observation=observation.strip(),
        updated_belief=updated_belief.strip(),
    ))
    store.upsert(p)
    return f"Step recorded on [{p.id}] (now {p.steps_done}/{p.target_steps} steps)."


def pursuit_complete(pursuit_id: str = "", summary: str = "", **_) -> str:
    """Mark a Pursuit complete with a brief summary of the deliverable produced.

    Parameters:
        pursuit_id: Required.
        summary: Required (>=10 chars) — what you produced and where it lives.
    """
    if not pursuit_id:
        return "ERROR: pursuit_id required."
    if not summary or len(summary.strip()) < 10:
        return "ERROR: summary required (>=10 chars) — name the deliverable + path."
    store = _store()
    p = store.get(pursuit_id)
    if not p:
        return f"ERROR: pursuit '{pursuit_id}' not found."
    if not p.active:
        return f"NOTE: pursuit '{pursuit_id}' is already inactive."
    p.complete(summary.strip())
    store.upsert(p)
    return f"Completed [{p.id}] {p.goal[:60]}."


def pursuit_status(pursuit_id: str = "", **_) -> str:
    """Show details for one Pursuit including recent steps and selector status.

    Parameters:
        pursuit_id: The Pursuit id, or empty to show the currently-selected one.
    """
    store = _store()
    if not pursuit_id:
        # Show the currently selected one (re-run selector cheaply)
        try:
            from repryntt.core.hormones.value_compass import ValueCompass
            from repryntt.core.pursuit import PursuitView
            ws = _operator_workspace()
            view = PursuitView(
                store=store,
                task_queue_path=ws / "task_queue.json",
                reasoning_chain_path=ws / "reasoning_chain.json",
            )
            vc = None
            try:
                vc = ValueCompass(
                    bootstrap_dir=Path.home() / ".repryntt" / "brain" / "bootstrap",
                    state_dir=ws,
                )
            except Exception:
                pass
            reasoning = select_pursuit(view.all(), vc)
            if reasoning.pursuit is None:
                return "No active pursuit selected. " + reasoning.rationale
            p = reasoning.pursuit
            header = (
                f"🧭 SELECTED PURSUIT\n"
                f"  why: {reasoning.rationale}\n"
                f"  deficit: duty={reasoning.deficit['duty']:+.2f} "
                f"growth={reasoning.deficit['growth']:+.2f} "
                f"exploration={reasoning.deficit['exploration']:+.2f}\n"
            )
        except Exception as e:
            return f"ERROR: cannot run selector: {e}"
    else:
        p = store.get(pursuit_id)
        if not p:
            return f"ERROR: pursuit '{pursuit_id}' not found."
        header = ""

    lines = [
        header,
        f"[{p.id}] {p.goal}",
        f"  deliverable: {p.deliverable}",
        f"  character={p.character} source={p.source} topic={p.topic or '—'}",
        f"  steps={p.steps_done}/{p.target_steps} active={p.active} locked={p.locked}",
    ]
    if p.phase_guide:
        lines.append("  phase_guide:")
        for i, ph in enumerate(p.phase_guide, 1):
            lines.append(f"    {i}. {ph}")
    if p.history:
        lines.append("  recent steps:")
        for s in p.history[-5:]:
            lines.append(
                f"    · {s.action[:60]}"
                + (f" → {s.observation[:60]}" if s.observation else "")
            )
    if p.abandoned:
        lines.append(f"  ABANDONED: {p.abandoned_reason}")
    return "\n".join([l for l in lines if l])
