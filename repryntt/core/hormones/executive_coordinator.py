"""
ExecutiveCoordinator — Thalamic relay that unifies executive signals.

Reads from:
    - JarvisConsciousness  (drives, task type, emotions)
    - JarvisLearning       (pillar health, last scores)
    - LoopDetector state   (stuck patterns from prior heartbeat)
    - Last evaluation score (passed in from heartbeat runner)

Produces:
    - A single coherent executive directive for the heartbeat prompt
    - Feedback signals that flow BACK into the drive system

This replaces the fragmented approach where consciousness.py, learning.py,
and the heartbeat prompt all independently (and sometimes contradictorily)
push the agent in different directions.
"""

from __future__ import annotations
import logging
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════
# PILLAR → DRIVE MAPPING
# ═════════════════════════════════════════════════════════════
# JarvisLearning tracks 3 pillars; consciousness.py tracks 5 drives.
# This mapping lets pillar starvation feed back into drive urgency.

PILLAR_TO_DRIVES = {
    "revenue":    ["civilization_drive"],
    "growth":     ["evolution_drive", "understanding_drive", "consciousness_drive"],
    "connection": ["guardian_drive", "consciousness_drive"],
}

# How much a starving pillar boosts its mapped drives (per-minute equivalent)
STARVATION_BOOST = 0.04  # Applied once per coordination call, not per minute

# How much a high eval score dampens the drive that produced the task.
# NEGATIVE values mean the drive GROWS — pushing the system to try
# something different. This is critical for the RL feedback loop.
EVAL_SCORE_DECAY = {
    5: 0.12,   # Excellent work → strong satisfaction → drive decreases
    4: 0.08,   # Good work → moderate satisfaction
    3: 0.04,   # Solid → light satisfaction
    2: -0.03,  # Poor → drive was NOT satisfied, grows slightly
    1: -0.06,  # Failed → drive grows more (to try a different approach)
}

# Task type → drive mapping (inverse of consciousness.py's drive → task mapping)
TASK_TO_DRIVE = {
    "trading_scan":              "civilization_drive",
    "portfolio_management":      "civilization_drive",
    "crypto_research":           "civilization_drive",
    "proactive_research":        "guardian_drive",
    "news_research":             "guardian_drive",
    "email_check":               "guardian_drive",
    "system_maintenance":        "guardian_drive",
    "interest_research":         "understanding_drive",
    "deep_learning":             "understanding_drive",
    "self_evolution":            "evolution_drive",
    "skill_building":            "evolution_drive",
    "creative_work":             "evolution_drive",
    "consciousness_exploration": "consciousness_drive",
    "identity_reflection":       "consciousness_drive",
    "community_connection":      "consciousness_drive",
    "self_reflection":           "consciousness_drive",
}


