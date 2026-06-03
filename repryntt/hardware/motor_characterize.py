"""repryntt.hardware.motor_characterize — Tank motor characterization.

What this CAN measure (on jacks):
  - Per-direction command latency (wall-clock vs commanded duration)
  - Left/right command success rate (sonar reflex blocks, GPIO errors)
  - Symmetry: visual confirmation the operator notes ("does it pull left?")

What this CANNOT measure with the current driver:
  - True PWM-vs-RPM curves. tank.py drives PWMA/PWMB as binary GPIO
    HIGH/LOW (see _set_motor_a, _set_motor_b). speed_pct < 1 ⇒ off,
    anything else ⇒ full power. There is no PWM modulation today.
    A real PWM sweep needs Jetson.GPIO.PWM(pin, freq).start(duty)
    integration first.
  - Absolute cm/sec without encoders or a calibrated floor run.

Operator workflow:
  1. Robot on jacks, motors free.
  2. python -m repryntt.hardware.motor_characterize
  3. Watch the four pulses. Note which side spins faster. Enter the
     observed RPM (or "skip") at the prompt.
  4. Later, on the floor, run with --floor and a tape measure. Time how
     far the robot goes in the requested duration.
  5. Calibration JSON is written to ~/.repryntt/data/motor_calibration.json
  6. Run with --suggest to print the constants to update in tank.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CALIB_PATH = Path.home() / ".repryntt" / "data" / "motor_calibration.json"
DEFAULT_DURATION_S = 2.0
DEFAULT_SPEED = 1.0  # bang-bang driver: only 0 or full anyway


@dataclass
class DirectionResult:
    direction: str
    commanded_s: float
    actual_s: float
    success: bool
    error: Optional[str] = None
    note: Optional[str] = None  # operator-entered observation


@dataclass
class CalibrationRecord:
    ts: float
    on_jacks: bool
    duration_s: float
    speed: float
    pwm_modulated: bool   # always False until we add real PWM
    results: List[DirectionResult] = field(default_factory=list)
    floor_distance_cm: Optional[float] = None
    floor_turn_degrees: Optional[float] = None


def _pulse(direction: str, fn, duration: float, speed: float) -> DirectionResult:
    t0 = time.time()
    res = fn(speed, duration)
    actual = time.time() - t0
    if res.get("success"):
        return DirectionResult(direction, duration, actual, True)
    err = res.get("error", "unknown")
    return DirectionResult(direction, duration, actual, False, error=err)


def characterize(
    on_jacks: bool = True,
    duration_s: float = DEFAULT_DURATION_S,
    speed: float = DEFAULT_SPEED,
    pause_s: float = 0.5,
    interactive: bool = True,
) -> CalibrationRecord:
    """Run a four-direction symmetry sweep. Records timing + operator notes."""
    from repryntt.hardware.tank import get_tank_controller

    tank = get_tank_controller()
    if not tank.initialize():
        raise RuntimeError("Tank controller GPIO unavailable — cannot characterize")

    rec = CalibrationRecord(
        ts=time.time(),
        on_jacks=on_jacks,
        duration_s=duration_s,
        speed=speed,
        pwm_modulated=False,
    )

    print(f"=== Motor characterization ({'JACKS' if on_jacks else 'FLOOR'}) ===")
    print(f"Pulse duration: {duration_s}s, commanded speed: {speed}")
    print("Driver is bang-bang HIGH/LOW — speed knob is informational only.\n")

    fns = [
        ("forward", tank.move_forward),
        ("backward", tank.move_backward),
        ("turn_left", tank.turn_left),
        ("turn_right", tank.turn_right),
    ]
    for name, fn in fns:
        if interactive:
            input(f"[ENTER] to pulse {name}: ")
        else:
            print(f"  → {name}")
        result = _pulse(name, fn, duration_s, speed)
        if interactive and result.success:
            note = input(f"    observation for {name} (RPM, drift, anything; or ENTER to skip): ").strip()
            if note:
                result.note = note
        rec.results.append(result)
        time.sleep(pause_s)

    tank.stop()

    if not on_jacks and interactive:
        try:
            d = input("Measured forward travel in cm during the forward pulse (ENTER to skip): ").strip()
            if d:
                rec.floor_distance_cm = float(d)
        except ValueError:
            pass
        try:
            deg = input("Measured turn_left rotation in degrees (ENTER to skip): ").strip()
            if deg:
                rec.floor_turn_degrees = float(deg)
        except ValueError:
            pass

    return rec


def save_calibration(rec: CalibrationRecord, path: Path = CALIB_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "ts": rec.ts,
        "on_jacks": rec.on_jacks,
        "duration_s": rec.duration_s,
        "speed": rec.speed,
        "pwm_modulated": rec.pwm_modulated,
        "floor_distance_cm": rec.floor_distance_cm,
        "floor_turn_degrees": rec.floor_turn_degrees,
        "results": [asdict(r) for r in rec.results],
    }
    with path.open("w") as f:
        json.dump(data, f, indent=2)
    print(f"\n✓ Calibration written to {path}")


def suggest_constants(path: Path = CALIB_PATH) -> Dict[str, Any]:
    """Read calibration JSON and print suggested tank.py constant overrides."""
    if not path.exists():
        print(f"No calibration at {path} — run without --suggest first.")
        return {}

    data = json.loads(path.read_text())
    suggestions: Dict[str, Any] = {}

    if data.get("on_jacks"):
        print("Calibration was on JACKS — only symmetry/timing, no absolute cm/s.")
        print("Re-run with --floor + tape measure to refine CM_PER_SEC_AT_FULL.")
    else:
        d_cm = data.get("floor_distance_cm")
        dur = data.get("duration_s", 0) or 0
        if d_cm and dur > 0:
            suggestions["CM_PER_SEC_AT_FULL"] = round(d_cm / dur, 1)
        deg = data.get("floor_turn_degrees")
        if deg and dur > 0:
            suggestions["DEG_PER_SEC_AT_FULL"] = round(abs(deg) / dur, 1)

    failures = [r for r in data.get("results", []) if not r.get("success")]
    if failures:
        print("\n⚠ Failed pulses (investigate before trusting calibration):")
        for r in failures:
            print(f"  - {r['direction']}: {r.get('error')}")

    if suggestions:
        print("\nSuggested tank.py overrides (edit lines 391-392 manually):")
        for k, v in suggestions.items():
            print(f"  {k} = {v}")
    else:
        print("\nNo new constants to suggest from this calibration.")
    return suggestions


def main() -> int:
    ap = argparse.ArgumentParser(description="Andrew motor characterization.")
    ap.add_argument("--floor", action="store_true",
                    help="Robot is on the floor (will measure absolute travel)")
    ap.add_argument("--duration", type=float, default=DEFAULT_DURATION_S,
                    help="Pulse duration per direction (default 2.0s)")
    ap.add_argument("--non-interactive", action="store_true",
                    help="Skip operator prompts (for automated runs)")
    ap.add_argument("--suggest", action="store_true",
                    help="Read latest calibration and print suggested constants")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING)

    if args.suggest:
        suggest_constants()
        return 0

    rec = characterize(
        on_jacks=not args.floor,
        duration_s=args.duration,
        interactive=not args.non_interactive,
    )
    save_calibration(rec)
    suggest_constants()
    return 0 if all(r.success for r in rec.results) else 1


if __name__ == "__main__":
    sys.exit(main())
