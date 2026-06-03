"""
repryntt.core.frameworks.schema — The one shape every framework fits.

Every operational pattern (research, build, diagnose, explore, ...) is an
instance of :class:`Framework`. Running executions are :class:`FrameworkInstance`
objects. Gates evaluate whether a state has produced its required outputs.

All classes are plain dataclasses with ``to_dict``/``from_dict`` helpers so
they can be persisted as JSON and authored by the agent.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


# ── Status enum ──────────────────────────────────────────────────────────

class InstanceStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


# ── Gate result ──────────────────────────────────────────────────────────

@dataclass
class GateResult:
    """Outcome of evaluating a state's gate against an instance's working_state."""
    passed: bool
    missing: List[str] = field(default_factory=list)   # missing required keys
    too_short: Dict[str, int] = field(default_factory=dict)   # key -> actual length
    too_few: Dict[str, int] = field(default_factory=dict)     # list key -> actual count
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── Framework state (one step in the state machine) ──────────────────────

@dataclass
class FrameworkState:
    """A single state in a framework's state machine."""

    name: str                                   # unique within framework (e.g. "gather")
    label: str                                  # human-readable (e.g. "Gather Evidence")
    guidance: str                               # injected into PLAN prompt when active
    tools: List[str] = field(default_factory=list)   # suggested tool names
    gate: Dict[str, Any] = field(default_factory=dict)
    #   gate schema:
    #     required_keys:    [str]            - working_state keys that must exist + be truthy
    #     min_length:       {key: int}       - str keys must be >= N chars
    #     min_list_length:  {key: int}       - list keys must have >= N items
    #     min_numeric:      {key: float}     - numeric keys must be >= N
    max_heartbeats: int = 3                     # after this many ticks, escalate
    on_fail_state: str = ""                     # state to jump to if gate fails after max_heartbeats
    on_pass_state: str = ""                     # state to advance to (default: next in list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FrameworkState":
        valid = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in valid})

    def evaluate_gate(self, working_state: Dict[str, Any]) -> GateResult:
        """Check if this state's gate is satisfied by the current working_state."""
        gate = self.gate or {}
        missing: List[str] = []
        too_short: Dict[str, int] = {}
        too_few: Dict[str, int] = {}

        required_keys = gate.get("required_keys") or []
        for key in required_keys:
            val = working_state.get(key)
            if val in (None, "", [], {}):
                missing.append(key)

        min_length = gate.get("min_length") or {}
        for key, minimum in min_length.items():
            val = working_state.get(key)
            if isinstance(val, str) and len(val) < minimum:
                too_short[key] = len(val)
            elif not isinstance(val, str):
                # Coerce — None counts as 0-length missing
                if key not in missing:
                    too_short[key] = 0

        min_list_length = gate.get("min_list_length") or {}
        for key, minimum in min_list_length.items():
            val = working_state.get(key)
            if isinstance(val, list) and len(val) < minimum:
                too_few[key] = len(val)
            elif not isinstance(val, list):
                too_few[key] = 0

        min_numeric = gate.get("min_numeric") or {}
        for key, minimum in min_numeric.items():
            val = working_state.get(key)
            try:
                if float(val) < float(minimum):
                    too_short[key] = int(float(val))
            except (TypeError, ValueError):
                if key not in missing:
                    missing.append(key)

        passed = not (missing or too_short or too_few)
        parts: List[str] = []
        if missing:
            parts.append(f"missing: {', '.join(missing)}")
        if too_short:
            parts.append("too short: " + ", ".join(f"{k}={v}" for k, v in too_short.items()))
        if too_few:
            parts.append("too few: " + ", ".join(f"{k}={v}" for k, v in too_few.items()))
        msg = "; ".join(parts) if parts else "gate passed"
        return GateResult(
            passed=passed,
            missing=missing,
            too_short=too_short,
            too_few=too_few,
            message=msg,
        )


# ── Framework (the spec itself) ──────────────────────────────────────────

