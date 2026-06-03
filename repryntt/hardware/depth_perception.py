"""
repryntt.hardware.depth_perception — Monocular Depth Estimation via Depth Anything v2.

Replaces the broken stereo depth with a neural depth estimator that works
from a SINGLE camera. No calibration, no stereo matching, no baseline errors.

This is what Tesla does — monocular depth networks trained on massive datasets
produce better depth maps than cheap stereo rigs. Boston Dynamics uses hardware
stereo + ToF, but we can't afford that. This is the next best thing.

Architecture:
    Camera frame (640x480 or 1280x720)
      → Depth Anything v2 Small (24.8M params, FP16)
      → Dense depth map (H×W float, relative depth)
      → Zone-based proximity extraction (left/center/right)
      → Per-detection depth lookup (YOLO bbox → median depth)

Performance on Jetson Orin Nano:
    Full res (518×924):  ~91ms
    Half res (252×448):  ~32ms
    GPU memory:          ~60MB

The depth values are RELATIVE (not metric), but that's fine for navigation:
    - Closer objects → lower depth values
    - We normalize to 0-1 proximity (like stereo does)
    - For absolute distance, we'd need metric fine-tuning or a scale reference
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────

MODEL_ID = "depth-anything/Depth-Anything-V2-Small-hf"
DEFAULT_INPUT_SIZE = (252, 448)  # half res for speed (~32ms)
FULL_INPUT_SIZE = (518, 924)     # full res for quality (~91ms)


class DepthEstimator:
    """Monocular depth estimation using Depth Anything v2.
    
    Produces dense per-pixel relative depth from a single camera image.
    Lazy-loads the model on first use. Runs on GPU with FP16.
    """

    def __init__(self, use_half_res: bool = True):
        self._model = None
        self._processor = None
        self._device = None
        self._load_attempted = False
        self._available = False
        self._use_half_res = use_half_res
        self._input_size = DEFAULT_INPUT_SIZE if use_half_res else FULL_INPUT_SIZE

    def _load(self) -> bool:
        """Lazy-load the depth model."""
        if self._load_attempted:
            return self._available
        self._load_attempted = True

        try:
            import torch
            from transformers import AutoModelForDepthEstimation, AutoImageProcessor

            self._device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

            self._processor = AutoImageProcessor.from_pretrained(
                MODEL_ID,
                size={"height": self._input_size[0], "width": self._input_size[1]},
                use_fast=False,
            )
            self._model = AutoModelForDepthEstimation.from_pretrained(MODEL_ID)

            if self._device.type == "cuda":
                self._model = self._model.half()

            self._model = self._model.to(self._device).eval()
            self._available = True

            params = sum(p.numel() for p in self._model.parameters()) / 1e6
            logger.info(f"🔭 Depth Anything v2 loaded: {params:.1f}M params, "
                        f"device={self._device}, input={self._input_size}")
            return True

        except ImportError as e:
            logger.warning(f"🔭 Depth model unavailable (missing dependency): {e}")
            return False
        except Exception as e:
            logger.error(f"🔭 Depth model load failed: {e}")
            return False

    @property
    def available(self) -> bool:
        if not self._load_attempted:
            self._load()
        return self._available

    def estimate_depth(self, frame: np.ndarray) -> Optional[DepthResult]:
        """Estimate depth from a BGR numpy frame.
        
        Returns a DepthResult with the depth map and zone-based proximity.
        """
        if not self._load():
            return None

        import torch
        from PIL import Image

        t0 = time.time()

        try:
            # Convert BGR (OpenCV) to RGB (PIL)
            if len(frame.shape) == 3 and frame.shape[2] == 3:
                rgb = frame[:, :, ::-1]
            else:
                rgb = frame
            pil_img = Image.fromarray(rgb)

            inputs = self._processor(images=pil_img, return_tensors="pt")
            inputs = {
                k: v.to(self._device).half()
                if v.dtype == torch.float32 and self._device.type == "cuda"
                else v.to(self._device)
                for k, v in inputs.items()
            }

            with torch.no_grad():
                outputs = self._model(**inputs)

            # Get depth map and convert to numpy
            depth_tensor = outputs.predicted_depth.squeeze(0)
            depth_map = depth_tensor.float().cpu().numpy()

            inference_ms = (time.time() - t0) * 1000

            return DepthResult(
                depth_map=depth_map,
                frame_shape=frame.shape[:2],
                inference_ms=round(inference_ms, 1),
            )

        except Exception as e:
            logger.error(f"Depth estimation failed: {e}")
            return None

    def estimate_depth_from_file(self, image_path: str) -> Optional['DepthResult']:
        """Estimate depth from an image file."""
        try:
            import cv2
            frame = cv2.imread(image_path)
            if frame is None:
                return None
            return self.estimate_depth(frame)
        except Exception as e:
            logger.error(f"Depth estimation from file failed: {e}")
            return None


class DepthResult:
    """Results from monocular depth estimation."""

    def __init__(self, depth_map: np.ndarray, frame_shape: Tuple[int, int],
                 inference_ms: float = 0.0):
        self.depth_map = depth_map  # H×W float, higher = farther
        self.frame_shape = frame_shape  # original frame (h, w)
        self.inference_ms = inference_ms

        # Normalize depth to 0-1 (0=closest, 1=farthest)
        d_min, d_max = depth_map.min(), depth_map.max()
        if d_max - d_min > 1e-6:
            self._normalized = (depth_map - d_min) / (d_max - d_min)
        else:
            self._normalized = np.zeros_like(depth_map)

        # Invert to proximity (0=far, 1=close) — matches stereo convention
        self._proximity = 1.0 - self._normalized

    @property
    def zone_proximity(self) -> Dict[str, float]:
        """Average proximity per zone (left/center/right thirds).
        
        Returns dict matching StereoDepth convention:
        0=clear, 1=very close obstacle.
        """
        h, w = self._proximity.shape
        third = w // 3
        return {
            "left": float(np.mean(self._proximity[:, :third])),
            "center": float(np.mean(self._proximity[:, third:2*third])),
            "right": float(np.mean(self._proximity[:, 2*third:])),
        }

    @property
    def bottom_zone_proximity(self) -> Dict[str, float]:
        """Proximity from the BOTTOM HALF only (floor-level obstacles).
        
        The top of the frame shows walls/ceiling/background — not relevant
        for driving. Bottom half is where ground-level obstacles live.
        This is what Tesla's occupancy network focuses on.
        """
        h, w = self._proximity.shape
        bottom = self._proximity[h//2:, :]
        third = w // 3
        return {
            "left": float(np.mean(bottom[:, :third])),
            "center": float(np.mean(bottom[:, third:2*third])),
            "right": float(np.mean(bottom[:, 2*third:])),
        }

    @property
    def min_proximity_zone(self) -> str:
        """Which zone has the LEAST obstruction (best direction to go)."""
        bz = self.bottom_zone_proximity
        return min(bz, key=bz.get)

    def depth_at_bbox(self, x1: float, y1: float, x2: float, y2: float) -> float:
        """Get median depth within a bounding box (from YOLO detection).
        
        Used to estimate distance to a detected object.
        Returns raw depth value (higher = farther).
        """
        h, w = self.depth_map.shape
        # Scale bbox from original frame coords to depth map coords
        scale_y = h / self.frame_shape[0]
        scale_x = w / self.frame_shape[1]

        y1_s = max(0, int(y1 * scale_y))
        y2_s = min(h, int(y2 * scale_y))
        x1_s = max(0, int(x1 * scale_x))
        x2_s = min(w, int(x2 * scale_x))

        if y2_s <= y1_s or x2_s <= x1_s:
            return 0.0

        roi = self.depth_map[y1_s:y2_s, x1_s:x2_s]
        return float(np.median(roi))

    def proximity_at_bbox(self, x1: float, y1: float, x2: float, y2: float) -> float:
        """Get proximity (0=far, 1=close) within a bounding box."""
        h, w = self._proximity.shape
        scale_y = h / self.frame_shape[0]
        scale_x = w / self.frame_shape[1]

        y1_s = max(0, int(y1 * scale_y))
        y2_s = min(h, int(y2 * scale_y))
        x1_s = max(0, int(x1 * scale_x))
        x2_s = min(w, int(x2 * scale_x))

        if y2_s <= y1_s or x2_s <= x1_s:
            return 0.0

        roi = self._proximity[y1_s:y2_s, x1_s:x2_s]
        return float(np.median(roi))

    def to_stereo_depth(self) -> 'StereoDepthCompat':
        """Convert to StereoDepth-compatible format.
        
        This lets us drop neural depth into everywhere stereo was used,
        with zero downstream code changes.
        """
        bz = self.bottom_zone_proximity
        # Estimate min distance in cm from proximity
        # This is rough — monocular depth is relative, not metric
        # But we can calibrate: proximity 0.8 ≈ 20cm, 0.5 ≈ 100cm, 0.2 ≈ 300cm
        max_prox = max(bz.values())
        if max_prox > 0.8:
            min_cm = 20.0
        elif max_prox > 0.6:
            min_cm = 50.0
        elif max_prox > 0.4:
            min_cm = 100.0
        elif max_prox > 0.2:
            min_cm = 200.0
        else:
            min_cm = 400.0

        return StereoDepthCompat(
            left_proximity=round(bz["left"], 3),
            center_proximity=round(bz["center"], 3),
            right_proximity=round(bz["right"], 3),
            min_distance_cm=min_cm,
            inference_ms=self.inference_ms,
        )

    def save_visualization(self, path: str) -> str:
        """Save a colorized depth map for debugging/logging."""
        try:
            import cv2
            # Normalize to 0-255 for visualization
            norm = (self._normalized * 255).astype(np.uint8)
            colored = cv2.applyColorMap(norm, cv2.COLORMAP_INFERNO)
            cv2.imwrite(path, colored)
            return path
        except Exception:
            return ""


class StereoDepthCompat:
    """Drop-in replacement for nav_cortex.StereoDepth from neural depth."""
    def __init__(self, left_proximity: float, center_proximity: float,
                 right_proximity: float, min_distance_cm: float,
                 inference_ms: float = 0.0):
        self.left_proximity = left_proximity
        self.center_proximity = center_proximity
        self.right_proximity = right_proximity
        self.left = left_proximity
        self.center = center_proximity
        self.right = right_proximity
        self.min_distance_cm = min_distance_cm
        self.compute_time_ms = inference_ms
        self.disparity_map_path = ""
        self.valid = True


# ── Person Following Controller ──────────────────────────────────────
# This is the "drive toward the human" logic.
# Tesla FSD handles this via their planner; BD Spot has "follow me" mode.
# We implement a simple proportional controller that steers toward
# the detected person's position and adjusts speed by their depth.

class PersonFollower:
    """Drives toward detected people using YOLO position + depth distance.
    
    This is the production behavior that makes the robot demo-worthy:
    1. YOLO detects person with bbox center_x
    2. Depth Anything gives distance to person
    3. Controller outputs: steer angle + speed
    
    Think of it like Tesla's "follow car" or Spot's "follow me" —
    but simpler since we only have 5 discrete actions.
    """

    # Proxemics zones (Hall 1966) — how close is appropriate
    INTIMATE = 45     # cm — too close, back up
    PERSONAL = 120    # cm — ideal for interaction
    SOCIAL = 360      # cm — approach zone
    PUBLIC = 760      # cm — too far to interact

    def __init__(self):
        self._tracking = False
        self._lost_frames = 0
        self._max_lost = 5  # frames without person before giving up

    def compute_action(self, person_center_x: float,
                       person_proximity: float,
                       person_area: float) -> Dict[str, Any]:
        """Compute motor action to approach/maintain distance to person.
        
        Args:
            person_center_x: normalized 0-1 (0=left, 0.5=center, 1=right)
            person_proximity: 0=far, 1=very close (from depth or area)
            person_area: bbox area fraction (backup distance estimate)
            
        Returns:
            {"action": str, "action_id": int, "reason": str, "speed": float}
        """
        self._tracking = True
        self._lost_frames = 0

        # Estimate distance zone from proximity
        if person_proximity > 0.75 or person_area > 0.25:
            zone = "intimate"
        elif person_proximity > 0.55 or person_area > 0.10:
            zone = "personal"
        elif person_proximity > 0.35 or person_area > 0.03:
            zone = "social"
        else:
            zone = "public"

        # Steering: where is the person in the frame?
        # center_x 0-0.33 = left, 0.33-0.67 = center, 0.67-1.0 = right
        if person_center_x < 0.30:
            steer = "left"
        elif person_center_x > 0.70:
            steer = "right"
        else:
            steer = "center"

        # Decision matrix
        if zone == "intimate":
            # Too close — hold position or back up
            return {
                "action": "stop",
                "action_id": 4,
                "reason": f"person very close (prox={person_proximity:.2f}), holding position",
                "speed": 0.0,
                "zone": zone,
            }

        elif zone == "personal":
            # Good distance for interaction — face the person
            if steer == "left":
                return {
                    "action": "turn_left",
                    "action_id": 2,
                    "reason": f"facing person on left at personal distance",
                    "speed": 0.15,
                    "zone": zone,
                }
            elif steer == "right":
                return {
                    "action": "turn_right",
                    "action_id": 3,
                    "reason": f"facing person on right at personal distance",
                    "speed": 0.15,
                    "zone": zone,
                }
            else:
                return {
                    "action": "stop",
                    "action_id": 4,
                    "reason": f"person centered at good distance, holding",
                    "speed": 0.0,
                    "zone": zone,
                }

        elif zone == "social":
            # Approachable — drive toward them
            if steer == "left":
                return {
                    "action": "turn_left",
                    "action_id": 2,
                    "reason": f"approaching person on left",
                    "speed": 0.2,
                    "zone": zone,
                }
            elif steer == "right":
                return {
                    "action": "turn_right",
                    "action_id": 3,
                    "reason": f"approaching person on right",
                    "speed": 0.2,
                    "zone": zone,
                }
            else:
                return {
                    "action": "forward",
                    "action_id": 0,
                    "reason": f"approaching person ahead (social distance)",
                    "speed": 0.25,
                    "zone": zone,
                }

        else:  # public
            # Far away — drive toward them faster
            if steer == "left":
                return {
                    "action": "turn_left",
                    "action_id": 2,
                    "reason": f"turning toward distant person on left",
                    "speed": 0.25,
                    "zone": zone,
                }
            elif steer == "right":
                return {
                    "action": "turn_right",
                    "action_id": 3,
                    "reason": f"turning toward distant person on right",
                    "speed": 0.25,
                    "zone": zone,
                }
            else:
                return {
                    "action": "forward",
                    "action_id": 0,
                    "reason": f"driving toward distant person ahead",
                    "speed": 0.3,
                    "zone": zone,
                }

    def person_lost(self) -> Dict[str, Any]:
        """Called when no person detected this frame."""
        self._lost_frames += 1
        if self._lost_frames >= self._max_lost:
            self._tracking = False
            return {
                "action": "stop",
                "action_id": 4,
                "reason": f"person lost for {self._lost_frames} frames, stopping",
                "speed": 0.0,
                "tracking": False,
            }
        # Brief loss — keep going in last direction
        return {
            "action": "forward",
            "action_id": 0,
            "reason": f"person briefly lost ({self._lost_frames}/{self._max_lost}), continuing",
            "speed": 0.15,
            "tracking": True,
        }

    @property
    def is_tracking(self) -> bool:
        return self._tracking


# ── Fused Perception Pipeline ────────────────────────────────────────
# This is the Tesla-style approach: run YOLO + Depth in parallel,
# fuse the results, and produce a rich perception output.

class FusedPerception:
    """Combined YOLO detection + monocular depth — the full perception stack.
    
    What Tesla does:
        8 cameras → HydraNet (detection + depth + lanes + signs) → occupancy grid
    What we do:
        1 camera → YOLO (detection, 24ms) + Depth Anything (depth, 32ms) → fused perception
        
    Total: ~50-60ms per frame = 15-20 FPS — enough for a tank robot at 0.3 m/s.
    """

    def __init__(self):
        self._yolo = None
        self._depth = None
        self._follower = PersonFollower()
        self._init_attempted = False

    def _init(self):
        if self._init_attempted:
            return
        self._init_attempted = True

        try:
            from repryntt.hardware.yolo_perception import get_yolo_detector
            self._yolo = get_yolo_detector()
            if not self._yolo.available:
                self._yolo = None
        except Exception as e:
            logger.warning(f"YOLO not available for fused perception: {e}")

        try:
            self._depth = get_depth_estimator()
            if not self._depth.available:
                self._depth = None
        except Exception as e:
            logger.warning(f"Depth not available for fused perception: {e}")

    def perceive(self, frame: np.ndarray) -> Dict[str, Any]:
        """Full perception pipeline on a BGR frame.
        
        Returns a rich perception dict that replaces Gemini's nav perception.
        """
        self._init()
        t0 = time.time()

        result = {
            "obstacles": {"left": 0.5, "center": 0.5, "right": 0.5, "above": 0.0},
            "floor": {"visible": True, "traversable": True, "surface": "unknown"},
            "path": {"best_direction": "stop", "confidence": 0.3, "reason": "no perception available"},
            "scene": "",
            "people": {"detected": False, "count": 0, "nearest_position": "none",
                       "nearest_distance_cm": 0, "description": ""},
            "distance_to_nearest_obstacle_cm": 200,
            "_local": True,
            "_yolo": False,
            "_depth": False,
        }

        # Run YOLO detection
        yolo_result = None
        if self._yolo is not None:
            yolo_result = self._yolo.detect_frame(frame)
            if yolo_result:
                result["_yolo"] = True
                result["_yolo_ms"] = yolo_result.inference_ms

                # Use YOLO perception as base
                nav = yolo_result.to_nav_perception()
                result["scene"] = nav["scene"]
                result["people"] = nav["people"]
                result["obstacles"] = nav["obstacles"]
                result["path"] = nav["path"]
                result["distance_to_nearest_obstacle_cm"] = nav["distance_to_nearest_obstacle_cm"]

        # Run depth estimation
        depth_result = None
        if self._depth is not None:
            depth_result = self._depth.estimate_depth(frame)
            if depth_result:
                result["_depth"] = True
                result["_depth_ms"] = depth_result.inference_ms

                # Override obstacle zones with neural depth (more accurate than YOLO bbox area)
                bz = depth_result.bottom_zone_proximity
                result["obstacles"]["left"] = round(bz["left"], 3)
                result["obstacles"]["center"] = round(bz["center"], 3)
                result["obstacles"]["right"] = round(bz["right"], 3)

                # Update best direction from depth
                best = depth_result.min_proximity_zone
                result["path"]["best_direction"] = best if best != "center" else "forward"
                result["path"]["confidence"] = 0.75
                result["path"]["reason"] = f"neural depth: least obstruction {best}"

                # Publish /scan to ROS2 (no-op if Nav2 not running)
                try:
                    from repryntt.hardware.ros2_publisher import publish_scan
                    publish_scan(depth_result._proximity)
                except Exception:
                    pass

                # Get per-person depth if YOLO detected people
                if yolo_result and yolo_result.people:
                    for person in yolo_result.people:
                        prox = depth_result.proximity_at_bbox(*person.bbox)
                        person_depth_raw = depth_result.depth_at_bbox(*person.bbox)

                        # Better distance estimate from depth model
                        if prox > 0.75:
                            est_cm = 40
                        elif prox > 0.55:
                            est_cm = 100
                        elif prox > 0.35:
                            est_cm = 250
                        else:
                            est_cm = 450

                        result["people"]["nearest_distance_cm"] = est_cm
                        result["people"]["_depth_proximity"] = round(prox, 3)
                        result["people"]["_depth_raw"] = round(person_depth_raw, 3)

                # Compute stereo-compatible output
                compat = depth_result.to_stereo_depth()
                result["_stereo_compat"] = {
                    "left": compat.left_proximity,
                    "center": compat.center_proximity,
                    "right": compat.right_proximity,
                    "min_distance_cm": compat.min_distance_cm,
                }
                result["distance_to_nearest_obstacle_cm"] = compat.min_distance_cm

        # Person-following decision
        if yolo_result and yolo_result.people:
            nearest = yolo_result.nearest_person
            person_prox = 0.5
            if depth_result and nearest:
                person_prox = depth_result.proximity_at_bbox(*nearest.bbox)
            follow = self._follower.compute_action(
                person_center_x=nearest.center_x,
                person_proximity=person_prox,
                person_area=nearest.area_fraction,
            )
            result["_follow"] = follow
        else:
            lost = self._follower.person_lost()
            result["_follow"] = lost

        result["_total_ms"] = round((time.time() - t0) * 1000, 1)
        return result

    def get_stereo_depth(self, frame: np.ndarray) -> Optional[StereoDepthCompat]:
        """Get StereoDepth-compatible output from neural depth.
        
        Drop-in replacement for the broken stereo pipeline.
        """
        self._init()
        if self._depth is None:
            return None
        depth_result = self._depth.estimate_depth(frame)
        if depth_result is None:
            return None
        return depth_result.to_stereo_depth()

    @property
    def follower(self) -> PersonFollower:
        return self._follower


# ── Singletons ────────────────────────────────────────────────────────

_depth_estimator: Optional[DepthEstimator] = None
_fused: Optional[FusedPerception] = None


def get_depth_estimator() -> DepthEstimator:
    global _depth_estimator
    if _depth_estimator is None:
        _depth_estimator = DepthEstimator(use_half_res=True)
    return _depth_estimator


def get_fused_perception() -> FusedPerception:
    global _fused
    if _fused is None:
        _fused = FusedPerception()
    return _fused