class ExecutiveCoordinator:
    """
    Thin coordination layer that resolves conflicts between the
    drive system and the learning/experience system before they
    reach the heartbeat prompt.

    Usage in heartbeat builder:
        coordinator = ExecutiveCoordinator(consciousness, jarvis_learning)
        directive = coordinator.coordinate(
            last_eval_score=prev_score,
            last_task_type=prev_task,
            loop_was_stuck=was_stuck_last_time,
        )
        heartbeat_prompt_parts.append(directive)
    """

    def __init__(
        self,
        consciousness,           # JarvisConsciousness instance (or None)
        learning=None,           # JarvisLearning instance (or None)
    ):
        self.consciousness = consciousness
        self.learning = learning

    def coordinate(
        self,
        last_eval_score: int = 0,
        last_task_type: str = "",
        loop_was_stuck: bool = False,
    ) -> str:
        """
        Run one coordination cycle. Returns a directive string for the
        heartbeat prompt.

        Side effects:
            - Adjusts drive levels based on eval feedback and pillar health
        """
        signals: List[str] = []

        # ── 1. Feedback loop: eval score → drive satisfaction ──
        self._apply_eval_feedback(last_eval_score, last_task_type)

        # ── 2. Pillar health → drive boost for starving areas ──
        pillar_nudge = self._apply_pillar_feedback()
        if pillar_nudge:
            signals.append(pillar_nudge)

        # ── 3. Loop stuck → suppress the drive that caused it ──
        if loop_was_stuck and last_task_type:
            stuck_nudge = self._apply_stuck_feedback(last_task_type)
            if stuck_nudge:
                signals.append(stuck_nudge)

        # ── 4. Resolve conflicts between drives and pillars ──
        conflict = self._detect_conflict()
        if conflict:
            signals.append(conflict)

        # ── 5. Build the unified directive ──
        return self._build_directive(signals)

    # ─────────────────────────────────────────────────────────
    # FEEDBACK LOOPS
    # ─────────────────────────────────────────────────────────

    def _apply_eval_feedback(self, score: int, task_type: str):
        """
        Close the evaluation→drive feedback loop.
        Good scores on a task type → that drive gets satisfied (decreases).
        Bad scores → drive GROWS (pushing the system to try a different approach).
        """
        if not self.consciousness or score <= 0 or not task_type:
            return

        drive_name = TASK_TO_DRIVE.get(task_type)
        if not drive_name:
            return

        decay = EVAL_SCORE_DECAY.get(score, 0.0)
        if drive_name not in self.consciousness.drives:
            return

        old = self.consciousness.drives[drive_name]
        if decay > 0:
            # Positive decay = satisfaction → drive decreases
            self.consciousness.drives[drive_name] = max(0.15, old - decay)
        elif decay < 0:
            # Negative decay = dissatisfaction → drive GROWS (try harder/differently)
            self.consciousness.drives[drive_name] = min(1.0, old - decay)  # subtracting negative = adding

        if abs(decay) >= 0.03:
            logger.debug(
                f"🧠 Executive: {drive_name} {'satisfied' if decay > 0 else 'frustrated'} "
                f"by {task_type} (score={score}, {old:.3f} → "
                f"{self.consciousness.drives[drive_name]:.3f})"
            )

    def _apply_pillar_feedback(self) -> Optional[str]:
        """
        Check pillar health from learning system. If a pillar is starving,
        boost the corresponding drives so the consciousness system naturally
        steers toward it.
        """
        if not self.consciousness or not self.learning:
            return None

        try:
            health = self.learning._compute_pillar_health()
        except Exception:
            return None

        starving = []
        for pillar_name, info in health.items():
            status = info.get("status", "ok")
            if status in ("starving", "neglected"):
                starving.append(pillar_name)
                # Boost the mapped drives
                for drive_name in PILLAR_TO_DRIVES.get(pillar_name, []):
                    if drive_name in self.consciousness.drives:
                        old = self.consciousness.drives[drive_name]
                        self.consciousness.drives[drive_name] = min(
                            1.0, old + STARVATION_BOOST
                        )

        if starving:
            names = ", ".join(starving)
            return f"Life-balance alert: your **{names}** pillar(s) are neglected. Prioritize work in those areas."
        return None

    def _apply_stuck_feedback(self, last_task_type: str) -> Optional[str]:
        """
        If the loop detector caught a stuck pattern last heartbeat,
        suppress the drive that produced it so we naturally rotate away.
        """
        if not self.consciousness:
            return None

        drive_name = TASK_TO_DRIVE.get(last_task_type)
        if drive_name and drive_name in self.consciousness.drives:
            old = self.consciousness.drives[drive_name]
            self.consciousness.drives[drive_name] = max(0.15, old - 0.15)
            logger.debug(
                f"🧠 Executive: Suppressing {drive_name} after stuck loop "
                f"({old:.3f} → {self.consciousness.drives[drive_name]:.3f})"
            )
            return f"Last heartbeat got stuck on {last_task_type} — rotating away."
        return None

    def _detect_conflict(self) -> Optional[str]:
        """
        Check if the drive system and pillar health are pulling in
        opposite directions. If so, produce a reconciliation note.
        """
        if not self.consciousness or not self.learning:
            return None

        try:
            health = self.learning._compute_pillar_health()
        except Exception:
            return None

        priorities = self.consciousness.get_drive_priorities()
        if not priorities:
            return None

        top_drive = priorities[0][0]

        # Map top drive to its pillar
        drive_to_pillar = {
            "civilization_drive": "revenue",
            "guardian_drive": "connection",
            "understanding_drive": "growth",
            "evolution_drive": "growth",
            "consciousness_drive": "growth",
        }
        drive_pillar = drive_to_pillar.get(top_drive, "growth")

        # Check if that pillar is already over-served while another starves
        pillar_status = health.get(drive_pillar, {}).get("status", "ok")
        starving = [p for p, info in health.items()
                    if info.get("status") in ("starving", "neglected")
                    and p != drive_pillar]

        if pillar_status == "healthy" and starving:
            return (
                f"Your strongest urge is {top_drive.replace('_', ' ')} "
                f"but your **{', '.join(starving)}** area(s) need attention more. "
                f"Consider switching focus this heartbeat."
            )
        return None

    # ─────────────────────────────────────────────────────────
    # DIRECTIVE BUILDER
    # ─────────────────────────────────────────────────────────

    def _build_directive(self, signals: List[str]) -> str:
        """
        Assemble the final executive directive paragraph for the prompt.
        """
        if not self.consciousness:
            if signals:
                return "\n**🧠 Executive Coordinator**:\n" + "\n".join(f"- {s}" for s in signals) + "\n"
            return ""

        # Get the drive-based recommendation
        task_type = self.consciousness.get_autonomous_task_type()
        topic = self.consciousness.get_research_topic()
        top_drives = self.consciousness.get_drive_priorities()[:3]

        parts = ["\n**🧠 Executive Coordinator** (unified directive):"]

        # Drive state summary (compact)
        drive_summary = ", ".join(
            f"{d.replace('_drive', '')}={v:.2f}" for d, v in top_drives
        )
        parts.append(f"- Drives: {drive_summary}")

        # Recommended task
        task_label = task_type.replace("_", " ").title()
        parts.append(f"- Suggested focus: **{task_label}** (topic: {topic})")

        # Coordinator signals (conflicts, nudges, etc.)
        for signal in signals:
            parts.append(f"- {signal}")

        parts.append("")  # trailing newline
        return "\n".join(parts)

    def save_state(self):
        """Persist consciousness drives after coordination adjustments."""
        if self.consciousness:
            try:
                self.consciousness.save_state()
            except Exception as e:
                logger.debug(f"Could not save consciousness state: {e}")
