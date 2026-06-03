"""
Pursuit — Unified scheduling primitive.

Replaces the three competing runtimes (task queue / reasoning chain /
curiosity exploration) with one ranked pool. Phase 1 is additive:
nothing in this package is wired into the heartbeat yet. The selector
and store live here so Phase 2 can flip a single feature flag to
cut the heartbeat over without touching legacy storage.

See: PLAN — Unified Autonomy Refactor (in conversation history).
Storage: ~/.repryntt/workspace/agents/operator/pursuits.json
"""

from .model import Pursuit, PursuitCharacter, PursuitSource
from .store import PursuitStore
from .view import PursuitView
from .selector import select_pursuit, score_pursuit
from .seeders import (
    seed_interest_pursuits,
    abandon_zombie_chain,
    parse_interests_md,
)

__all__ = [
    "Pursuit",
    "PursuitCharacter",
    "PursuitSource",
    "PursuitStore",
    "PursuitView",
    "select_pursuit",
    "score_pursuit",
    "seed_interest_pursuits",
    "abandon_zombie_chain",
    "parse_interests_md",
]
