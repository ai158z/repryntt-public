"""
ReplanTriggers — Dynamic Replanning Event Handlers
===================================================

Listens for significant state changes and triggers task queue reprioritization.
Does NOT replace the existing scheduler — it adds reactive replanning on top.

Trigger sources:
    - Drive shift: dominant drive changed (from hormone bridge)
    - Role transition: primary relational mode changed
    - External event: urgent message, marketplace job, sensor alert
    - Completion milestone: major task done
    - Time-based: morning plan, midday check-in, evening reflection

Integration:
    - Calls TaskQueue.replan(trigger_event) when triggered
    - Calls GoalGenerator.generate_daily_goals() for fresh goals on major shifts
    - Emits telemetry events via existing emit_event()
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from repryntt.core.awareness.world_state import WorldState, get_world_state

logger = logging.getLogger("repryntt.scheduling.replan_triggers")


@dataclass
class TriggerEvent:
    """Represents a replanning trigger."""
    event_type: str  # drive_shift, role_transition, external_event, milestone, time_based
    source: str
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    severity: float = 0.5  # 0.0-1.0, how urgently to replan


# Minimum interval between replans to prevent thrashing
MIN_REPLAN_INTERVAL_S = 300  # 5 minutes

# Severity threshold — only replan if trigger is significant enough
REPLAN_THRESHOLD = 0.4


class ReplanTriggers:
    """Monitors state changes and triggers task queue replanning.

    Register callbacks via on_replan() to be notified when replanning is needed.
    The primary callback should be TaskQueue.replan().
    """

    def __init__(self, world_state: Optional[WorldState] = None):
        self._world_state = world_state or get_world_state()
        self._callbacks: List[Callable[[TriggerEvent], None]] = []
        self._last_replan_time: float = 0.0
        self._last_dominant_drive: str = ""
        self._last_primary_mode: str = ""
        self._trigger_history: List[TriggerEvent] = []

    def on_replan(self, callback: Callable[[TriggerEvent], None]) -> None:
        """Register a callback for when replanning is triggered."""
        self._callbacks.append(callback)

    def check_drive_shift(self, current_drive: str) -> Optional[TriggerEvent]:
        """Check if dominant drive has changed. Call after hormone bridge sync."""
        if not self._last_dominant_drive:
            self._last_dominant_drive = current_drive
            return None

        if current_drive != self._last_dominant_drive:
            old = self._last_dominant_drive
            self._last_dominant_drive = current_drive
            event = TriggerEvent(
                event_type="drive_shift",
                source="hormone_bridge",
                details={"old_drive": old, "new_drive": current_drive},
                severity=0.6,
            )
            return self._maybe_trigger(event)
        return None

    def check_role_transition(self, current_mode: str) -> Optional[TriggerEvent]:
        """Check if primary relational mode has changed."""
        if not self._last_primary_mode:
            self._last_primary_mode = current_mode
            return None

        if current_mode != self._last_primary_mode:
            old = self._last_primary_mode
            self._last_primary_mode = current_mode
            event = TriggerEvent(
                event_type="role_transition",
                source="relational_mode",
                details={"old_mode": old, "new_mode": current_mode},
                severity=0.5,
            )
            return self._maybe_trigger(event)
        return None

    def notify_external_event(self, event_name: str,
                              severity: float = 0.5,
                              details: Optional[Dict] = None) -> Optional[TriggerEvent]:
        """Called when an external event arrives that might require replanning."""
        event = TriggerEvent(
            event_type="external_event",
            source=event_name,
            details=details or {},
            severity=severity,
        )
        return self._maybe_trigger(event)

    def notify_milestone(self, task_name: str,
                         details: Optional[Dict] = None) -> Optional[TriggerEvent]:
        """Called when a major task completes."""
        event = TriggerEvent(
            event_type="milestone",
            source=task_name,
            details=details or {},
            severity=0.4,
        )
        return self._maybe_trigger(event)

    def notify_time_based(self, period: str) -> Optional[TriggerEvent]:
        """Called by scheduler for morning/midday/evening checkpoints."""
        severity_map = {
            "morning": 0.8,  # Morning plan is high-priority
            "midday": 0.4,
            "evening": 0.3,  # Evening is reflection, less urgent
        }
        event = TriggerEvent(
            event_type="time_based",
            source=period,
            details={"period": period},
            severity=severity_map.get(period, 0.3),
        )
        return self._maybe_trigger(event)

    def _maybe_trigger(self, event: TriggerEvent) -> Optional[TriggerEvent]:
        """Check cooldown and severity threshold before triggering replan."""
        now = time.time()

        if event.severity < REPLAN_THRESHOLD:
            logger.debug("Trigger below threshold (%.2f < %.2f): %s",
                         event.severity, REPLAN_THRESHOLD, event.event_type)
            return None

        if now - self._last_replan_time < MIN_REPLAN_INTERVAL_S:
            # Override cooldown for high-severity events
            if event.severity < 0.7:
                logger.debug("Trigger during cooldown (%.0fs remaining): %s",
                             MIN_REPLAN_INTERVAL_S - (now - self._last_replan_time),
                             event.event_type)
                return None

        # Trigger replan
        self._last_replan_time = now
        self._trigger_history.append(event)
        if len(self._trigger_history) > 50:
            self._trigger_history = self._trigger_history[-25:]

        logger.info("Replan triggered: %s (severity=%.2f, source=%s)",
                    event.event_type, event.severity, event.source)

        for callback in self._callbacks:
            try:
                callback(event)
            except Exception as e:
                logger.error("Replan callback error: %s", e)

        # Emit telemetry
        try:
            from repryntt.telemetry import emit_event
            emit_event("replan_triggered", {
                "event_type": event.event_type,
                "source": event.source,
                "severity": event.severity,
            })
        except (ImportError, Exception):
            pass

        return event

    def get_history(self) -> List[Dict[str, Any]]:
        """Get recent trigger history for debugging/telemetry."""
        return [
            {
                "event_type": t.event_type,
                "source": t.source,
                "severity": t.severity,
                "timestamp": t.timestamp,
            }
            for t in self._trigger_history
        ]


_singleton: Optional[ReplanTriggers] = None


def get_replan_triggers(world_state: Optional[WorldState] = None) -> ReplanTriggers:
    """Singleton accessor."""
    global _singleton
    if _singleton is None:
        _singleton = ReplanTriggers(world_state)
    return _singleton
