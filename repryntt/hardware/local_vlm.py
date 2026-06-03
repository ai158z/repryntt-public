"""
repryntt.hardware.local_vlm — On-Device Vision Language Model.

Runs a small VLM directly on the Jetson GPU for fast reflexive vision.
This is the "visual cortex" — processes raw camera frames into structured
perception data (obstacles, direction, scene type) in ~200-500ms instead
of 2-5s via remote API.

Architecture (mirrors biological vision):
    Retina (camera)      → raw pixels
    Visual cortex (this) → fast, local scene understanding
    Prefrontal (Gemini)  → deep analysis when needed
    Brain (Andrew/LLM)   → conscious interpretation + decisions

Tiered vision routing:
    Tier 1 — Local VLM (~200ms): every explorer step. Obstacles, direction,
             basic scene classification. Runs on device, no network.
    Tier 2 — Gemini Flash (~2s): every Nth step or on scene change. Rich
             scene description, person identification, spatial reasoning.
    Tier 3 — Gemini Pro (~3-5s): on-demand via nav_look. Deep analysis,
             landmark identification, multi-frame reasoning.

Model selection (Jetson Orin Nano Super, 7.4 GB shared memory):
    Primary:  SmolVLM-256M-Instruct (INT4, ~200MB GPU)
    Fallback: CPU-only inference if GPU memory is too tight
    Safety:   graceful degradation to API-only if model won't load

The model is loaded lazily on first use and can be unloaded to free
memory for other workloads (e.g., Depth Anything, YOLO).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

MODEL_ID = "HuggingFaceTB/SmolVLM-256M-Instruct"
MODEL_CACHE_DIR = str(Path.home() / ".repryntt" / "models" / "local_vlm")

NAV_PROMPT = (
    "You are a robot's visual cortex. Analyze this camera image and return "
    "ONLY a JSON object:\n"
    '{"obstacles":{"left":0.0-1.0,"center":0.0-1.0,"right":0.0-1.0},'
    '"path":{"best_direction":"forward/left/right/backward/stop",'
    '"confidence":0.0-1.0,"reason":"brief"},'
    '"scene":"one-line description",'
    '"scene_type":"hallway/room/doorway/outdoor/stairs/obstacle/unknown",'
    '"people_detected":false,'
    '"distance_to_nearest_obstacle_cm":0-500}\n'
    "Be precise. 0=clear, 1=blocked. Return ONLY JSON."
)

SCENE_CLASSIFY_PROMPT = (
    "Classify this image into exactly ONE category. Reply with ONLY the word:\n"
    "hallway, room, kitchen, bathroom, bedroom, living_room, garage, "
    "patio, yard, driveway, sidewalk, street, doorway, stairs, elevator, "
    "closet, storage, office, unknown"
)


class LocalVLM:
    """On-device vision language model for fast reflexive perception.

    Loads a small quantized VLM (SmolVLM-256M) on the Jetson GPU.
    Provides structured perception output matching nav_cortex format.

    Thread-safe: inference is serialized via a lock. The model is loaded
    lazily and can be explicitly unloaded to free GPU memory.
    """

    def __init__(self, model_id: str = MODEL_ID,
                 cache_dir: str = MODEL_CACHE_DIR):
        self._model_id = model_id
        self._cache_dir = cache_dir
        self._model = None
        self._processor = None
        self._device = None
        self._lock = threading.Lock()
        self._load_attempted = False
        self._load_error: Optional[str] = None
        self._inference_count = 0
        self._total_inference_ms = 0.0
        self._last_inference_ms = 0.0

    @property
    def available(self) -> bool:
        """True if the model is loaded and ready for inference."""
        return self._model is not None and self._processor is not None

    @property
    def stats(self) -> Dict[str, Any]:
        avg = (self._total_inference_ms / self._inference_count
               if self._inference_count > 0 else 0)
        return {
            "available": self.available,
            "model_id": self._model_id,
            "device": str(self._device) if self._device else None,
            "inference_count": self._inference_count,
            "avg_inference_ms": round(avg),
            "last_inference_ms": round(self._last_inference_ms),
            "load_error": self._load_error,
        }

    def load(self, force: bool = False) -> bool:
        """Load the VLM onto the GPU (or CPU fallback).

        Returns True if the model is ready for inference.
        Safe to call multiple times — no-ops if already loaded.
        """
        if self.available and not force:
            return True
        if self._load_attempted and not force:
            return False

        with self._lock:
            if self.available and not force:
                return True

            self._load_attempted = True
            self._load_error = None

            try:
                import torch
                from transformers import AutoProcessor, AutoModelForVision2Seq

                gpu_free = 0.0
                if torch.cuda.is_available():
                    gpu_free = torch.cuda.mem_get_info()[0] / 1024**3

                logger.info(
                    f"Loading local VLM: {self._model_id} "
                    f"(GPU free: {gpu_free:.1f} GB)"
                )

                os.makedirs(self._cache_dir, exist_ok=True)

                self._processor = AutoProcessor.from_pretrained(
                    self._model_id,
                    cache_dir=self._cache_dir,
                    trust_remote_code=True,
                )

                load_kwargs: Dict[str, Any] = {
                    "cache_dir": self._cache_dir,
                    "trust_remote_code": True,
                    "low_cpu_mem_usage": True,
                }

                if torch.cuda.is_available() and gpu_free >= 0.5:
                    try:
                        from transformers import BitsAndBytesConfig
                        load_kwargs["quantization_config"] = BitsAndBytesConfig(
                            load_in_4bit=True,
                            bnb_4bit_compute_dtype=torch.float16,
                            bnb_4bit_quant_type="nf4",
                        )
                        load_kwargs["device_map"] = "auto"
                        # Cap GPU reservation so accelerate doesn't grab 90%
                        # of shared Jetson memory. SmolVLM-256M INT4 needs
                        # ~200-300 MB; leave the rest for the OS, llama-server,
                        # repryntt-core, and Cursor.
                        gpu_budget_gb = min(1.5, gpu_free * 0.35)
                        load_kwargs["max_memory"] = {
                            0: f"{gpu_budget_gb:.1f}GiB",
                            "cpu": "512MiB",
                        }
                        self._device = "cuda (INT4)"
                    except ImportError:
                        load_kwargs["torch_dtype"] = torch.float16
                        load_kwargs["device_map"] = "auto"
                        self._device = "cuda (FP16)"
                else:
                    load_kwargs["torch_dtype"] = torch.float32
                    self._device = "cpu"
                    logger.warning(
                        f"GPU memory too low ({gpu_free:.1f} GB) — "
                        f"loading VLM on CPU (slower but functional)"
                    )

                self._model = AutoModelForVision2Seq.from_pretrained(
                    self._model_id, **load_kwargs
                )

                if hasattr(self._model, 'eval'):
                    self._model.eval()

                logger.info(
                    f"Local VLM loaded: {self._model_id} on {self._device}"
                )
                return True

            except Exception as e:
                self._load_error = str(e)[:300]
                self._model = None
                self._processor = None
                logger.error(f"Failed to load local VLM: {e}", exc_info=True)
                return False

    def unload(self) -> None:
        """Free GPU/CPU memory by unloading the model."""
        with self._lock:
            if self._model is not None:
                del self._model
                self._model = None
            if self._processor is not None:
                del self._processor
                self._processor = None
            self._load_attempted = False
            self._device = None

            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass

            logger.info("Local VLM unloaded — GPU memory freed")

    def perceive(self, image_path: str,
                 prompt: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Run local VLM inference on a camera frame.

        Returns structured perception dict matching nav_cortex format,
        or None if inference fails. Fast path: ~200-500ms on Jetson GPU.

        Args:
            image_path: Path to JPEG/PNG camera frame.
            prompt: Override the default navigation prompt.
        """
        if not self.available:
            if not self.load():
                return None

        if not os.path.isfile(image_path):
            return None

        with self._lock:
            t0 = time.time()
            try:
                from PIL import Image
                import torch

                image = Image.open(image_path).convert("RGB")

                # Resize for speed — 384px is enough for obstacle detection
                max_dim = 384
                if max(image.size) > max_dim:
                    ratio = max_dim / max(image.size)
                    new_size = (int(image.size[0] * ratio),
                                int(image.size[1] * ratio))
                    image = image.resize(new_size, Image.LANCZOS)

                user_prompt = prompt or NAV_PROMPT

                messages = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image"},
                            {"type": "text", "text": user_prompt},
                        ],
                    }
                ]
                text_input = self._processor.apply_chat_template(
                    messages, add_generation_prompt=True
                )
                inputs = self._processor(
                    text=text_input,
                    images=[image],
                    return_tensors="pt",
                )

                device = next(self._model.parameters()).device
                inputs = {k: v.to(device) if hasattr(v, 'to') else v
                          for k, v in inputs.items()}

                with torch.inference_mode():
                    output_ids = self._model.generate(
                        **inputs,
                        max_new_tokens=256,
                        do_sample=False,
                        temperature=1.0,
                    )

                # Decode only the generated tokens
                input_len = inputs["input_ids"].shape[1]
                generated = output_ids[0][input_len:]
                raw_text = self._processor.decode(
                    generated, skip_special_tokens=True
                ).strip()

                elapsed_ms = (time.time() - t0) * 1000
                self._inference_count += 1
                self._total_inference_ms += elapsed_ms
                self._last_inference_ms = elapsed_ms

                result = self._parse_perception(raw_text)
                if result is not None:
                    result["_local_vlm"] = True
                    result["_inference_ms"] = round(elapsed_ms)
                    logger.debug(
                        f"Local VLM: {elapsed_ms:.0f}ms, "
                        f"scene={result.get('scene', '')[:60]}"
                    )
                return result

            except Exception as e:
                elapsed_ms = (time.time() - t0) * 1000
                logger.warning(f"Local VLM inference failed ({elapsed_ms:.0f}ms): {e}")
                return None

    def classify_scene(self, image_path: str) -> Optional[str]:
        """Fast scene-type classification. Returns a single word like
        'hallway', 'kitchen', 'outdoor', etc. ~100-200ms.
        """
        result = self.perceive(image_path, prompt=SCENE_CLASSIFY_PROMPT)
        if result and result.get("scene"):
            scene = result["scene"].strip().lower().split()[0]
            scene = scene.strip('.,;:"\'')
            return scene
        return None

    def _parse_perception(self, raw_text: str) -> Optional[Dict[str, Any]]:
        """Parse VLM output into structured perception dict.

        Handles messy model output: markdown fences, partial JSON,
        natural language mixed with JSON. Returns nav_cortex-compatible
        dict or falls back to extracting what we can.
        """
        cleaned = raw_text.strip()

        # Strip markdown code fences
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()

        # Try direct JSON parse
        start = cleaned.find("{")
        if start >= 0:
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
                cleaned = cleaned[start:] + '}'
                end = len(cleaned)

            try:
                result = json.loads(cleaned[start:end])
                if isinstance(result, dict):
                    return self._normalize_perception(result)
            except json.JSONDecodeError:
                pass

        # Fallback: extract what we can from natural language
        return self._fallback_parse(raw_text)

    def _normalize_perception(self, raw: Dict) -> Dict[str, Any]:
        """Normalize parsed JSON to match nav_cortex.perceive() format."""
        obstacles = raw.get("obstacles", {})
        path = raw.get("path", {})

        # Ensure obstacle values are floats in [0, 1]
        for key in ("left", "center", "right"):
            try:
                obstacles[key] = max(0.0, min(1.0, float(obstacles.get(key, 0.5))))
            except (ValueError, TypeError):
                obstacles[key] = 0.5
        obstacles.setdefault("above", 0.3)

        # Normalize direction
        direction = str(path.get("best_direction", "stop")).lower().strip()
        valid_dirs = {"forward", "left", "right", "backward", "stop"}
        if direction not in valid_dirs:
            for d in valid_dirs:
                if d in direction:
                    direction = d
                    break
            else:
                direction = "stop"

        try:
            confidence = max(0.0, min(1.0, float(path.get("confidence", 0.5))))
        except (ValueError, TypeError):
            confidence = 0.5

        return {
            "obstacles": obstacles,
            "floor": {"visible": True, "traversable": True, "surface": "unknown"},
            "path": {
                "best_direction": direction,
                "confidence": confidence,
                "reason": str(path.get("reason", ""))[:200],
            },
            "scene": str(raw.get("scene", ""))[:300],
            "scene_type": str(raw.get("scene_type", "unknown")).lower().strip(),
            "people": {
                "detected": bool(raw.get("people_detected", False)),
                "count": 0,
                "nearest_position": "none",
                "nearest_distance_cm": 0,
                "description": "",
            },
            "tether_visible": False,
            "distance_to_nearest_obstacle_cm": int(
                raw.get("distance_to_nearest_obstacle_cm", 100)
            ),
        }

    def _fallback_parse(self, text: str) -> Optional[Dict[str, Any]]:
        """Extract basic perception from natural language when JSON parse fails.

        Small models sometimes output prose instead of JSON. Extract
        what we can: keywords for obstacles, directions, scene description.
        """
        if not text or len(text) < 5:
            return None

        lower = text.lower()

        # Scene description is the full text (truncated)
        scene = text[:300]

        # Obstacle heuristics from keywords
        center = 0.5
        left_obs = 0.5
        right_obs = 0.5

        if any(w in lower for w in ("blocked", "wall ahead", "obstacle ahead",
                                     "cannot proceed", "dead end")):
            center = 0.8
        elif any(w in lower for w in ("clear ahead", "open ahead", "path forward",
                                       "clear path", "open space")):
            center = 0.2

        if "blocked left" in lower or "wall left" in lower:
            left_obs = 0.8
        elif "open left" in lower or "clear left" in lower:
            left_obs = 0.2

        if "blocked right" in lower or "wall right" in lower:
            right_obs = 0.8
        elif "open right" in lower or "clear right" in lower:
            right_obs = 0.2

        # Direction heuristics
        direction = "forward"
        if center >= 0.7:
            direction = "left" if left_obs < right_obs else "right"
        if any(w in lower for w in ("turn left", "go left")):
            direction = "left"
        elif any(w in lower for w in ("turn right", "go right")):
            direction = "right"
        elif any(w in lower for w in ("back up", "reverse", "go back")):
            direction = "backward"
        elif any(w in lower for w in ("stop", "halt", "do not move")):
            direction = "stop"

        # Scene type heuristics
        scene_type = "unknown"
        for st in ("hallway", "corridor", "kitchen", "bathroom", "bedroom",
                    "living room", "garage", "patio", "yard", "driveway",
                    "sidewalk", "street", "doorway", "stairs", "outdoor"):
            if st in lower:
                scene_type = st.replace(" ", "_")
                break

        people = any(w in lower for w in ("person", "people", "human",
                                           "someone", "man", "woman", "child"))

        return {
            "obstacles": {
                "left": left_obs, "center": center,
                "right": right_obs, "above": 0.3,
            },
            "floor": {"visible": True, "traversable": True, "surface": "unknown"},
            "path": {
                "best_direction": direction,
                "confidence": 0.4,
                "reason": f"fallback parse from natural language ({len(text)} chars)",
            },
            "scene": scene,
            "scene_type": scene_type,
            "people": {
                "detected": people,
                "count": 1 if people else 0,
                "nearest_position": "center" if people else "none",
                "nearest_distance_cm": 200 if people else 0,
                "description": "",
            },
            "tether_visible": False,
            "distance_to_nearest_obstacle_cm": 100,
            "_fallback_parse": True,
        }


# ── Singleton ────────────────────────────────────────────────────────

_local_vlm: Optional[LocalVLM] = None
_singleton_lock = threading.Lock()


def get_local_vlm() -> LocalVLM:
    """Get or create the singleton LocalVLM instance.

    Does NOT load the model — call .load() or .perceive() to trigger loading.
    This keeps import-time fast.
    """
    global _local_vlm
    if _local_vlm is None:
        with _singleton_lock:
            if _local_vlm is None:
                _local_vlm = LocalVLM()
    return _local_vlm
