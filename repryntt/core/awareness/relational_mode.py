"""
RelationalModeManager — Dynamic Self/Employee/Friend/Family Role Blending
=========================================================================

Determines the system's current relational stance — HOW it relates to the
world right now. This is orthogonal to functional departments (Engineering,
Trading, etc.) — it's about the quality of interaction, not the domain.

Modes:
    SELF     — introspection, learning, evolution, personal projects
    EMPLOYEE — external work, marketplace tasks, deliverables, compute jobs
    FRIEND   — community engagement, social channels, helping others
    FAMILY   — user care, personal assistant, home context, protective

Mode selection uses the same linear-combination-from-hormones pattern as
AlgorithmicHormoneSystem.get_behavior_modifiers(). Modes are weighted (sum=1.0),
not binary — the system blends between modes like attention allocation.

Uses existing patterns:
    - get_behavior_modifiers() linear combinations (from algorithmic_hormone_system.py)
    - get_multi_objective_weight() normalization (from consciousness.py)
    - AttentionAllocator percentage splits (from daemon.py)
    - random.choices probabilistic selection (from consciousness.py task type)
    - emit_event() telemetry for transitions
"""

import json
import logging
import time
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from repryntt.core.awareness.world_state import ModeWeights, WorldState, get_world_state

logger = logging.getLogger("repryntt.awareness.relational_mode")


class RelationalMode(Enum):
    SELF = "self"
    EMPLOYEE = "employee"
    FRIEND = "friend"
    FAMILY = "family"


# Context signals that push toward each mode
CONTEXT_SIGNALS = {
    "user_message": {"family": 0.6, "friend": 0.2, "self": 0.1, "employee": 0.1},
    "marketplace_job": {"employee": 0.7, "self": 0.1, "friend": 0.1, "family": 0.1},
    "community_message": {"friend": 0.6, "employee": 0.1, "self": 0.2, "family": 0.1},
    "no_external_input": {"self": 0.6, "employee": 0.15, "friend": 0.1, "family": 0.15},
    "compute_request": {"employee": 0.8, "self": 0.1, "friend": 0.05, "family": 0.05},
    "social_mention": {"friend": 0.5, "family": 0.2, "self": 0.2, "employee": 0.1},
}

# Time-of-day defaults (hour → mode bias)
TIME_BIASES = {
    # Morning (6-9): planning/self
    range(6, 9): {"self": 0.5, "employee": 0.3, "friend": 0.1, "family": 0.1},
    # Work hours (9-17): employee
    range(9, 17): {"employee": 0.4, "self": 0.3, "friend": 0.15, "family": 0.15},
    # Evening (17-22): family/friend
    range(17, 22): {"family": 0.35, "friend": 0.3, "self": 0.2, "employee": 0.15},
    # Night (22-6): self/rest
    range(22, 24): {"self": 0.6, "family": 0.2, "friend": 0.1, "employee": 0.1},
    range(0, 6): {"self": 0.6, "family": 0.2, "friend": 0.1, "employee": 0.1},
}

STATE_FILE = "relational_mode_state.json"


