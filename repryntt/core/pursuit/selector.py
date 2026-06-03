"""
Pursuit selector — one ranked query replaces three competing gates.

Selection rules (in order):
  1. Active operator-locked pursuits always win (sovereignty).
  2. Otherwise: pick the highest-scored active pursuit, where the score is
     dominated by the budget deficit of the pursuit's character. This is
     what forces exploration when ValueCompass shows e.g. 14D/6G/0E.

Phase 1: pure function, no heartbeat wiring. Selector reasoning is
returned alongside the pick so the plan prompt can show *why* a pursuit
was chosen — no more hidden gates.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .model import Pursuit

# ── Tunable weights ────────────────────────────────────────
W_DEFICIT = 3.0      # most-starved character wins
W_PRIORITY = 1.5     # operator weight
W_RECENCY = 0.5      # don't starve any one pursuit forever
STALENESS_HALFLIFE = 6 * 3600.0  # 6 hours — penalty grows past this


@dataclass
class SelectorReasoning:
    pursuit: Optional[Pursuit]
    score: float
    deficit: Dict[str, float]
    rationale: str
    candidates_considered: int


def _budget_deficit(value_compass) -> Dict[str, float]:
    """Returns {duty, growth, exploration} deficits (target - actual)."""
    if value_compass is None:
        return {"duty": 0.0, "growth": 0.0, "exploration": 0.10}
    try:
        status = value_compass.get_budget_status()
    except Exception:
        return {"duty": 0.0, "growth": 0.0, "exploration": 0.10}
    return {
        "duty": float(status.get("duty_target", 0.70)) - float(status.get("duty_pct", 0.0)),
        "growth": float(status.get("growth_target", 0.20)) - float(status.get("growth_pct", 0.0)),
        "exploration": (
            float(status.get("exploration_target", 0.10))
            - float(status.get("exploration_pct", 0.0))
        ),
    }


def _staleness_factor(p: Pursuit) -> float:
    """0..1 — higher means more stale, deserving a recency bonus."""
    age = max(0.0, time.time() - p.last_touched)
    return min(1.0, age / STALENESS_HALFLIFE)


def score_pursuit(p: Pursuit, deficit: Dict[str, float]) -> float:
    """Pure scoring function — no side effects. Negative deficit means surplus."""
    char_deficit = deficit.get(p.character, 0.0)
    score = (
        W_DEFICIT * char_deficit
        + W_PRIORITY * float(p.priority or 0.0)
        + W_RECENCY * _staleness_factor(p)
    )
    return score


def select_pursuit(
    pool: Iterable[Pursuit],
    value_compass=None,
) -> SelectorReasoning:
    """
    Pick the pursuit the heartbeat should advance.

    Returns a SelectorReasoning so callers can surface *why* — Phase 5
    will inject this into the plan prompt.
    """
    active = [p for p in pool if p.active]
    deficit = _budget_deficit(value_compass)

    if not active:
        return SelectorReasoning(
            pursuit=None,
            score=0.0,
            deficit=deficit,
            rationale="no active pursuits in pool",
            candidates_considered=0,
        )

    # 1. Operator/locked sovereignty
    locked = [p for p in active if p.locked and p.source in ("operator", "daily_plan")]
    if locked:
        winner = max(locked, key=lambda p: (p.priority, -p.last_touched))
        return SelectorReasoning(
            pursuit=winner,
            score=float("inf"),
            deficit=deficit,
            rationale=f"operator-locked override (source={winner.source})",
            candidates_considered=len(active),
        )

    # 2. Score and pick
    scored: List[Tuple[float, Pursuit]] = [
        (score_pursuit(p, deficit), p) for p in active
    ]
    scored.sort(key=lambda x: x[0], reverse=True)
    top_score, winner = scored[0]

    rationale = (
        f"character={winner.character} "
        f"deficit={deficit.get(winner.character, 0.0):+.2f} "
        f"priority={winner.priority:+.2f} "
        f"stale={_staleness_factor(winner):.2f} "
        f"source={winner.source}"
    )

    return SelectorReasoning(
        pursuit=winner,
        score=top_score,
        deficit=deficit,
        rationale=rationale,
        candidates_considered=len(active),
    )
