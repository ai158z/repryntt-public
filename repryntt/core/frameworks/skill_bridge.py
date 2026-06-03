"""
repryntt.core.frameworks.skill_bridge — Wires skills into framework guidance.

Frameworks describe *how* to proceed (state machines). Skills describe *what
the agent knows* (knowledge, behaviors). On their own, an active framework
state has no awareness of which installed skills are relevant to it.

This module closes that loop: given a framework + state, it asks the skill
registry for the most relevant skill packages and returns a short text
fragment suitable for appending to the PLAN-prompt guidance.

Design notes
------------
* Soft dependency on the skills subsystem. If skills aren't available, the
  bridge silently returns no suggestions — frameworks remain fully usable.
* Pure scoring (no LLM calls) so it stays cheap to call every tick.
* Top-K capped (default 3) so we never bloat the prompt window.
* Match signal = overlap between the skill's name/tags/description and the
  active state's name/label/tools plus the framework's tags/match_keywords.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional, Sequence, Set

if TYPE_CHECKING:  # pragma: no cover — import only for type checking
    from repryntt.core.frameworks.schema import Framework, FrameworkState

logger = logging.getLogger("repryntt.frameworks.skill_bridge")


# ── Public dataclass ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class SkillHint:
    """A single skill suggestion the agent can act on."""
    skill_id: str
    name: str
    description: str
    score: int
    installed: bool


# ── Tokenisation helpers ─────────────────────────────────────────────────

def _tokens(*parts: str) -> Set[str]:
    """Split free text into a set of lowercased tokens (>=3 chars)."""
    out: Set[str] = set()
    for p in parts:
        if not p:
            continue
        for chunk in str(p).replace("/", " ").replace("-", " ").replace(",", " ").split():
            tok = chunk.strip().strip(".:;()[]{}").lower()
            if len(tok) >= 3:
                out.add(tok)
    return out


def _state_signal(framework: "Framework", state: "FrameworkState") -> Set[str]:
    """Tokens describing what this state cares about."""
    sig = _tokens(state.name, state.label, state.guidance,
                  *(state.tools or []))
    sig.update(_tokens(framework.label, framework.id,
                       *(framework.tags or []),
                       *(framework.match_keywords or [])))
    return sig


def _skill_signal(skill) -> Set[str]:
    """Tokens describing what this skill is about."""
    return _tokens(
        skill.name,
        skill.description,
        *(skill.tags or []),
    )


# ── Matching ─────────────────────────────────────────────────────────────

def _score(state_sig: Set[str], skill_sig: Set[str]) -> int:
    """Number of overlapping tokens (cheap, deterministic, explainable)."""
    if not state_sig or not skill_sig:
        return 0
    return len(state_sig & skill_sig)


def relevant_skills(
    framework: "Framework",
    state: "FrameworkState",
    *,
    limit: int = 3,
    min_score: int = 1,
    registry=None,
) -> List[SkillHint]:
    """Return the top-``limit`` skills relevant to this framework state.

    Returns an empty list if the skills subsystem is unavailable or no skill
    crosses ``min_score`` overlap. ``registry`` is for dependency injection in
    tests; production callers can omit it.
    """
    if registry is None:
        try:
            from repryntt.core.skills.skill_registry import SkillRegistry
            registry = SkillRegistry()
        except Exception as e:  # pragma: no cover — skills system absent
            logger.debug("skills subsystem unavailable: %s", e)
            return []

    try:
        packages = registry.scan()
    except Exception as e:
        logger.debug("skill scan failed: %s", e)
        return []
    if not packages:
        return []

    try:
        installed_ids = set(registry._get_installed_ids())
    except Exception:
        installed_ids = set()

    state_sig = _state_signal(framework, state)
    scored: List[SkillHint] = []
    for pkg in packages:
        score = _score(state_sig, _skill_signal(pkg))
        if score < min_score:
            continue
        scored.append(SkillHint(
            skill_id=pkg.skill_id,
            name=pkg.name,
            description=pkg.description,
            score=score,
            installed=pkg.skill_id in installed_ids,
        ))

    scored.sort(key=lambda h: (h.score, h.installed), reverse=True)
    return scored[:max(0, limit)]


# ── Prompt rendering ─────────────────────────────────────────────────────

def render_skill_hints(hints: Sequence[SkillHint]) -> str:
    """Render skill hints as a short text block for the PLAN prompt.

    Returns "" when there are no hints, so callers can unconditionally
    concatenate with a leading newline.
    """
    if not hints:
        return ""
    lines = ["   Relevant skills:"]
    for h in hints:
        marker = "✓" if h.installed else "·"
        desc = h.description.strip().splitlines()[0] if h.description else ""
        if len(desc) > 90:
            desc = desc[:87] + "..."
        suffix = f" — {desc}" if desc else ""
        lines.append(f"     {marker} {h.name}{suffix}")
    return "\n".join(lines)


__all__ = ["SkillHint", "relevant_skills", "render_skill_hints"]
