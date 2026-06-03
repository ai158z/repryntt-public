"""
repryntt.agents.coherence_eval — Deterministic 5-axis coherence scoring.

The deep-dive measured that 77% of self-evals collapse to a single flat score
(uniformly 3/5) because the prompt asks the model to introspect on an axis it
has no anchor for. This module replaces the parts of self-evaluation that can
be measured directly from artifacts on disk:

    identity   — model-judged (subjective: do I sound like myself?)
    task       — DETERMINISTIC for code artifacts (did the success criterion verify?)
    goal       — model-judged (did this advance my locked chain's goal?)
    reality    — DETERMINISTIC for code artifacts (does the code actually run?)
    semantic   — model-judged (is the language coherent/grounded?)

For non-code artifacts we return the model-only path and let the LLM score.
For code artifacts, `reality` and `task` come from real subprocess execution —
the model never gets to claim 5/5 on a module that doesn't import.

Output schema (per axis):
    {"score": int 1..5, "source": "computed" | "model", "detail": str}

Aggregate: `{axes: {...}, claimed_vs_computed: {...}, coherence: float 0..10}`
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import task_types


# ─── Score primitives ─────────────────────────────────────────────────


@dataclass
class AxisScore:
    score: int            # 1..5
    source: str           # "computed" or "model"
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"score": int(self.score), "source": self.source, "detail": self.detail[:400]}


def _clamp(n: int) -> int:
    return max(1, min(5, int(n)))


# ─── Computed axes for code artifacts ─────────────────────────────────


def score_reality_for_code(expected_location: str,
                           artifact_root: Optional[Path] = None) -> AxisScore:
    """Reality axis from real interpreter feedback.

        5 — module imports AND tests pass
        3 — module imports but tests fail OR no tests yet
        1 — module fails to import / file missing / syntax error

    Anything else (non-code, non-Python) returns score=3, source="model" so
    the caller routes to the model's judgement for that axis.
    """
    if not expected_location or not expected_location.endswith(".py"):
        return AxisScore(3, "model", "non-code artifact — see model judgement")

    v = task_types.verify_python_module(expected_location, artifact_root=artifact_root,
                                        allow_no_tests=False)
    if v.passed:
        return AxisScore(5, "computed", v.detail)
    # Distinguish "imports but tests fail / missing" from "doesn't even import"
    if "import failed" in v.detail or "file not found" in v.detail:
        return AxisScore(1, "computed", v.detail)
    # imports-ok but pytest didn't pass / no tests yet
    return AxisScore(3, "computed", v.detail)


def score_reality_for_motion(motion_report: Any) -> Optional[AxisScore]:
    """Reality axis from proprioception (Fix #E4 — embodied coherence).

    Inputs a hardware.proprioception.MotionReport (or anything with the
    .has_recent_command / .consistent / .stuck_streak attributes). Returns:

        5 — recent commands moved the body as expected
        3 — no recent motion commands (idle is not a reality failure)
        2 — one cycle of commanded motion with no observed motion
        1 — sustained stuck streak (3+ consecutive discrepancies)

    Returns None when the input isn't a usable motion report — caller
    falls back to other axes.
    """
    if motion_report is None:
        return None
    has_cmd = getattr(motion_report, "has_recent_command", False)
    consistent = getattr(motion_report, "consistent", True)
    streak = int(getattr(motion_report, "stuck_streak", 0) or 0)
    summary = getattr(motion_report, "summary", "") or ""

    if not has_cmd:
        return AxisScore(3, "computed", "no recent motion commands — idle")
    if consistent:
        return AxisScore(5, "computed", summary or "commanded motion observed")
    if streak >= 3:
        return AxisScore(1, "computed", f"sustained stuck streak {streak}: {summary[:120]}")
    return AxisScore(2, "computed", summary[:160] or "commanded motion did not occur")


def score_task_for_code(expected_location: str,
                       success_criterion: str,
                       artifact_root: Optional[Path] = None) -> AxisScore:
    """Task axis: capped at 3 unless the success_criterion verifies.

    Without a success_criterion we can't claim 5 — that's the whole point of
    typed deliverables. Returns 3 for "criterion absent" and the model can't
    overwrite it.
    """
    if not success_criterion:
        return AxisScore(3, "computed", "no success_criterion declared — cannot earn task=5")
    if not expected_location or not expected_location.endswith(".py"):
        return AxisScore(3, "model", "non-code artifact — see model judgement")
    v = task_types.verify_python_module(expected_location, artifact_root=artifact_root,
                                        allow_no_tests=False)
    if v.passed:
        return AxisScore(5, "computed", f"criterion verified: {v.detail}")
    return AxisScore(2, "computed", f"criterion failed: {v.detail}")


# ─── Model-judged axes (the rubric prompt builds on this) ─────────────


# Rubric snippet the prompt-builder splices in. Forbids uniform scores —
# this is the single most-impactful prompt edit we make in this fix.
DIFFERENTIATION_RULE = (
    "RUBRIC RULES — read before scoring:\n"
    "  • Score each axis 1..5 independently. Anchors:\n"
    "      identity: 5 = sounds clearly like me, 1 = unrecognizable / generic\n"
    "      goal:     5 = advanced the locked chain's goal, 1 = no progress / off-goal\n"
    "      semantic: 5 = every claim is grounded in tool data, 1 = vague / unsourced\n"
    "  • At least TWO axes MUST differ by ≥ 1 point. A uniform vector\n"
    "    (e.g. {3,3,3}) means you did not actually evaluate — the system\n"
    "    will reject it and re-prompt you.\n"
    "  • The `reality` and `task` axes are computed by the system from real\n"
    "    execution; DO NOT score them yourself. Only score identity, goal, semantic.\n"
)


# ─── Aggregate ────────────────────────────────────────────────────────


@dataclass
class CoherenceVerdict:
    axes: Dict[str, AxisScore]
    claimed_vs_computed: Dict[str, Dict[str, int]] = field(default_factory=dict)

    @property
    def coherence(self) -> float:
        """Aggregate 0..10 from the 5 axes."""
        if not self.axes:
            return 0.0
        total = sum(a.score for a in self.axes.values())
        # 5 axes × 5 = 25 max → scale to 10
        return round(total / 25.0 * 10.0, 2)

    @property
    def is_flat(self) -> bool:
        """All axes identical → flattening collapse."""
        vals = {a.score for a in self.axes.values()}
        return len(vals) <= 1 and len(self.axes) >= 3

    def to_dict(self) -> Dict[str, Any]:
        return {
            "axes": {k: v.to_dict() for k, v in self.axes.items()},
            "coherence": self.coherence,
            "is_flat": self.is_flat,
            "claimed_vs_computed": self.claimed_vs_computed,
        }


def evaluate_artifact(task_like: Any,
                      artifact_root: Optional[Path] = None,
                      model_scores: Optional[Dict[str, int]] = None,
                      model_details: Optional[Dict[str, str]] = None) -> CoherenceVerdict:
    """Build a 5-axis verdict.

    Inputs:
      task_like      — Task object or dict (uses expected_location, success_criterion)
      artifact_root  — where files live; falls back to cwd / ~/.repryntt/workspace
      model_scores   — optional {"identity": int, "goal": int, "semantic": int}
                       supplied by the LLM's self-eval. May also include
                       "task" / "reality" — if so we record them in
                       claimed_vs_computed for audit but ignore them.

    Outputs: a CoherenceVerdict with all 5 axes filled.
    """
    def _g(k: str) -> str:
        if isinstance(task_like, dict):
            return task_like.get(k, "") or ""
        return getattr(task_like, k, "") or ""

    loc = _g("expected_location")
    crit = _g("success_criterion")

    axes: Dict[str, AxisScore] = {}
    axes["reality"] = score_reality_for_code(loc, artifact_root=artifact_root)
    axes["task"] = score_task_for_code(loc, crit, artifact_root=artifact_root)

    # Subjective axes come from the model (or default to 3 if absent)
    model_scores = model_scores or {}
    model_details = model_details or {}
    for axis in ("identity", "goal", "semantic"):
        raw = _clamp(model_scores.get(axis, 3))
        axes[axis] = AxisScore(raw, "model", model_details.get(axis, ""))

    verdict = CoherenceVerdict(axes=axes)

    # Audit: did the model try to claim something we measured?
    for axis in ("reality", "task"):
        if axis in model_scores:
            claimed = _clamp(model_scores[axis])
            computed = axes[axis].score
            if claimed != computed:
                verdict.claimed_vs_computed[axis] = {
                    "claimed": claimed,
                    "computed": computed,
                    "delta": claimed - computed,
                }
    return verdict
