"""
repryntt.hardware.nav_cortex — Vision-to-Action Navigation Cortex.

Bridges the gap between what Andrew SEES (camera frames) and what he DOES
(motor commands). This is the sensorimotor loop:

    Camera → Perception (Gemini Vision) → World State → Policy (Q-table) → Motor

Two operating modes:
    1. REACTIVE — Vision-only: Gemini analyzes the scene and returns a nav
       command directly (obstacle left → turn right). Works immediately,
       no training needed. Slower (~2-3s per decision due to API call).

    2. LEARNED — RL policy: Camera frame → scene features → discretized
       observation vector → Q-table lookup → best action. Requires a
       pre-trained Q-table from tank_sim_train. Fast (~50ms per decision).

    3. HYBRID (default) — Uses learned policy when confident (max Q-value
       above threshold), falls back to reactive for ambiguous situations.

The cortex maintains a spatial map of recent observations to build
situational awareness across multiple frames.

Architecture reference (what the big boys do):
    Tesla FSD:   8 cameras → HydraNet CNN → occupancy grid → planner → PID
    Boston Dynamics: stereo depth → voxel map → MPC trajectory → torque ctrl
    Us:          2 CSI cams → Gemini VLM → nav features → Q-table → GPIO on/off
    
Same software pipeline, dramatically different compute budgets.
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

logger = logging.getLogger(__name__)

# Reject frames that are too old to trust for movement decisions.
MAX_FRAME_AGE_SEC = 3.0

# ── Action mapping (matches tank_sim.py and tank.py) ─────────────────

ACTION_NAMES = ["forward", "backward", "turn_left", "turn_right", "stop"]
ACTION_FORWARD = 0
ACTION_BACKWARD = 1
ACTION_TURN_LEFT = 2
ACTION_TURN_RIGHT = 3
ACTION_STOP = 4

# ── Navigation prompt for Gemini Vision ──────────────────────────────

NAV_VISION_PROMPT = """You are the vision system of a small tracked robot (tank) navigating indoors.
Analyze this camera image and return a JSON object with these fields:

{
  "obstacles": {
    "left": 0.0-1.0,      // obstacle proximity on the left (0=clear, 1=blocked)
    "center": 0.0-1.0,    // obstacle proximity dead ahead
    "right": 0.0-1.0,     // obstacle proximity on the right
    "above": 0.0-1.0      // overhead clearance concern (cables, shelves)
  },
  "floor": {
    "visible": true/false,        // can you see the floor?
    "traversable": true/false,    // is the floor safe to drive on?
    "surface": "concrete/wood/carpet/unknown"
  },
  "path": {
    "best_direction": "forward/left/right/backward/stop",
    "confidence": 0.0-1.0,
    "reason": "brief explanation"
  },
  "scene": "one-line description of what you see",
  "people": {
    "detected": true/false,       // are there any people visible?
    "count": 0,                   // how many people visible
    "nearest_position": "left/center/right/none",  // where is the closest person in frame
    "nearest_distance_cm": 0-500, // estimated distance to closest person
    "description": ""             // brief description: what they look like, what they're doing
  },
  "tether_visible": true/false,   // can you see the USB power cable?
  "distance_to_nearest_obstacle_cm": 0-500  // rough estimate
}