@dataclass
class Framework:
    """A framework spec. Persisted as JSON under ~/.repryntt/frameworks/."""

    id: str                                     # stable slug, e.g. "deep_research"
    label: str
    description: str
    states: List[FrameworkState]
    match_keywords: List[str] = field(default_factory=list)
    version: int = 1
    lineage: List[str] = field(default_factory=list)      # ancestor framework ids
    tags: List[str] = field(default_factory=list)
    success_criteria: str = ""                            # human-written summary
    author: str = "system"                                 # "system" or agent name
    created: float = field(default_factory=time.time)
    # Outcome history (rolling — trimmed by registry)
    runs: int = 0
    wins: int = 0                                          # completed with score >= 3
    losses: int = 0                                        # failed/abandoned/score<3

    # ── Derived metrics ──

    @property
    def win_rate(self) -> float:
        if self.runs == 0:
            return 0.0
        return self.wins / max(1, self.runs)

    def state_names(self) -> List[str]:
        return [s.name for s in self.states]

    def get_state(self, name: str) -> Optional[FrameworkState]:
        for s in self.states:
            if s.name == name:
                return s
        return None

    # ── Serialisation ──

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "states": [s.to_dict() for s in self.states],
            "match_keywords": self.match_keywords,
            "version": self.version,
            "lineage": self.lineage,
            "tags": self.tags,
            "success_criteria": self.success_criteria,
            "author": self.author,
            "created": self.created,
            "runs": self.runs,
            "wins": self.wins,
            "losses": self.losses,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Framework":
        states = [FrameworkState.from_dict(s) for s in d.get("states", [])]
        valid = {f for f in cls.__dataclass_fields__}
        kw = {k: v for k, v in d.items() if k in valid and k != "states"}
        kw["states"] = states
        return cls(**kw)

    def next_state_after(self, current: str) -> Optional[str]:
        """Resolve the next state given the current one (honours on_pass_state)."""
        cur = self.get_state(current)
        if cur and cur.on_pass_state:
            return cur.on_pass_state
        names = self.state_names()
        if current not in names:
            return None
        idx = names.index(current)
        if idx + 1 >= len(names):
            return None
        return names[idx + 1]


# ── Framework instance (a running execution) ─────────────────────────────

@dataclass
class FrameworkInstance:
    """A running execution of a framework. Persisted as JSON per instance."""

    id: str                                     # stable unique id
    framework_id: str                           # spec id
    framework_version: int                      # locked at spawn time
    goal: str                                   # what this run is trying to accomplish
    target: str = ""                            # entity the work is about (MoonPay, place_44, etc.)
    status: InstanceStatus = InstanceStatus.ACTIVE
    current_state: str = ""                     # name of active state
    heartbeats_in_state: int = 0                # ticks spent in current state
    working_state: Dict[str, Any] = field(default_factory=dict)   # the evolving artifact bundle
    transitions: List[Dict[str, Any]] = field(default_factory=list)  # state transition log
    score: int = 0                              # final outcome score (1-5), 0 = not scored
    notes: str = ""
    spawned_by: str = ""                        # agent name or "auto"
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    completed: float = 0.0

    @classmethod
    def new(cls, framework: Framework, goal: str, *,
            target: str = "", spawned_by: str = "auto",
            initial_state: str = "") -> "FrameworkInstance":
        raw = f"{framework.id}|{goal}|{time.time()}|{target}"
        iid = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        start = initial_state or (framework.states[0].name if framework.states else "")
        return cls(
            id=iid,
            framework_id=framework.id,
            framework_version=framework.version,
            goal=goal,
            target=target,
            current_state=start,
            spawned_by=spawned_by,
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value if isinstance(self.status, InstanceStatus) else str(self.status)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FrameworkInstance":
        valid = {f for f in cls.__dataclass_fields__}
        kw = {k: v for k, v in d.items() if k in valid}
        if isinstance(kw.get("status"), str):
            try:
                kw["status"] = InstanceStatus(kw["status"])
            except ValueError:
                kw["status"] = InstanceStatus.ACTIVE
        return cls(**kw)

    def record_transition(self, from_state: str, to_state: str,
                          reason: str, gate: Optional[GateResult] = None) -> None:
        self.transitions.append({
            "at": time.time(),
            "from": from_state,
            "to": to_state,
            "reason": reason,
            "gate": gate.to_dict() if gate else None,
        })
        self.updated = time.time()


__all__ = [
    "Framework",
    "FrameworkState",
    "FrameworkInstance",
    "GateResult",
    "InstanceStatus",
]
