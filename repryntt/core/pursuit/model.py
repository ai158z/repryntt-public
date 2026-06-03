"""
Pursuit dataclass — the single primitive that replaces task / chain / exploration.

Phase 1: pure data model + serialization. No heartbeat integration yet.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Literal, Optional

PursuitSource = Literal[
    "operator",       # human-issued task
    "daily_plan",     # daily plan item
    "interest",       # standing pursuit derived from INTERESTS.md
    "curiosity",      # one-shot curiosity probe
    "follow_up",      # auto_followup spawn from a prior plan
    "self",           # agent-authored
]

PursuitCharacter = Literal["duty", "growth", "exploration"]


@dataclass
class PursuitStep:
    """One heartbeat-step inside a pursuit. Carries why, not just what."""
    ts: float
    hypothesis: str = ""
    action: str = ""
    observation: str = ""
    updated_belief: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PursuitStep":
        return cls(
            ts=float(d.get("ts", time.time())),
            hypothesis=str(d.get("hypothesis", "")),
            action=str(d.get("action", "")),
            observation=str(d.get("observation", "")),
            updated_belief=str(d.get("updated_belief", "")),
        )


@dataclass
class Pursuit:
    """Unified scheduling unit."""

    goal: str
    deliverable: str
    source: PursuitSource = "self"
    character: PursuitCharacter = "duty"
    id: str = field(default_factory=lambda: f"p_{uuid.uuid4().hex[:12]}")
    phase_guide: List[str] = field(default_factory=list)
    target_steps: int = 1
    state: Dict[str, Any] = field(default_factory=dict)
    history: List[PursuitStep] = field(default_factory=list)
    locked: bool = False
    priority: float = 0.0
    created_at: float = field(default_factory=time.time)
    last_touched: float = field(default_factory=time.time)
    completed: bool = False
    abandoned: bool = False
    abandoned_reason: str = ""
    # Provenance — where did this pursuit come from in the legacy system?
    legacy_ref: Optional[str] = None  # e.g. "task_queue:abc123" or "reasoning_chain"
    # Topic tag (for interest-derived pursuits — e.g. "physics", "cancer")
    topic: str = ""

    # ── Lifecycle verbs ────────────────────────────────────

    def touch(self) -> None:
        self.last_touched = time.time()

    def append_step(self, step: PursuitStep) -> None:
        self.history.append(step)
        self.touch()

    def abandon(self, reason: str) -> None:
        """The agent's right to drop dead work."""
        self.abandoned = True
        self.abandoned_reason = reason or "no reason given"
        self.touch()

    def observe(self, note: str) -> None:
        """Heartbeat that consumes a tick without requiring action."""
        self.append_step(PursuitStep(
            ts=time.time(),
            hypothesis="(observe-only)",
            action="observe",
            observation=note,
            updated_belief="",
        ))

    def complete(self, summary: str = "") -> None:
        self.completed = True
        if summary:
            self.append_step(PursuitStep(
                ts=time.time(),
                hypothesis="",
                action="complete",
                observation=summary,
                updated_belief="",
            ))
        self.touch()

    # ── Status helpers ─────────────────────────────────────

    @property
    def active(self) -> bool:
        return not (self.completed or self.abandoned)

    @property
    def steps_done(self) -> int:
        return len(self.history)

    @property
    def staleness_seconds(self) -> float:
        return max(0.0, time.time() - self.last_touched)

    # ── Serialization ──────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["history"] = [asdict(s) for s in self.history]
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Pursuit":
        history_raw = d.get("history", []) or []
        history = [PursuitStep.from_dict(s) for s in history_raw]
        return cls(
            goal=str(d.get("goal", "")),
            deliverable=str(d.get("deliverable", "")),
            source=d.get("source", "self"),
            character=d.get("character", "duty"),
            id=str(d.get("id") or f"p_{uuid.uuid4().hex[:12]}"),
            phase_guide=list(d.get("phase_guide", []) or []),
            target_steps=int(d.get("target_steps", 1) or 1),
            state=dict(d.get("state", {}) or {}),
            history=history,
            locked=bool(d.get("locked", False)),
            priority=float(d.get("priority", 0.0) or 0.0),
            created_at=float(d.get("created_at", time.time()) or time.time()),
            last_touched=float(d.get("last_touched", time.time()) or time.time()),
            completed=bool(d.get("completed", False)),
            abandoned=bool(d.get("abandoned", False)),
            abandoned_reason=str(d.get("abandoned_reason", "")),
            legacy_ref=d.get("legacy_ref"),
            topic=str(d.get("topic", "")),
        )