Be precise with obstacle proximity — the robot is only 15cm wide and moves
on flat ground. It's tethered by a USB cable (~1m), so it can't go far.
IMPORTANT: Always report people accurately — the robot is social and wants
to interact with humans it encounters.
Return ONLY the JSON, no markdown fences."""


# ── Spatial memory (recent observations) ─────────────────────────────

@dataclass
class StereoDepth:
    """Depth measurement from stereo camera pair."""
    left_proximity: float       # 0=clear, 1=blocked
    center_proximity: float
    right_proximity: float
    min_distance_cm: float      # estimated closest obstacle distance
    disparity_map_path: str = ""  # saved colorized depth map
    compute_time_ms: float = 0.0
    valid: bool = True


@dataclass
class NavObservation:
    """Single observation from the nav cortex."""
    timestamp: float
    action_taken: int           # what action preceded this observation
    obstacles: Dict[str, float] # left/center/right/above proximity 0-1
    best_direction: str
    confidence: float
    scene: str
    sensor_vector: np.ndarray   # 11-dim vector matching sim format
    stereo_depth: Optional[StereoDepth] = None  # real depth if available


@dataclass
class SpatialMemory:
    """Rolling buffer of recent navigation observations.
    
    Gives the agent a sense of "where have I been" and "what did I see".
    Like a tiny SLAM system without the actual map — just recent history.
    """
    max_size: int = 20
    observations: List[NavObservation] = field(default_factory=list)

    def add(self, obs: NavObservation) -> None:
        self.observations.append(obs)
        if len(self.observations) > self.max_size:
            self.observations.pop(0)

    def last(self) -> Optional[NavObservation]:
        return self.observations[-1] if self.observations else None

    def recent_actions(self, n: int = 5) -> List[str]:
        """Last N actions taken."""
        return [ACTION_NAMES[o.action_taken] for o in self.observations[-n:]]

    def is_stuck(self) -> bool:
        """Detect if we're oscillating (same 2 actions repeating).
        
        Triggers on A-B-A-B (4 observations) or A-A-A (3 identical non-forward).
        """
        recent = self.recent_actions(4)
        if len(recent) < 3:
            return False
        # A-A-A pattern (3 identical non-forward actions)
        if recent[-1] == recent[-2] == recent[-3] and recent[-1] != "forward":
            return True
        if len(recent) >= 4:
            # A-B-A-B pattern
            if recent[-1] == recent[-3] and recent[-2] == recent[-4] and recent[-1] != recent[-2]:
                return True
        return False

    def least_used_action(self) -> int:
        """Return the action ID used least in recent history (excluding stop)."""
        recent = self.recent_actions(6)
        counts = {a: 0 for a in ACTION_NAMES if a != "stop"}
        for a in recent:
            if a in counts:
                counts[a] += 1
        least = min(counts, key=counts.get)
        return ACTION_NAMES.index(least)

    def summary(self) -> Dict[str, Any]:
        return {
            "observations": len(self.observations),
            "recent_actions": self.recent_actions(5),
            "is_stuck": self.is_stuck(),
            "last_scene": self.observations[-1].scene if self.observations else "none",
        }


# ── Core navigation cortex ───────────────────────────────────────────

class NavCortex:
    """Vision-to-action navigation cortex.
    
    Captures a camera frame, extracts navigation-relevant features via
    Gemini Vision, maps them to the same observation format the RL sim
    uses, then either looks up the trained Q-table or falls back to
    reactive control.
    """

    def __init__(self, brain_path: Optional[str] = None,
                 use_stereo: bool = True):
        self.brain_path = brain_path or str(
            Path.home() / ".repryntt" / "brain")
        self.spatial_memory = SpatialMemory()
        self.q_table: Optional[Dict[tuple, np.ndarray]] = None
        self.q_confidence_threshold = 2.0  # min Q-value spread to trust policy
        self.mode = "hybrid"  # "reactive", "learned", "hybrid"
        self.use_stereo = use_stereo and CV2_AVAILABLE  # use stereo depth when available
        self._last_capture_path: Optional[str] = None
        self._last_depth: Optional[StereoDepth] = None
        # Stereo matching parameters (tuned for our IMX219 pair)
        self._stereo_matcher = None
        if CV2_AVAILABLE:
            self._stereo_matcher = cv2.StereoSGBM_create(
                minDisparity=0,
                numDisparities=64,     # search range (must be ×16)
                blockSize=9,
                P1=8 * 3 * 9**2,       # smoothness penalty
                P2=32 * 3 * 9**2,
                disp12MaxDiff=1,
                uniquenessRatio=10,
                speckleWindowSize=100,
                speckleRange=32,
            )
        # Waveshare IMX219-83 stereo camera: 8cm baseline, 83° H-FoV
        # Focal length = (width/2) / tan(fov/2) = 723px at 1280 wide
        self._baseline_cm = 8.0
        self._focal_px = 723.0

        # ── nvargus-daemon auto-recovery state ──
        # CSI camera on Jetson is single-consumer through nvargus-daemon.
        # When a previous gst-launch doesn't release cleanly, subsequent
        # captures hang for 20s. After N consecutive timeouts we ask
        # systemd to restart nvargus-daemon — the lightest possible
        # remediation that doesn't require a daemon restart.
        self._consecutive_capture_timeouts = 0
        self._last_argus_recovery_at = 0.0
        # Don't attempt recovery more often than every 5 minutes.
        self._argus_recovery_cooldown_s = 300.0
        # Trigger recovery after this many consecutive 20s timeouts.
        self._argus_recovery_threshold = 2

    # ── Camera capture (reuses the working GStreamer pipeline) ────────

    def capture_frame(self, camera_id: int = 0) -> Optional[str]:
        """Capture a single frame from CSI camera. Returns path to JPEG.

        Pulls the latest frame from camera_broker (single-producer per
        sensor — see hardware/camera_broker.py) and writes it to disk.
        Post-processes with aggressive CLAHE+gamma for low-light scenes.
        """
        if not CV2_AVAILABLE:
            return None

        img_dir = Path.home() / ".repryntt" / "data" / "sensory" / "vision"
        date_dir = img_dir / time.strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)

        ts = time.strftime("%H-%M-%S") + f"-{int((time.time() % 1) * 1000):03d}"
        out_path = str(date_dir / f"nav_{camera_id}_{ts}.jpg")

        try:
            from repryntt.hardware.camera_broker import broker
            frame, ts_ms = broker.get_latest(
                camera_id, max_age_ms=MAX_FRAME_AGE_SEC * 1000.0, timeout_s=3.0,
            )
            if frame is None:
                self._consecutive_capture_timeouts += 1
                logger.warning(
                    "Nav cortex camera capture got no frame "
                    "(%d consecutive timeouts)",
                    self._consecutive_capture_timeouts,
                )
                self._maybe_recover_argus_daemon()
                return None
            cv2.imwrite(out_path, frame)
            self._brighten_image(out_path)
            img = cv2.imread(out_path)
            if img is None or img.size == 0:
                logger.warning("Nav capture unreadable image: %s", out_path)
                return None
            self._last_capture_path = out_path
            self._consecutive_capture_timeouts = 0
            return out_path
        except Exception as e:
            logger.warning(f"Nav cortex camera capture failed: {e}")
            return None

    def _maybe_recover_argus_daemon(self) -> None:
        """Restart nvargus-daemon if CSI captures keep timing out.

        nvargus-daemon owns the Argus session for the CSI cameras on
        Jetson. When a previous gst-launch leaks a camera handle, every
        subsequent capture hangs for 20s. A lightweight `systemctl
        restart nvargus-daemon` clears the wedged state without
        bouncing our own daemon.

        Guarded by a cooldown + a consecutive-failure threshold so we
        don't thrash the camera service.
        """
        if self._consecutive_capture_timeouts < self._argus_recovery_threshold:
            return
        now = time.time()
        if (now - self._last_argus_recovery_at) < self._argus_recovery_cooldown_s:
            return
        # Only attempt if `systemctl` exists and we have either root or
        # passwordless sudo for this unit. We never prompt for a password.
        try:
            import shutil
            systemctl = shutil.which("systemctl")
            sudo = shutil.which("sudo")
            if not systemctl:
                logger.info("argus-recovery skipped: no systemctl on PATH")
                return
            cmd = (
                [sudo, "-n", systemctl, "restart", "nvargus-daemon"]
                if sudo and os.geteuid() != 0
                else [systemctl, "restart", "nvargus-daemon"]
            )
            logger.warning(
                "🚑 Auto-recovery: restarting nvargus-daemon after %d "
                "consecutive CSI capture timeouts",
                self._consecutive_capture_timeouts,
            )
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15,
            )
            self._last_argus_recovery_at = now
            if r.returncode == 0:
                self._consecutive_capture_timeouts = 0
                logger.info("✅ nvargus-daemon restarted — CSI camera should recover")
            else:
                logger.warning(
                    "argus-recovery failed (rc=%d): %s",
                    r.returncode, (r.stderr or "")[:200],
                )
                logger.warning(
                    "  → grant passwordless sudo for this unit by adding "
                    "to /etc/sudoers.d/repryntt-argus:\n"
                    "    %s ALL=(root) NOPASSWD: /bin/systemctl restart nvargus-daemon",
                    os.environ.get("USER", "reprynt"),
                )
        except Exception as e:
            logger.warning("argus-recovery exception: %s", e)

    def _brighten_image(self, path: str) -> None:
        """Adaptive CLAHE + gamma — aggressively boost low-light images.
        
        The IMX219 in dark rooms produces mean brightness ~15-25.
        These ARE recoverable — there's signal in the noise, just very dim.
        Only skip if truly black (<8, pure sensor noise) or already bright.
        """
        try:
            img = cv2.imread(path)
            if img is None:
                return
            mean_brightness = img.mean()
            if mean_brightness < 8 or mean_brightness >= 120:
                return  # truly black (pure noise) or bright enough

            # Convert to LAB for luminance-aware enhancement
            lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
            l, a, b = cv2.split(lab)

            # Very dark (8-40): aggressive CLAHE + strong gamma
            if mean_brightness < 40:
                clahe = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(4, 4))
                l = clahe.apply(l)
                # Apply gamma correction — lower gamma = brighter
                gamma = max(0.25, mean_brightness / 120.0)
            else:
                clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
                l = clahe.apply(l)
                gamma = max(0.5, min(0.9, mean_brightness / 130.0))

            lab = cv2.merge([l, a, b])
            img = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
            lut = np.array([((i / 255.0) ** gamma) * 255 for i in range(256)]).astype("uint8")
            img = cv2.LUT(img, lut)
            cv2.imwrite(path, img)

            new_mean = img.mean()
            if new_mean > mean_brightness * 1.5:
                logger.debug(f"Brightened {mean_brightness:.0f} → {new_mean:.0f}")
        except Exception:
            pass  # original image still valid

    # ── Stereo depth computation ─────────────────────────────────────

    def capture_stereo(self) -> Optional[StereoDepth]:
        """Capture from both cameras and compute stereo depth map.

        Returns StereoDepth with per-zone obstacle proximity (0=clear, 1=blocked)
        and estimated distance to nearest obstacle. This is REAL measured depth
        from triangulation — not a guess from a single image.
        """
        if not CV2_AVAILABLE or self._stereo_matcher is None:
            return None

        img_dir = Path.home() / ".repryntt" / "data" / "sensory" / "vision"
        date_dir = img_dir / time.strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%H-%M-%S")

        left_path = str(date_dir / f"stereo_L_{ts}.jpg")
        right_path = str(date_dir / f"stereo_R_{ts}.jpg")
        depth_path = str(date_dir / f"depth_{ts}.jpg")

        t0 = time.time()

        # Pull a synchronized stereo pair from the broker.
        try:
            from repryntt.hardware.camera_broker import broker
            left, right, _ts = broker.get_latest_pair(
                sensor_ids=(0, 1), sync_tolerance_ms=80.0,
                max_age_ms=MAX_FRAME_AGE_SEC * 1000.0, timeout_s=3.0,
            )
        except Exception as e:
            logger.warning(f"Stereo broker fetch failed: {e}")
            return None
        if left is None or right is None:
            logger.warning("Stereo capture returned no frames from broker")
            return None
        cv2.imwrite(left_path, left)
        cv2.imwrite(right_path, right)

        gray_l = cv2.cvtColor(left, cv2.COLOR_BGR2GRAY)
        gray_r = cv2.cvtColor(right, cv2.COLOR_BGR2GRAY)

        # Compute disparity map
        disparity = self._stereo_matcher.compute(gray_l, gray_r).astype(np.float32) / 16.0

        # Save colorized depth map
        disp_norm = cv2.normalize(disparity, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
        disp_color = cv2.applyColorMap(disp_norm, cv2.COLORMAP_JET)
        cv2.imwrite(depth_path, disp_color)

        # Also brighten and save the left frame as the primary image
        self._brighten_image(left_path)
        self._last_capture_path = left_path

        # Analyze zones (left third, center third, right third)
        h, w = disparity.shape
        third = w // 3
        zones = {
            "left": disparity[:, :third],
            "center": disparity[:, third:2*third],
            "right": disparity[:, 2*third:],
        }

        proximities = {}
        min_distance = 500.0  # cm

        for name, zone in zones.items():
            valid = zone[zone > 0]
            if len(valid) > 0:
                # High disparity = close object
                # Proximity = fraction of pixels that are "close" (disparity > 20)
                close_frac = float(np.sum(valid > 20) / len(valid))
                proximities[name] = round(min(1.0, close_frac), 3)

                # Distance estimation: depth_cm = baseline * focal / disparity
                max_disp = float(np.percentile(valid, 95))  # 95th percentile = closest
                if max_disp > 1:
                    dist = (self._baseline_cm * self._focal_px) / max_disp
                    min_distance = min(min_distance, dist)
            else:
                proximities[name] = 0.0  # no valid data = assume clear

        compute_ms = (time.time() - t0) * 1000

        depth = StereoDepth(
            left_proximity=proximities.get("left", 0.0),
            center_proximity=proximities.get("center", 0.0),
            right_proximity=proximities.get("right", 0.0),
            min_distance_cm=round(min_distance, 1),
            disparity_map_path=depth_path,
            compute_time_ms=round(compute_ms),
        )
        self._last_depth = depth
        return depth

    # ── Gemini Vision perception ─────────────────────────────────────

    # Tiered vision: local VLM handles most frames, Gemini for deep analysis
    _perceive_call_count: int = 0
    _DEEP_ANALYSIS_INTERVAL: int = 5
    _local_vlm_enabled: Optional[bool] = None  # cached from ai_config.local_vlm.enabled

    def _is_local_vlm_enabled(self) -> bool:
        """Read local_vlm.enabled from ai_config.json once and cache it.

        When false, every frame routes straight to the remote VLM
        (ai_provider.vision in ai_config — currently NVIDIA NIM). Set this
        to free up Jetson GPU memory in dev mode.
        """
        if self._local_vlm_enabled is None:
            try:
                cfg_path = Path(self.brain_path) / "ai_config.json"
                with open(cfg_path, "r") as f:
                    cfg = json.load(f)
                self._local_vlm_enabled = bool(
                    cfg.get("local_vlm", {}).get("enabled", True))
            except Exception:
                self._local_vlm_enabled = True  # safe default
        return self._local_vlm_enabled

    def perceive(self, image_path: str,
                 spatial_context: str = "",
                 prior_frames: Optional[List[str]] = None,
                 force_remote: bool = False) -> Dict[str, Any]:
        """Tiered vision perception — visual cortex with fast + deep paths.

        Tier 1 (local VLM, ~200ms): Every frame. On-device obstacle
            detection, direction, basic scene classification. No network.
        Tier 2 (Gemini Flash, ~2s): Every Nth frame or when local VLM
            fails. Rich scene description, spatial reasoning.
        Tier 3 (force_remote=True): On-demand deep analysis with full
            spatial context and multi-frame history.

        Args:
            image_path: Current frame (newest, primary frame).
            spatial_context: Text block describing robot pose/frontiers.
            prior_frames: Older frame paths for multi-frame context.
            force_remote: Skip local VLM, go straight to Gemini.
        """
        try:
            if not os.path.exists(image_path):
                return self._fallback_perception("image_missing")
            age_sec = max(0.0, time.time() - os.path.getmtime(image_path))
            if age_sec > MAX_FRAME_AGE_SEC:
                logger.warning(
                    "Nav perception rejected stale frame: %.2fs old (%s)",
                    age_sec, image_path,
                )
                return self._fallback_perception(f"stale_frame:{age_sec:.2f}s")

            self._perceive_call_count += 1

            # ── Tier 1: Local VLM (fast, on-device) ──
            if not force_remote and self._is_local_vlm_enabled():
                local_result = self._perceive_local(image_path)
                if local_result is not None:
                    # Periodically supplement with deep analysis for richer
                    # scene descriptions and landmark detection
                    if (self._perceive_call_count % self._DEEP_ANALYSIS_INTERVAL == 0
                            and not prior_frames):
                        self._enrich_with_remote(
                            local_result, image_path, spatial_context)
                    return local_result

            # ── Tier 2/3: Remote VLM (Gemini) ──
            return self._perceive_remote(
                image_path, spatial_context, prior_frames)

        except Exception as e:
            logger.warning(f"Nav perception failed: {e}")
            return self._fallback_perception("")

    def _perceive_local(self, image_path: str) -> Optional[Dict[str, Any]]:
        """Tier 1: fast on-device VLM perception (~200-500ms)."""
        try:
            from repryntt.hardware.local_vlm import get_local_vlm
            vlm = get_local_vlm()
            result = vlm.perceive(image_path)
            if result is not None:
                logger.debug(
                    f"Tier 1 (local): {result.get('_inference_ms', '?')}ms, "
                    f"scene={result.get('scene', '')[:50]}"
                )
            return result
        except Exception as e:
            logger.debug(f"Local VLM unavailable: {e}")
            return None

    def _perceive_remote(self, image_path: str,
                         spatial_context: str = "",
                         prior_frames: Optional[List[str]] = None,
                         ) -> Dict[str, Any]:
        """Tier 2/3: remote Gemini VLM perception (~2-5s)."""
        try:
            if spatial_context:
                full_prompt = (
                    f"{spatial_context.strip()}\n\n"
                    f"{NAV_VISION_PROMPT}"
                )
            else:
                full_prompt = NAV_VISION_PROMPT

            if prior_frames:
                from repryntt.tools.media import analyze_images_with_vision
                ordered = [p for p in prior_frames if p and os.path.isfile(p)]
                ordered.append(image_path)
                raw = analyze_images_with_vision(
                    self.brain_path, ordered, full_prompt)
            else:
                from repryntt.tools.media import analyze_image_with_vision
                raw = analyze_image_with_vision(
                    self.brain_path, image_path, full_prompt)

            if isinstance(raw, str):
                cleaned = self._clean_vlm_text(raw)
                parsed = self._parse_remote_perception(cleaned)
                if parsed is not None:
                    parsed["_remote_vlm"] = True
                    return parsed

                text_fallback = self._perception_from_text(cleaned)
                if text_fallback is not None:
                    text_fallback["_remote_vlm_text_fallback"] = True
                    return text_fallback

                return self._visual_fallback_perception(
                    image_path, raw_text=cleaned, reason="remote_unparseable")

        except json.JSONDecodeError as e:
            logger.warning(f"Remote VLM JSON parse failed: {e}")
            return self._visual_fallback_perception(
                image_path, raw_text=str(raw) if raw else "",
                reason="remote_json_error")
        except Exception as e:
            logger.warning(f"Remote VLM perception failed: {e}")
            return self._visual_fallback_perception(
                image_path, raw_text=str(e), reason="remote_exception")

    def _clean_vlm_text(self, raw: str) -> str:
        """Strip common wrapper text from VLM output before parsing."""
        cleaned = (raw or "").strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
        return cleaned

    def _parse_remote_perception(self, cleaned: str) -> Optional[Dict[str, Any]]:
        """Parse and normalize JSON even when the VLM omits some fields."""
        start = cleaned.find("{")
        if start < 0:
            return None

        depth = 0
        end = start
        for i, ch in enumerate(cleaned[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        else:
            logger.debug("Remote VLM returned incomplete JSON: %s", cleaned[:200])
            return None

        result = json.loads(cleaned[start:end])
        if not isinstance(result, dict):
            return None
        if "error" in result and len(result) <= 2:
            return None
        return self._normalize_remote_perception(result)

    def _normalize_remote_perception(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Fill missing VLM fields with conservative defaults."""
        obstacles = dict(raw.get("obstacles") or {})
        for key in ("left", "center", "right"):
            try:
                obstacles[key] = max(0.0, min(1.0, float(obstacles.get(key, 0.5))))
            except (TypeError, ValueError):
                obstacles[key] = 0.5
        try:
            obstacles["above"] = max(0.0, min(1.0, float(obstacles.get("above", 0.3))))
        except (TypeError, ValueError):
            obstacles["above"] = 0.3

        floor = dict(raw.get("floor") or {})
        floor.setdefault("visible", True)
        floor.setdefault("traversable", True)
        floor.setdefault("surface", "unknown")

        path = dict(raw.get("path") or {})
        direction = str(path.get("best_direction", "stop")).lower().strip()
        aliases = {
            "ahead": "forward",
            "straight": "forward",
            "go_forward": "forward",
            "turn_left": "left",
            "turn_right": "right",
            "reverse": "backward",
            "back": "backward",
        }
        direction = aliases.get(direction, direction)
        valid_dirs = {"forward", "left", "right", "backward", "stop"}
        if direction not in valid_dirs:
            direction = "stop"
        try:
            confidence = max(0.0, min(1.0, float(path.get("confidence", 0.35))))
        except (TypeError, ValueError):
            confidence = 0.35

        people_raw = raw.get("people")
        if isinstance(people_raw, dict):
            people = people_raw
        else:
            detected = bool(raw.get("people_detected", False))
            people = {
                "detected": detected,
                "count": 1 if detected else 0,
                "nearest_position": "center" if detected else "none",
                "nearest_distance_cm": 200 if detected else 0,
                "description": "",
            }

        try:
            distance_cm = int(raw.get("distance_to_nearest_obstacle_cm", 100))
        except (TypeError, ValueError):
            distance_cm = 100

        return {
            "obstacles": obstacles,
            "floor": floor,
            "path": {
                "best_direction": direction,
                "confidence": confidence,
                "reason": str(path.get("reason", "remote VLM normalized"))[:200],
            },
            "scene": str(raw.get("scene", ""))[:300] or "camera frame analyzed",
            "scene_type": str(raw.get("scene_type", "unknown")).lower().strip(),
            "people": people,
            "tether_visible": bool(raw.get("tether_visible", False)),
            "distance_to_nearest_obstacle_cm": max(0, min(500, distance_cm)),
        }

    def _perception_from_text(self, text: str) -> Optional[Dict[str, Any]]:
        """Salvage useful scene text when a VLM ignores the JSON contract."""
        if not text or len(text.strip()) < 5:
            return None

        lower = text.lower()
        hard_fail = (
            "api key", "vision api failed", "no analysis returned",
            "rate limit", "timed out", "timeout", "permission_denied",
            "image not found", "no gemini api key", "provider failed",
        )
        if any(token in lower for token in hard_fail):
            return None

        center = 0.5
        left = 0.5
        right = 0.5
        if any(token in lower for token in (
                "clear path", "open floor", "open space", "wooden floor",
                "floor visible", "room", "living room", "hallway")):
            center = 0.35
        if any(token in lower for token in (
                "blocked", "obstacle", "wall ahead", "close to", "clutter")):
            center = 0.7
        if "left" in lower and any(token in lower for token in ("open", "clear", "doorway")):
            left = 0.3
        if "right" in lower and any(token in lower for token in ("open", "clear", "doorway")):
            right = 0.3

        direction = "forward" if center < 0.55 else ("left" if left <= right else "right")
        if "stop" in lower or "do not move" in lower:
            direction = "stop"
        elif "turn left" in lower or "go left" in lower:
            direction = "left"
        elif "turn right" in lower or "go right" in lower:
            direction = "right"
        elif "back up" in lower or "reverse" in lower:
            direction = "backward"

        scene_type = "unknown"
        for label, value in (
                ("living room", "living_room"),
                ("hallway", "hallway"),
                ("corridor", "hallway"),
                ("doorway", "doorway"),
                ("kitchen", "kitchen"),
                ("bedroom", "bedroom"),
                ("room", "room"),
                ("stairs", "stairs")):
            if label in lower:
                scene_type = value
                break

        people = any(token in lower for token in (
            "person", "people", "human", "someone", "man", "woman", "child"))

        return {
            "obstacles": {"left": left, "center": center, "right": right, "above": 0.3},
            "floor": {
                "visible": "floor" in lower or scene_type in {"room", "living_room", "hallway"},
                "traversable": center < 0.7,
                "surface": "wood" if "wood" in lower else "unknown",
            },
            "path": {
                "best_direction": direction,
                "confidence": 0.45,
                "reason": "salvaged from non-JSON VLM text",
            },
            "scene": text[:300],
            "scene_type": scene_type,
            "people": {
                "detected": people,
                "count": 1 if people else 0,
                "nearest_position": "center" if people else "none",
                "nearest_distance_cm": 200 if people else 0,
                "description": "",
            },
            "tether_visible": "cable" in lower or "cord" in lower or "tether" in lower,
            "distance_to_nearest_obstacle_cm": 100,
        }

    def _visual_fallback_perception(self, image_path: str, raw_text: str = "",
                                    reason: str = "fallback") -> Dict[str, Any]:
        """Last-resort local CV fallback for a real, readable camera frame."""
        if CV2_AVAILABLE and image_path and os.path.isfile(image_path):
            img = cv2.imread(image_path)
            if img is not None and img.size > 0:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                mean_brightness = float(gray.mean())
                h, _w = gray.shape
                lower = gray[h // 2:, :]
                thirds = np.array_split(lower, 3, axis=1)
                texture = [float(cv2.Laplacian(zone, cv2.CV_64F).var())
                           for zone in thirds]
                max_texture = max(texture) if texture else 1.0

                if mean_brightness < 18:
                    best_dir = "stop"
                    confidence = 0.25
                    scene = "camera frame is very dark; local vision fallback active"
                    center = left = right = 0.65
                else:
                    proximities = [
                        max(0.15, min(0.75, value / max(max_texture, 1.0)))
                        for value in texture
                    ]
                    left, center, right = proximities
                    best_dir = "forward" if center < 0.55 else (
                        "left" if left <= right else "right")
                    confidence = 0.35
                    scene = (
                        "camera frame readable; remote VLM failed, using local "
                        f"CV fallback (brightness={mean_brightness:.0f})"
                    )

                logger.warning(
                    "Remote VLM fallback used (%s): %s; raw=%s",
                    reason, image_path, (raw_text or "")[:160],
                )
                return {
                    "obstacles": {
                        "left": round(left, 2),
                        "center": round(center, 2),
                        "right": round(right, 2),
                        "above": 0.3,
                    },
                    "floor": {
                        "visible": mean_brightness >= 18,
                        "traversable": center < 0.7,
                        "surface": "unknown",
                    },
                    "path": {
                        "best_direction": best_dir,
                        "confidence": confidence,
                        "reason": f"local CV fallback after {reason}",
                    },
                    "scene": scene,
                    "scene_type": "unknown",
                    "people": {
                        "detected": False,
                        "count": 0,
                        "nearest_position": "none",
                        "nearest_distance_cm": 0,
                        "description": "",
                    },
                    "tether_visible": False,
                    "distance_to_nearest_obstacle_cm": 100,
                    "_visual_fallback": True,
                }

        return self._fallback_perception(raw_text or reason)

    def _enrich_with_remote(self, local_result: Dict[str, Any],
                            image_path: str,
                            spatial_context: str = "") -> None:
        """Supplement local VLM result with a remote deep-analysis pass.

        Runs in background thread so it doesn't block the explorer loop.
        Updates the local_result dict in-place with richer scene description
        and landmark data from Gemini.
        """
        def _enrich():
            try:
                remote = self._perceive_remote(image_path, spatial_context)
                if remote and remote.get("scene"):
                    remote_scene = remote.get("scene", "")
                    if len(remote_scene) > len(local_result.get("scene", "")):
                        local_result["scene"] = remote_scene
                    if remote.get("people", {}).get("detected"):
                        local_result["people"] = remote["people"]
                    local_result["_enriched"] = True
            except Exception:
                pass

        import threading
        threading.Thread(
            target=_enrich, name="vlm-enrich", daemon=True
        ).start()

    def _fallback_perception(self, raw_text: str) -> Dict[str, Any]:
        """When Gemini response isn't parseable, return conservative defaults.

        Sets _perception_failed=True so downstream code (experience logger,
        trainer) can filter these rows on a structured flag instead of
        substring-matching the scene text.
        """
        return {
            "obstacles": {"left": 0.5, "center": 0.5, "right": 0.5, "above": 0.3},
            "floor": {"visible": True, "traversable": True, "surface": "unknown"},
            "path": {"best_direction": "stop", "confidence": 0.2,
                     "reason": f"perception unclear: {raw_text[:100]}"},
            "scene": "",
            "tether_visible": False,
            "distance_to_nearest_obstacle_cm": 100,
            "_perception_failed": True,
        }

    # ── Perception → Observation vector ──────────────────────────────

    def perception_to_obs(self, perception: Dict[str, Any],
                          heading_deg: float = 0.0,
                          goal_dist: float = 0.5,
                          goal_angle: float = 0.0,
                          stereo: Optional[StereoDepth] = None) -> np.ndarray:
        """Convert perception to the 11-dim observation vector used by Q-learner.
        
        If stereo depth is available, uses REAL measured obstacle proximity
        instead of Gemini's guesses. Stereo data always takes priority.
        
        Sim observation format:
            [0:8]  - Ray distances (0=blocked, 1=clear)
            [8]    - Heading (0-1, normalized)
            [9]    - Distance to goal (0-1)
            [10]   - Angle to goal (-1 to 1)
        """
        # Prefer stereo depth (real measurements) over VLM guesses
        if stereo is not None and stereo.valid:
            left = 1.0 - stereo.left_proximity
            center = 1.0 - stereo.center_proximity
            right = 1.0 - stereo.right_proximity
        else:
            obs = perception.get("obstacles", {})
            left = 1.0 - float(obs.get("left", 0.5))
            center = 1.0 - float(obs.get("center", 0.5))
            right = 1.0 - float(obs.get("right", 0.5))

        # Distribute 3 zones across 8 rays (matching sim's 0°, 45°, 90°, ... 315°)
        # Front = rays 0,1,7  |  Left = rays 2,3  |  Back = rays 4  |  Right = rays 5,6
        rays = np.array([
            center,                          # 0° (dead ahead)
            (center + right) / 2,            # 45° (front-right)
            right,                           # 90° (right)
            (right + 0.8) / 2,               # 135° (rear-right, assume mostly clear)
            0.8,                             # 180° (behind, assume clear-ish)
            (left + 0.8) / 2,                # 225° (rear-left)
            left,                            # 270° (left)
            (left + center) / 2,             # 315° (front-left)
        ], dtype=np.float32)

        heading_norm = (heading_deg % 360) / 360.0
        goal_dist_norm = np.clip(goal_dist, 0, 1)
        goal_angle_norm = np.clip(goal_angle / 180.0, -1, 1)

        return np.concatenate([
            rays,
            [heading_norm, goal_dist_norm, goal_angle_norm]
        ]).astype(np.float32)

    # ── Q-table loading / saving ─────────────────────────────────────

    def load_q_table(self, path: Optional[str] = None) -> bool:
        """Load a trained Q-table from disk."""
        if path is None:
            path = os.path.join(self.brain_path, "nav_q_table.npz")
        if not os.path.exists(path):
            logger.info(f"No Q-table found at {path}")
            return False

        try:
            data = np.load(path, allow_pickle=True)
            self.q_table = data["q_table"].item()  # dict stored as 0-d array
            logger.info(f"Loaded Q-table: {len(self.q_table)} states from {path}")
            return True
        except Exception as e:
            logger.warning(f"Failed to load Q-table: {e}")
            return False

    def save_q_table(self, path: Optional[str] = None) -> str:
        """Save current Q-table to disk."""
        if self.q_table is None:
            return "No Q-table to save"
        if path is None:
            path = os.path.join(self.brain_path, "nav_q_table.npz")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        np.savez_compressed(path, q_table=self.q_table)
        return f"Saved Q-table ({len(self.q_table)} states) to {path}"

    def train_and_load(self, episodes: int = 500, grid_size: int = 15,
                       obstacles: int = 6) -> Dict[str, Any]:
        """Train a fresh Q-table in simulation and load it for real nav."""
        from repryntt.hardware.tank_sim import TankSimEnv, TankQLearner

        env = TankSimEnv(grid_size=grid_size, num_obstacles=obstacles)
        learner = TankQLearner()
        results = learner.train(env, episodes=episodes, verbose=False)
        self.q_table = learner.q_table
        self.save_q_table()
        results["loaded_for_nav"] = True
        results["q_table_path"] = os.path.join(self.brain_path, "nav_q_table.npz")
        return results

    # ── Policy decision ──────────────────────────────────────────────

    def _discretize(self, obs: np.ndarray) -> tuple:
        """Match TankQLearner's discretization."""
        bins = np.digitize(obs, bins=np.linspace(-1, 1, 6)[1:-1])
        return tuple(bins)

    def _q_table_action(self, obs: np.ndarray) -> Tuple[int, float]:
        """Look up best action from Q-table. Returns (action, confidence)."""
        if self.q_table is None:
            return ACTION_STOP, 0.0

        state = self._discretize(obs)
        q_vals = self.q_table.get(state, None)
        if q_vals is None:
            return ACTION_STOP, 0.0

        best = int(np.argmax(q_vals))
        # Confidence = gap between best and second-best Q-value
        sorted_q = np.sort(q_vals)[::-1]
        confidence = sorted_q[0] - sorted_q[1] if len(sorted_q) > 1 else 0.0
        return best, confidence

    def _reactive_action(self, perception: Dict[str, Any]) -> Tuple[int, float]:
        """Pure reactive control from Gemini's nav assessment."""
        path_info = perception.get("path", {})
        direction = path_info.get("best_direction", "stop").lower()
        confidence = float(path_info.get("confidence", 0.5))

        direction_map = {
            "forward": ACTION_FORWARD,
            "backward": ACTION_BACKWARD,
            "left": ACTION_TURN_LEFT,
            "right": ACTION_TURN_RIGHT,
            "stop": ACTION_STOP,
        }
        action = direction_map.get(direction, ACTION_STOP)
        return action, confidence

    def _stereo_reactive_action(self, stereo: StereoDepth) -> Tuple[int, float]:
        """Pure reactive control from stereo depth alone.
        
        No API call needed — just geometry. If center is blocked, turn
        toward the clearer side. If everything is close, back up.
        """
        l, c, r = stereo.left_proximity, stereo.center_proximity, stereo.right_proximity

        if c < 0.3 and l < 0.3 and r < 0.3:
            # All clear — drive forward
            return ACTION_FORWARD, 0.8
        elif c >= 0.6:
            # Center blocked
            if l < r:
                return ACTION_TURN_LEFT, 0.7
            else:
                return ACTION_TURN_RIGHT, 0.7
        elif l >= 0.6 and r >= 0.6:
            # Both sides blocked
            if c < 0.4:
                return ACTION_FORWARD, 0.4  # squeeze through
            else:
                return ACTION_BACKWARD, 0.6
        elif l >= 0.5:
            return ACTION_TURN_RIGHT, 0.6
        elif r >= 0.5:
            return ACTION_TURN_LEFT, 0.6
        else:
            return ACTION_FORWARD, 0.5

    def decide(self, perception: Dict[str, Any],
               heading: float = 0.0,
               stereo: Optional[StereoDepth] = None) -> Dict[str, Any]:
        """Main decision function — pick the best action given perception.
        
        If stereo depth is available, it provides the obstacle data (real
        measured distances). Gemini perception adds semantic understanding
        (what things are, floor assessment, scene context).
        
        Returns dict with action, confidence, method, and reasoning.
        """
        obs = self.perception_to_obs(perception, heading_deg=heading, stereo=stereo)

        if self.mode == "reactive":
            # If stereo available, use geometry-only reactive (no API needed)
            if stereo is not None and stereo.valid:
                action, conf = self._stereo_reactive_action(stereo)
                method = "stereo_reactive"
            else:
                action, conf = self._reactive_action(perception)
                method = "reactive"
        elif self.mode == "learned":
            action, conf = self._q_table_action(obs)
            method = "learned"
        else:  # hybrid — try MLP driver policy first, then Q-table, then reactive
            action, conf, method = None, 0.0, "reactive"

            # Layer 1: MLP driver policy (if trained and available)
            try:
                from repryntt.hardware.driver_policy import get_driver_policy
                driver = get_driver_policy()
                if driver.available:
                    # Build YOLO feature vector if YOLO is running
                    yolo_vec = self._get_yolo_features(perception, stereo, heading)
                    if yolo_vec is not None:
                        decision = driver.decide(yolo_vec)
                        if not decision["needs_gemini"]:
                            mlp_action = decision["action_id"]
                            mlp_conf = decision["confidence"]
                            # Forward-bias: if MLP wants to turn but center is
                            # clear (low proximity), prefer forward to break
                            # oscillation between turn_left/turn_right.
                            center_prox = perception.get("obstacles", {}).get("center", 0.5)
                            recent = self.spatial_memory.recent_actions(3)
                            last_was_turn = len(recent) > 0 and recent[-1] in ("turn_left", "turn_right")
                            if (mlp_action in (ACTION_TURN_LEFT, ACTION_TURN_RIGHT)
                                    and center_prox < 0.35 and last_was_turn
                                    and mlp_conf < 0.85):
                                action = ACTION_FORWARD
                                conf = 0.6
                                method = "driver_policy+forward_bias"
                            else:
                                action = mlp_action
                                conf = mlp_conf
                                method = "driver_policy"
            except Exception as e:
                logger.debug(f"Driver policy unavailable: {e}")

            # Layer 2: Q-table (if MLP didn't decide)
            if action is None:
                q_action, q_conf = self._q_table_action(obs)
                if q_conf >= self.q_confidence_threshold:
                    action, conf, method = q_action, q_conf, "learned"

            # Layer 3: Stereo reactive (if above didn't decide)
            if action is None:
                if stereo is not None and stereo.valid:
                    action, conf = self._stereo_reactive_action(stereo)
                    method = "stereo_reactive"
                else:
                    r_action, r_conf = self._reactive_action(perception)
                    action, conf, method = r_action, r_conf, "reactive"

        # Safety override: if stereo says something is very close, stop/back up
        if stereo is not None and stereo.valid and stereo.min_distance_cm < 15:
            action = ACTION_BACKWARD
            method = "safety_override"
            conf = 0.9

        # Anti-stuck: if oscillating, pick the least-used recent action
        if self.spatial_memory.is_stuck():
            action = self.spatial_memory.least_used_action()
            method = "anti-stuck"
            conf = 0.3
            logger.info(f"🔄 Anti-stuck override → {ACTION_NAMES[action]}")

        # Record observation
        nav_obs = NavObservation(
            timestamp=time.time(),
            action_taken=action,
            obstacles=perception.get("obstacles", {}),
            best_direction=perception.get("path", {}).get("best_direction", "stop"),
            confidence=conf,
            scene=perception.get("scene", ""),
            sensor_vector=obs,
            stereo_depth=stereo,
        )
        self.spatial_memory.add(nav_obs)

        result = {
            "action": ACTION_NAMES[action],
            "action_id": action,
            "confidence": round(conf, 3),
            "method": method,
            "scene": perception.get("scene", ""),
            "obstacles": perception.get("obstacles", {}),
            "spatial_memory": self.spatial_memory.summary(),
        }
        if stereo is not None:
            result["stereo"] = {
                "left": stereo.left_proximity,
                "center": stereo.center_proximity,
                "right": stereo.right_proximity,
                "min_distance_cm": stereo.min_distance_cm,
                "depth_map": stereo.disparity_map_path,
            }
        return result

    def _get_yolo_features(self, perception: Dict[str, Any],
                           stereo: Optional[StereoDepth],
                           heading: float) -> Optional[np.ndarray]:
        """Build YOLO-enriched feature vector for the MLP driver policy.
        
        If YOLO perception is embedded in the perception dict (from
        yolo_perception.py / depth_perception.py), use that.
        Otherwise build stereo-only features.
        Returns 50-dim vector or None if no useful features available.
        
        Feature vector layout (50-dim):
            [0:3]   obstacle density (left, center, right) — from depth or stereo
            [3:6]   person presence per zone
            [6:9]   nearest obstacle area per zone
            [9:12]  nearest person area per zone
            [12:15] object count per zone
            [15]    person count (0-1, clamped at 3)
            [16]    obstacle count (0-1, clamped at 10)
            [17]    detection count (0-1, clamped at 20)
            [18]    has_person (0/1)
            [19]    nearest person area fraction
            [20:28] top-8 COCO class presence
            [28:31] depth: left/center/right proximity (neural depth OR stereo)
            [31:34] temporal: last action one-hot (fwd/turn/back)
            [34:37] goal: distance, angle, heading
            [37:50] reserved
        """
        try:
            # Check if perception already has YOLO data (from FusedPerception)
            if perception.get("_yolo"):
                vec = np.zeros(50, dtype=np.float32)
                oz = perception.get("obstacles", {})
                vec[0] = oz.get("left", 0.0)
                vec[1] = oz.get("center", 0.0)
                vec[2] = oz.get("right", 0.0)

                people = perception.get("people", {})
                if people.get("detected"):
                    vec[18] = 1.0
                    vec[15] = min(people.get("count", 0) / 3.0, 1.0)
                    # Person area from depth proximity (rough proxy)
                    depth_prox = people.get("_depth_proximity", 0.0)
                    if depth_prox > 0:
                        pos = people.get("nearest_position", "center")
                        zone = {"left": 0, "center": 1, "right": 2}.get(pos, 1)
                        vec[3 + zone] = 1.0  # person presence in zone
                        vec[9 + zone] = depth_prox  # person area proxy in zone
                        vec[19] = depth_prox  # nearest person area fraction

            elif stereo is not None and stereo.valid:
                # No YOLO perception — use stereo-only features
                vec = np.zeros(50, dtype=np.float32)
                oz = perception.get("obstacles", {})
                vec[0] = oz.get("left", 0.0)
                vec[1] = oz.get("center", 0.0)
                vec[2] = oz.get("right", 0.0)
            else:
                return None

            # Depth indices [28:31] — prefer neural depth, fall back to stereo
            stereo_compat = perception.get("_stereo_compat")
            if stereo_compat:
                # Neural depth from Depth Anything v2 (more accurate)
                vec[28] = stereo_compat.get("left", 0.0)
                vec[29] = stereo_compat.get("center", 0.0)
                vec[30] = stereo_compat.get("right", 0.0)
            elif stereo is not None and stereo.valid:
                # Hardware stereo fallback
                vec[28] = stereo.left_proximity
                vec[29] = stereo.center_proximity
                vec[30] = stereo.right_proximity

            # Temporal features (last action)
            last = self.spatial_memory.last()
            if last is not None:
                if last.action_taken == ACTION_FORWARD:
                    vec[31] = 1.0
                elif last.action_taken in (ACTION_TURN_LEFT, ACTION_TURN_RIGHT):
                    vec[32] = 1.0
                elif last.action_taken == ACTION_BACKWARD:
                    vec[33] = 1.0

            # Heading
            vec[36] = (heading % 360) / 360.0

            return vec
        except Exception as e:
            logger.debug(f"YOLO feature extraction failed: {e}")
            return None

    # ── Full sensorimotor loop (capture → perceive → decide → act) ───

    def navigate_step(self, camera_id: int = 0,
                      execute: bool = False,
                      speed: float = 0.5,
                      duration: float = 0.8,
                      use_stereo: Optional[bool] = None) -> Dict[str, Any]:
        """One full navigation cycle: see → think → act.
        
        Vision-first: captures a camera frame, sends it to the VLM
        for scene analysis, then decides based on the VLM's response.
        Stereo depth is optional — only used as a safety check.
        
        Args:
            camera_id: Which camera to use (0 or 1).
            execute: If True, actually send motor commands. If False,
                     just return the decision (dry run).
            speed: Motor speed 0.0-1.0 for the command.
            duration: How long to run the motor command.
            use_stereo: If True, capture stereo pair so the depth path
                        (StereoSGBM or DepthAnything via FusedPerception)
                        can override Gemini's obstacle guesses. If None,
                        honors self.use_stereo (constructor default True).

        Returns:
            Dict with perception, decision, and motor result.
        """
        do_stereo = use_stereo if use_stereo is not None else self.use_stereo
        result: Dict[str, Any] = {"timestamp": time.time()}
        stereo_depth = None

        # 1. CAPTURE (stereo pair or single frame)
        t0 = time.time()
        if do_stereo:
            stereo_depth = self.capture_stereo()
            image_path = self._last_capture_path  # left frame
            if stereo_depth is None:
                # Stereo failed — fall back to single camera
                image_path = self.capture_frame(camera_id)
            else:
                result["stereo_compute_ms"] = stereo_depth.compute_time_ms
        else:
            image_path = self.capture_frame(camera_id)

        if not image_path:
            return {**result, "error": "Camera capture failed",
                    "decision": {"action": "stop", "method": "error"}}
        result["capture_time_ms"] = round((time.time() - t0) * 1000)
        result["image_path"] = image_path

        # 2. PERCEIVE (Gemini VLM for semantic understanding)
        t1 = time.time()
        perception = self.perceive(image_path)
        result["perception_time_ms"] = round((time.time() - t1) * 1000)
        result["perception"] = perception

        # If stereo depth available, override obstacle values with real data
        if stereo_depth is not None:
            perception["obstacles"] = {
                "left": stereo_depth.left_proximity,
                "center": stereo_depth.center_proximity,
                "right": stereo_depth.right_proximity,
                "above": perception.get("obstacles", {}).get("above", 0.0),
            }
            perception["distance_to_nearest_obstacle_cm"] = stereo_depth.min_distance_cm
            result["depth_map"] = stereo_depth.disparity_map_path

        # 3. DECIDE
        decision = self.decide(perception, stereo=stereo_depth)
        result["decision"] = decision

        # 4. ACT (if enabled)
        if execute:
            t2 = time.time()
            motor_result = self._execute_action(
                decision["action_id"], speed, duration)
            result["motor_time_ms"] = round((time.time() - t2) * 1000)
            result["motor_result"] = motor_result
        else:
            result["motor_result"] = {"executed": False, "reason": "dry_run"}

        result["total_time_ms"] = round((time.time() - result["timestamp"]) * 1000)

        # Log experience for future learning
        if execute:
            self._log_experience(result)
            # Update persistent spatial map
            try:
                from repryntt.hardware.spatial_map import get_spatial_map
                smap = get_spatial_map()
                action_name = ACTION_NAMES[decision.get("action_id", 4)]
                scene = decision.get("scene", "")
                obstacles = perception.get("obstacles", {})
                smap.record_move(action_name, speed, duration,
                                 scene=scene, obstacles=obstacles)
                # Check for open paths we're not taking (frontiers)
                best_dir = perception.get("path", {}).get("best_direction", "")
                smap.record_observation(scene, obstacles=obstacles,
                                        best_direction=best_dir)
            except Exception as e:
                logger.debug(f"Spatial map update failed: {e}")

        return result

    def _execute_action(self, action_id: int, speed: float,
                        duration: float) -> Dict[str, Any]:
        """Send the decided action to motors.

        Uses the motor_daemon first so one process owns GPIO.  Falls back
        to /cmd_vel_brain (ROS2) only when the daemon is unavailable.

        Set NAV_CORTEX_DRY_RUN=1 to skip motor I/O entirely while still
        recording the would-be command. Used by Phase 6 shadow mode to
        validate the see→decide→log loop on jacks without firing GPIO.
        """
        action_name = ACTION_NAMES[action_id] if 0 <= action_id < len(ACTION_NAMES) else "stop"

        if os.environ.get("NAV_CORTEX_DRY_RUN") == "1":
            return {"success": True, "command": action_name,
                    "method": "shadow_dry_run", "duration": duration}

        # Primary path: motor_daemon via motor_client. Per-command session is
        # fine here because nav_cortex calls are infrequent (1 every few s);
        # the explorer's longer-running session covers tighter loops.
        try:
            from repryntt.hardware.motor_client import (
                DaemonUnavailable, Preempted, Priority, session as _ms,
            )
            with _ms(priority=Priority.AUTONOMOUS,
                     holder_label="nav_cortex",
                     wait_timeout_s=2.0,
                     require_daemon=True) as s:
                if action_id == ACTION_FORWARD:
                    return s.move_forward(speed, duration)
                elif action_id == ACTION_BACKWARD:
                    return s.move_backward(speed, duration)
                elif action_id == ACTION_TURN_LEFT:
                    return s.turn_left(speed, duration)
                elif action_id == ACTION_TURN_RIGHT:
                    return s.turn_right(speed, duration)
                else:
                    return s.stop()
        except Preempted:
            return {"success": False, "error": "preempted_by_higher_priority",
                    "method": "motor_daemon"}
        except DaemonUnavailable:
            pass
        except Exception as e:
            logger.error(f"Motor execution failed: {e}")
            return {"success": False, "error": str(e)[:200]}

        # Fallback path: publish to ROS2 /cmd_vel_brain for dev or legacy
        # setups that have no motor daemon running.
        try:
            from repryntt.hardware.ros2_publisher import publish_cmd_vel
            if publish_cmd_vel(action_name, speed):
                return {"success": True, "command": action_name,
                        "method": "ros2_cmd_vel", "duration": duration}
        except Exception:
            pass

        return {"success": False, "error": "motor daemon unavailable"}

    # ── Multi-step autonomous navigation ─────────────────────────────

    def navigate_sequence(self, steps: int = 5, camera_id: int = 0,
                          execute: bool = False, speed: float = 0.4,
                          duration: float = 0.6,
                          pause_between: float = 1.0) -> Dict[str, Any]:
        """Run multiple navigate_step() in sequence — a short autonomous walk.
        
        Args:
            steps: Number of see-think-act cycles.
            execute: Actually move the robot (False = planning only).
            pause_between: Seconds between steps (let motors settle, 
                          camera re-stabilize).
        
        Returns:
            Summary with all step results, action distribution, timing.
        """
        results = []
        action_counts = {name: 0 for name in ACTION_NAMES}

        for i in range(steps):
            step_result = self.navigate_step(
                camera_id=camera_id, execute=execute,
                speed=speed, duration=duration)
            results.append(step_result)

            action = step_result.get("decision", {}).get("action", "stop")
            action_counts[action] = action_counts.get(action, 0) + 1

            # Check for errors
            if "error" in step_result:
                break

            # Pause between steps
            if i < steps - 1 and pause_between > 0:
                time.sleep(pause_between)

        total_time = sum(r.get("total_time_ms", 0) for r in results)
        return {
            "steps_completed": len(results),
            "steps_requested": steps,
            "action_distribution": action_counts,
            "total_time_ms": total_time,
            "avg_step_time_ms": round(total_time / max(1, len(results))),
            "executed": execute,
            "spatial_memory": self.spatial_memory.summary(),
            "step_details": results,
        }

    # ── Status / introspection ───────────────────────────────────────

    def _log_experience(self, step_result: Dict[str, Any]) -> None:
        """Log a navigation experience to JSONL for future learning.
        
        Every real navigation step is recorded: image path, stereo depth,
        perception, decision, motor result. This data can be used for:
        - Imitation learning (train CNN to predict actions from images)
        - Q-table refinement from real outcomes  
        - Debugging navigation failures after the fact
        """
        # Operator brake: while ~/.repryntt/EMBODIED_PAUSED exists, refuse
        # to write nav experience. Stops sensor-broken runs from
        # contaminating the training corpus. Remove the file to resume.
        # Set EMBODIED_SHADOW=1 to bypass the brake but route writes to a
        # quarantined nav_experience_shadow/ dir — Phase 6 shadow mode.
        shadow = os.environ.get("EMBODIED_SHADOW") == "1"
        if (Path.home() / ".repryntt" / "EMBODIED_PAUSED").exists() and not shadow:
            return

        sub = "nav_experience_shadow" if shadow else "nav_experience"
        log_dir = Path.home() / ".repryntt" / "data" / sub
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{time.strftime('%Y-%m-%d')}.jsonl"

        mr = step_result.get("motor_result", {}) or {}
        perception = step_result.get("perception", {}) or {}
        entry = {
            "ts": step_result.get("timestamp", time.time()),
            "image": step_result.get("image_path", ""),
            "depth_map": step_result.get("depth_map", ""),
            "decision": step_result.get("decision", {}).get("action", ""),
            "method": step_result.get("decision", {}).get("method", ""),
            "confidence": step_result.get("decision", {}).get("confidence", 0),
            # Honor either "success" (ROS2/tank paths) or "executed" (dry_run path).
            "executed": mr.get("success", mr.get("executed", False)),
            "motor_success": mr.get("success", False),
            "motor_method": mr.get("method", ""),
            "perception_failed": bool(perception.get("_perception_failed", False)),
            "capture_ms": step_result.get("capture_time_ms", 0),
            "perception_ms": step_result.get("perception_time_ms", 0),
            "total_ms": step_result.get("total_time_ms", 0),
        }
        # Add stereo data if present
        stereo = step_result.get("decision", {}).get("stereo")
        if stereo:
            entry["stereo_left"] = stereo.get("left", 0)
            entry["stereo_center"] = stereo.get("center", 0)
            entry["stereo_right"] = stereo.get("right", 0)
            entry["stereo_min_cm"] = stereo.get("min_distance_cm", 0)

        # Add neural depth / fused perception data if present
        if perception.get("_depth"):
            sc = perception.get("_stereo_compat", {})
            entry["depth_left"] = sc.get("left", 0)
            entry["depth_center"] = sc.get("center", 0)
            entry["depth_right"] = sc.get("right", 0)
            entry["depth_min_cm"] = sc.get("min_distance_cm", 0)
            entry["depth_ms"] = perception.get("_depth_ms", 0)
            # If no stereo data, use neural depth as stereo columns
            # so the trainer can learn from depth-equipped steps
            if "stereo_left" not in entry:
                entry["stereo_left"] = sc.get("left", 0)
                entry["stereo_center"] = sc.get("center", 0)
                entry["stereo_right"] = sc.get("right", 0)
                entry["stereo_min_cm"] = sc.get("min_distance_cm", 0)
        if perception.get("_yolo"):
            entry["yolo"] = True
            entry["yolo_ms"] = perception.get("_yolo_ms", 0)
            people = perception.get("people", {})
            entry["person_detected"] = people.get("detected", False)
            entry["person_count"] = people.get("count", 0)
            entry["person_distance_cm"] = people.get("nearest_distance_cm", 0)
        if perception.get("_local"):
            entry["local_perception"] = True
            entry["total_perception_ms"] = perception.get("_total_ms", 0)

        # Add scene description
        entry["scene"] = step_result.get("decision", {}).get("scene", "")[:200]

        try:
            with open(log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.debug(f"Nav experience log failed: {e}")

    def get_experience_stats(self) -> Dict[str, Any]:
        """Get stats on collected navigation experience data."""
        log_dir = Path.home() / ".repryntt" / "data" / "nav_experience"
        if not log_dir.exists():
            return {"total_experiences": 0, "days": 0, "files": []}
        
        files = sorted(log_dir.glob("*.jsonl"))
        total = 0
        for f in files:
            try:
                total += sum(1 for _ in open(f))
            except Exception:
                pass
        
        return {
            "total_experiences": total,
            "days": len(files),
            "log_dir": str(log_dir),
            "files": [f.name for f in files[-5:]],  # last 5 days
        }

    def status(self) -> Dict[str, Any]:
        """Get cortex status — mode, Q-table, spatial memory, stereo."""
        return {
            "mode": self.mode,
            "stereo_enabled": self.use_stereo,
            "stereo_matcher": self._stereo_matcher is not None,
            "q_table_loaded": self.q_table is not None,
            "q_table_states": len(self.q_table) if self.q_table else 0,
            "q_confidence_threshold": self.q_confidence_threshold,
            "spatial_memory": self.spatial_memory.summary(),
            "last_capture": self._last_capture_path,
            "last_depth": {
                "left": self._last_depth.left_proximity,
                "center": self._last_depth.center_proximity,
                "right": self._last_depth.right_proximity,
                "min_distance_cm": self._last_depth.min_distance_cm,
            } if self._last_depth else None,
            "experience": self.get_experience_stats(),
        }


# ── Singleton ────────────────────────────────────────────────────────

_nav_cortex: Optional[NavCortex] = None


def get_nav_cortex(brain_path: Optional[str] = None) -> NavCortex:
    """Get or create the global NavCortex instance."""
    global _nav_cortex
    if _nav_cortex is None:
        _nav_cortex = NavCortex(brain_path)
        _nav_cortex.load_q_table()  # try to load saved policy
    return _nav_cortex
