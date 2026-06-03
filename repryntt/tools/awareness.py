#!/usr/bin/env python3
"""
Environment Awareness — Time context, daemon state, and system introspection.

Migrated from SAIGE/brain/brain_system.py Phase 7.
"""

import time
import logging
from datetime import datetime
from typing import Any, Dict

logger = logging.getLogger(__name__)


def get_current_time(format: str = "full", **kwargs) -> Dict[str, Any]:
    """Get comprehensive current time information.

    Args:
        format: 'full' (default), 'brief', or 'timestamp'.
    """
    now = datetime.now()
    hour = now.hour
    if hour < 6:
        time_of_day = "late_night"
    elif hour < 12:
        time_of_day = "morning"
    elif hour < 17:
        time_of_day = "afternoon"
    elif hour < 21:
        time_of_day = "evening"
    else:
        time_of_day = "night"

    if format == "timestamp":
        return {"timestamp": time.time()}
    if format == "brief":
        return {
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
        }

    return {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "day_of_week": now.strftime("%A"),
        "time_of_day": time_of_day,
        "timezone": time.tzname[0],
        "year": now.year,
        "timestamp": time.time(),
    }


def get_current_time_context() -> str:
    """Get current time context string for AI prompt awareness."""
    try:
        info = get_current_time()
        return (
            f"CURRENT TIME CONTEXT:\n"
            f"- Date: {info.get('date', 'unknown')} ({info.get('day_of_week', 'unknown')})\n"
            f"- Time: {info.get('time', 'unknown')} ({info.get('time_of_day', 'unknown')})\n"
            f"- Timezone: {info.get('timezone', 'unknown')}\n"
            f"- Year: {info.get('year', 'unknown')} (Note: AI model trained on data up to 2023)"
        )
    except Exception as e:
        logger.warning(f"Could not get time context: {e}")
        return "CURRENT TIME CONTEXT: Unable to determine current time"


def get_daemon_state(brain_system) -> Dict[str, Any]:
    """Get the daemon's current operational state."""
    try:
        state = {
            "uptime": time.time() - getattr(brain_system, "start_time", time.time()),
            "active_chains": 0,
            "personality_name": "Unknown",
            "hormone_dominant": "neutral",
        }
        pb = getattr(brain_system, "personality_brain", {})
        if pb:
            state["personality_name"] = pb.get("personality", {}).get("name", "Unknown")
        hs = getattr(brain_system, "hormone_system", None)
        if hs:
            dominant, _ = hs.get_dominant_circuit()
            state["hormone_dominant"] = dominant
        return state
    except Exception as e:
        logger.warning(f"Error getting daemon state: {e}")
        return {"error": str(e)}
