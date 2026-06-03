"""
repryntt.agents.intake_gate — Task admissibility check.

Runs synchronously at task creation. Rejects tasks whose title / description /
success criterion contains operator-defined "blocked vocabulary," is
self-referential (downstream_consumer points back at the agent), or targets
an agent-internal location.

Defaults to PERMISSIVE — the blocklist is per-installation and ships empty.
Operators populate `~/.repryntt/brain/intake_blocklist.json` with regex
patterns that match the framework-jargon their own deployment has drifted
into. Out of the box there is no opinionated word list baked into the code.

Shared constants (INTERNAL_CONSUMERS, ANDREW_INTERNAL_PATHS,
OPERATOR_VISIBLE_PREFIXES, ALLOWED_ARTIFACT_TYPES) are also imported by
critic_gate.py — keep them here so the two gates stay in lock-step.

Surface:
    check_admissibility(task_dict, *, strict=True) -> {"accepted": bool, "reasons": [...]}
    blocklist_hits(text) -> [str, ...]    # operator-configurable
    reload_blocklist()                    # call to pick up config edits
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Operator-configurable vocabulary blocklist
#
# Loaded from ~/.repryntt/brain/intake_blocklist.json (per-installation; not
# in the public repo). Schema:
#     {
#       "patterns": ["regex1", "regex2", ...],
#       "description": "free-form note about what this blocks and why"
#     }
#
# Each pattern is a Python regex applied with re.IGNORECASE. The default
# (when the file is missing) is an empty list — no blocking. Public-repo code
# never bakes in specific terms.
# ---------------------------------------------------------------------------

BLOCKLIST_CONFIG_PATH = os.path.expanduser(
    "~/.repryntt/brain/intake_blocklist.json"
)

# Default block threshold: the number of *distinct* blocked terms a piece of
# text must contain before a write/intake guard rejects it. Operators tighten
# or loosen via the "threshold" field in intake_blocklist.json. 2 is the
# permissive default (tolerates a single passing mention); 1 is strict (any
# blocked term triggers rejection); 0 disables the threshold mode.
_DEFAULT_BLOCK_THRESHOLD = 2

_LOCK = threading.Lock()
_BLOCKLIST_PATTERNS: Tuple[str, ...] = ()
_BLOCKLIST_RE: "re.Pattern[str] | None" = None
_BLOCK_THRESHOLD: int = _DEFAULT_BLOCK_THRESHOLD


def _load_patterns_from_disk() -> Tuple[Tuple[str, ...], int]:
    """Returns (patterns, threshold). Threshold falls back to default if absent."""
    try:
        if not os.path.exists(BLOCKLIST_CONFIG_PATH):
            return (), _DEFAULT_BLOCK_THRESHOLD
        with open(BLOCKLIST_CONFIG_PATH) as f:
            cfg = json.load(f)
        pats = cfg.get("patterns") or []
        threshold = cfg.get("threshold", _DEFAULT_BLOCK_THRESHOLD)
        try:
            threshold = max(1, int(threshold))
        except (TypeError, ValueError):
            threshold = _DEFAULT_BLOCK_THRESHOLD
        # Validate: drop entries that fail to compile so a bad config doesn't
        # break the daemon
        good = []
        for p in pats:
            if not isinstance(p, str):
                continue
            try:
                re.compile(p, re.IGNORECASE)
                good.append(p)
            except re.error as e:
                logger.warning(f"intake_gate: ignoring invalid pattern {p!r}: {e}")
        return tuple(good), threshold
    except Exception:
        logger.debug("intake_gate: could not read blocklist config", exc_info=True)
        return (), _DEFAULT_BLOCK_THRESHOLD


def reload_blocklist() -> int:
    """(Re)read the blocklist from disk. Returns count of active patterns."""
    global _BLOCKLIST_PATTERNS, _BLOCKLIST_RE, _BLOCK_THRESHOLD
    with _LOCK:
        _BLOCKLIST_PATTERNS, _BLOCK_THRESHOLD = _load_patterns_from_disk()
        if _BLOCKLIST_PATTERNS:
            _BLOCKLIST_RE = re.compile("|".join(_BLOCKLIST_PATTERNS), re.IGNORECASE)
        else:
            _BLOCKLIST_RE = None
    return len(_BLOCKLIST_PATTERNS)


def block_threshold() -> int:
    """Operator-configured threshold for blocklist write-side guards.

    Number of distinct blocked terms a piece of text must contain before the
    guards reject it. Configure via the ``threshold`` field of
    ``~/.repryntt/brain/intake_blocklist.json``; defaults to 2.
    """
    return _BLOCK_THRESHOLD


# Load at module import. If the config doesn't exist, this is a no-op.
reload_blocklist()


def blocklist_hits(text: str) -> List[str]:
    """Return the list of distinct blocklist phrases found in `text`.
    Empty if the operator hasn't configured a blocklist (default).
    """
    if not text or _BLOCKLIST_RE is None:
        return []
    matches = _BLOCKLIST_RE.findall(text)
    return sorted({m.lower() for m in matches if m})


# Back-compat alias for existing call sites. Will be retired.
pattern4_hits = blocklist_hits


# ---------------------------------------------------------------------------
# Forbidden / preferred locations for `expected_location`
# (These are about WHERE deliverables go — not about vocabulary.)
# ---------------------------------------------------------------------------
ANDREW_INTERNAL_PATHS: Tuple[str, ...] = (
    "skills/",
    "RECALL_archive/",
    "agent_workspaces/jarvis/research/",
    "agent_workspaces/jarvis/skill_packages/",
    "bootstrap/PULSE.md",
    "bootstrap/RECALL.md",
    "brain/skills/",
    "brain/bootstrap/",
)

OPERATOR_VISIBLE_PREFIXES: Tuple[str, ...] = (
    "workspace/agents/operator/",
    "analysis/",
    "deliverables/",
    "code/",
    "reports/",
    "data/",
    ".repryntt/workspace/agents/operator/",
)

# ---------------------------------------------------------------------------
# Forbidden values for `downstream_consumer`
# ---------------------------------------------------------------------------
INTERNAL_CONSUMERS: Tuple[str, ...] = (
    "self", "andrew", "jarvis", "agent", "internal",
    "myself", "the agent", "the system", "ai", "llm",
)

# ---------------------------------------------------------------------------
# Recognised artifact types — keep in sync with the critic-gate role map.
# ---------------------------------------------------------------------------
ALLOWED_ARTIFACT_TYPES: Tuple[str, ...] = (
    "code",
    "smart_contract",
    "research_md",
    "analysis_md",
    "plan_md",
    "design_md",
    "legal_md",
    "financial_model",
    "tokenomics",
    "patent_claim",
    "curriculum_md",
    "marketing_copy",
    "report",
    "data_extract",
    "robotics_doc",
    "hr_doc",
    "real_estate_analysis",
)


REQUIRED_FIELDS = (
    "expected_artifact_type",
    "expected_location",
    "downstream_consumer",
    "success_criterion",
)


def check_admissibility(task: Dict[str, Any], *, strict: bool = True) -> Dict[str, Any]:
    """Run the pre-queue admissibility checks on a candidate task.

    Philosophy: the intake gate is for blocking obvious-bad-actor patterns,
    not for enforcing a metadata schema. Typed deliverable fields are
    *recommended* and unlock the completion-time critic gate when present,
    but their absence alone does not block. Out of the box there is no
    vocabulary blocklist — the operator opts in via
    `~/.repryntt/brain/intake_blocklist.json`.

    Blocking reasons (one is enough to reject):
      - `expected_artifact_type` set but not in the allowed taxonomy
      - Operator-configured blocklist patterns match the human-readable
        text (title / description / success_criterion)
      - `downstream_consumer` set AND internal (self / andrew / jarvis / ...)
      - `expected_location` set AND under an agent-internal path prefix

    Returns {"accepted": bool, "reasons": [...], "needs_typing": bool}.
    """
    reasons: List[str] = []

    title = (task.get("title") or "").strip()
    description = (task.get("description") or "").strip()
    artifact_type = (task.get("expected_artifact_type") or "").strip().lower()
    expected_location = (task.get("expected_location") or "").strip()
    downstream_consumer = (task.get("downstream_consumer") or "").strip().lower()
    success_criterion = (task.get("success_criterion") or "").strip()

    # 1. If artifact_type is provided it must be in the taxonomy.
    if artifact_type and artifact_type not in ALLOWED_ARTIFACT_TYPES:
        reasons.append(
            f"`expected_artifact_type` = {artifact_type!r} is not in the allowed set "
            f"({', '.join(ALLOWED_ARTIFACT_TYPES)})"
        )

    # 2. Operator-configured blocklist in human-readable text.
    combined_text = f"{title}\n{description}\n{success_criterion}"
    hits = blocklist_hits(combined_text)
    if hits:
        reasons.append(
            "Vocabulary blocklist matched in task: "
            + ", ".join(hits)
            + ". The operator has flagged this language for rejection at intake; "
            "rewrite the task using plain operator-relevant terms."
        )

    # 3. If downstream_consumer is set, it cannot be self-referential.
    if downstream_consumer and downstream_consumer in INTERNAL_CONSUMERS:
        reasons.append(
            f"`downstream_consumer` = {downstream_consumer!r} is internal. "
            f"Specify an external role (operator, customer, developer, auditor, ...)."
        )

    # 4. If expected_location is set, it cannot be under an agent-internal path.
    if expected_location:
        loc_norm = expected_location.lstrip("./").lstrip("/")
        if any(loc_norm.startswith(p.lstrip("./").lstrip("/")) or p in loc_norm
               for p in ANDREW_INTERNAL_PATHS):
            reasons.append(
                f"`expected_location` = {expected_location!r} points at an "
                f"agent-internal path. Place artifacts in operator-visible "
                f"directories (e.g. {', '.join(OPERATOR_VISIBLE_PREFIXES[:4])})."
            )

    accepted = not reasons
    needs_typing = not all((task.get(f) or "").strip() for f in REQUIRED_FIELDS)
    if not accepted and strict:
        logger.info(f"❌ Task rejected at intake: {title!r} — {len(reasons)} reasons")
    elif not accepted and not strict:
        logger.warning(f"⚠️ Task admissibility issues (non-blocking): {title!r} — {reasons}")
        accepted = True  # In permissive mode, let it through but log.

    return {"accepted": accepted, "reasons": reasons, "needs_typing": needs_typing}
