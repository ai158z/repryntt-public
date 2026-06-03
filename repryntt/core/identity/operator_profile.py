"""
repryntt.core.identity.operator_profile — Auto-learning operator model.

Goes beyond the static OPERATOR.md bootstrap file with a living profile
that Andrew auto-updates from interactions. Tracks communication style,
expertise areas, active projects, schedule patterns, preferences from
corrections, and interaction stats.

Storage: ~/.repryntt/brain/operator_profile.json
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_PROFILE_DIR = os.path.expanduser("~/.repryntt/brain")
PROFILE_FILENAME = "operator_profile.json"
MAX_PREFERENCES = 50
MAX_EXPERTISE_AREAS = 30
MAX_PROJECTS = 15
MAX_STYLE_OBSERVATIONS = 20


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class OperatorProfile:
    """Living, auto-learning profile of the operator.

    Loaded at daemon start, persisted on every update. Thread-safe.
    """

    def __init__(self, profile_dir: str = DEFAULT_PROFILE_DIR):
        self._path = os.path.join(profile_dir, PROFILE_FILENAME)
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = self._default_profile()
        self._dirty = False
        self._load()

    @staticmethod
    def _default_profile() -> Dict[str, Any]:
        return {
            "version": 1,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "style": {
                "verbosity": "unknown",
                "technical_depth": "unknown",
                "tone": "unknown",
                "observations": [],
            },
            "expertise": [],
            "projects": [],
            "schedule": {
                "timezone": "unknown",
                "typical_hours": "unknown",
                "observations": [],
            },
            "preferences": [],
            "stats": {
                "total_sessions": 0,
                "total_messages": 0,
                "first_interaction": None,
                "last_interaction": None,
                "most_used_tools": {},
            },
        }

    # ── Persistence ──────────────────────────────────────────────

    def _load(self):
        if not os.path.isfile(self._path):
            return
        try:
            with open(self._path) as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                # Merge with defaults so new fields get added on upgrade
                default = self._default_profile()
                for key in default:
                    if key not in saved:
                        saved[key] = default[key]
                self._data = saved
                logger.debug(f"Operator profile loaded: {self._path}")
        except Exception as e:
            logger.warning(f"Failed to load operator profile: {e}")

    def save(self):
        """Persist profile to disk."""
        with self._lock:
            if not self._dirty:
                return
            self._data["updated_at"] = _now_iso()
            try:
                os.makedirs(os.path.dirname(self._path), exist_ok=True)
                tmp = self._path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(self._data, f, indent=2, ensure_ascii=False)
                os.replace(tmp, self._path)
                self._dirty = False
                logger.debug("Operator profile saved")
            except Exception as e:
                logger.warning(f"Failed to save operator profile: {e}")

    # ── Expertise ────────────────────────────────────────────────

    def record_expertise(self, area: str, confidence: float = 0.5):
        """Record or update an expertise area with confidence 0.0-1.0."""
        area = area.strip().lower()
        if not area:
            return
        confidence = max(0.0, min(1.0, confidence))
        with self._lock:
            expertise = self._data.get("expertise", [])
            for entry in expertise:
                if entry.get("area") == area:
                    # Weighted update — increase confidence over time
                    old = entry.get("confidence", 0.5)
                    entry["confidence"] = round(
                        old * 0.6 + confidence * 0.4, 2)
                    entry["last_seen"] = _now_iso()
                    entry["mentions"] = entry.get("mentions", 1) + 1
                    self._dirty = True
                    return
            expertise.append({
                "area": area,
                "confidence": round(confidence, 2),
                "first_seen": _now_iso(),
                "last_seen": _now_iso(),
                "mentions": 1,
            })
            if len(expertise) > MAX_EXPERTISE_AREAS:
                expertise.sort(key=lambda e: e.get("mentions", 0))
                expertise[:] = expertise[-MAX_EXPERTISE_AREAS:]
            self._data["expertise"] = expertise
            self._dirty = True

    # ── Projects ─────────────────────────────────────────────────

    def record_project(self, project: str):
        """Record an active project / focus area."""
        project = project.strip()
        if not project:
            return
        with self._lock:
            projects = self._data.get("projects", [])
            for p in projects:
                if p.get("name", "").lower() == project.lower():
                    p["last_mentioned"] = _now_iso()
                    p["mentions"] = p.get("mentions", 1) + 1
                    self._dirty = True
                    return
            projects.append({
                "name": project,
                "first_mentioned": _now_iso(),
                "last_mentioned": _now_iso(),
                "mentions": 1,
            })
            if len(projects) > MAX_PROJECTS:
                projects.sort(key=lambda p: p.get("last_mentioned", ""))
                projects[:] = projects[-MAX_PROJECTS:]
            self._data["projects"] = projects
            self._dirty = True

    # ── Preferences ──────────────────────────────────────────────

    def record_preference(self, preference: str, source: str = "observation"):
        """Record a learned preference from operator corrections or patterns.

        Args:
            preference: The preference text, e.g. "prefers concise responses"
            source: Where this was learned — "correction", "explicit", "observation"
        """
        preference = preference.strip()
        if not preference:
            return
        with self._lock:
            prefs = self._data.get("preferences", [])
            # Deduplicate by lowercase match
            pref_lower = preference.lower()
            for p in prefs:
                if p.get("text", "").lower() == pref_lower:
                    p["reinforced"] = p.get("reinforced", 0) + 1
                    p["last_seen"] = _now_iso()
                    self._dirty = True
                    return
            prefs.append({
                "text": preference,
                "source": source,
                "learned_at": _now_iso(),
                "last_seen": _now_iso(),
                "reinforced": 0,
            })
            if len(prefs) > MAX_PREFERENCES:
                prefs.sort(key=lambda p: p.get("reinforced", 0))
                prefs[:] = prefs[-MAX_PREFERENCES:]
            self._data["preferences"] = prefs
            self._dirty = True

    # ── Style ────────────────────────────────────────────────────

    def record_style(self, observation: str):
        """Record a communication style observation."""
        observation = observation.strip()
        if not observation:
            return
        with self._lock:
            style = self._data.get("style", {})
            obs = style.get("observations", [])
            obs_lower = observation.lower()
            if obs_lower not in [o.lower() for o in obs]:
                obs.append(observation)
                if len(obs) > MAX_STYLE_OBSERVATIONS:
                    obs[:] = obs[-MAX_STYLE_OBSERVATIONS:]
                style["observations"] = obs
                self._data["style"] = style
                self._dirty = True

    def set_style_field(self, field: str, value: str):
        """Set a style field (verbosity, technical_depth, tone)."""
        if field not in ("verbosity", "technical_depth", "tone"):
            return
        with self._lock:
            style = self._data.get("style", {})
            style[field] = value.strip()
            self._data["style"] = style
            self._dirty = True

    # ── Schedule ─────────────────────────────────────────────────

    def record_schedule(self, observation: str):
        """Record a schedule pattern observation."""
        observation = observation.strip()
        if not observation:
            return
        with self._lock:
            sched = self._data.get("schedule", {})
            obs = sched.get("observations", [])
            if observation not in obs:
                obs.append(observation)
                if len(obs) > 10:
                    obs[:] = obs[-10:]
                sched["observations"] = obs
                self._data["schedule"] = sched
                self._dirty = True

    def set_timezone(self, tz: str):
        with self._lock:
            self._data.setdefault("schedule", {})["timezone"] = tz.strip()
            self._dirty = True

    # ── Stats ────────────────────────────────────────────────────

    def record_session(self):
        """Increment session count and update timestamps."""
        with self._lock:
            stats = self._data.get("stats", {})
            stats["total_sessions"] = stats.get("total_sessions", 0) + 1
            now = _now_iso()
            if not stats.get("first_interaction"):
                stats["first_interaction"] = now
            stats["last_interaction"] = now
            self._data["stats"] = stats
            self._dirty = True

    def record_message(self):
        """Increment message count."""
        with self._lock:
            stats = self._data.get("stats", {})
            stats["total_messages"] = stats.get("total_messages", 0) + 1
            stats["last_interaction"] = _now_iso()
            self._data["stats"] = stats
            self._dirty = True

    def record_tool_use(self, tool_name: str):
        """Track which tools the operator triggers most."""
        with self._lock:
            stats = self._data.get("stats", {})
            tools = stats.get("most_used_tools", {})
            tools[tool_name] = tools.get(tool_name, 0) + 1
            stats["most_used_tools"] = tools
            self._data["stats"] = stats
            self._dirty = True

    # ── Explicit Note ────────────────────────────────────────────

    def add_note(self, observation: str) -> Dict[str, str]:
        """Andrew explicitly records an observation about the operator.

        Auto-classifies into the right bucket based on content.
        """
        observation = observation.strip()
        if not observation:
            return {"status": "error", "message": "Empty observation"}

        obs_lower = observation.lower()

        # Auto-classify
        if any(kw in obs_lower for kw in
               ("prefers", "don't", "doesn't like", "always", "never",
                "wants", "hates", "likes")):
            self.record_preference(observation, source="explicit")
            category = "preference"
        elif any(kw in obs_lower for kw in
                 ("expert in", "knows", "background in", "experienced with",
                  "skilled at", "specializes")):
            self.record_expertise(observation, confidence=0.8)
            category = "expertise"
        elif any(kw in obs_lower for kw in
                 ("working on", "project", "building", "developing",
                  "focused on", "current goal")):
            self.record_project(observation)
            category = "project"
        elif any(kw in obs_lower for kw in
                 ("timezone", "usually active", "schedule", "morning",
                  "evening", "night owl")):
            self.record_schedule(observation)
            category = "schedule"
        elif any(kw in obs_lower for kw in
                 ("concise", "verbose", "technical", "casual", "formal",
                  "brief", "detailed")):
            self.record_style(observation)
            category = "style"
        else:
            self.record_preference(observation, source="explicit")
            category = "general"

        self.save()
        return {"status": "recorded", "category": category,
                "observation": observation}

    # ── Context Injection ────────────────────────────────────────

    def get_context(self) -> str:
        """Generate a compact summary for injection into the heartbeat prompt.

        Returns ~200-400 tokens of formatted operator context, or empty
        string if profile is too thin to be useful.
        """
        with self._lock:
            data = self._data

        parts = []

        # Style
        style = data.get("style", {})
        style_parts = []
        if style.get("verbosity") and style["verbosity"] != "unknown":
            style_parts.append(style["verbosity"])
        if style.get("technical_depth") and style["technical_depth"] != "unknown":
            style_parts.append(style["technical_depth"])
        if style.get("tone") and style["tone"] != "unknown":
            style_parts.append(style["tone"])
        for obs in style.get("observations", [])[:3]:
            style_parts.append(obs)
        if style_parts:
            parts.append(f"Style: {', '.join(style_parts)}")

        # Expertise (top 8 by confidence)
        expertise = sorted(
            data.get("expertise", []),
            key=lambda e: e.get("confidence", 0),
            reverse=True,
        )[:8]
        if expertise:
            exp_strs = []
            for e in expertise:
                conf = e.get("confidence", 0.5)
                level = "high" if conf >= 0.7 else "medium" if conf >= 0.4 else "low"
                exp_strs.append(f"{e['area']} ({level})")
            parts.append(f"Expertise: {', '.join(exp_strs)}")

        # Active projects (top 5 by recency)
        projects = sorted(
            data.get("projects", []),
            key=lambda p: p.get("last_mentioned", ""),
            reverse=True,
        )[:5]
        if projects:
            proj_names = [p["name"] for p in projects]
            parts.append(f"Active focus: {', '.join(proj_names)}")

        # Schedule
        sched = data.get("schedule", {})
        sched_parts = []
        if sched.get("timezone") and sched["timezone"] != "unknown":
            sched_parts.append(sched["timezone"])
        if sched.get("typical_hours") and sched["typical_hours"] != "unknown":
            sched_parts.append(f"active {sched['typical_hours']}")
        for obs in sched.get("observations", [])[:2]:
            sched_parts.append(obs)
        if sched_parts:
            parts.append(f"Schedule: {', '.join(sched_parts)}")

        # Preferences (top 8 by reinforcement)
        prefs = sorted(
            data.get("preferences", []),
            key=lambda p: p.get("reinforced", 0),
            reverse=True,
        )[:8]
        if prefs:
            pref_strs = [f'"{p["text"]}"' for p in prefs]
            parts.append(f"Preferences: {', '.join(pref_strs)}")

        if not parts:
            return ""

        return (
            "## OPERATOR PROFILE (auto-learned)\n"
            + "\n".join(parts)
        )

    # ── View (tool-facing) ───────────────────────────────────────

    def view(self) -> Dict[str, Any]:
        """Return the full profile data for inspection."""
        with self._lock:
            return dict(self._data)


# ── Singleton ────────────────────────────────────────────────────────

_instance: Optional[OperatorProfile] = None
_instance_lock = threading.Lock()


def get_operator_profile(profile_dir: str = DEFAULT_PROFILE_DIR) -> OperatorProfile:
    """Get or create the singleton OperatorProfile."""
    global _instance
    if _instance is not None:
        return _instance
    with _instance_lock:
        if _instance is not None:
            return _instance
        _instance = OperatorProfile(profile_dir)
        return _instance
