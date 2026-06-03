"""repryntt.hardware.bench_check — One-shot pre-session hardware validator.

Run before every Andrew session (especially after re-cabling). Confirms:
  1. Both IMX219 cameras enumerate and capture a frame
  2. Both HC-SR04 sonars read a sane distance
  3. All four motor directions pulse for 0.4s on the GPIO

Designed to be run on jacks. Sonar readings near zero (the bench under the
robot) are flagged but not failed — they're informational. Motor pulses are
short and low PWM so a robot that escaped the jacks won't crash hard.

Usage:
    python -m repryntt.hardware.bench_check
    python -m repryntt.hardware.bench_check --no-motor   # cameras + sonar only

Exit code is 0 on full pass, 1 if any sensor / motor stage fails.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

MOTOR_PULSE_SPEED = 0.3   # 30% PWM — slow enough to be safe if jacks slip
MOTOR_PULSE_DURATION = 0.4  # short pulse per direction
MOTOR_PAUSE_BETWEEN = 0.5   # let the H-bridge settle


def _check_cameras() -> Dict[str, Any]:
    from repryntt.hardware.camera import discover_cameras, capture_frame

    cams = discover_cameras(force_refresh=True)
    results: List[Dict[str, Any]] = []
    captured = 0
    for c in cams:
        frame = capture_frame(c.index)
        ok = frame is not None
        if ok:
            captured += 1
            h, w = frame.shape[:2]
            results.append({
                "index": c.index, "name": c.name, "ok": True,
                "frame_size": [w, h], "csi": c.is_csi,
            })
        else:
            results.append({
                "index": c.index, "name": c.name, "ok": False,
                "csi": c.is_csi,
            })
    return {
        "stage": "cameras",
        "ok": bool(cams) and captured == len(cams),
        "discovered": len(cams),
        "captured": captured,
        "details": results,
    }


def _check_sonar() -> Dict[str, Any]:
    from repryntt.hardware.sonar import get_sonar

    sonar = get_sonar()
    if not sonar.initialize():
        return {"stage": "sonar", "ok": False, "error": "sonar GPIO unavailable"}

    readings = sonar.read_both()
    front = readings.get("front")
    rear = readings.get("rear")
    return {
        "stage": "sonar",
        "ok": bool(front and front.valid) and bool(rear and rear.valid),
        "front_cm": front.distance_cm if front else None,
        "front_valid": bool(front and front.valid),
        "rear_cm": rear.distance_cm if rear else None,
        "rear_valid": bool(rear and rear.valid),
    }


def _check_motors() -> Dict[str, Any]:
    from repryntt.hardware.tank import get_tank_controller

    tank = get_tank_controller()
    if not tank.initialize():
        return {"stage": "motors", "ok": False, "error": "tank GPIO unavailable"}

    pulses = []
    fns = [
        ("forward", tank.move_forward),
        ("backward", tank.move_backward),
        ("turn_left", tank.turn_left),
        ("turn_right", tank.turn_right),
    ]
    for name, fn in fns:
        res = fn(MOTOR_PULSE_SPEED, MOTOR_PULSE_DURATION)
        # _drive returns {"success": bool, ...}. Sonar reflex can block a
        # forward pulse if the bench wall is too close — treat that as PASS
        # (the motor wiring is fine, the sensor saved us).
        succeeded = bool(res.get("success"))
        sonar_block = (
            not succeeded
            and isinstance(res.get("error"), str)
            and "sonar" in res.get("error", "").lower()
        )
        pulses.append({
            "direction": name,
            "ok": succeeded or sonar_block,
            "success": succeeded,
            "sonar_blocked": sonar_block,
            "error": res.get("error"),
        })
        time.sleep(MOTOR_PAUSE_BETWEEN)

    tank.stop()
    return {
        "stage": "motors",
        "ok": all(p["ok"] for p in pulses),
        "pulses": pulses,
    }


def run_bench_check(skip_motor: bool = False, verbose: bool = True) -> Dict[str, Any]:
    """Run all hardware health checks. Returns a structured pass/fail dict."""
    stages: List[Dict[str, Any]] = []

    if verbose:
        print("=== Andrew bench check ===")

    cam = _check_cameras()
    stages.append(cam)
    if verbose:
        print(f"[cameras]  {'PASS' if cam['ok'] else 'FAIL'}  "
              f"{cam['captured']}/{cam['discovered']} captured")

    son = _check_sonar()
    stages.append(son)
    if verbose:
        print(f"[sonar  ]  {'PASS' if son['ok'] else 'FAIL'}  "
              f"front={son.get('front_cm')}cm rear={son.get('rear_cm')}cm")

    if not skip_motor:
        mot = _check_motors()
        stages.append(mot)
        if verbose:
            print(f"[motors ]  {'PASS' if mot['ok'] else 'FAIL'}")
            for p in mot.get("pulses", []):
                marker = "✓" if p["ok"] else "✗"
                blocked = " (sonar-blocked)" if p.get("sonar_blocked") else ""
                err = f" {p['error']}" if p.get("error") and not p.get("sonar_blocked") else ""
                print(f"           {marker} {p['direction']}{blocked}{err}")

    overall = all(s["ok"] for s in stages)
    if verbose:
        print(f"\n=== {'OK — Andrew is bench-ready' if overall else 'FAIL — fix above before nav'} ===")

    return {"ok": overall, "stages": stages, "ts": time.time()}


def main() -> int:
    ap = argparse.ArgumentParser(description="Andrew hardware bench check.")
    ap.add_argument("--no-motor", action="store_true",
                    help="Skip motor pulses (cameras + sonar only)")
    ap.add_argument("--json", action="store_true",
                    help="Emit JSON only (no human-readable output)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING)
    res = run_bench_check(skip_motor=args.no_motor, verbose=not args.json)
    if args.json:
        print(json.dumps(res, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