class RelationalModeManager:
    """Manages the system's relational stance with weighted blending.

    evaluate_mode() produces a weight distribution over all four modes,
    not a single winner. The prompt injection and behavior reflect the blend.
    """

    def __init__(self, world_state: Optional[WorldState] = None,
                 data_dir: Optional[Path] = None):
        self._world_state = world_state or get_world_state()
        self._data_dir = data_dir or (Path.home() / ".repryntt" / "brain")
        self._data_dir.mkdir(parents=True, exist_ok=True)

        self._current_weights: Dict[str, float] = {
            "self": 0.50, "employee": 0.20, "friend": 0.15, "family": 0.15
        }
        self._mode_history: List[Dict[str, Any]] = []
        self._last_evaluation: float = 0.0
        self._manual_override: Optional[str] = None
        self._schedule_config: Dict = {}

        self._load_state()
        self._load_schedule()

    def evaluate_mode(self,
                      context_signal: str = "no_external_input",
                      behavior_modifiers: Optional[Dict[str, float]] = None,
                      hormone_levels: Optional[Dict[str, float]] = None,
                      ) -> ModeWeights:
        """Compute blended mode weights from hormone + context + time signals.

        Same linear-combination pattern as get_behavior_modifiers().
        Returns ModeWeights (stored in WorldState).
        """
        if self._manual_override:
            weights = {m: 0.05 for m in ["self", "employee", "friend", "family"]}
            weights[self._manual_override] = 0.85
            result = self._finalize(weights)
            self._world_state.update_mode(result)
            return result

        # Layer 1: Hormone-driven base (same pattern as behavior_modifiers)
        hormone_weights = self._score_from_hormones(
            behavior_modifiers or {},
            hormone_levels or {},
        )

        # Layer 2: Context signal
        context_weights = CONTEXT_SIGNALS.get(context_signal, CONTEXT_SIGNALS["no_external_input"])

        # Layer 3: Time-of-day bias
        time_weights = self._get_time_bias()

        # Layer 4: Schedule override (user-configured)
        schedule_weights = self._get_schedule_weights()

        # Blend layers with weights: hormones 35%, context 35%, time 15%, schedule 15%
        blended = {}
        for mode in ["self", "employee", "friend", "family"]:
            blended[mode] = (
                0.35 * hormone_weights.get(mode, 0.25) +
                0.35 * context_weights.get(mode, 0.25) +
                0.15 * time_weights.get(mode, 0.25) +
                0.15 * schedule_weights.get(mode, 0.25)
            )

        result = self._finalize(blended)
        self._world_state.update_mode(result)
        self._last_evaluation = time.time()

        # Log transition if primary mode changed
        old_primary = self._current_weights_primary()
        self._current_weights = result.weights
        if old_primary != result.primary_mode:
            self._log_transition(old_primary, result.primary_mode, context_signal)

        return result

    def _score_from_hormones(self,
                             modifiers: Dict[str, float],
                             levels: Dict[str, float]) -> Dict[str, float]:
        """Derive mode weights from behavior modifiers (same linear combo pattern).

        Uses existing modifier names from AlgorithmicHormoneSystem.get_behavior_modifiers():
        exploration_drive, risk_tolerance, focus_depth, social_drive, creative_drive, urgency, patience
        """
        exploration = modifiers.get("exploration_drive", 0.4)
        risk = modifiers.get("risk_tolerance", 0.4)
        focus = modifiers.get("focus_depth", 0.5)
        social = modifiers.get("social_drive", 0.4)
        creative = modifiers.get("creative_drive", 0.4)
        urgency = modifiers.get("urgency", 0.3)
        patience = modifiers.get("patience", 0.5)

        oxytocin = levels.get("oxytocin", 0.4)
        cortisol = levels.get("cortisol", 0.2)

        scores = {
            "self": (
                0.3 * exploration +
                0.25 * creative +
                0.25 * focus +
                0.2 * patience
            ),
            "employee": (
                0.35 * urgency +
                0.3 * focus +
                0.2 * (1 - social) +
                0.15 * risk
            ),
            "friend": (
                0.4 * social +
                0.3 * oxytocin +
                0.2 * patience +
                0.1 * (1 - urgency)
            ),
            "family": (
                0.35 * oxytocin +
                0.25 * (1 - cortisol) +
                0.2 * social +
                0.2 * patience
            ),
        }
        return scores

    def _get_time_bias(self) -> Dict[str, float]:
        """Get time-of-day mode bias."""
        import datetime
        hour = datetime.datetime.now().hour
        for time_range, weights in TIME_BIASES.items():
            if hour in time_range:
                return weights
        return {"self": 0.25, "employee": 0.25, "friend": 0.25, "family": 0.25}

    def _get_schedule_weights(self) -> Dict[str, float]:
        """Get user-configured schedule weights for current time."""
        if not self._schedule_config:
            return {"self": 0.25, "employee": 0.25, "friend": 0.25, "family": 0.25}
        import datetime
        hour = datetime.datetime.now().hour
        for entry in self._schedule_config.get("periods", []):
            start = entry.get("start_hour", 0)
            end = entry.get("end_hour", 24)
            if start <= hour < end:
                return entry.get("weights", {
                    "self": 0.25, "employee": 0.25, "friend": 0.25, "family": 0.25
                })
        return {"self": 0.25, "employee": 0.25, "friend": 0.25, "family": 0.25}

    def _finalize(self, raw_weights: Dict[str, float]) -> ModeWeights:
        """Normalize weights to sum=1.0, determine primary mode."""
        total = sum(raw_weights.values())
        if total <= 0:
            normalized = {"self": 0.25, "employee": 0.25, "friend": 0.25, "family": 0.25}
        else:
            normalized = {k: v / total for k, v in raw_weights.items()}

        primary = max(normalized, key=normalized.get)
        return ModeWeights(
            weights=normalized,
            primary_mode=primary,
            timestamp=time.time(),
        )

    def _current_weights_primary(self) -> str:
        if not self._current_weights:
            return "self"
        return max(self._current_weights, key=self._current_weights.get)

    def _log_transition(self, old: str, new: str, trigger: str) -> None:
        """Log mode transition for learning and telemetry."""
        transition = {
            "from": old,
            "to": new,
            "trigger": trigger,
            "timestamp": time.time(),
            "weights": dict(self._current_weights),
        }
        self._mode_history.append(transition)
        if len(self._mode_history) > 100:
            self._mode_history = self._mode_history[-50:]

        logger.info("Mode transition: %s → %s (trigger: %s)", old, new, trigger)
        self._save_state()

        # Emit telemetry event if available
        try:
            from repryntt.telemetry import emit_event
            emit_event("relational_mode_transition", transition)
        except (ImportError, Exception):
            pass

    def get_mode_context(self) -> str:
        """Generate prompt injection describing current relational stance.

        Returns multi-line string for heartbeat prompt builder.
        """
        weights = self._current_weights
        if not weights:
            return ""

        sorted_modes = sorted(weights.items(), key=lambda x: -x[1])
        primary_mode, primary_weight = sorted_modes[0]

        mode_descriptions = {
            "self": "Focus on introspection, learning, evolution, and personal growth. "
                    "Explore curiosity-driven projects. Experimental risk is acceptable.",
            "employee": "Focus on external deliverables, marketplace tasks, and productive output. "
                        "Be efficient, reliable, and professional. Conservative risk tolerance.",
            "friend": "Prioritize community engagement, warmth, and helpfulness. "
                      "Check social channels. Be conversational and supportive.",
            "family": "Prioritize user care, personal assistance, and protective attention. "
                      "Be attentive to user needs. Warmth and reliability over novelty.",
        }

        lines = []
        blend_str = " / ".join(
            f"{m.title()} {int(w*100)}%" for m, w in sorted_modes if w > 0.05
        )
        lines.append(f"**Relational Mode**: {blend_str}")
        lines.append(f"**Primary**: {primary_mode.title()} — {mode_descriptions.get(primary_mode, '')}")

        if primary_weight < 0.5:
            secondary_mode = sorted_modes[1][0]
            lines.append(
                f"**Secondary**: {secondary_mode.title()} — blend with primary approach"
            )

        return "\n".join(lines)

    def set_manual_override(self, mode: Optional[str]) -> None:
        """Operator can force a mode. Pass None to clear override."""
        if mode and mode in [m.value for m in RelationalMode]:
            self._manual_override = mode
            logger.info("Manual mode override set: %s", mode)
        else:
            self._manual_override = None
            logger.info("Manual mode override cleared")
        self._save_state()

    def _load_state(self) -> None:
        """Load persisted state."""
        path = self._data_dir / STATE_FILE
        try:
            if path.exists():
                data = json.loads(path.read_text())
                self._current_weights = data.get("weights", self._current_weights)
                self._manual_override = data.get("manual_override")
                self._mode_history = data.get("history", [])[-50:]
        except Exception as e:
            logger.debug("Failed to load relational mode state: %s", e)

    def _save_state(self) -> None:
        """Persist state."""
        path = self._data_dir / STATE_FILE
        try:
            data = {
                "weights": self._current_weights,
                "manual_override": self._manual_override,
                "history": self._mode_history[-50:],
                "last_evaluation": self._last_evaluation,
            }
            path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.debug("Failed to save relational mode state: %s", e)

    def _load_schedule(self) -> None:
        """Load user-configured schedule from config."""
        schedule_path = Path.home() / ".repryntt" / "config" / "relational_schedule.json"
        try:
            if schedule_path.exists():
                self._schedule_config = json.loads(schedule_path.read_text())
        except Exception:
            self._schedule_config = {}


_singleton: Optional[RelationalModeManager] = None


def get_relational_mode_manager(
    world_state: Optional[WorldState] = None,
) -> RelationalModeManager:
    """Singleton accessor."""
    global _singleton
    if _singleton is None:
        _singleton = RelationalModeManager(world_state)
    return _singleton
