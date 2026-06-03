"""
repryntt.hardware.yolo_perception — YOLOv8-nano Object Detection Layer.

Real-time local object detection at 15-30 FPS on Jetson Orin Nano.
No API calls, no network, no LLM. Just a 6MB TensorRT model.

Replaces the 3-5s Gemini API call for basic perception:
- Person detection (bounding box, confidence, position)
- Obstacle classification (furniture, walls, doors)
- Object inventory per frame

This feeds the MLP driver policy with a rich feature vector
instead of the lossy 11-dim Q-table observation.

Architecture:
    Camera frame (640x480)
      → YOLOv8-nano TensorRT (FP16, ~30ms)
      → Detections: [{class, bbox, confidence, position, distance_est}]
      → Feature vector (50-dim) for driver policy
      → Structured perception dict for social behavior

COCO classes we care about for indoor nav:
    0: person, 56: chair, 57: couch, 58: potted plant, 59: bed,
    60: dining table, 62: tv, 63: laptop, 72: refrigerator,
    73: book, 74: clock, 75: vase
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ── COCO class names (80 classes) ─────────────────────────────────────

COCO_NAMES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep",
    "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
    "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
    "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork",
    "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv",
    "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
    "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush",
]

# Classes relevant for indoor navigation
INDOOR_NAV_CLASSES = {
    0: "person",
    56: "chair", 57: "couch", 58: "potted plant", 59: "bed",
    60: "dining table", 62: "tv", 63: "laptop",
    72: "refrigerator", 74: "clock",
    # Other useful ones
    13: "bench", 39: "bottle", 41: "cup",
    61: "toilet", 70: "sink",
    # Animals (pets)
    15: "cat", 16: "dog",
}

# Obstacle classes — things the robot shouldn't drive into
OBSTACLE_CLASSES = {56, 57, 58, 59, 60, 62, 63, 72, 13}

# Person class
PERSON_CLASS = 0

# Feature vector zones: left third, center third, right third of frame
ZONE_LEFT = 0
ZONE_CENTER = 1
ZONE_RIGHT = 2


@dataclass
class Detection:
    """Single YOLO detection."""
    class_id: int
    class_name: str
    confidence: float
    bbox: Tuple[float, float, float, float]  # x1, y1, x2, y2 (pixels)
    center_x: float  # normalized 0-1
    center_y: float  # normalized 0-1
    area_fraction: float  # bbox area / frame area (proxy for distance)
    zone: int  # 0=left, 1=center, 2=right


@dataclass
class YoloPerception:
    """Structured perception from a single YOLO inference."""
    detections: List[Detection] = field(default_factory=list)
    people: List[Detection] = field(default_factory=list)
    obstacles: List[Detection] = field(default_factory=list)
    inference_ms: float = 0.0
    frame_shape: Tuple[int, int] = (0, 0)  # h, w

    @property
    def has_person(self) -> bool:
        return len(self.people) > 0

    @property
    def person_count(self) -> int:
        return len(self.people)

    @property
    def nearest_person(self) -> Optional[Detection]:
        """Person with largest bounding box (closest)."""
        if not self.people:
            return None
        return max(self.people, key=lambda d: d.area_fraction)

    @property
    def obstacle_zones(self) -> Dict[str, float]:
        """Obstacle density per zone (0=clear, 1=blocked).
        
        Based on total obstacle bbox area in each third of the frame.
        This is what the driver policy uses instead of stereo.
        """
        zone_areas = [0.0, 0.0, 0.0]
        for det in self.obstacles:
            zone_areas[det.zone] += det.area_fraction
        # Clamp to 0-1
        return {
            "left": min(1.0, zone_areas[ZONE_LEFT] * 3),
            "center": min(1.0, zone_areas[ZONE_CENTER] * 3),
            "right": min(1.0, zone_areas[ZONE_RIGHT] * 3),
        }

    def to_nav_perception(self) -> Dict[str, Any]:
        """Convert to the same format as Gemini's nav perception dict.
        
        This lets us drop YOLO in wherever Gemini perception was used,
        without changing downstream code.
        """
        oz = self.obstacle_zones
        nearest = self.nearest_person

        return {
            "obstacles": {
                "left": round(oz["left"], 3),
                "center": round(oz["center"], 3),
                "right": round(oz["right"], 3),
                "above": 0.0,
            },
            "floor": {
                "visible": True,
                "traversable": True,
                "surface": "unknown",
            },
            "path": {
                "best_direction": self._best_direction(oz),
                "confidence": 0.7,
                "reason": f"YOLO: {len(self.detections)} objects detected",
            },
            "scene": self._scene_description(),
            "people": {
                "detected": self.has_person,
                "count": self.person_count,
                "nearest_position": (
                    ["left", "center", "right"][nearest.zone]
                    if nearest else "none"
                ),
                "nearest_distance_cm": (
                    self._estimate_person_distance(nearest)
                    if nearest else 0
                ),
                "description": (
                    f"{nearest.class_name} (conf={nearest.confidence:.2f})"
                    if nearest else ""
                ),
            },
            "distance_to_nearest_obstacle_cm": self._nearest_obstacle_distance(),
            "_yolo": True,  # flag so downstream knows this is YOLO, not Gemini
            "_inference_ms": self.inference_ms,
        }

    def _best_direction(self, oz: Dict[str, float]) -> str:
        """Simple direction from obstacle zones."""
        if oz["center"] < 0.3:
            return "forward"
        if oz["left"] < oz["right"]:
            return "left"
        if oz["right"] < oz["left"]:
            return "right"
        return "backward"

    def _scene_description(self) -> str:
        """Generate a human-readable scene description from detections."""
        if not self.detections:
            return "Empty scene, no objects detected"
        names = [d.class_name for d in self.detections[:8]]
        unique = list(dict.fromkeys(names))  # dedupe preserving order
        return f"Indoor scene with: {', '.join(unique)}"

    def _estimate_person_distance(self, det: Detection) -> float:
        """Rough distance from bbox area. Bigger bbox = closer."""
        if det.area_fraction > 0.3:
            return 50   # very close
        elif det.area_fraction > 0.15:
            return 100
        elif det.area_fraction > 0.05:
            return 200
        elif det.area_fraction > 0.02:
            return 350
        return 500

    def _nearest_obstacle_distance(self) -> float:
        """Estimate distance to nearest obstacle from bbox area."""
        if not self.obstacles:
            return 500
        biggest = max(self.obstacles, key=lambda d: d.area_fraction)
        if biggest.area_fraction > 0.3:
            return 20
        elif biggest.area_fraction > 0.15:
            return 50
        elif biggest.area_fraction > 0.05:
            return 100
        return 200


class YoloDetector:
    """YOLOv8-nano detector with TensorRT acceleration.
    
    Lazy-loads the model on first inference to avoid startup cost.
    Falls back to PyTorch if TensorRT engine not available.
    """

    def __init__(self, engine_path: Optional[str] = None,
                 conf_threshold: float = 0.35,
                 iou_threshold: float = 0.45):
        self._model = None
        self._engine_path = engine_path
        self._conf_threshold = conf_threshold
        self._iou_threshold = iou_threshold
        self._load_attempted = False
        self._available = False

    def _load_model(self) -> bool:
        """Lazy-load YOLO model. Tries TensorRT engine first, then PyTorch."""
        if self._load_attempted:
            return self._available
        self._load_attempted = True

        try:
            from ultralytics import YOLO

            # Try TensorRT engine first (fastest)
            if self._engine_path:
                engine = Path(self._engine_path)
            else:
                # Check standard locations
                candidates = [
                    Path.home() / "repryntt" / "yolov8n.engine",
                    Path.home() / ".repryntt" / "models" / "yolov8n.engine",
                    Path.home() / "yolov8n.engine",
                    Path("yolov8n.engine"),
                ]
                engine = None
                for c in candidates:
                    if c.exists():
                        engine = c
                        break

            if engine and engine.exists():
                self._model = YOLO(str(engine), task="detect")
                logger.info(f"👁️ YOLO loaded: TensorRT engine ({engine}, "
                            f"{engine.stat().st_size / 1024 / 1024:.1f} MB)")
                self._available = True
                return True

            # Fall back to ONNX
            onnx_candidates = [
                Path.home() / "repryntt" / "yolov8n.onnx",
                Path.home() / ".repryntt" / "models" / "yolov8n.onnx",
            ]
            for onnx in onnx_candidates:
                if onnx.exists():
                    self._model = YOLO(str(onnx), task="detect")
                    logger.info(f"👁️ YOLO loaded: ONNX ({onnx})")
                    self._available = True
                    return True

            # Fall back to PyTorch (.pt) — slower but always works
            pt_candidates = [
                Path.home() / "repryntt" / "yolov8n.pt",
                Path.home() / ".repryntt" / "models" / "yolov8n.pt",
                Path("yolov8n.pt"),
            ]
            for pt in pt_candidates:
                if pt.exists():
                    self._model = YOLO(str(pt))
                    logger.info(f"👁️ YOLO loaded: PyTorch ({pt}) — consider exporting to TensorRT for 10x speed")
                    self._available = True
                    return True

            logger.warning("👁️ No YOLO model found — run: python3 -c \"from ultralytics import YOLO; "
                           "YOLO('yolov8n.pt').export(format='engine', half=True)\"")
            return False

        except ImportError:
            logger.warning("👁️ ultralytics not installed — YOLO perception unavailable")
            return False
        except Exception as e:
            logger.error(f"👁️ YOLO load failed: {e}")
            return False

    @property
    def available(self) -> bool:
        if not self._load_attempted:
            self._load_model()
        return self._available

    def detect_frame(self, frame: np.ndarray) -> Optional[YoloPerception]:
        """Run YOLO on a BGR numpy frame. Returns structured perception."""
        if not self._load_model():
            return None

        # If a previous inference disabled the model (e.g. torchvision
        # ABI mismatch), skip silently — the perception layer treats
        # missing YOLO as "no people detected" rather than crashing.
        if getattr(self, "_inference_broken", False):
            return None

        t0 = time.time()
        try:
            results = self._model(
                frame,
                conf=self._conf_threshold,
                iou=self._iou_threshold,
                verbose=False,
            )
        except Exception as e:
            # Log the first failure loudly, then disable to stop spamming
            # the daemon log every nav step. The most common cause is a
            # torchvision/torch ABI mismatch (custom CUDA wheel vs PyPI
            # torchvision). Reset _inference_broken once the wheel is
            # rebuilt and the daemon restarted.
            if not getattr(self, "_inference_broken", False):
                logger.error(
                    "YOLO inference failed (disabling for this run): %s", e,
                )
                logger.error(
                    "  → likely torch/torchvision ABI mismatch. Rebuild "
                    "torchvision against the custom torch CUDA wheel; do "
                    "NOT pip install torchvision (it pulls CPU torch and "
                    "clobbers the custom Jetson wheel)."
                )
                self._inference_broken = True
                self._available = False
            return None

        inference_ms = (time.time() - t0) * 1000
        h, w = frame.shape[:2]
        frame_area = h * w

        detections = []
        people = []
        obstacles = []

        if results and len(results) > 0:
            boxes = results[0].boxes
            if boxes is not None and len(boxes) > 0:
                for i in range(len(boxes)):
                    cls_id = int(boxes.cls[i].item())
                    conf = float(boxes.conf[i].item())
                    x1, y1, x2, y2 = boxes.xyxy[i].tolist()

                    cx = (x1 + x2) / 2 / w
                    cy = (y1 + y2) / 2 / h
                    area = (x2 - x1) * (y2 - y1) / frame_area

                    # Zone: left/center/right third
                    if cx < 0.33:
                        zone = ZONE_LEFT
                    elif cx < 0.67:
                        zone = ZONE_CENTER
                    else:
                        zone = ZONE_RIGHT

                    det = Detection(
                        class_id=cls_id,
                        class_name=COCO_NAMES[cls_id] if cls_id < len(COCO_NAMES) else f"class_{cls_id}",
                        confidence=round(conf, 3),
                        bbox=(x1, y1, x2, y2),
                        center_x=round(cx, 3),
                        center_y=round(cy, 3),
                        area_fraction=round(area, 4),
                        zone=zone,
                    )
                    detections.append(det)

                    if cls_id == PERSON_CLASS:
                        people.append(det)
                    if cls_id in OBSTACLE_CLASSES:
                        obstacles.append(det)

        return YoloPerception(
            detections=detections,
            people=people,
            obstacles=obstacles,
            inference_ms=round(inference_ms, 1),
            frame_shape=(h, w),
        )

    def detect_image(self, image_path: str) -> Optional[YoloPerception]:
        """Run YOLO on an image file."""
        try:
            import cv2
            frame = cv2.imread(image_path)
            if frame is None:
                logger.warning(f"Could not read image: {image_path}")
                return None
            return self.detect_frame(frame)
        except Exception as e:
            logger.error(f"YOLO image detection failed: {e}")
            return None

    def to_feature_vector(self, perception: YoloPerception) -> np.ndarray:
        """Convert YOLO perception to a fixed-size feature vector for the MLP policy.
        
        50-dim vector:
            [0:3]   — obstacle density per zone (left, center, right)
            [3:6]   — person presence per zone (0/1)
            [6:9]   — nearest obstacle area per zone (proxy for distance)
            [9:12]  — nearest person area per zone (proxy for distance)
            [12:15] — object count per zone
            [15]    — total person count (clamped 0-3)
            [16]    — total obstacle count (clamped 0-10)
            [17]    — total detection count (clamped 0-20)
            [18]    — has_person (0/1)
            [19]    — nearest person area fraction (0 if none)
            [20:28] — top-8 COCO class presence (person, chair, couch, table, tv, door, cat, dog)
            [28:31] — reserved for stereo depth (left, center, right proximity)
            [31:34] — reserved for temporal (last action one-hot: fwd/turn/back)
            [34:37] — reserved for goal (goal_dist, goal_angle, heading)
            [37:50] — zero padding for future features
        
        Total: 50 floats, all normalized 0-1 or -1 to 1.
        """
        vec = np.zeros(50, dtype=np.float32)

        oz = perception.obstacle_zones
        vec[0] = oz["left"]
        vec[1] = oz["center"]
        vec[2] = oz["right"]

        # Person presence per zone
        for p in perception.people:
            vec[3 + p.zone] = 1.0

        # Nearest obstacle area per zone
        zone_max_obs = [0.0, 0.0, 0.0]
        for o in perception.obstacles:
            zone_max_obs[o.zone] = max(zone_max_obs[o.zone], o.area_fraction)
        vec[6:9] = np.clip(zone_max_obs, 0, 1)

        # Nearest person area per zone
        zone_max_person = [0.0, 0.0, 0.0]
        for p in perception.people:
            zone_max_person[p.zone] = max(zone_max_person[p.zone], p.area_fraction)
        vec[9:12] = np.clip(zone_max_person, 0, 1)

        # Object count per zone
        zone_counts = [0, 0, 0]
        for d in perception.detections:
            zone_counts[d.zone] += 1
        vec[12] = min(zone_counts[0] / 10.0, 1.0)
        vec[13] = min(zone_counts[1] / 10.0, 1.0)
        vec[14] = min(zone_counts[2] / 10.0, 1.0)

        # Counts
        vec[15] = min(perception.person_count / 3.0, 1.0)
        vec[16] = min(len(perception.obstacles) / 10.0, 1.0)
        vec[17] = min(len(perception.detections) / 20.0, 1.0)
        vec[18] = 1.0 if perception.has_person else 0.0

        # Nearest person area
        np_det = perception.nearest_person
        vec[19] = np_det.area_fraction if np_det else 0.0

        # Top-8 class presence
        top_classes = [0, 56, 57, 60, 62, 61, 15, 16]  # person, chair, couch, table, tv, toilet, cat, dog
        present_classes = {d.class_id for d in perception.detections}
        for i, cls in enumerate(top_classes):
            vec[20 + i] = 1.0 if cls in present_classes else 0.0

        return vec

    def augment_feature_vector(self, vec: np.ndarray,
                               stereo_left: float = 0.0,
                               stereo_center: float = 0.0,
                               stereo_right: float = 0.0,
                               last_action: int = 4,
                               goal_dist: float = 0.5,
                               goal_angle: float = 0.0,
                               heading: float = 0.0) -> np.ndarray:
        """Add stereo, temporal, and goal features to the YOLO feature vector."""
        vec = vec.copy()
        vec[28] = stereo_left
        vec[29] = stereo_center
        vec[30] = stereo_right

        # Last action one-hot (fwd=0, turn=1, back=2)
        if last_action == 0:  # forward
            vec[31] = 1.0
        elif last_action in (2, 3):  # turn
            vec[32] = 1.0
        elif last_action == 1:  # backward
            vec[33] = 1.0

        vec[34] = np.clip(goal_dist, 0, 1)
        vec[35] = np.clip(goal_angle / 180.0, -1, 1)
        vec[36] = (heading % 360) / 360.0

        return vec


# ── Singleton ─────────────────────────────────────────────────────────

_detector: Optional[YoloDetector] = None


def get_yolo_detector() -> YoloDetector:
    """Get or create the singleton YOLO detector."""
    global _detector
    if _detector is None:
        _detector = YoloDetector()
    return _detector
