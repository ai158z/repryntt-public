"""
repryntt.hardware.proprioception — Software-only "did I actually move?" channel.

Hardware reality: Andrew has cameras + depth + sonar but no IMU and no wheel
encoders. The motor stack returns command-receipt (`{success: true, motor_speeds_pct: ...}`)
not motion-feedback, so when commanded motion fails to occur — wheels in air,
stuck against a wall, motor daemon refused the lease, robot lifted up — the
brain has no signal that anything is wrong. The next vision tick just shows
the same scene.

This module closes the loop using only sensors we already have:

  • Optical flow magnitude (from hardware.optical_flow) — frame-to-frame
    motion. If commanded motion is present but flow is below noise floor,
    body is not moving.
  • Sonar trend — distance to the obstacle in front. Stable across
    multiple commanded-motion cycles = corroborating evidence of no motion.
  • Command history — what we asked for, when, expected motion shape.

Output: a `MotionReport` joining commanded vs observed. Surfaces:
  • did_move:           bool
  • expected_motion:    what we should have observed
  • discrepancy:        plain-English description of any mismatch
  • stuck_streak:       consecutive discrepant cycles (drives nudge/escalate)
  • summary:            one-line heartbeat-prompt context

Once a BNO055 IMU is wired in, this module gains accurate yaw/pitch/accel
and the visual-flow heuristic becomes a fallback rather than the primary
signal. Until then, this is the proprioception channel.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Tunables ──────────────────────────────────────────────────────────

# How long after a command we still consider its motion-window "active".
# Motor commands include their own duration; we add this margin to cover
# acceleration/settle time.
COMMAND_OBSERVATION_MARGIN_S = 1.5

# Optical-flow magnitude (mean pixel displacement) below which we treat
# the frame pair as "no motion". The flow module's own FLOW_NOISE_FLOOR
# is the per-pixel threshold; this is the cross-frame mean.
MOTION_FLOW_FLOOR = 1.0

# How many consecutive discrepant cycles trigger the "stuck" escalation.
# 1 = first detected → nudge in next prompt
# 2 = sustained → log to stuck telemetry
# 3+ = persistent → caller decides to halt
STUCK_NUDGE_AT = 1
STUCK_ESCALATE_AT = 3

# Ring-buffer sizes
CMD_HISTORY_LEN = 20
OBS_HISTORY_LEN = 20

# Sonar — how much change across a command window counts as "the world moved".
# Less than this with commanded forward/backward = corroborates stuck.
SONAR_MIN_DELTA_CM = 2.0


# ── Records ───────────────────────────────────────────────────────────


@dataclass
class CommandRecord:
    """One motor command we issued — what we asked for, when."""
    timestamp: float
    command: str             # forward / backward / turn_left / turn_right / stop / ...
    speed_pct: float         # 0-100
    duration_s: float        # how long the command runs

    @property
    def end_time(self) -> float:
        return self.timestamp + self.duration_s + COMMAND_OBSERVATION_MARGIN_S

    @property
    def expects_translation(self) -> bool:
        return self.command in ("forward", "backward", "move_forward", "move_backward") or \
               self.command.startswith("move_")

    @property
    def expects_rotation(self) -> bool:
        return ("turn_" in self.command) or self.command.startswith("spin_") or \
               self.command.startswith("turn_left") or self.command.startswith("turn_right")

    @property
    def expects_motion(self) -> bool:
        return self.expects_translation or self.expects_rotation


@dataclass
class ObservationRecord:
    """One sensor snapshot — what the cameras and sonar saw."""
    timestamp: float
    flow_magnitude: float = 0.0
    flow_significant: bool = False
    flow_dx: float = 0.0
    flow_dy: float = 0.0
    sonar_front_cm: Optional[float] = None
    sonar_rear_cm: Optional[float] = None
    note: str = ""


@dataclass
class MotionReport:
    """Joined commanded-vs-observed verdict at one point in time."""
    timestamp: float
    has_recent_command: bool = False
    last_command: Optional[CommandRecord] = None
    expected_motion: str = "none"        # "translation", "rotation", "none"
    observed_flow_magnitude: float = 0.0
    consistent: bool = True              # observation matches expectation
    discrepancy: str = ""                # plain-English mismatch
    sonar_front_cm: Optional[float] = None
    sonar_front_delta_cm: Optional[float] = None
    stuck_streak: int = 0
    did_move: Optional[bool] = None      # tri-state: True / False / None (no recent command)
    summary: str = ""                    # one-line for prompt injection

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "has_recent_command": self.has_recent_command,
            "command": self.last_command.command if self.last_command else None,
            "expected_motion": self.expected_motion,
            "observed_flow_magnitude": round(self.observed_flow_magnitude, 3),
            "consistent": self.consistent,
            "discrepancy": self.discrepancy,
            "sonar_front_cm": self.sonar_front_cm,
            "sonar_front_delta_cm": self.sonar_front_delta_cm,
            "stuck_streak": self.stuck_streak,
            "did_move": self.did_move,
            "summary": self.summary,
        }


# ── Tracker ───────────────────────────────────────────────────────────


class ProprioceptionTracker:
    """Process-wide tracker. Singleton.

    Three integration points:
      1. Motor code calls record_command(...) right after issuing a PWM command.
      2. Vision/sonar loop calls record_observation(...) every tick.
      3. Brain code calls latest_report() / format_for_heartbeat() before
         each LLM call so Andrew sees "I commanded forward 1s ago and the
         world hasn't moved".

    Thread-safe. Falls through harmlessly if no commands or observations
    have been recorded yet — never raises.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._commands: Deque[CommandRecord] = deque(maxlen=CMD_HISTORY_LEN)
        self._observations: Deque[ObservationRecord] = deque(maxlen=OBS_HISTORY_LEN)
        self._stuck_streak: int = 0
        self._last_report: Optional[MotionReport] = None
        self._last_motion_signature: str = ""

    # ── Recording ────────────────────────────────────────────────────

    def record_command(self, command: str, speed_pct: float = 50.0,
                       duration_s: float = 1.0) -> None:
        """Call right after a motor command is issued.

        The brain code reads from this to know what motion to expect when
        comparing the next vision tick against the previous one.
        """
        if not command:
            return
        rec = CommandRecord(
            timestamp=time.time(),
            command=str(command),
            speed_pct=float(speed_pct),
            duration_s=float(max(0.05, duration_s)),
        )
        with self._lock:
            self._commands.append(rec)

    def record_observation(self,
                           flow_magnitude: float = 0.0,
                           flow_significant: bool = False,
                           flow_dx: float = 0.0,
                           flow_dy: float = 0.0,
                           sonar_front_cm: Optional[float] = None,
                           sonar_rear_cm: Optional[float] = None,
                           note: str = "") -> MotionReport:
        """Call once per perception tick. Returns a fresh MotionReport.

        Always returns a report — when there's no recent motor command,
        the report's `has_recent_command=False` and `consistent=True` so
        callers don't need to special-case "robot is idle".
        """
        obs = ObservationRecord(
            timestamp=time.time(),
            flow_magnitude=float(flow_magnitude),
            flow_significant=bool(flow_significant),
            flow_dx=float(flow_dx),
            flow_dy=float(flow_dy),
            sonar_front_cm=sonar_front_cm,
            sonar_rear_cm=sonar_rear_cm,
            note=note,
        )
        with self._lock:
            self._observations.append(obs)
            report = self._build_report_locked(obs)
            self._last_report = report
        return report

    # ── Reporting ────────────────────────────────────────────────────

    def latest_report(self) -> Optional[MotionReport]:
        with self._lock:
            return self._last_report

    def format_for_heartbeat(self, max_chars: int = 400) -> str:
        """Return a multi-line context block for prompt injection.

        Empty string when there's nothing useful to say (no recent command,
        no observations) — caller can decide to omit the section.
        """
        rep = self.latest_report()
        if rep is None:
            return ""
        lines: List[str] = []
        if rep.has_recent_command and rep.last_command:
            cmd = rep.last_command
            age = time.time() - cmd.timestamp
            lines.append(
                f"MOTION FEEDBACK: commanded {cmd.command} {age:.1f}s ago "
                f"(speed={cmd.speed_pct:.0f}%, duration={cmd.duration_s:.1f}s). "
                f"Expected {rep.expected_motion}; observed flow magnitude "
                f"{rep.observed_flow_magnitude:.2f}px."
            )
            if rep.discrepancy:
                lines.append(f"  ⚠️ {rep.discrepancy}")
                if rep.stuck_streak >= STUCK_NUDGE_AT:
                    lines.append(
                        f"  STUCK STREAK: {rep.stuck_streak} consecutive "
                        f"discrepant cycles. Consider stopping motion commands "
                        f"and investigating before continuing."
                    )
        elif rep.observed_flow_magnitude > 0:
            lines.append(
                f"MOTION FEEDBACK: no recent command; ambient flow "
                f"{rep.observed_flow_magnitude:.2f}px "
                f"({'significant' if rep.observed_flow_magnitude >= MOTION_FLOW_FLOOR else 'quiet'})."
            )
        if rep.sonar_front_cm is not None:
            line = f"SONAR: front {rep.sonar_front_cm:.0f}cm"
            if rep.sonar_front_delta_cm is not None:
                trend = ("closing" if rep.sonar_front_delta_cm < -SONAR_MIN_DELTA_CM
                         else "receding" if rep.sonar_front_delta_cm > SONAR_MIN_DELTA_CM
                         else "stable")
                line += f" (Δ {rep.sonar_front_delta_cm:+.0f}cm — {trend})"
            lines.append(line)
        out = "\n".join(lines)
        return out[:max_chars]

    # ── Internal ─────────────────────────────────────────────────────

    def _build_report_locked(self, obs: ObservationRecord) -> MotionReport:
        # Find the most recent command whose observation window is still open.
        recent_cmd: Optional[CommandRecord] = None
        for c in reversed(self._commands):
            if c.command in ("stop", ""):
                continue
            if c.end_time >= obs.timestamp:
                recent_cmd = c
                break

        # Sonar delta vs prior observation.
        prior_obs: Optional[ObservationRecord] = None
        if len(self._observations) >= 2:
            prior_obs = self._observations[-2]
        sonar_delta: Optional[float] = None
        if obs.sonar_front_cm is not None and prior_obs is not None \
                and prior_obs.sonar_front_cm is not None:
            sonar_delta = obs.sonar_front_cm - prior_obs.sonar_front_cm

        rep = MotionReport(
            timestamp=obs.timestamp,
            observed_flow_magnitude=obs.flow_magnitude,
            sonar_front_cm=obs.sonar_front_cm,
            sonar_front_delta_cm=sonar_delta,
        )

        if recent_cmd is None or not recent_cmd.expects_motion:
            # No active motion command — observation has no expectation to bind to.
            rep.has_recent_command = False
            rep.expected_motion = "none"
            rep.consistent = True
            rep.did_move = None
            self._stuck_streak = 0
            rep.summary = "idle"
            return rep

        rep.has_recent_command = True
        rep.last_command = recent_cmd
        rep.expected_motion = ("translation" if recent_cmd.expects_translation
                               else "rotation")

        moved = obs.flow_magnitude >= MOTION_FLOW_FLOOR
        # Corroboration: sonar should change for translation commands.
        sonar_corroborates_motion = True
        if recent_cmd.expects_translation and sonar_delta is not None:
            sonar_corroborates_motion = abs(sonar_delta) >= SONAR_MIN_DELTA_CM

        if moved and (not recent_cmd.expects_translation or sonar_corroborates_motion):
            rep.consistent = True
            rep.did_move = True
            self._stuck_streak = 0
            rep.summary = (
                f"commanded {recent_cmd.command}; observed motion "
                f"(flow={obs.flow_magnitude:.2f}px)"
            )
        else:
            # Discrepancy: commanded motion did not produce observable motion.
            rep.consistent = False
            rep.did_move = False
            self._stuck_streak += 1
            rep.stuck_streak = self._stuck_streak
            why_parts = [
                f"commanded {recent_cmd.command} ({recent_cmd.duration_s:.1f}s "
                f"at {recent_cmd.speed_pct:.0f}%)",
                f"flow magnitude {obs.flow_magnitude:.2f}px (need ≥{MOTION_FLOW_FLOOR})",
            ]
            if recent_cmd.expects_translation and sonar_delta is not None \
                    and not sonar_corroborates_motion:
                why_parts.append(
                    f"front sonar Δ {sonar_delta:+.1f}cm (need ≥{SONAR_MIN_DELTA_CM}cm)"
                )
            rep.discrepancy = (
                "Body did not respond to motion command — possible causes: "
                "wheels off ground, motor daemon refused lease, blocked, lifted, "
                "or wiring fault. "
                + "; ".join(why_parts) + "."
            )
            rep.summary = (
                f"STUCK: commanded {recent_cmd.command} but body did not move "
                f"(flow={obs.flow_magnitude:.2f}px, streak={self._stuck_streak})"
            )
        rep.stuck_streak = self._stuck_streak
        return rep


# ── Singleton ─────────────────────────────────────────────────────────


_singleton: Optional[ProprioceptionTracker] = None
_singleton_lock = threading.Lock()


def get_proprioception() -> ProprioceptionTracker:
    """Process-wide proprioception tracker."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = ProprioceptionTracker()
        return _singleton


__all__ = [
    "ProprioceptionTracker",
    "MotionReport",
    "CommandRecord",
    "ObservationRecord",
    "get_proprioception",
    "MOTION_FLOW_FLOOR",
    "STUCK_NUDGE_AT",
    "STUCK_ESCALATE_AT",
]
