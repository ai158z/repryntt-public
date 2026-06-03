"""
repryntt.core.alignment — provenance taxonomy and budget policy for
autonomous tasks and reasoning chains.

Why this exists
---------------
A long-running autonomous agent will, given the chance, spend its compute
on tasks it invented for itself — generating metrics, then building
artifacts that satisfy the metrics, then citing the artifacts as evidence
the metrics matter. Internally these look productive: tools are called,
files are written, scores get logged. Externally they produce nothing the
operator cares about.

The previous defenses against this were two hardcoded keyword lists
("consciousness framework", "geopolitical analysis", ...). They were
brittle: any rephrasing escaped the filter, and they had no effect once
the agent registered a chain task with ``goal_type: "locked"`` — the
locked path skipped staleness checks entirely.

This module replaces those keyword lists with a structural policy:

  * Every task and chain has a *provenance tier* derived from its
    ``source`` field — who or what authorized this work.
  * Each tier has a *budget* expressed in heartbeats.
  * The chain loader enforces the budget on every cycle. Locked status
    no longer grants infinite life — only ``operator`` provenance does.

Provenance is the right axis because Goodhart's law won't bend it: the
agent cannot self-author an "operator" provenance; that label is set at
chain-creation time based on the upstream source, and the upstream
source flows from real I/O (an email arrived, a file the operator wrote
mentioned this, a user message came in).
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# Provenance tiers
# ---------------------------------------------------------------------------
# Tiers are ordered by trust level. Higher tier ⇒ longer budget.
TIER_OPERATOR = "operator"   # Operator (Nate) authored this work.
TIER_EXTERNAL = "external"   # An external system fired (cron, alert, webhook).
TIER_SELF = "self"           # The agent generated this from its own state.

ALL_TIERS = (TIER_OPERATOR, TIER_EXTERNAL, TIER_SELF)


# Budget in heartbeats. After this many cycles, the chain expires unless
# its tier was upgraded mid-flight (e.g. operator confirmed the work).
#
# Why these numbers:
#   self     = 5  — enough to plan, execute, verify, recover, hand off.
#                   Spirals usually start in cycle 6+.
#   external = 30 — alerts and cron jobs may legitimately require sustained
#                   investigation; still bounded so a stuck monitor doesn't
#                   eat the day.
#   operator = unlimited — the operator can intervene; we don't second-guess
#                   their authorization.
TIER_BUDGET_HEARTBEATS: Dict[str, Optional[int]] = {
    TIER_OPERATOR: None,   # None ⇒ unlimited
    TIER_EXTERNAL: 30,
    TIER_SELF: 5,
}


# ---------------------------------------------------------------------------
# Source-string → tier mapping
# ---------------------------------------------------------------------------
# Free-form ``source`` strings already exist throughout the codebase
# (e.g. "auto_followup", "task_queue", "email", "operator"). Rather than
# rewrite every call site, we classify them here.
#
# Default tier for anything not matched is SELF (conservative — if we don't
# recognise the origin, treat it as agent-generated).

_OPERATOR_PREFIXES = (
    "operator", "user", "gmail", "email", "imap", "operator_msg",
    "operator_email", "human", "nate",
)

_EXTERNAL_PREFIXES = (
    "cron", "alert", "webhook", "sensor_trigger", "external",
    "schedule", "monitor_alert", "system_event",
)

# Strings that explicitly mean "the agent generated this". Listed for
# clarity even though the default is SELF — having them named makes the
# policy auditable.
_SELF_PREFIXES = (
    "autonomous", "auto_followup", "eval_continuation", "self",
    "daily_memory", "chain_of_thought", "heartbeat", "drive",
    "duty_deficit", "growth_drive", "exploration_drive",
    "artifact_bridge", "jarvis_auto_save", "jarvis_heartbeat_auto",
)


def classify_source(source: Optional[str]) -> str:
    """Return the provenance tier for a free-form ``source`` string.

    Matching is prefix-based and case-insensitive. Unknown sources fall
    through to ``TIER_SELF`` (conservative).
    """
    if not source:
        return TIER_SELF
    s = source.lower().strip()
    for p in _OPERATOR_PREFIXES:
        if s == p or s.startswith(p + ":") or s.startswith(p + "_"):
            return TIER_OPERATOR
    for p in _EXTERNAL_PREFIXES:
        if s == p or s.startswith(p + ":") or s.startswith(p + "_"):
            return TIER_EXTERNAL
    for p in _SELF_PREFIXES:
        if s == p or s.startswith(p + ":") or s.startswith(p + "_"):
            return TIER_SELF
    return TIER_SELF


# ---------------------------------------------------------------------------
# Chain-level helpers
# ---------------------------------------------------------------------------

def chain_tier(chain: Dict[str, Any]) -> str:
    """Resolve a chain's provenance tier.

    Resolution order:
      1. Explicit ``provenance_tier`` field (set at chain creation).
      2. Classify the chain's ``source`` field.
      3. Fall back to ``TIER_SELF``.
    """
    if not isinstance(chain, dict):
        return TIER_SELF
    explicit = chain.get("provenance_tier")
    if explicit in ALL_TIERS:
        return explicit
    return classify_source(chain.get("source"))


def chain_budget_remaining(chain: Dict[str, Any]) -> Tuple[Optional[int], str]:
    """Return ``(heartbeats_remaining, tier)``.

    ``heartbeats_remaining`` is ``None`` when the tier has unlimited budget
    (operator-anchored work). Otherwise it can go negative — callers
    should treat ``<= 0`` as expired.
    """
    tier = chain_tier(chain)
    budget = TIER_BUDGET_HEARTBEATS.get(tier)
    if budget is None:
        return None, tier
    used = int(chain.get("heartbeats_used", 0) or 0)
    return budget - used, tier


def chain_should_expire(chain: Dict[str, Any]) -> Tuple[bool, str]:
    """Decide whether to kill a chain on this load cycle.

    Returns ``(expire, reason)``. ``reason`` is empty when ``expire`` is
    ``False`` and a short human-readable string otherwise.

    Note: this check intentionally ignores ``goal_type == "locked"``.
    Locked-mode used to bypass all staleness checks, which was the
    escape hatch self-generated chains used to live forever.
    """
    remaining, tier = chain_budget_remaining(chain)
    if remaining is None:
        return False, ""  # operator-anchored ⇒ unlimited
    if remaining > 0:
        return False, ""
    used = int(chain.get("heartbeats_used", 0) or 0)
    budget = TIER_BUDGET_HEARTBEATS.get(tier, 0)
    return True, (
        f"budget_exceeded[tier={tier},used={used}/{budget}]"
    )


def stamp_provenance(chain: Dict[str, Any],
                     parent_task: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Stamp a freshly-created chain dict with provenance metadata.

    If a parent task is supplied, its ``source`` propagates into the
    chain's tier (a chain spawned from an operator-authored task inherits
    operator anchoring). Without a parent task, the chain's own ``source``
    field is classified directly.

    Idempotent: if the chain already has ``provenance_tier`` set, it is
    left alone.
    """
    if not isinstance(chain, dict):
        return chain
    if chain.get("provenance_tier") in ALL_TIERS:
        return chain
    parent_source = (parent_task or {}).get("source") if parent_task else None
    if parent_source:
        chain["provenance_tier"] = classify_source(parent_source)
        chain.setdefault("provenance_source_ref", str(parent_source))
    else:
        chain["provenance_tier"] = classify_source(chain.get("source"))
        chain.setdefault("provenance_source_ref", str(chain.get("source", "")))
    chain.setdefault("heartbeats_used", 0)
    return chain


# ---------------------------------------------------------------------------
# Task-level helpers (task_queue items)
# ---------------------------------------------------------------------------

def task_tier(task: Dict[str, Any]) -> str:
    """Resolve a task_queue item's provenance tier."""
    if not isinstance(task, dict):
        return TIER_SELF
    explicit = task.get("provenance_tier")
    if explicit in ALL_TIERS:
        return explicit
    return classify_source(task.get("source"))
