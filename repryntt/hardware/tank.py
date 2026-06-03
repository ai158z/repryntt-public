"""
repryntt.hardware.tank — Tracked Tank Body Controller (Direct GPIO).

Differential-drive tracked vehicle with 2 DC gear motors driven by
a TB6612FNG H-bridge connected directly to the Jetson Orin Nano GPIO header.

Architecture:
    Jetson Orin Nano (this code)
        ↕ GPIO (Jetson.GPIO chardev)
    TB6612FNG H-bridge
        ↕ Motor A / Motor B outputs
    2× DC gear motors (left track, right track)

Wiring (BOARD pin numbering):
    Pin 1  (3.3V)  → TB6612 VCC
    Pin 6  (GND)   → TB6612 GND
    Pin 17 (3.3V)  → TB6612 STBY (always enabled)
    Pin 29 (GPIO)  → TB6612 AIN1 (Motor A direction 1)
    Pin 31 (GPIO)  → TB6612 AIN2 (Motor A direction 2)
    Pin 32 (PWM)   → TB6612 PWMA (Motor A speed)
    Pin 33 (PWM)   → TB6612 PWMB (Motor B speed)
    Pin 35 (GPIO)  → TB6612 BIN1 (Motor B direction 1)
    Pin 37 (GPIO)  → TB6612 BIN2 (Motor B direction 2)
    External 6V    → TB6612 VM   (motor power supply)

Pinmux: The systemd service repryntt-pinmux.service sets all 6 GPIO
pins to GPIO output mode (0x0400) on every boot. Without this, the
Tegra234 default pinmux routes some of these pads to SPI/I2S functions.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── GPIO conditional import ──────────────────────────────────────────

try:
    import Jetson.GPIO as GPIO
    GPIO_AVAILABLE = True
except (ImportError, RuntimeError) as exc:
    logger.warning("Tank controller: Jetson.GPIO unavailable at import: %s", exc)
    GPIO_AVAILABLE = False

# ── Pin assignments (BOARD numbering) ────────────────────────────────

PIN_AIN1 = 29   # Motor A direction 1
PIN_AIN2 = 31   # Motor A direction 2
PIN_PWMA = 32   # Motor A speed (PWM-capable)
PIN_BIN1 = 35   # Motor B direction 1
PIN_BIN2 = 37   # Motor B direction 2
PIN_PWMB = 33   # Motor B speed (PWM-capable)

ALL_MOTOR_PINS = [PIN_AIN1, PIN_AIN2, PIN_PWMA, PIN_BIN1, PIN_BIN2, PIN_PWMB]

# ── Tank physical limits ─────────────────────────────────────────────

MAX_SPEED_PCT = 100         # max PWM duty cycle %
MAX_COMMAND_DURATION = 10.0 # seconds
DEFAULT_SPEED_PCT = 60      # comfortable default


# ── Body state ───────────────────────────────────────────────────────

@dataclass
class TankBodyState:
    """Current state of the tank body."""
    is_moving: bool = False
    left_speed_pct: float = 0.0
    right_speed_pct: float = 0.0
    left_direction: str = "stopped"   # "forward", "reverse", "stopped"
    right_direction: str = "stopped"
    emergency_stopped: bool = False
    last_command_time: float = 0.0
    last_command: str = ""
    total_commands: int = 0
    gpio_initialized: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_moving": self.is_moving,
            "left_motor": {"speed_pct": self.left_speed_pct, "direction": self.left_direction},
            "right_motor": {"speed_pct": self.right_speed_pct, "direction": self.right_direction},
            "emergency_stopped": self.emergency_stopped,
            "last_command": self.last_command,
            "last_command_time": self.last_command_time,
            "total_commands": self.total_commands,
            "gpio_initialized": self.gpio_initialized,
        }


# ── Tank controller ──────────────────────────────────────────────────

class TankController:
    """Direct GPIO tank body controller for TB6612FNG + Jetson Orin Nano.

    Provides movement primitives that Andrew can call:
    - move_forward / move_backward
    - turn_left / turn_right / spin
    - stop / emergency_stop
    - get_body_status
    """

    # Release GPIO lines after this many seconds without a motor command,
    # so other processes (teleop in nexus_app vs. explorer in the daemon)
    # can take turns owning the chardev locks without fighting forever.
    # First post-release command pays a ~0.5s init cost.
    IDLE_RELEASE_TIMEOUT_S = 3.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = TankBodyState()
        self._initialized = False
        self._last_cmd_ts: float = 0.0
        self._idle_thread: Optional[threading.Thread] = None
        self._idle_stop = threading.Event()

    @property
    def is_available(self) -> bool:
        return self._initialized

    @property
    def body_state(self) -> TankBodyState:
        return self._state

    def initialize(self) -> bool:
        """Initialize GPIO pins for motor control. Returns True on success."""
        if not GPIO_AVAILABLE:
            logger.warning("Tank controller: Jetson.GPIO not available")
            return False

        if self._initialized:
            return True

        try:
            GPIO.setmode(GPIO.BOARD)
            GPIO.setwarnings(False)

            # Setup all motor pins as outputs, initially LOW (motors off)
            for pin in ALL_MOTOR_PINS:
                GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)

            # Give pins a moment to stabilize after setup
            time.sleep(0.5)

            self._initialized = True
            self._state.gpio_initialized = True
            self._last_cmd_ts = time.time()
            self._start_idle_watcher()
            logger.info("Tank controller initialized (direct GPIO, TB6612FNG)")
            return True

        except Exception as e:
            # EBUSY ("Device or resource busy") happens when a previous
            # process held these gpiochip lines and the kernel hasn't
            # released them, OR when something in this process already
            # claimed them. Try once to release & re-claim before giving up.
            msg = str(e)
            if "Device or resource busy" in msg or "Errno 16" in msg:
                logger.warning(
                    "Tank init EBUSY on motor pins — attempting cleanup + retry: %s",
                    msg,
                )
                try:
                    GPIO.cleanup(ALL_MOTOR_PINS)
                except Exception:
                    pass
                time.sleep(0.3)
                try:
                    GPIO.setmode(GPIO.BOARD)
                    GPIO.setwarnings(False)
                    for pin in ALL_MOTOR_PINS:
                        GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)
                    time.sleep(0.5)
                    self._initialized = True
                    self._state.gpio_initialized = True
                    self._last_cmd_ts = time.time()
                    self._start_idle_watcher()
                    logger.info("Tank controller initialized after EBUSY recovery")
                    return True
                except Exception as e2:
                    logger.error("Tank controller init retry failed: %s", e2)
                    return False
            logger.error("Tank controller init failed: %s", e)
            return False

    def _ensure_init(self) -> bool:
        """Lazy-initialize on first use."""
        if not self._initialized:
            return self.initialize()
        return True

    # ── Low-level motor control ──────────────────────────────────────

    def _set_motor_a(self, speed_pct: float, forward: bool = True) -> None:
        """Drive motor A. forward=True for forward. Uses GPIO HIGH/LOW (full power)."""
        speed_pct = max(0, min(100, abs(speed_pct)))
        if speed_pct < 1:
            GPIO.output(PIN_AIN1, GPIO.LOW)
            GPIO.output(PIN_AIN2, GPIO.LOW)
            GPIO.output(PIN_PWMA, GPIO.LOW)
            self._state.left_direction = "stopped"
            self._state.left_speed_pct = 0
        else:
            if forward:
                # Reversed: AIN2=HIGH, AIN1=LOW (correct for chassis orientation)
                GPIO.output(PIN_AIN1, GPIO.LOW)
                GPIO.output(PIN_AIN2, GPIO.HIGH)
                self._state.left_direction = "forward"
            else:
                GPIO.output(PIN_AIN1, GPIO.HIGH)
                GPIO.output(PIN_AIN2, GPIO.LOW)
                self._state.left_direction = "reverse"
            GPIO.output(PIN_PWMA, GPIO.HIGH)
            self._state.left_speed_pct = 100

    def _set_motor_b(self, speed_pct: float, forward: bool = True) -> None:
        """Drive motor B. forward=True for forward. Uses GPIO HIGH/LOW (full power).
        NOTE: Motor B direction is inverted because it's mounted mirrored on the chassis.
        """
        speed_pct = max(0, min(100, abs(speed_pct)))
        if speed_pct < 1:
            GPIO.output(PIN_BIN1, GPIO.LOW)
            GPIO.output(PIN_BIN2, GPIO.LOW)
            GPIO.output(PIN_PWMB, GPIO.LOW)
            self._state.right_direction = "stopped"
            self._state.right_speed_pct = 0
        else:
            if forward:
                # BIN1=HIGH, BIN2=LOW (correct for chassis orientation)
                GPIO.output(PIN_BIN1, GPIO.HIGH)
                GPIO.output(PIN_BIN2, GPIO.LOW)
                self._state.right_direction = "forward"
            else:
                GPIO.output(PIN_BIN1, GPIO.LOW)
                GPIO.output(PIN_BIN2, GPIO.HIGH)
                self._state.right_direction = "reverse"
            GPIO.output(PIN_PWMB, GPIO.HIGH)
            self._state.right_speed_pct = 100

    def _stop_all(self) -> None:
        """Stop both motors immediately."""
        self._set_motor_a(0)
        self._set_motor_b(0)
        self._state.is_moving = False

    # ── Movement primitives ──────────────────────────────────────────

    # Reactive safety thresholds — gate motor commands on sonar so the
    # controller refuses unsafe motion regardless of what called it.
    SAFETY_FRONT_STOP_CM = 18.0   # below this: refuse forward motion
    SAFETY_REAR_STOP_CM = 15.0    # below this: refuse reverse motion

    def _sonar_safety_check(self, command_name: str) -> Optional[Dict[str, Any]]:
        """Reactive safety reflex.

        Returns an error dict if the sonar shows we're too close to a wall
        in the direction we want to move, otherwise None. Failure of the
        sonar itself is non-fatal (returns None) — we don't want a flaky
        sensor to stop the robot from ever moving.
        """
        # Only gate primitives that translate into the body — turns are
        # in-place pivots; safe to allow even tight to a wall.
        if command_name not in ("forward", "backward"):
            return None
        try:
            from repryntt.hardware.sonar import get_sonar
            sonar = get_sonar()
            if not sonar._initialized and not sonar.initialize():
                return None
            if command_name == "forward" and sonar._front_ok:
                r = sonar.read_front(samples=1)
                if r.valid and r.distance_cm < self.SAFETY_FRONT_STOP_CM:
                    return {
                        "success": False,
                        "error": f"sonar_safety: front={r.distance_cm:.0f}cm "
                                 f"< {self.SAFETY_FRONT_STOP_CM:.0f}cm — "
                                 f"refusing forward",
                        "front_cm": r.distance_cm,
                    }
            if command_name == "backward" and sonar._rear_ok:
                r = sonar.read_rear(samples=1)
                if r.valid and r.distance_cm < self.SAFETY_REAR_STOP_CM:
                    return {
                        "success": False,
                        "error": f"sonar_safety: rear={r.distance_cm:.0f}cm "
                                 f"< {self.SAFETY_REAR_STOP_CM:.0f}cm — "
                                 f"refusing reverse",
                        "rear_cm": r.distance_cm,
                    }
        except Exception as e:
            logger.debug("sonar_safety check skipped: %s", e)
        return None

    def _drive(self, left_pct: float, left_fwd: bool,
               right_pct: float, right_fwd: bool,
               duration: float, command_name: str) -> Dict[str, Any]:
        """Core drive command. Sets both motors, waits, then stops."""
        if not self._ensure_init():
            return {"success": False, "error": "Tank controller not initialized — GPIO unavailable"}

        if self._state.emergency_stopped:
            return {"success": False, "error": "Emergency stop active — call reset_emergency_stop() first"}

        # Reactive sonar safety reflex (sub-50ms, runs at the controller
        # level so it gates ALL callers — explorer, nav_cortex, and any
        # tool the LLM invokes directly).
        safety_block = self._sonar_safety_check(command_name)
        if safety_block is not None:
            logger.info("🛑 %s blocked: %s", command_name, safety_block.get("error"))
            return safety_block

        duration = max(0, min(MAX_COMMAND_DURATION, duration))

        with self._lock:
            self._set_motor_a(left_pct, left_fwd)
            self._set_motor_b(right_pct, right_fwd)
            self._state.is_moving = True
            self._state.last_command_time = time.time()
            self._state.last_command = command_name
            self._state.total_commands += 1
            self._last_cmd_ts = time.time()

        if duration > 0:
            # Mid-command safety re-check for forward/backward — if a wall
            # appears mid-move, cut motors early.
            if command_name in ("forward", "backward") and duration > 0.3:
                end_t = time.time() + duration
                check_interval = 0.15
                while time.time() < end_t:
                    sleep_for = min(check_interval, end_t - time.time())
                    time.sleep(max(0, sleep_for))
                    block = self._sonar_safety_check(command_name)
                    if block is not None:
                        logger.info(
                            "🛑 %s aborted mid-command: %s",
                            command_name, block.get("error"),
                        )
                        with self._lock:
                            self._stop_all()
                        block["aborted_mid_command"] = True
                        block["partial_duration_s"] = round(
                            duration - max(0, end_t - time.time()), 2
                        )
                        return block
            else:
                time.sleep(duration)
            with self._lock:
                self._stop_all()

        # ── Proprioception: record what we just commanded so the brain
        # can compare it against the next observed frame/sonar. Without
        # encoders/IMU this is the only "did I really move?" channel.
        try:
            from repryntt.hardware.proprioception import get_proprioception
            # The effective speed is the max of the two motors (close enough
            # for the discrepancy heuristic — encoders will replace this).
            _speed_pct = max(left_pct, right_pct)
            get_proprioception().record_command(
                command=command_name,
                speed_pct=_speed_pct,
                duration_s=duration,
            )
        except Exception:
            logger.debug("proprioception.record_command failed (non-fatal)", exc_info=True)

        return {
            "success": True,
            "command": command_name,
            "left_motor": {"speed_pct": left_pct, "direction": "forward" if left_fwd else "reverse"},
            "right_motor": {"speed_pct": right_pct, "direction": "forward" if right_fwd else "reverse"},
            "duration": round(duration, 2),
        }

    def move_forward(self, speed: float = 0.6, duration: float = 1.0) -> Dict[str, Any]:
        """Drive forward. speed is 0.0-1.0 (fraction of max), duration in seconds."""
        pct = max(10, min(100, abs(speed) * 100 if speed <= 1.0 else speed))
        return self._drive(pct, True, pct, True, duration, "forward")

    def move_backward(self, speed: float = 0.6, duration: float = 1.0) -> Dict[str, Any]:
        """Drive backward. speed is 0.0-1.0 (fraction of max), duration in seconds."""
        pct = max(10, min(100, abs(speed) * 100 if speed <= 1.0 else speed))
        return self._drive(pct, False, pct, False, duration, "backward")

    def turn_left(self, speed: float = 0.5, duration: float = 1.0) -> Dict[str, Any]:
        """Pivot turn left (left track backward, right track forward)."""
        pct = max(10, min(100, abs(speed) * 100 if speed <= 1.0 else speed))
        return self._drive(pct, False, pct, True, duration, "turn_left")

    def turn_right(self, speed: float = 0.5, duration: float = 1.0) -> Dict[str, Any]:
        """Pivot turn right (left track forward, right track backward)."""
        pct = max(10, min(100, abs(speed) * 100 if speed <= 1.0 else speed))
        return self._drive(pct, True, pct, False, duration, "turn_right")

    def spin(self, degrees: float = 180, speed: float = 0.5) -> Dict[str, Any]:
        """Spin in place by degrees. Positive=left, negative=right.
        Duration estimated from degrees and speed (no encoder feedback yet).
        """
        pct = max(10, min(100, abs(speed) * 100 if speed <= 1.0 else speed))
        # Rough estimate: ~1 second per 90 degrees at 50% speed
        duration = abs(degrees) / 90.0 * (50.0 / max(pct, 10))
        duration = min(duration, MAX_COMMAND_DURATION)
        if degrees >= 0:
            return self._drive(pct, False, pct, True, duration, f"spin_left_{abs(degrees)}deg")
        else:
            return self._drive(pct, True, pct, False, duration, f"spin_right_{abs(degrees)}deg")

    # ── Closed-loop displacement (encoder-ready) ─────────────────────

    # Empirical calibration: cm of travel per second at 100% PWM.
    # Set this from a stopwatch measurement on the actual surface.
    # Override at runtime: TankController.CM_PER_SEC_AT_FULL = <value>
    # When wheel encoders land, swap the time-based loop below for a
    # real closed-loop controller that reads ticks.
    CM_PER_SEC_AT_FULL = 22.0     # measured on hardwood; recalibrate per surface
    DEG_PER_SEC_AT_FULL = 90.0    # in-place pivot rate at full PWM

    def move_distance(self, distance_cm: float,
                      speed: float = 0.5) -> Dict[str, Any]:
        """Drive forward (+) or backward (−) by a target distance in cm.

        Without wheel encoders this uses a time-based estimate from
        CM_PER_SEC_AT_FULL. The sonar safety reflex still gates motion,
        so the actual travelled distance may be less if a wall appears.
        Returns {success, requested_cm, estimated_cm, ...}.
        """
        if abs(distance_cm) < 1.0:
            return {"success": True, "estimated_cm": 0.0, "requested_cm": distance_cm}

        pct = max(10, min(100, abs(speed) * 100 if speed <= 1.0 else speed))
        rate = self.CM_PER_SEC_AT_FULL * (pct / 100.0)
        if rate <= 0:
            return {"success": False, "error": "speed too low"}

        duration = min(abs(distance_cm) / rate, MAX_COMMAND_DURATION)

        if distance_cm >= 0:
            r = self._drive(pct, True, pct, True, duration, f"move_forward_{abs(distance_cm):.0f}cm")
        else:
            r = self._drive(pct, False, pct, False, duration, f"move_backward_{abs(distance_cm):.0f}cm")

        # Estimate actual distance travelled (caps at safety abort).
        actual_duration = r.get("partial_duration_s", duration) if isinstance(r, dict) else duration
        estimated_cm = rate * actual_duration * (1 if distance_cm >= 0 else -1)
        if isinstance(r, dict):
            r["requested_cm"] = round(distance_cm, 1)
            r["estimated_cm"] = round(estimated_cm, 1)
        return r

    def turn_degrees(self, degrees: float,
                     speed: float = 0.5) -> Dict[str, Any]:
        """In-place pivot by target degrees. Positive=left, negative=right.

        Time-based estimate using DEG_PER_SEC_AT_FULL — replace with
        IMU-based closed loop once a BNO055 is wired in.
        """
        if abs(degrees) < 1.0:
            return {"success": True, "estimated_degrees": 0.0}

        pct = max(10, min(100, abs(speed) * 100 if speed <= 1.0 else speed))
        rate = self.DEG_PER_SEC_AT_FULL * (pct / 100.0)
        if rate <= 0:
            return {"success": False, "error": "speed too low"}

        duration = min(abs(degrees) / rate, MAX_COMMAND_DURATION)
        if degrees >= 0:
            r = self._drive(pct, False, pct, True, duration, f"turn_left_{abs(degrees):.0f}deg")
        else:
            r = self._drive(pct, True, pct, False, duration, f"turn_right_{abs(degrees):.0f}deg")
        if isinstance(r, dict):
            r["requested_degrees"] = round(degrees, 1)
            r["estimated_degrees"] = round(rate * duration * (1 if degrees >= 0 else -1), 1)
        return r

    def drive_continuous(self, left_vel: float, right_vel: float) -> None:
        """Non-blocking velocity command for continuous ROS2 cmd_vel control.

        left_vel / right_vel: -1.0 (full reverse) to +1.0 (full forward).
        Call repeatedly (e.g. from a /cmd_vel subscriber at 10 Hz).
        Caller is responsible for stopping via drive_continuous(0, 0).
        Sonar safety check is bypassed here — cmd_vel callers (Nav2) handle
        collision avoidance at the planner level; double-checking here at
        GPIO resolution would just add latency.
        """
        if not self._ensure_init():
            return

        if self._state.emergency_stopped:
            return

        def _apply(vel: float, set_motor, state_dir_attr, state_speed_attr):
            pct = max(0, min(100, abs(vel) * 100))
            fwd = vel >= 0
            if pct < 1:
                set_motor(0, True)
            else:
                set_motor(pct, fwd)

        with self._lock:
            _apply(left_vel,  self._set_motor_a,
                   "_state.left_direction",  "_state.left_speed_pct")
            _apply(right_vel, self._set_motor_b,
                   "_state.right_direction", "_state.right_speed_pct")
            self._state.is_moving = abs(left_vel) > 0.01 or abs(right_vel) > 0.01
            self._state.last_command_time = time.time()
            self._state.last_command = f"vel L={left_vel:+.2f} R={right_vel:+.2f}"

    def stop(self) -> Dict[str, Any]:
        """Graceful stop — kills PWM and sets direction pins LOW."""
        if not self._ensure_init():
            return {"success": False, "error": "Tank controller not initialized"}

        with self._lock:
            self._stop_all()

        return {"success": True, "message": "Tank stopped"}

    def emergency_stop(self) -> Dict[str, Any]:
        """Emergency stop — immediately kills all motor power."""
        if not self._ensure_init():
            return {"success": False, "error": "Tank controller not initialized"}

        with self._lock:
            self._stop_all()
            self._state.emergency_stopped = True

        return {"success": True, "message": "EMERGENCY STOP activated — motors killed"}

    def reset_emergency_stop(self) -> Dict[str, Any]:
        """Reset emergency stop — re-enables motor control."""
        self._state.emergency_stopped = False
        return {"success": True, "message": "Emergency stop reset — motors re-enabled"}

    def get_body_status(self) -> Dict[str, Any]:
        """Get full body status — motor state, command history, GPIO state."""
        return {
            "success": True,
            "body": self._state.to_dict(),
            "hardware": {
                "gpio_available": GPIO_AVAILABLE,
                "controller_initialized": self._initialized,
                "driver": "TB6612FNG",
                "interface": "Jetson.GPIO (chardev)",
                "pins": {
                    "AIN1": PIN_AIN1, "AIN2": PIN_AIN2, "PWMA": PIN_PWMA,
                    "BIN1": PIN_BIN1, "BIN2": PIN_BIN2, "PWMB": PIN_PWMB,
                },
                "max_command_duration_s": MAX_COMMAND_DURATION,
            },
        }

    def shutdown(self) -> None:
        """Clean up GPIO resources."""
        if self._initialized:
            try:
                self._stop_all()
                GPIO.cleanup(ALL_MOTOR_PINS)
            except Exception as e:
                logger.debug("Tank shutdown cleanup: %s", e)
            self._initialized = False
            self._state.gpio_initialized = False
            logger.info("Tank controller shut down")
        self._idle_stop.set()

    def _start_idle_watcher(self) -> None:
        """Spawn (idempotent) the thread that releases GPIO after idle."""
        if self._idle_thread is not None and self._idle_thread.is_alive():
            return
        self._idle_stop.clear()

        def _watch():
            while not self._idle_stop.is_set():
                # Sleep in small chunks so shutdown signals respond quickly.
                self._idle_stop.wait(timeout=0.5)
                if not self._initialized:
                    return
                idle = time.time() - self._last_cmd_ts
                if idle > self.IDLE_RELEASE_TIMEOUT_S and not self._state.is_moving:
                    logger.info(
                        "Tank: idle for %.1fs — releasing GPIO so other "
                        "processes can claim the chardev lines",
                        idle,
                    )
                    try:
                        self.shutdown()
                    except Exception as e:
                        logger.debug("Tank idle release failed: %s", e)
                    return

        self._idle_thread = threading.Thread(
            target=_watch, name="tank-idle-watcher", daemon=True,
        )
        self._idle_thread.start()


# ── Singleton ────────────────────────────────────────────────────────

_tank: Optional[TankController] = None
_safety_installed = False


def _install_safety_handlers() -> None:
    """Register atexit + SIGINT/SIGTERM hooks that kill motors on shutdown.

    Pre-Phase-7 the chain was missing — Ctrl-C mid-move would leave the
    H-bridge driven HIGH and the tracks spinning until brown-out. This
    function is idempotent; called from get_tank_controller().
    """
    global _safety_installed
    if _safety_installed:
        return
    _safety_installed = True

    import atexit
    import signal

    def _emergency_cleanup(*_args):
        global _tank
        if _tank is not None and _tank._initialized:
            try:
                _tank._stop_all()
            except Exception:
                pass

    atexit.register(_emergency_cleanup)

    # Chain to previous handlers so we don't break Ctrl-C / SIGTERM behavior.
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            prev = signal.getsignal(sig)
        except (ValueError, OSError):
            continue

        def _make_handler(prev_handler, signal_obj):
            def _h(signum, frame):
                _emergency_cleanup()
                if callable(prev_handler) and prev_handler not in (
                    signal.SIG_DFL, signal.SIG_IGN,
                ):
                    try:
                        prev_handler(signum, frame)
                    except KeyboardInterrupt:
                        raise
                elif prev_handler == signal.SIG_DFL and signum == signal.SIGINT:
                    raise KeyboardInterrupt
            return _h

        try:
            signal.signal(sig, _make_handler(prev, sig))
        except (ValueError, OSError):
            # Not on the main thread — skip (signal.signal only works there).
            pass


def get_tank_controller() -> TankController:
    """Get or create the global TankController singleton."""
    global _tank
    if _tank is None:
        _tank = TankController()
        _install_safety_handlers()
    return _tank
