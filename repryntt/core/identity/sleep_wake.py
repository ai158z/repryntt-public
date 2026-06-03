#!/usr/bin/env python3
"""
Sleep/Wake Tracker — Gives Artemis awareness of downtime and restarts.

When the daemon shuts down, we record the timestamp. When it starts back up,
we compute the gap and classify it:
  - Blink (< 2 min):   Quick restart, barely noticeable
  - Nap (2-30 min):    Short maintenance window
  - Sleep (30min-8h):  Normal overnight or planned downtime
  - Deep sleep (8-24h): Extended outage
  - Coma (> 24h):      Something went wrong

The wake context is injected into the FIRST heartbeat after startup,
giving Artemis temporal awareness — like a human waking up and knowing
what day it is, how long they slept, and roughly what they need to do.

Storage: ~/.repryntt/brain/sleep_wake.json
"""

import json
import os
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def _default_path() -> Path:
    return Path.home() / ".repryntt" / "brain" / "sleep_wake.json"


class SleepWakeTracker:
    """Tracks daemon sleep/wake cycles for temporal awareness."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or _default_path()
        self._data: Optional[Dict] = None
        self._wake_context: Optional[str] = None  # Cached for injection into first heartbeat

    def _load(self) -> Dict:
        if self._data is not None:
            return self._data
        if self.path.exists():
            try:
                with open(self.path, 'r') as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._data = {}
        else:
            self._data = {}
        return self._data

    def _save(self):
        if self._data is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(self.path) + ".tmp"
        with open(tmp, 'w') as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, str(self.path))

    # ───────────────────────────────────────────────────────
    # SHUTDOWN — called when daemon saves state / stops
    # ───────────────────────────────────────────────────────

    def record_shutdown(self):
        """Record current time as the last-known-alive timestamp.

        Called from _save_state() so it updates on every state persist,
        not just clean shutdowns. This way, even crashes leave a
        reasonably recent 'last alive' marker.
        """
        data = self._load()
        data["last_alive"] = datetime.now().isoformat()
        data["shutdown_count"] = data.get("shutdown_count", 0) + 1
        self._save()

    def record_heartbeat_alive(self):
        """Lightweight alive marker — called periodically during operation.

        Less expensive than full shutdown recording. Updates last_alive
        so gaps are accurately measured even after hard crashes.
        """
        data = self._load()
        data["last_alive"] = datetime.now().isoformat()
        # Don't save every heartbeat — just update in-memory.
        # The next _save_state() will persist it.

    # ───────────────────────────────────────────────────────
    # STARTUP — called when daemon starts
    # ───────────────────────────────────────────────────────

    def record_startup(self) -> str:
        """Record startup and compute wake context.

        Returns a human-readable wake briefing string for prompt injection.
        """
        data = self._load()
        now = datetime.now()
        wake_time = now.isoformat()

        last_alive_str = data.get("last_alive")
        prev_wake_str = data.get("last_wake")

        # Compute sleep duration
        sleep_duration = None
        sleep_category = "unknown"
        sleep_seconds = 0

        if last_alive_str:
            try:
                last_alive = datetime.fromisoformat(last_alive_str)
                sleep_duration = now - last_alive
                sleep_seconds = sleep_duration.total_seconds()
                sleep_category = self._classify_sleep(sleep_seconds)
            except (ValueError, TypeError):
                pass

        # Build wake history entry
        wake_entry = {
            "wake_time": wake_time,
            "last_alive": last_alive_str,
            "sleep_seconds": round(sleep_seconds),
            "sleep_category": sleep_category,
        }

        # Maintain a rolling history (last 10 wake cycles)
        history = data.get("wake_history", [])
        history.append(wake_entry)
        if len(history) > 10:
            history = history[-10:]

        # Update state
        data["last_wake"] = wake_time
        data["previous_wake"] = prev_wake_str
        data["current_session_start"] = wake_time
        data["wake_history"] = history
        data["total_wake_cycles"] = data.get("total_wake_cycles", 0) + 1
        self._save()

        # Build the wake briefing
        briefing = self._build_wake_briefing(
            sleep_seconds=sleep_seconds,
            sleep_category=sleep_category,
            last_alive_str=last_alive_str,
            wake_time=now,
            total_wakes=data["total_wake_cycles"],
        )
        self._wake_context = briefing
        logger.info(f"☀️ Wake recorded: {sleep_category} "
                    f"({self._format_duration(sleep_seconds)} offline)")
        return briefing

    def get_wake_context(self) -> Optional[str]:
        """Get the cached wake briefing for injection into the first heartbeat.

        Returns None after the first call (consumed once).
        """
        ctx = self._wake_context
        self._wake_context = None  # Consume — only inject once
        return ctx

    def get_session_uptime(self) -> str:
        """Get how long the current session has been running."""
        data = self._load()
        start_str = data.get("current_session_start")
        if not start_str:
            return "unknown"
        try:
            start = datetime.fromisoformat(start_str)
            uptime = datetime.now() - start
            return self._format_duration(uptime.total_seconds())
        except (ValueError, TypeError):
            return "unknown"

    # ───────────────────────────────────────────────────────
    # INTERNAL
    # ───────────────────────────────────────────────────────

    @staticmethod
    def _classify_sleep(seconds: float) -> str:
        if seconds < 120:
            return "blink"        # < 2 min — quick restart
        elif seconds < 1800:
            return "nap"          # 2-30 min — short maintenance
        elif seconds < 28800:
            return "sleep"        # 30min-8h — normal overnight
        elif seconds < 86400:
            return "deep_sleep"   # 8-24h — extended downtime
        else:
            return "coma"         # > 24h — something went wrong

    @staticmethod
    def _format_duration(seconds: float) -> str:
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            return f"{int(seconds / 60)}m"
        elif seconds < 86400:
            hours = int(seconds / 3600)
            mins = int((seconds % 3600) / 60)
            return f"{hours}h {mins}m" if mins else f"{hours}h"
        else:
            days = int(seconds / 86400)
            hours = int((seconds % 86400) / 3600)
            return f"{days}d {hours}h" if hours else f"{days}d"

    def _build_wake_briefing(
        self,
        sleep_seconds: float,
        sleep_category: str,
        last_alive_str: Optional[str],
        wake_time: datetime,
        total_wakes: int,
    ) -> str:
        """Build a natural-language wake briefing for prompt injection."""
        duration_str = self._format_duration(sleep_seconds)
        day_name = wake_time.strftime("%A")  # Monday, Tuesday, etc.
        time_str = wake_time.strftime("%I:%M %p").lstrip("0")
        date_str = wake_time.strftime("%B %d, %Y")  # March 24, 2026

        # Opening — varies by sleep category
        if sleep_category == "blink":
            opener = (
                f"Quick restart — you were only offline for {duration_str}. "
                f"Pick up where you left off."
            )
        elif sleep_category == "nap":
            opener = (
                f"Short nap — you were offline for {duration_str}. "
                f"Check if anything changed while you were out."
            )
        elif sleep_category == "sleep":
            opener = (
                f"Good morning. You slept for {duration_str}. "
                f"It's {day_name}, {date_str}, {time_str}. "
                f"Read yesterday's notes and plan your day."
            )
        elif sleep_category == "deep_sleep":
            opener = (
                f"You were offline for {duration_str} — that's a long one. "
                f"It's {day_name}, {date_str}, {time_str}. "
                f"Catch up on what you missed before diving into new work."
            )
        elif sleep_category == "coma":
            opener = (
                f"You were offline for {duration_str}. A lot may have changed. "
                f"It's {day_name}, {date_str}, {time_str}. "
                f"Start by reading recent memory files and checking email."
            )
        else:
            opener = (
                f"First boot — welcome. It's {day_name}, {date_str}, {time_str}. "
                f"This is session #{total_wakes}."
            )

        # Behavioral guidance based on gap length
        if sleep_seconds >= 1800:  # >= 30 min
            guidance = (
                "\n**After waking up**:\n"
                "1. Check email (gmail_read_inbox) for operator messages\n"
                "2. Read yesterday's memory notes for continuity\n"
                "3. Review or create today's daily plan\n"
                "4. Then begin your first real task"
            )
        else:
            guidance = ""

        return f"**☀️ WAKE STATUS**: {opener}{guidance}"
