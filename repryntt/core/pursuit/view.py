"""
PursuitView — read-only adapter that surfaces legacy task_queue and
reasoning_chain entries as Pursuits without touching their storage.

Phase 1: this is a passive translator. No legacy file is mutated by
this module. Phase 2 will use it as the unified read path so the
selector sees one pool regardless of where each item is stored.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .model import Pursuit, PursuitStep
from .store import PursuitStore

logger = logging.getLogger(__name__)


def _safe_read_json(path: Path) -> Any:
    try:
        if not path.exists():
            return None
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.debug(f"PursuitView: cannot read {path}: {e}")
        return None


class PursuitView:
    """Unified read view over (store, task_queue.json, reasoning_chain.json)."""

    def __init__(
        self,
        store: PursuitStore,
        task_queue_path: Optional[str | Path] = None,
        reasoning_chain_path: Optional[str | Path] = None,
    ):
        self.store = store
        self.task_queue_path = Path(task_queue_path) if task_queue_path else None
        self.reasoning_chain_path = (
            Path(reasoning_chain_path) if reasoning_chain_path else None
        )

    # ── Public API ─────────────────────────────────────────

    def all(self) -> List[Pursuit]:
        """All active pursuits from every source, deduplicated by legacy_ref."""
        pool: List[Pursuit] = []
        seen_refs = set()

        # Native pursuits first (highest fidelity)
        for p in self.store.active():
            pool.append(p)
            if p.legacy_ref:
                seen_refs.add(p.legacy_ref)

        # Then legacy task_queue rows
        for p in self._pursuits_from_task_queue():
            if p.legacy_ref and p.legacy_ref in seen_refs:
                continue
            pool.append(p)
            if p.legacy_ref:
                seen_refs.add(p.legacy_ref)

        # Then the active reasoning chain (at most one)
        chain = self._pursuit_from_reasoning_chain()
        if chain and (not chain.legacy_ref or chain.legacy_ref not in seen_refs):
            pool.append(chain)

        return pool

    # ── Translators ────────────────────────────────────────

    def _pursuits_from_task_queue(self) -> List[Pursuit]:
        if not self.task_queue_path:
            return []
        raw = _safe_read_json(self.task_queue_path)
        if not isinstance(raw, dict):
            return []
        tasks = raw.get("tasks") or raw.get("queue") or []
        out: List[Pursuit] = []
        for t in tasks:
            if not isinstance(t, dict):
                continue
            status = (t.get("status") or "").lower()
            if status in ("completed", "done", "abandoned", "cancelled"):
                continue
            tid = str(t.get("id") or t.get("task_id") or "")
            goal = str(t.get("goal") or t.get("description") or t.get("title") or "")
            if not goal:
                continue
            source = "operator" if t.get("source") == "operator" else "daily_plan"
            out.append(Pursuit(
                goal=goal,
                deliverable=str(t.get("deliverable") or t.get("success_criteria") or ""),
                source=source,
                character="duty",
                phase_guide=list(t.get("phase_guide") or []),
                target_steps=int(t.get("target_steps") or 1),
                locked=bool(t.get("locked", False)),
                priority=float(t.get("priority") or 0.0),
                created_at=float(t.get("created_at") or time.time()),
                last_touched=float(t.get("last_touched") or t.get("updated_at") or time.time()),
                legacy_ref=f"task_queue:{tid}" if tid else None,
            ))
        return out

    def _pursuit_from_reasoning_chain(self) -> Optional[Pursuit]:
        if not self.reasoning_chain_path:
            return None
        raw = _safe_read_json(self.reasoning_chain_path)
        if not isinstance(raw, dict):
            return None
        if raw.get("complete") or raw.get("abandoned"):
            return None
        goal = str(raw.get("goal") or "")
        if not goal:
            return None
        steps_raw = raw.get("steps") or []
        history: List[PursuitStep] = []
        for s in steps_raw:
            if isinstance(s, dict):
                history.append(PursuitStep(
                    ts=float(s.get("ts") or time.time()),
                    hypothesis=str(s.get("hypothesis", "")),
                    action=str(s.get("action") or s.get("plan") or ""),
                    observation=str(s.get("observation") or s.get("result") or ""),
                    updated_belief=str(s.get("updated_belief", "")),
                ))
        goal_type = (raw.get("goal_type") or "").lower()
        source_raw = (raw.get("source") or "").lower()
        if source_raw == "auto_followup":
            source = "follow_up"
        elif source_raw in ("operator", "daily_plan", "interest", "curiosity", "self"):
            source = source_raw
        else:
            source = "self"
        character = "duty"
        if source == "follow_up":
            character = "growth"
        return Pursuit(
            goal=goal,
            deliverable=str(raw.get("deliverable") or raw.get("success_criteria") or ""),
            source=source,
            character=character,
            phase_guide=list(raw.get("phase_guide") or []),
            target_steps=int(raw.get("target_steps") or len(history) or 1),
            state=dict(raw.get("working_state") or {}),
            history=history,
            locked=(goal_type == "locked"),
            priority=float(raw.get("priority") or 0.0),
            created_at=float(raw.get("created_at") or time.time()),
            last_touched=float(raw.get("last_touched") or time.time()),
            legacy_ref="reasoning_chain",
        )
