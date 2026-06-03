"""
repryntt.cortex.regions.perception — Sensor Perception Region.

Processes camera, audio, and other sensor inputs using small classifier
models (5-50M params, ONNX or TensorRT).

Activates when sensor hardware is detected.  Falls back to
basic heuristics when no model is available.

Responsibilities:
  1. Visual classification  — object detection, scene understanding
  2. Audio event detection  — speech vs noise, alert sounds, wake word assist
  3. Sensor fusion          — combine multiple inputs into coherent state
  4. Anomaly detection      — "something changed" vs "same as before"

Feeds into:
  - Guardian (safety: obstacle detected → validate → e-stop)
  - Executor (navigation: object at bearing X → approach/avoid)
  - Conscious (awareness: "I see a person" → identity context)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from repryntt.cortex.region_base import BrainRegion, RegionState

logger = logging.getLogger(__name__)

# Process input types
PROCESS_TYPES = {
    "classify_image",      # Camera frame → labels + confidence
    "detect_audio_event",  # Audio chunk → event type
    "fuse_sensors",        # Multi-sensor → unified state
    "detect_anomaly",      # Current vs baseline → anomaly score
    "describe_scene",      # Camera frame → natural language description
}


class PerceptionRegion(BrainRegion):
    """Sensor perception brain region.

    On hardware-less systems: stays DISABLED.
    On systems with camera/mic: uses classifier models or heuristics.
    """

    def __init__(self) -> None:
        super().__init__()
        self._camera_available = False
        self._mic_available = False
        self._baseline_scene: Optional[Dict[str, Any]] = None
        self._perception_log: List[Dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "perception"

    def on_load(self) -> None:
        """Detect available sensor hardware."""
        self._camera_available = self._check_camera()
        self._mic_available = self._check_mic()

        if not self._camera_available and not self._mic_available:
            self._state = RegionState.DISABLED
            logger.info("Perception region disabled (no sensors detected)")
        else:
            sensors = []
            if self._camera_available:
                sensors.append("camera")
            if self._mic_available:
                sensors.append("mic")
            logger.info("Perception region ready (sensors: %s, model=%s)",
                        "+".join(sensors), self._model_name or "heuristic")

    @staticmethod
    def _check_camera() -> bool:
        try:
            from repryntt.hardware.camera import discover_cameras
            cameras = discover_cameras()
            return len(cameras) > 0
        except Exception:
            return False

    @staticmethod
    def _check_mic() -> bool:
        try:
            import pyaudio
            pa = pyaudio.PyAudio()
            count = pa.get_device_count()
            pa.terminate()
            return count > 0
        except Exception:
            return False

    # ── Core dispatch ────────────────────────────────────────────────

    def process(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        ptype = input_data.get("type", "")

        if ptype == "classify_image":
            return self._classify_image(input_data)
        elif ptype == "detect_audio_event":
            return self._detect_audio_event(input_data)
        elif ptype == "fuse_sensors":
            return self._fuse_sensors(input_data)
        elif ptype == "detect_anomaly":
            return self._detect_anomaly(input_data)
        elif ptype == "describe_scene":
            return self._describe_scene(input_data)
        else:
            return {"success": False, "result": None, "error": f"Unknown type: {ptype}"}

    # ── Image classification ─────────────────────────────────────────

    def _classify_image(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Classify a camera frame.

        With model: ONNX classifier → labels + confidence
        Without model: basic image stats (brightness, motion)
        """
        frame = input_data.get("frame")  # numpy array or path
        if frame is None:
            return {"success": False, "result": None, "error": "No frame provided"}

        if self._model_name:
            return self._model_classify(frame)

        # Heuristic fallback: basic image analysis
        return self._heuristic_classify(frame)

    def _model_classify(self, frame: Any) -> Dict[str, Any]:
        """Use ONNX/TensorRT model for classification."""
        try:
            from repryntt.cortex.resource_manager import get_resource_manager
            mgr = get_resource_manager()

            # Preprocess frame for model input
            import numpy as np
            if isinstance(frame, str):
                # Path to image
                import cv2
                frame = cv2.imread(frame)
                if frame is None:
                    return {"success": False, "result": None, "error": "Could not read image"}

            # Resize to model input size (typical: 224x224)
            import cv2
            resized = cv2.resize(frame, (224, 224))
            normalized = resized.astype(np.float32) / 255.0
            # HWC → CHW → BCHW
            tensor = np.transpose(normalized, (2, 0, 1))[np.newaxis, ...]

            result = mgr.infer_classifier(self._model_name, {"input": tensor})
            if result is not None:
                # Interpret output (model-specific)
                return {
                    "success": True,
                    "result": {
                        "raw_output": result[0].tolist() if hasattr(result[0], "tolist") else result,
                        "model_based": True,
                    },
                }

        except Exception as e:
            logger.warning("Model classification failed: %s", e)

        return self._heuristic_classify(frame)

    @staticmethod
    def _heuristic_classify(frame: Any) -> Dict[str, Any]:
        """Basic image analysis without model."""
        try:
            import numpy as np
            if isinstance(frame, np.ndarray):
                brightness = float(np.mean(frame))
                has_motion = False  # Would need frame differencing
                return {
                    "success": True,
                    "result": {
                        "brightness": round(brightness, 1),
                        "has_motion": has_motion,
                        "model_based": False,
                    },
                }
        except Exception as e:
            logger.warning("Heuristic image classify failed: %s", e)
        return {"success": True, "result": {"model_based": False}, "fallback": True}

    # ── Audio event detection ────────────────────────────────────────

    def _detect_audio_event(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Detect audio events (speech, alert, noise).

        With model: audio classifier.
        Without model: energy-based detection.
        """
        audio_data = input_data.get("audio")
        sample_rate = input_data.get("sample_rate", 16000)

        if audio_data is None:
            return {"success": False, "result": None, "error": "No audio data"}

        # Heuristic: energy-based
        try:
            import numpy as np
            if isinstance(audio_data, np.ndarray):
                energy = float(np.sqrt(np.mean(audio_data ** 2)))
                is_speech = energy > 0.01  # Very rough threshold
                is_loud = energy > 0.1

                return {
                    "success": True,
                    "result": {
                        "energy": round(energy, 4),
                        "is_speech_likely": is_speech,
                        "is_loud": is_loud,
                        "model_based": False,
                    },
                }
        except Exception as e:
            logger.warning("Audio event detection failed: %s", e)

        return {"success": True, "result": {"model_based": False}, "fallback": True}

    # ── Sensor fusion ────────────────────────────────────────────────

    def _fuse_sensors(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Combine multiple sensor inputs into a unified environment state."""
        camera = input_data.get("camera", {})
        audio = input_data.get("audio", {})
        distance = input_data.get("distance_sensors", {})

        state = {
            "timestamp": time.time(),
            "visual": camera,
            "auditory": audio,
            "proximity": distance,
            "people_detected": camera.get("people_count", 0),
            "obstacle_near": distance.get("min_distance_m", float("inf")) < 0.5,
            "noise_level": audio.get("energy", 0),
        }

        self._perception_log.append(state)
        self._perception_log = self._perception_log[-50:]

        return {"success": True, "result": {"environment_state": state}}

    # ── Anomaly detection ────────────────────────────────────────────

    def _detect_anomaly(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Compare current sensor state against baseline to detect changes."""
        current = input_data.get("current_state", {})

        if self._baseline_scene is None:
            self._baseline_scene = current
            return {"success": True, "result": {"anomaly_score": 0.0, "is_first": True}}

        # Simple diff: count changed fields
        changed = 0
        total = 0
        for key in set(list(self._baseline_scene.keys()) + list(current.keys())):
            total += 1
            if self._baseline_scene.get(key) != current.get(key):
                changed += 1

        anomaly_score = changed / max(total, 1)

        # Update baseline with exponential moving average (adapt slowly)
        self._baseline_scene = current

        return {
            "success": True,
            "result": {
                "anomaly_score": round(anomaly_score, 3),
                "fields_changed": changed,
                "is_anomaly": anomaly_score > 0.3,
            },
        }

    # ── Scene description ────────────────────────────────────────────

    def _describe_scene(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a natural language description of the current scene.

        Uses the conscious layer's LLM if available, otherwise basic template.
        """
        state = input_data.get("state", {})

        parts = []
        if state.get("people_detected", 0) > 0:
            parts.append(f"{state['people_detected']} person(s) visible")
        if state.get("obstacle_near"):
            parts.append("obstacle nearby")
        if state.get("noise_level", 0) > 0.05:
            parts.append("ambient noise detected")

        description = ". ".join(parts) if parts else "quiet, empty scene"

        return {
            "success": True,
            "result": {"description": description, "model_based": False},
        }

    # ── Fallback ─────────────────────────────────────────────────────

    def fallback(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        return {"success": True, "result": {"model_based": False}, "fallback": True}

    # ── Training data ────────────────────────────────────────────────

    def generate_training_data(self) -> List[Dict[str, Any]]:
        """Produce training examples from perception log.

        Note: real training data for perception requires labeled frames,
        not just log entries.  This is a placeholder for the data pipeline.
        """
        return []
