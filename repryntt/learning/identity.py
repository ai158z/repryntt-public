"""
repryntt.learning.identity — Identity & Self-Evolution Domain Adapter
======================================================================
Maps personality/consciousness/behavioral events into the LearningEngine
so the agent can learn from its own growth patterns.

Tracks:
  - Which emotional states correlate with productive outcomes
  - Which personality traits/dimensions lead to better interactions
  - Which drive levels produce the best autonomous behavior
  - What behavioral patterns work vs fail (tool combos, communication style)

Integration points:
  - JarvisConsciousness.process_event()  → log emotion/drive context
  - PersonalityManager.modify_personality_trait() → log trait changes
  - Heartbeat cycle outcomes → record productivity as outcome score
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from repryntt.learning.engine import LearningEngine

logger = logging.getLogger(__name__)

DOMAIN = "identity"

# Categories tracked within identity domain
CAT_EMOTION_STATE = "emotional_state"     # mood at time of action
CAT_DRIVE_BALANCE = "drive_balance"       # which drive was dominant
CAT_TRAIT_CHANGE = "trait_change"         # personality trait modification
CAT_BEHAVIOR = "behavioral_pattern"       # tool usage, communication style
CAT_AUTONOMOUS = "autonomous_cycle"       # autonomous heartbeat productivity
CAT_INTERACTION = "operator_interaction"  # interactions with the user


class IdentityLearner:
    """Adapter that feeds identity/consciousness events into LearningEngine."""

    def __init__(self, engine: LearningEngine):
        self.engine = engine

    # ── Event Capture ─────────────────────────────────────────────────

    def on_heartbeat(self, emotions: Dict[str, float],
                     drives: Dict[str, float],
                     mood: str,
                     tasks_attempted: int = 0,
                     tasks_completed: int = 0,
                     tools_used: int = 0,
                     details: str = "") -> str:
        """Log an autonomous heartbeat cycle with emotional context.

        Returns event_id. Call record_heartbeat_outcome() later with
        a productivity score (how useful was this cycle?).
        """
        dominant_drive = max(drives, key=drives.get) if drives else "unknown"
        dominant_emotion = max(emotions, key=emotions.get) if emotions else "unknown"

        eid = self.engine.log_event(
            domain=DOMAIN,
            category=CAT_AUTONOMOUS,
            action="heartbeat",
            context={
                "mood": mood,
                "dominant_drive": dominant_drive,
                "dominant_emotion": dominant_emotion,
                "emotions": dict(emotions),
                "drives": dict(drives),
                "tasks_attempted": tasks_attempted,
                "tasks_completed": tasks_completed,
                "tools_used": tools_used,
                "details": details[:200] if details else "",
            },
            tags=["auto", f"mood:{mood}", f"drive:{dominant_drive}"],
        )
        return eid

    def record_heartbeat_outcome(self, event_id: str,
                                 productivity_score: float,
                                 details: Dict[str, Any] = None) -> bool:
        """Record how productive/useful a heartbeat cycle was.

        Args:
            event_id: From on_heartbeat().
            productivity_score: -1.0 (wasted cycle) to +1.0 (highly productive).
            details: Optional dict with specific results.
        """
        return self.engine.record_outcome(
            event_id, score=productivity_score, details=details,
        )

    def on_interaction(self, interaction_type: str,
                       emotions: Dict[str, float],
                       mood: str,
                       success: bool = True,
                       tools_used: int = 0,
                       details: str = "") -> str:
        """Log an operator interaction with emotional context.

        interaction_type: "chat", "command", "cold_call", "delegation", etc.
        Returns event_id.
        """
        dominant_emotion = max(emotions, key=emotions.get) if emotions else "unknown"

        eid = self.engine.log_event(
            domain=DOMAIN,
            category=CAT_INTERACTION,
            action=interaction_type,
            context={
                "mood": mood,
                "dominant_emotion": dominant_emotion,
                "emotions": dict(emotions),
                "tools_used": tools_used,
                "success": success,
                "details": details[:200] if details else "",
            },
            tags=["interaction", interaction_type],
        )
        # If success/failure is known immediately, record outcome
        score = 0.5 if success else -0.3
        self.engine.record_outcome(eid, score=score, details={"immediate": True})
        return eid

    def on_trait_change(self, trait_name: str, old_value: Any,
                        new_value: Any, reason: str = "",
                        emotions: Dict[str, float] = None) -> str:
        """Log a personality trait or dimension change.

        Returns event_id. Outcome is whether the change improved behavior.
        """
        eid = self.engine.log_event(
            domain=DOMAIN,
            category=CAT_TRAIT_CHANGE,
            action="modify",
            context={
                "trait": trait_name,
                "old_value": str(old_value),
                "new_value": str(new_value),
                "reason": reason,
                "emotions": dict(emotions) if emotions else {},
            },
            tags=["trait", trait_name],
        )
        return eid

    def on_drive_satisfied(self, drive_name: str, activity: str,
                           drive_before: float, drive_after: float,
                           emotions: Dict[str, float] = None) -> str:
        """Log when a drive is satisfied by an activity.

        Returns event_id. Score based on drive reduction (bigger drop = better satisfaction).
        """
        eid = self.engine.log_event(
            domain=DOMAIN,
            category=CAT_DRIVE_BALANCE,
            action="satisfied",
            context={
                "drive": drive_name,
                "activity": activity,
                "drive_before": drive_before,
                "drive_after": drive_after,
                "reduction": drive_before - drive_after,
                "emotions": dict(emotions) if emotions else {},
            },
            tags=["drive", drive_name],
        )
        # Immediate outcome: how much the drive was reduced
        reduction = drive_before - drive_after
        score = min(1.0, max(-0.5, reduction * 4.0))  # 0.25 reduction → 1.0
        self.engine.record_outcome(eid, score=score, details={
            "reduction": reduction, "immediate": True
        })
        return eid

    # ── Analysis & Briefs ─────────────────────────────────────────────

    def get_identity_brief(self, max_chars: int = 1500) -> str:
        """Generate a self-awareness brief for prompt injection.

        Tells the agent what emotional states / drive levels correlate
        with its best work, so it can self-regulate.
        """
        return self.engine.get_learning_brief(DOMAIN, max_chars=max_chars)

    def get_optimal_conditions(self) -> Dict[str, Any]:
        """Analyze which emotional/drive states produce best outcomes.

        Returns context features common to top-performing events.
        """
        insights = self.engine.analyze(DOMAIN)
        result = {}
        for insight in insights:
            if insight.win_rate >= 0.5 and insight.sample_count >= 8:
                best = insight.best_context
                if "emotions" in best:
                    result.setdefault("optimal_emotions", []).append({
                        "category": insight.category,
                        "emotions": best["emotions"],
                        "win_rate": insight.win_rate,
                    })
                if "dominant_drive" in best:
                    result.setdefault("optimal_drives", []).append({
                        "category": insight.category,
                        "drive": best["dominant_drive"],
                        "win_rate": insight.win_rate,
                    })
                if "mood" in best:
                    result.setdefault("optimal_moods", []).append({
                        "category": insight.category,
                        "mood": best["mood"],
                        "win_rate": insight.win_rate,
                    })
        return result

    def get_growth_report(self) -> Dict[str, Any]:
        """Comprehensive self-evolution report for dashboard / introspection."""
        stats = self.engine.get_domain_stats(DOMAIN)
        insights = self.engine.analyze(DOMAIN)

        report = {
            "domain": DOMAIN,
            "stats": stats,
            "categories": {},
        }
        for i in insights:
            report["categories"][i.category] = {
                "sample_count": i.sample_count,
                "win_rate": round(i.win_rate, 3),
                "trend": i.trend,
                "confidence": i.confidence,
            }
        return report

    def get_stats(self) -> Dict[str, Any]:
        """Domain stats for Jarvis introspection tool."""
        return self.engine.get_domain_stats(DOMAIN)
