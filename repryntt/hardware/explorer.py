"""
repryntt.hardware.explorer — Autonomous Exploration Daemon.

This is the missing "Layer 2" — a behavioral loop that runs INDEPENDENTLY
of the LLM heartbeat. Tesla/BD have continuous sensorimotor loops; Andrew
had nothing between heartbeats. This fixes that.

Architecture:
    Layer 3 (LLM) → sets goals: "explore hallway", "go to living room"
    Layer 2 (THIS) → autonomous see→move→see loop, follows goals
    Layer 1 (tank.py) → raw motor GPIO control

The LLM doesn't micromanage each step — it sets exploration goals and
reads back what the explorer found. The explorer handles:
    - Continuous capture → perceive → decide → move cycles
    - Obstacle avoidance (reactive, no LLM needed)
    - Frontier-chasing (go toward unexplored directions)
    - Spatial map updates (automatic)
    - Safety: stop if stuck, stop if obstacle too close, stop on timeout

Usage:
    explorer = get_explorer()
    explorer.start("explore freely")   # LLM says "go explore"
    ...                                 # runs autonomously in background
    status = explorer.status()          # LLM checks what happened
    explorer.stop()                     # LLM says "stop"
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Exploration parameters
DEFAULT_SPEED = 0.3          # conservative speed (0-1)
DEFAULT_STEP_DURATION = 0.5  # seconds per movement
PAUSE_BETWEEN_STEPS = 1.5   # let camera settle after movement
MAX_STEPS_PER_RUN = 100000   # no artificial cap — user/Andrew decides
STUCK_THRESHOLD = 5           # stop actions in a row = stuck
CYCLE_TIME_LIMIT = 0          # 0 = no time limit (steps are the constraint)
MIN_CLEARANCE = 0.7           # center obstacle > this = too close, turn
SNAPSHOT_INTERVAL = 5         # save a visual snapshot every N steps
JOURNAL_INTERVAL = 10         # write exploration memory every N steps
MAX_AUTO_CONTINUES = 3        # how many times robotics_nudge may chain runs


@dataclass
class ExplorerState:
    """Current state of the autonomous explorer."""
    running: bool = False
    goal: str = ""
    steps_taken: int = 0
    steps_limit: int = MAX_STEPS_PER_RUN
    started_at: float = 0.0
    stopped_at: float = 0.0
    stop_reason: str = ""
    places_discovered: int = 0
    distance_cm: float = 0.0
    last_scene: str = ""
    last_action: str = ""
    last_action_reason: str = ""
    last_vlm_direction: str = ""
    last_vlm_confidence: float = 0.0
    last_image_path: str = ""
    consecutive_stops: int = 0
    snapshots_saved: int = 0
    errors: List[str] = None
    journal: List[str] = None
    vision_feed: List[Dict[str, Any]] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []
        if self.journal is None:
            self.journal = []
        if self.vision_feed is None:
            self.vision_feed = []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "goal": self.goal,
            "steps_taken": self.steps_taken,
            "steps_limit": self.steps_limit,
            "elapsed_sec": round(time.time() - self.started_at, 1) if self.running else round(self.stopped_at - self.started_at, 1),
            "stop_reason": self.stop_reason,
            "places_discovered": self.places_discovered,
            "distance_cm": round(self.distance_cm, 1),
            "last_scene": self.last_scene[:200],
            "last_action": self.last_action,
            "last_action_reason": self.last_action_reason,
            "last_vlm_direction": self.last_vlm_direction,
            "last_vlm_confidence": self.last_vlm_confidence,
            "snapshots_saved": self.snapshots_saved,
            "journal": self.journal,
            "errors": self.errors[-5:],
        }


class Explorer:
    """Autonomous exploration daemon — Layer 2 behavioral control.

    Architecture mirrors biological vision-motor systems:
        Brain (Andrew/LLM) → sets goals AND steers via conscious intent
        Visual cortex (VLM) → processes raw images into scene understanding
        Motor cortex (this) → executes movement with obstacle avoidance
        Body (tank.py) → raw GPIO motor control

    The brain sees what the visual cortex sees (live vision feed) and can
    set conscious intent ("go left, I see a doorway") at any time. The
    motor layer follows the brain's intent, only overriding for safety
    (obstacle avoidance). When no intent is set, the VLM handles direction.

    Loop:
    1. Capture camera frame
    2. VLM analyzes scene (visual cortex)
    3. Check Andrew's conscious intent (brain override)
    4. If intent set and path safe → follow intent
    5. If no intent → follow VLM suggestion / reactive policy
    6. Safety checks override everything
    7. Execute motor command, update spatial map
    8. Push frame to live vision feed (so brain sees it next heartbeat)
    """

    def __init__(self):
        self._state = ExplorerState()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        # Robotics nudge chain counter — reset when a user/framework
        # kick starts a brand-new session, incremented on auto-continue.
        self._auto_continue_count = 0
        # Conscious navigation intent — set by Andrew (the brain) to tell
        # the body WHERE to go. The VLM/reactive layer handles obstacle
        # avoidance, but Andrew's intent is the #1 directional priority.
        # Format: {"direction": "left"|"right"|"forward"|"backward",
        #          "reason": "I see a doorway to the left",
        #          "set_at": timestamp, "expires_steps": N}
        self._conscious_intent: Optional[Dict[str, Any]] = None
        self._intent_lock = threading.Lock()

    def start(self, goal: str = "explore freely",
              steps: int = MAX_STEPS_PER_RUN,
              speed: float = DEFAULT_SPEED,
              _auto_continue: bool = False) -> Dict[str, Any]:
        """Start autonomous exploration in background thread.
        
        Args:
            goal: What to explore ("explore freely", "find the hallway", etc.)
            steps: Max steps before auto-stop.
            speed: Motor speed 0-1 (0.3 = cautious, 0.5 = normal).
            
        Returns:
            Status dict confirming start or error.
        """
        with self._lock:
            if self._state.running:
                return {"error": "Already exploring", "state": self._state.to_dict()}

            # Reset auto-continue counter on fresh user/framework calls only
            if not _auto_continue:
                self._auto_continue_count = 0

            self._stop_event.clear()
            self._state = ExplorerState(
                running=True,
                goal=goal,
                steps_limit=steps,
                started_at=time.time(),
            )

            self._thread = threading.Thread(
                target=self._explore_loop,
                args=(speed,),
                name="explorer-daemon",
                daemon=True,
            )
            self._thread.start()
            _tag = " [auto-continue]" if _auto_continue else ""
            logger.info(f"🧭 Explorer started{_tag}: goal='{goal}', max_steps={steps}, speed={speed}")
            return {"started": True, "goal": goal, "state": self._state.to_dict()}

    def set_intent(self, direction: str, reason: str = "",
                   duration_steps: int = 20) -> Dict[str, Any]:
        """Set conscious navigation intent — Andrew's brain telling the body where to go.

        The explorer loop will prioritize this direction over VLM suggestions
        (unless safety requires an override). This is the brain→body pathway:
        Andrew sees through the VLM, decides where to go, and the body executes.

        Args:
            direction: "left", "right", "forward", "backward", or a natural
                       description like "toward the doorway", "away from the wall"
            reason: Why Andrew wants to go this way (for logging/memory)
            duration_steps: How many steps to maintain this intent before
                           it expires and the VLM takes over again (default 20)

        Returns:
            Confirmation dict with the active intent.
        """
        dir_map = {
            "left": "turn_left", "turn_left": "turn_left",
            "right": "turn_right", "turn_right": "turn_right",
            "forward": "forward", "ahead": "forward", "straight": "forward",
            "backward": "backward", "back": "backward", "reverse": "backward",
        }
        dir_lower = direction.lower().strip()
        mapped = dir_map.get(dir_lower)
        if not mapped:
            for key, val in dir_map.items():
                if key in dir_lower:
                    mapped = val
                    break
        if not mapped:
            mapped = "forward"

        with self._intent_lock:
            self._conscious_intent = {
                "direction": mapped,
                "raw_direction": direction,
                "reason": reason or f"Andrew chose: {direction}",
                "set_at": time.time(),
                "set_at_step": self._state.steps_taken,
                "expires_steps": duration_steps,
            }
        logger.info(
            f"🧠 Conscious intent set: {mapped} "
            f"(reason: {reason or direction}, duration: {duration_steps} steps)"
        )
        return {
            "intent_set": True,
            "direction": mapped,
            "reason": reason or direction,
            "duration_steps": duration_steps,
            "explorer_running": self._state.running,
            "current_step": self._state.steps_taken,
        }

    def clear_intent(self) -> Dict[str, Any]:
        """Clear conscious intent — let VLM take over directional decisions."""
        with self._intent_lock:
            old = self._conscious_intent
            self._conscious_intent = None
        logger.info("🧠 Conscious intent cleared — VLM resumes directional control")
        return {"cleared": True, "previous_intent": old}

    def get_intent(self) -> Optional[Dict[str, Any]]:
        """Get current conscious intent, or None if expired/unset."""
        with self._intent_lock:
            if self._conscious_intent is None:
                return None
            intent = self._conscious_intent
            steps_since = self._state.steps_taken - intent.get("set_at_step", 0)
            if steps_since >= intent.get("expires_steps", 20):
                self._conscious_intent = None
                return None
            return {**intent, "steps_remaining": intent["expires_steps"] - steps_since}

    def stop(self, reason: str = "manual") -> Dict[str, Any]:
        """Stop the exploration loop."""
        self._stop_event.set()
        with self._lock:
            self._state.stop_reason = reason
            self._state.running = False
            self._state.stopped_at = time.time()
        logger.info(f"🧭 Explorer stopped: reason={reason}, steps={self._state.steps_taken}")
        return self._state.to_dict()

    def status(self) -> Dict[str, Any]:
        """Get current exploration status."""
        with self._lock:
            return self._state.to_dict()

    def get_live_state(self) -> Dict[str, Any]:
        """Return a snapshot of live explorer state for the consciousness dashboard.

        Combines the exploration status with the current conscious intent and
        the latest vision feed entry so the dashboard can render one coherent
        picture of what the robot is seeing, thinking, and doing right now.
        """
        with self._lock:
            state = self._state.to_dict()
            latest_feed = self._state.vision_feed[-1] if self._state.vision_feed else {}
            image_path = self._state.last_image_path

        intent = self.get_intent()

        return {
            "running": state["running"],
            "goal": state["goal"],
            "step": state["steps_taken"],
            "steps_limit": state["steps_limit"],
            "elapsed_sec": state["elapsed_sec"],
            "last_image_path": image_path,
            "reasoning": {
                "scene": latest_feed.get("scene", state.get("last_scene", "")),
                "recommendation": latest_feed.get("direction", state.get("last_vlm_direction", "")),
                "confidence": latest_feed.get("confidence", state.get("last_vlm_confidence", 0.0)),
                "obstacles": latest_feed.get("obstacles", {}),
            },
            "decision": {
                "action": state.get("last_action", ""),
                "reason": state.get("last_action_reason", ""),
            },
            "intent_override": intent,
            "places_discovered": state["places_discovered"],
        }

    def _explore_loop(self, speed: float):
        """Main exploration loop — runs in background thread.

        Vision-first: every step sends the camera image to a VLM
        (Gemini/NVIDIA) which analyzes the scene and recommends a
        direction. The VLM sees the actual image and reasons about
        obstacles, walls, doorways, and paths — no stereo sensor needed.

        Stereo depth is ONLY used as a physical safety check (<15cm).

        Wraps the entire loop in an AUTONOMOUS motor lease so multi-step
        plans aren't interleaved with other clients. Operator teleop can
        preempt at any time — nav_cortex._execute_action returns a
        "preempted_by_higher_priority" result and we exit cleanly.
        """
        from repryntt.hardware.motor_client import (
            DaemonUnavailable, Priority, session as _ms,
        )

        try:
            with _ms(priority=Priority.AUTONOMOUS,
                     holder_label="explorer",
                     wait_timeout_s=10.0):
                self._explore_loop_inner(speed)
        except DaemonUnavailable:
            logger.warning(
                "explorer: motor_daemon unreachable — running without lease "
                "(direct GPIO fallback). Start the daemon for proper "
                "preempt/queue behavior."
            )
            self._explore_loop_inner(speed)

    def _explore_loop_inner(self, speed: float):
        try:
            from repryntt.hardware.nav_cortex import get_nav_cortex
            from repryntt.hardware.spatial_map import get_spatial_map
            from repryntt.hardware.local_perception import get_occupancy_grid
            from repryntt.hardware.social_behavior import get_social_behavior

            cortex = get_nav_cortex()
            smap = get_spatial_map()
            grid = get_occupancy_grid()
            social = get_social_behavior()
            scene = ""

            # Rolling buffer of recent frame paths for multi-frame VLM
            # context. 3 frames = ~30s of history at typical step timing,
            # enough for the VLM to perceive motion and place recognition
            # without blowing the token budget.
            _prior_frames: List[str] = []
            _PRIOR_FRAMES_MAX = 3

            while not self._stop_event.is_set():
                # Safety checks
                elapsed = time.time() - self._state.started_at
                if CYCLE_TIME_LIMIT > 0 and elapsed > CYCLE_TIME_LIMIT:
                    self.stop("time_limit")
                    break
                if self._state.steps_taken >= self._state.steps_limit:
                    self.stop("step_limit")
                    break
                if self._state.consecutive_stops >= STUCK_THRESHOLD:
                    self.stop("stuck")
                    break

                # ── SEE — capture camera frame ──
                image_path = None
                perception = None
                _is_dark = False
                _is_degraded = False
                try:
                    image_path = cortex.capture_frame(camera_id=0)
                    if not image_path:
                        self._state.errors.append("capture_failed")
                        time.sleep(2)
                        continue
                    frame_age_ms = max(0.0, (time.time() - os.path.getmtime(image_path)) * 1000.0)
                    frame_name = os.path.basename(image_path)
                except Exception as e:
                    self._state.errors.append(f"capture: {e}")
                    time.sleep(2)
                    continue

                # ── OPTICAL FLOW — local motion detection between VLM ticks ──
                # Cheap OpenCV Farneback flow (~20-50ms on Jetson). Detects
                # scene change so we can react faster than the VLM can.
                # Runs on the local CPU — no API cost, no model load.
                _flow = None
                try:
                    from repryntt.hardware.optical_flow import get_optical_flow
                    _flow = get_optical_flow().check(image_path)
                    if _flow.significant:
                        logger.info(f"👁️ Optical flow: {_flow.summary()}")
                except Exception as _of_e:
                    logger.debug(f"optical flow check failed: {_of_e}")

                # ── PROPRIOCEPTION — join flow + sonar with commanded motion ──
                # Closes the "did I actually move?" loop without an IMU. If
                # the motor stack issued a command in the last few seconds
                # and the flow magnitude is below the noise floor + sonar
                # didn't shift, the tracker raises a discrepancy that the
                # heartbeat surfaces to the brain.
                try:
                    from repryntt.hardware.proprioception import get_proprioception
                    _sonar_front_cm = None
                    _sonar_rear_cm = None
                    try:
                        from repryntt.hardware.sonar import get_sonar
                        _sonar_both = get_sonar().read_both()
                        _front = _sonar_both.get("front")
                        _rear = _sonar_both.get("rear")
                        if _front and _front.valid:
                            _sonar_front_cm = _front.distance_cm
                        if _rear and _rear.valid:
                            _sonar_rear_cm = _rear.distance_cm
                    except Exception:
                        # Sonar absent or GPIO unavailable — proprioception
                        # still runs with just flow data.
                        pass
                    if _flow is not None and _flow.error is None:
                        _prop_report = get_proprioception().record_observation(
                            flow_magnitude=_flow.mean_magnitude,
                            flow_significant=_flow.significant,
                            flow_dx=_flow.dominant_dx,
                            flow_dy=_flow.dominant_dy,
                            sonar_front_cm=_sonar_front_cm,
                            sonar_rear_cm=_sonar_rear_cm,
                        )
                        if _prop_report and not _prop_report.consistent:
                            logger.warning(
                                f"🧍 Proprioception: {_prop_report.summary}"
                            )
                except Exception as _prop_e:
                    logger.debug(f"proprioception update failed: {_prop_e}")

                # ── PERCEIVE — VLM analyzes the camera image ──
                # The VLM (Gemini/NVIDIA) looks at the actual image and
                # returns: obstacles, path direction, scene description,
                # people detected. This IS the navigation brain.
                #
                # We also pass a spatial_context block (pose, distance
                # travelled, nearest unknown frontier) and up to 3 prior
                # frames, so the VLM can reason about location and motion
                # instead of treating every frame as frame-zero.
                _spatial_ctx = ""
                try:
                    from repryntt.hardware.spatial_context import (
                        build_spatial_context,
                    )
                    _spatial_ctx = build_spatial_context(max_frontiers=3)
                except Exception as _sc_e:
                    logger.debug(f"spatial_context build failed: {_sc_e}")

                try:
                    perception = cortex.perceive(
                        image_path,
                        spatial_context=_spatial_ctx,
                        prior_frames=list(_prior_frames),
                    )
                except Exception as e:
                    logger.warning(f"VLM perception failed: {e}")
                    self._state.errors.append(f"vlm: {e}")

                # Roll frame buffer after perceive so the current frame
                # becomes prior context for the NEXT tick.
                _prior_frames.append(image_path)
                if len(_prior_frames) > _PRIOR_FRAMES_MAX:
                    _prior_frames.pop(0)

                if perception is None:
                    perception = cortex._fallback_perception("")

                obstacles = perception.get("obstacles", {})
                best_dir = perception.get("path", {}).get("best_direction", "stop")
                confidence = float(perception.get("path", {}).get("confidence", 0))
                scene = perception.get("scene", "")
                reason = perception.get("path", {}).get("reason", "")
                self._state.last_scene = scene
                self._state.last_vlm_direction = best_dir
                self._state.last_vlm_confidence = confidence
                if image_path:
                    self._state.last_image_path = image_path

                # Push to live vision feed (rolling buffer, last 5 frames)
                # This is how Andrew's heartbeat sees what the VLM sees
                # in real-time — the visual cortex → brain pathway.
                self._state.vision_feed.append({
                    "step": self._state.steps_taken + 1,
                    "time": time.strftime("%H:%M:%S"),
                    "scene": scene[:300] if scene else "",
                    "direction": best_dir,
                    "confidence": round(confidence, 2),
                    "obstacles": obstacles,
                })
                if len(self._state.vision_feed) > 5:
                    self._state.vision_feed.pop(0)

                # Detect dark/degraded from VLM scene description
                _scene_lower = scene.lower() if scene else ""
                _dark_keywords = ("dark", "black", "no light",
                                  "no visible", "covered", "obstruct",
                                  "no visual", "insufficient light")
                _is_dark = any(kw in _scene_lower for kw in _dark_keywords)

                _blur_keywords = ("blurry", "pixelated", "out of focus",
                                  "out-of-focus", "no discernible",
                                  "speckled", "completely gray",
                                  "heavily pixelated", "unrecognizable")
                # Require TWO keywords to trigger — single-keyword triggers
                # fire on outdoor scenes ("speckled concrete", "gray wall")
                # and cause spurious backup-only loops. Operator saw this
                # 2026-04-23: pool-paint bucket scene flagged degraded.
                _blur_hits = sum(1 for kw in _blur_keywords if kw in _scene_lower)
                _is_degraded = _blur_hits >= 2

                if _is_dark:
                    logger.info("🌑 Dark scene detected — switching to wander mode")
                    confidence = 0.1
                    best_dir = "turn_left" if self._state.steps_taken % 3 != 0 else "forward"

                if _is_degraded and not _is_dark:
                    logger.info("📷 Degraded image — camera may be obstructed, backing up")
                    confidence = 0.1
                    best_dir = "backward" if self._state.steps_taken % 2 == 0 else "turn_left"

                # Record frontiers from what the VLM sees
                if reason and not _is_dark and not _is_degraded:
                    try:
                        smap._add_frontier(best_dir, f"Vision: {reason}")
                    except Exception:
                        pass

                # ── DECIDE — VLM direction is primary ──
                # The VLM has already analyzed the image and recommended
                # a direction. Use that directly via reactive rules.
                action, action_reason = self._reactive_decide(
                    obstacles, best_dir, confidence)

                # Hard override: degraded image MUST back up
                if _is_degraded and action == "forward":
                    action = "backward"
                    action_reason = "degraded_image_override"

                # ── OPTICAL FLOW — reactive awareness between VLM ticks ──
                # If lots of motion is detected AND we're planning to move
                # forward, pause for one tick so the VLM can recheck next
                # iteration. Something unexpected entered the scene.
                #
                # CRITICAL: optical flow cannot distinguish ego-motion
                # ("I moved → world looked like it moved") from world-motion
                # ("something moved past me"). Without this gate, every
                # successful forward command made the next step pause
                # itself — that's why the robot only travelled ~3 ft in
                # 30 steps. Skip the pause when we just commanded motion
                # OR when the tank reports it moved within the last ~1s.
                _ego_moving_recently = False
                try:
                    from repryntt.hardware.motor_client import daemon_status
                    _body = (daemon_status(require_daemon=True).get("body") or {}).get("body") or {}
                    _last_time = float(_body.get("last_command_time") or 0.0)
                    _last_command = str(_body.get("last_command") or "")
                    if _last_time and (time.time() - _last_time) < 1.5:
                        if _last_command in (
                            "forward", "backward", "turn_left", "turn_right",
                        ) or _last_command.startswith((
                            "move_forward_", "move_backward_",
                            "turn_left_", "turn_right_",
                            "spin_left_", "spin_right_",
                        )):
                            _ego_moving_recently = True
                except Exception:
                    pass

                if (_flow is not None and _flow.significant
                        and action == "forward"
                        and not _ego_moving_recently):
                    # Very high motion ratio = something moving fast across
                    # the frame. Stop this step, let next VLM tick decide.
                    # Threshold raised from 0.25 → 0.55 because indoor
                    # scenes with even mild ego-drift hit 0.4–0.6 routinely.
                    if _flow.motion_pixel_ratio > 0.55:
                        action = "stop"
                        action_reason = (
                            f"flow_pause (ratio={_flow.motion_pixel_ratio:.0%}, "
                            f"mag={_flow.mean_magnitude:.1f}px)"
                        )
                        logger.info(
                            f"👁️ Motion override: pausing forward motion "
                            f"({_flow.summary()})"
                        )

                # ── DEPTH ANYTHING v2 — relative monocular depth ──────
                # Wired in alongside the VLM perception. Gives us per-zone
                # proximity numbers that don't depend on a cloud call and
                # don't rely on the VLM correctly parsing the scene. When
                # the depth model is unavailable (no torch, no GPU, model
                # download failed) this section silently falls through —
                # explorer keeps working off the VLM perception alone.
                _depth_info: Dict[str, Any] = {}
                try:
                    from repryntt.hardware.depth_perception import get_depth_estimator
                    _de = get_depth_estimator()
                    if _de.available:
                        _depth_result = _de.estimate_depth_from_file(image_path)
                        if _depth_result is not None:
                            _bz = _depth_result.bottom_zone_proximity
                            _depth_info = {
                                "zone_proximity": _depth_result.zone_proximity,
                                "bottom_zone_proximity": _bz,
                                "clearest_direction": _depth_result.min_proximity_zone,
                                "inference_ms": _depth_result.inference_ms,
                            }
                            # Surface the depth verdict into the perception
                            # dict so the VLM-driven nav logic and the
                            # heartbeat prompt both see it.
                            try:
                                perception.setdefault("depth", {}).update(_depth_info)
                            except Exception:
                                pass
                            logger.debug(
                                f"🔭 Depth zones (bottom): "
                                f"L={_bz['left']:.2f} C={_bz['center']:.2f} R={_bz['right']:.2f} "
                                f"clearest={_depth_info['clearest_direction']}"
                            )
                except Exception as _de_e:
                    logger.debug(f"depth estimation skipped: {_de_e}")

                # ── SAFETY CHECK — sonar provides collision avoidance ──
                # SmolVLM obstacle scores (left/center/right 0-1) plus the
                # depth model above already drive navigation decisions.
                # Hardware sonar handles the last-cm emergency stops.

                # ── PERSON DETECTION — SmolVLM already detects people ──
                # The VLM perception dict includes people_detected, so we
                # use that directly instead of loading a separate YOLO model.
                _people_info = perception.get("people", {})

                # Anti-stuck: if oscillating, pick the least-used action
                try:
                    from repryntt.hardware.nav_cortex import NavObservation, StereoDepth as _SD
                    if cortex.spatial_memory.is_stuck():
                        action_id = cortex.spatial_memory.least_used_action()
                        action = ["forward", "backward", "turn_left", "turn_right", "stop"][action_id]
                        action_reason = "anti-stuck"
                        logger.info(f"🔄 Anti-stuck override → {action}")
                except Exception:
                    pass

                self._state.last_action = action
                self._state.last_action_reason = action_reason

                # ── SOCIAL BEHAVIOR — person detection & interaction ──
                if perception and scene and not _is_dark and not _is_degraded:
                    try:
                        robot_loc = {"x": smap.x, "y": smap.y}
                        # Use VLM-reported people or YOLO-detected people
                        social_result = social.process_scene(
                            scene, perception,
                            stereo_distance_cm=None,
                            robot_location=robot_loc,
                        )
                        social_action = social_result.get("action", "continue")

                        if social_action != "continue":
                            override = social_result.get("motor_override")
                            speak_text = social_result.get("speak_text")
                            social_reason = social_result.get("reason", "")

                            logger.info(f"🤝 Social override: {social_action} "
                                        f"(motor={override}, reason={social_reason})")

                            if override:
                                action = override
                                action_reason = f"social: {social_reason}"

                            if speak_text:
                                try:
                                    social.speak(speak_text)
                                except Exception as e:
                                    logger.debug(f"Social speech failed: {e}")

                            if social_action in ("engage", "hold"):
                                action = "stop"
                                action_reason = f"social: {social_reason}"
                                self._state.journal.append(
                                    f"Step {self._state.steps_taken + 1}: "
                                    f"🤝 SOCIAL — {social_action}: {social_reason}. "
                                    f"Scene: {scene[:100]}")
                    except Exception as e:
                        logger.debug(f"Social behavior error: {e}")

                # Record to spatial memory for anti-stuck tracking
                try:
                    from repryntt.hardware.nav_cortex import NavObservation
                    nav_obs = NavObservation(
                        timestamp=time.time(),
                        action_taken=["forward", "backward", "turn_left", "turn_right", "stop"].index(action),
                        obstacles=obstacles,
                        best_direction=best_dir,
                        confidence=confidence,
                        scene=scene,
                        sensor_vector=cortex.perception_to_obs(perception, heading_deg=smap.heading),
                    )
                    cortex.spatial_memory.add(nav_obs)
                except Exception:
                    pass

                step_num_preview = self._state.steps_taken + 1
                scene_short = (scene[:80] + "...") if scene and len(scene) > 80 else (scene or "no scene")
                # VLM-vs-executed audit: if a safety/social override flipped
                # the VLM's call, surface it plainly so brain-vs-eyes
                # disagreements are debuggable from the log.
                _vlm_flipped = (best_dir and best_dir != action
                                and action_reason and
                                ("safety" in action_reason or "social" in action_reason
                                 or "anti-stuck" in action_reason
                                 or "override" in action_reason))
                _vlm_tag = (
                    f" vlm_said={best_dir}@{confidence:.1f} (FLIPPED→{action})"
                    if _vlm_flipped
                    else f" vlm={best_dir}@{confidence:.1f}"
                )
                logger.info(f"🧭 Step {step_num_preview}/{self._state.steps_limit}: "
                            f"action={action} ({action_reason}),{_vlm_tag} "
                            f"frame={frame_name} age={frame_age_ms:.0f}ms, "
                            f"scene={scene_short}")

                # ── ACT ──
                if action != "stop":
                    try:
                        motor_result = cortex._execute_action(
                            ["forward", "backward", "turn_left", "turn_right", "stop"].index(action),
                            speed,
                            DEFAULT_STEP_DURATION,
                        )
                        executed = motor_result.get("success", False)
                        if executed:
                            smap.record_move(action, speed, DEFAULT_STEP_DURATION,
                                             scene=scene, obstacles=obstacles)
                            self._state.distance_cm += speed * DEFAULT_STEP_DURATION * 50
                            self._state.consecutive_stops = 0
                        else:
                            self._state.errors.append(f"motor: {motor_result}")
                    except Exception as e:
                        self._state.errors.append(f"motor: {e}")
                else:
                    self._state.consecutive_stops += 1

                # ── RECORD ──
                try:
                    _scene_type = perception.get("scene_type", "")
                    smap.record_observation(
                        scene, obstacles=obstacles, best_direction=best_dir,
                        scene_type=_scene_type)
                    self._state.places_discovered = len(smap.places)

                    # Record notable landmarks from scene description
                    if _scene_type in ("doorway", "stairs", "elevator"):
                        smap.record_landmark(
                            scene[:100], landmark_type=_scene_type)

                    # Tick the nav planner — auto-advance multi-step plans
                    try:
                        from repryntt.hardware.nav_planner import get_nav_planner
                        _planner = get_nav_planner()
                        if _planner.active:
                            _plan_intent = _planner.tick(
                                smap.x, smap.y,
                                scene_type=_scene_type,
                                scene_desc=scene)
                            if _plan_intent:
                                with self._intent_lock:
                                    self._conscious_intent = {
                                        "direction": _plan_intent["direction"],
                                        "reason": _plan_intent["reason"],
                                        "set_at": time.time(),
                                        "set_at_step": self._state.steps_taken,
                                        "expires_steps": 999,
                                        "source": "nav_planner",
                                    }
                    except Exception as _pe:
                        logger.debug(f"Nav planner tick error: {_pe}")
                except Exception:
                    pass

                self._state.steps_taken += 1
                step_num = self._state.steps_taken

                # ── VISUAL SNAPSHOT — save image for Andrew's memory ──
                if step_num % SNAPSHOT_INTERVAL == 0:
                    try:
                        snap_path = cortex.capture_frame(camera_id=0)
                        if snap_path:
                            self._state.snapshots_saved += 1
                            # Get a scene description for this snapshot
                            if not scene:
                                try:
                                    p = cortex.perceive(snap_path)
                                    scene = p.get("scene", "")
                                    self._state.last_scene = scene
                                except Exception:
                                    pass
                    except Exception:
                        pass

                # ── JOURNAL ENTRY — accumulate what Andrew saw ──
                if scene and (step_num % JOURNAL_INTERVAL == 0 or step_num == 1):
                    entry = (f"Step {step_num}: At ({smap.x:.0f},{smap.y:.0f}cm), "
                             f"facing {smap.heading:.0f}°, action={action}. "
                             f"Saw: {scene[:150]}")
                    self._state.journal.append(entry)

                # ── PAUSE ──
                # VLM calls take 2-5s, so shorter pause is fine.
                # The VLM response time IS the pacing mechanism.
                if not self._stop_event.is_set():
                    self._stop_event.wait(0.5)

        except Exception as e:
            logger.error(f"Explorer loop crashed: {e}", exc_info=True)
            self._state.errors.append(f"crash: {e}")
        finally:
            with self._lock:
                if self._state.running:
                    self._state.running = False
                    self._state.stopped_at = time.time()
                    if not self._state.stop_reason:
                        self._state.stop_reason = "loop_ended"
            # Make sure motors stop through the daemon; direct TankController
            # access here can re-claim GPIO lines and trigger EBUSY.
            try:
                from repryntt.hardware.motor_client import Priority, session as motor_session
                with motor_session(
                    priority=Priority.SAFETY,
                    holder_label="explorer_cleanup",
                    wait_timeout_s=1.0,
                    require_daemon=True,
                ) as sess:
                    sess.stop()
            except Exception:
                pass
            # Reset optical flow cache so the next run starts clean
            try:
                from repryntt.hardware.optical_flow import get_optical_flow
                get_optical_flow().reset()
            except Exception:
                pass
            # Reset social behavior FSM when exploration ends
            try:
                social.reset()
                # Append social interaction log to journal
                interactions = social.get_interaction_log()
                if interactions:
                    self._state.journal.append(
                        f"\n🤝 Social encounters during exploration: "
                        f"{len(interactions)}")
                    for ix in interactions:
                        self._state.journal.append(
                            f"  - {ix['time']}: {ix['outcome']} "
                            f"({ix['duration_sec']}s, {ix['approach_steps']} approach steps) "
                            f"— said: \"{ix['greeting'][:60]}\" "
                            f"— saw: {ix['person'][:80]}")
            except Exception as e:
                logger.debug(f"Social reset/log failed: {e}")
            # ── FLUSH JOURNAL TO MEMORY — so Andrew remembers the run ──
            self._flush_journal_to_memory()
            logger.info(f"🧭 Explorer loop done: {self._state.steps_taken} steps, "
                        f"{self._state.snapshots_saved} snapshots, "
                        f"reason={self._state.stop_reason}")

            # ── ROBOTICS AUTO-CONTINUE ──
            # If we hit step_limit but an embodied_explore framework is still
            # asking for more movement, kick off another run with a fresh
            # frontier goal so a 5-step nibble doesn't end the session.
            try:
                from repryntt.hardware.robotics_nudge import should_auto_continue_explorer
                if (should_auto_continue_explorer(self._state.stop_reason)
                        and self._auto_continue_count < MAX_AUTO_CONTINUES):
                    self._auto_continue_count += 1
                    next_goal = self._state.goal or "continue exploring open frontiers"
                    logger.info(
                        f"🦾 Auto-continuing exploration ({self._auto_continue_count}/{MAX_AUTO_CONTINUES}) — "
                        f"framework still needs movement "
                        f"(prev reason={self._state.stop_reason}, prev steps={self._state.steps_taken})"
                    )
                    time.sleep(2.0)  # brief rest so threads/camera settle

                    _continue_steps = max(self._state.steps_limit, 50)
                    def _kick(_g=next_goal, _s=speed, _st=_continue_steps):
                        try:
                            self.start(goal=_g, steps=_st, speed=_s,
                                       _auto_continue=True)
                        except Exception as _e:
                            logger.warning(f"Auto-continue start failed: {_e}")
                    threading.Thread(target=_kick, name="explorer-auto-continue",
                                     daemon=True).start()
                elif self._auto_continue_count >= MAX_AUTO_CONTINUES:
                    logger.info(
                        f"🦾 Auto-continue cap reached ({MAX_AUTO_CONTINUES}) — "
                        f"Andrew must call nav_explore himself next time"
                    )
            except Exception as _ac_err:
                logger.debug(f"Auto-continue check failed (non-fatal): {_ac_err}")

    def _flush_journal_to_memory(self):
        """Write exploration journal to Andrew's daily memory bank.
        
        Called when exploration stops. This is how Andrew REMEMBERS what
        he saw — the journal entries become part of his persistent memory
        that he reads in future heartbeats.
        """
        if not self._state.journal:
            return
        try:
            from repryntt.hardware.spatial_map import get_spatial_map
            smap = get_spatial_map()

            memory_dir = Path.home() / ".repryntt" / "brain" / "bootstrap"
            memory_dir.mkdir(parents=True, exist_ok=True)

            # Write to daily memory file (same format Andrew's append_daily_memory uses)
            agent_mem_dir = (Path.home() / ".repryntt" / "workspace" /
                            "agents" / "operator" / "memory")
            agent_mem_dir.mkdir(parents=True, exist_ok=True)
            mem_file = agent_mem_dir / f"{time.strftime('%Y-%m-%d')}.md"

            journal_text = (
                f"\n\n## 🧭 Exploration Run ({time.strftime('%H:%M')})\n"
                f"**Goal:** {self._state.goal}\n"
                f"**Result:** {self._state.steps_taken} steps, "
                f"{self._state.distance_cm:.0f}cm traveled, "
                f"{self._state.places_discovered} places, "
                f"{self._state.snapshots_saved} photos saved\n"
                f"**Stopped:** {self._state.stop_reason}\n\n"
                f"### What I Saw:\n"
            )
            for entry in self._state.journal:
                journal_text += f"- {entry}\n"

            # Add spatial map summary
            journal_text += f"\n### Spatial Map State:\n{smap.get_exploration_context()}\n"

            # Also note where snapshots are saved
            snap_dir = (Path.home() / ".repryntt" / "data" / "sensory" /
                        "vision" / time.strftime("%Y-%m-%d"))
            journal_text += f"\n*Visual snapshots saved to: {snap_dir}*\n"

            with open(mem_file, "a") as f:
                f.write(journal_text)

            logger.info(f"📝 Exploration journal written: {len(self._state.journal)} entries "
                        f"→ {mem_file.name}")
        except Exception as e:
            logger.warning(f"Failed to flush exploration journal: {e}")

    def _reactive_decide(self, obstacles: Dict, best_dir: str,
                         confidence: float) -> tuple:
        """Reactive decision with conscious intent priority.

        Priority order:
        1. SAFETY — if blocked, avoid the obstacle regardless of intent
        2. CONSCIOUS INTENT — Andrew's brain said "go this way"
        3. VLM suggestion — visual cortex recommendation
        4. Spatial map frontiers
        5. Default forward / stop
        """
        center = obstacles.get("center", 0.5)
        left = obstacles.get("left", 0.5)
        right = obstacles.get("right", 0.5)

        # Priority 1: SAFETY — blocked path must be avoided
        if center > MIN_CLEARANCE:
            if left < right:
                return "turn_left", f"safety: center blocked ({center:.1f}), left clearer ({left:.1f})"
            elif right < left:
                return "turn_right", f"safety: center blocked ({center:.1f}), right clearer ({right:.1f})"
            else:
                # Tie: use VLM direction rather than defaulting to backward every time
                if best_dir == "left":
                    return "turn_left", f"safety: center blocked, VLM tiebreak left"
                elif best_dir == "right":
                    return "turn_right", f"safety: center blocked, VLM tiebreak right"
                else:
                    return "backward", f"safety: all blocked (L={left:.1f} C={center:.1f} R={right:.1f})"

        # Priority 2: CONSCIOUS INTENT — Andrew's brain chose a direction
        intent = self.get_intent()
        if intent is not None:
            intent_dir = intent["direction"]
            intent_reason = intent.get("reason", "")
            steps_left = intent.get("steps_remaining", 0)

            # Check if the intended direction is safe
            safe = True
            if intent_dir == "forward" and center >= 0.6:
                safe = False
            elif intent_dir == "turn_left" and left >= 0.8:
                safe = False
            elif intent_dir == "turn_right" and right >= 0.8:
                safe = False

            if safe:
                return intent_dir, (
                    f"conscious intent: {intent_reason} "
                    f"({steps_left} steps remaining)"
                )
            else:
                logger.info(
                    f"🧠 Conscious intent ({intent_dir}) blocked by obstacle — "
                    f"body overriding for safety"
                )

        # Priority 3: VLM suggestion
        if confidence > 0.4 and best_dir in ("forward", "left", "right"):
            if best_dir == "forward":
                return "forward", f"vision says forward (conf={confidence:.1f})"
            elif best_dir == "left":
                return "turn_left", f"vision says left (conf={confidence:.1f})"
            elif best_dir == "right":
                return "turn_right", f"vision says right (conf={confidence:.1f})"

        # Priority 4: default forward if clear
        if center < 0.5:
            return "forward", f"center clear ({center:.1f}), moving ahead"

        if left < right and left < 0.5:
            return "turn_left", f"moderate center ({center:.1f}), left clearer"
        elif right < left and right < 0.5:
            return "turn_right", f"moderate center ({center:.1f}), right clearer"

        # Priority 5: spatial map frontiers
        try:
            from repryntt.hardware.spatial_map import get_spatial_map
            smap = get_spatial_map()
            if smap.frontiers:
                frontier = smap.frontiers[0]
                direction = frontier.get("direction", "")
                if direction == "left":
                    return "turn_left", f"frontier to left"
                elif direction == "right":
                    return "turn_right", f"frontier to right"
                else:
                    return "forward", f"frontier ahead"
        except Exception:
            pass

        try:
            from repryntt.hardware.spatial_context import (
                frontier_bias_direction,
            )
            bias = frontier_bias_direction()
            if bias:
                return bias, f"grid frontier bias → {bias}"
        except Exception:
            pass

        return "stop", f"uncertain (L={left:.1f} C={center:.1f} R={right:.1f})"


# ── Singleton ────────────────────────────────────────────────────────

_explorer: Optional[Explorer] = None


def get_explorer() -> Explorer:
    """Get or create the singleton explorer."""
    global _explorer
    if _explorer is None:
        _explorer = Explorer()
    return _explorer


def get_live_vision_context() -> Optional[str]:
    """Return a formatted vision feed for injection into Andrew's heartbeat.

    This is the vision→brain pathway. When the explorer is running, this
    returns what the VLM is currently seeing so Andrew has real-time
    visual awareness — like a visual cortex feeding the conscious brain.

    Returns None if the explorer is not running or has no vision data.
    """
    if _explorer is None:
        return None
    state = _explorer._state
    if not state.running:
        return None
    if not state.vision_feed:
        return None

    lines = [
        "\n👁️ **LIVE VISION FEED — What Your Eyes See Right Now**",
        f"Explorer running: step {state.steps_taken}/{state.steps_limit}, "
        f"goal: {state.goal}",
        f"Distance traveled: {state.distance_cm:.0f}cm, "
        f"places found: {state.places_discovered}",
        "",
    ]
    for frame in state.vision_feed:
        scene = frame.get("scene", "")
        if not scene:
            continue
        step = frame.get("step", "?")
        t = frame.get("time", "")
        direction = frame.get("direction", "?")
        conf = frame.get("confidence", 0)
        obs = frame.get("obstacles", {})
        lines.append(
            f"  Step {step} ({t}): {scene}"
        )
        lines.append(
            f"    → VLM says: go {direction} (confidence {conf:.0%}), "
            f"obstacles L={obs.get('left', '?')}/C={obs.get('center', '?')}/"
            f"R={obs.get('right', '?')}"
        )

    if state.last_action:
        lines.append(
            f"\n  Current action: {state.last_action} "
            f"({state.last_action_reason})"
        )

    # Show active conscious intent if any
    if _explorer is not None:
        intent = _explorer.get_intent()
        if intent:
            lines.append(
                f"\n  🧠 Your active intent: go {intent['direction']} "
                f"— {intent.get('reason', '')} "
                f"({intent.get('steps_remaining', 0)} steps remaining)"
            )
        else:
            lines.append(
                "\n  🧠 No active intent — VLM is choosing directions for you."
            )

    lines.append(
        "\nThis is YOUR vision. The VLM is your visual cortex — "
        "it processes the raw image. YOU are the conscious brain. "
        "Look at what you see and decide where to go:\n"
        "  • Call `nav_set_intent(direction='left', reason='I see a doorway')` "
        "to steer your body\n"
        "  • Call `nav_clear_intent()` to let reflexes take over\n"
        "  • Your intent overrides the VLM (only safety can override you)\n"
        "  • Tell the user what you see — describe it like looking "
        "through your own eyes"
    )
    return "\n".join(lines)
