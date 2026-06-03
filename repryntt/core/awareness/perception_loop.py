"""
PerceptionLoop — Continuous Sensory Update Thread
=================================================

Background daemon thread that continuously updates WorldState from hardware:
- Optional camera poll → shared capture_camera path → world_state.visual_scene
- Sonar → distance readings → world_state.proximity
- System → thermals/memory → world_state.body_state

Audio (Whisper STT) is handled separately by the conversational_awareness
listen loop since it requires longer recording windows and VAD.

Resource contention strategy (Jetson Orin Nano, 8GB shared):
- Check _brain_inferring Event before VLM call — skip if brain is mid-inference
- Check torch.cuda.mem_get_info() >= 0.5GB before VLM — fall back to cached
- Check psutil memory before cycle — sleep if critically low
- Sonar/GPIO/thermals are CPU-only and always run regardless
- Camera polling is disabled by default; presence/nav/tool captures feed
  WorldState separately so the daemon does not keep the CSI camera hot.

Follows codebase patterns:
- threading.Lock + daemon thread (same as explorer.py, consciousness daemon)
- _stop_event for clean shutdown (same as explorer.py)
- *_AVAILABLE flags for graceful degradation (same as tank.py, ros2.py)
- Feature-flagged via self._world_state_enabled in AgentDaemon
"""

import logging
import threading
import time
from typing import Optional
import os

from repryntt.core.awareness.world_state import (
    AudioContext,
    BodyState,
    ProximityData,
    SceneSummary,
    WorldState,
    get_world_state,
)

logger = logging.getLogger("repryntt.awareness.perception")

# Cadence configuration
PERCEPTION_INTERVAL_S = 3.0
VISION_INTERVAL_S = 120.0
SONAR_INTERVAL_S = 2.0
BODY_INTERVAL_S = 10.0
LOW_MEMORY_WARNING_INTERVAL_S = 60.0

# Resource guards
MIN_FREE_MEMORY_MB = 768
MIN_FREE_VRAM_MB = 512

# Keep steady-state WorldState cheap. Presence detection, nav tools, and
# explicit capture_camera calls already bridge visual observations into
# WorldState, so continuous camera polling should be opt-in only.
WORLDSTATE_CAMERA_POLL_ENABLED = (
    os.environ.get("REPRYNTT_WORLDSTATE_CAMERA_POLL", "").strip().lower()
    in {"1", "true", "yes", "on"}
)

# Optional imports — graceful degradation
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False


class PerceptionLoop:
    """Continuous sensory update thread for WorldState.

    Runs as a daemon thread. Respects resource constraints and
    gracefully degrades when hardware/models are unavailable.
    """

    def __init__(self, world_state: Optional[WorldState] = None,
                 brain_inferring: Optional[threading.Event] = None):
        self._world_state = world_state or get_world_state()
        self._brain_inferring = brain_inferring or threading.Event()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False

        # Hardware handles (lazy-loaded)
        self._sonar = None
        self._local_vlm = None
        self._camera_index: int = 0

        # Timing
        self._last_vlm_time = 0.0
        self._last_sonar_time = 0.0
        self._last_body_time = 0.0
        self._last_low_memory_warning = 0.0

    def start(self) -> None:
        """Start the perception loop background thread."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="PerceptionLoop",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "PerceptionLoop started (interval=%.1fs, camera_poll=%s)",
            PERCEPTION_INTERVAL_S,
            WORLDSTATE_CAMERA_POLL_ENABLED,
        )

    def stop(self) -> None:
        """Stop the perception loop."""
        self._stop_event.set()
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("PerceptionLoop stopped")

    def is_ready(self) -> bool:
        """True after at least one successful cycle."""
        return self._world_state.is_ready()

    def _run(self) -> None:
        """Main loop — runs until stop_event is set."""
        logger.info("PerceptionLoop thread running")
        while not self._stop_event.is_set():
            try:
                self._cycle()
            except Exception as e:
                logger.error("PerceptionLoop cycle error: %s", e, exc_info=True)
            self._stop_event.wait(timeout=PERCEPTION_INTERVAL_S)

    def _cycle(self) -> None:
        """One perception cycle — update what's due."""
        now = time.time()

        if not self._check_system_resources():
            return

        # Sonar — always run (CPU-only, fast)
        if now - self._last_sonar_time >= SONAR_INTERVAL_S:
            self._update_sonar()
            self._last_sonar_time = now

        # Body state — always run (CPU-only, reads sysfs)
        if now - self._last_body_time >= BODY_INTERVAL_S:
            self._update_body()
            self._last_body_time = now

        # Vision — disabled by default; presence/nav/tool captures bridge into
        # WorldState without adding a steady camera workload.
        if (WORLDSTATE_CAMERA_POLL_ENABLED
                and now - self._last_vlm_time >= VISION_INTERVAL_S):
            if not self._brain_inferring.is_set() and self._check_vram():
                self._update_vision()
                self._last_vlm_time = now
            else:
                logger.debug("Vision poll skipped (brain_inferring=%s, vram_ok=%s)",
                             self._brain_inferring.is_set(),
                             self._check_vram())

    def _check_system_resources(self) -> bool:
        """Return False if system memory is critically low (skip entire cycle)."""
        if not PSUTIL_AVAILABLE:
            return True
        mem = psutil.virtual_memory()
        if mem.available < MIN_FREE_MEMORY_MB * 1024 * 1024:
            now = time.time()
            if now - self._last_low_memory_warning >= LOW_MEMORY_WARNING_INTERVAL_S:
                logger.warning(
                    "PerceptionLoop: low memory (%.0f MB free), skipping cycle",
                    mem.available / (1024 * 1024),
                )
                self._last_low_memory_warning = now
            return False
        return True

    def _check_vram(self) -> bool:
        """Check if enough GPU VRAM is free for VLM inference."""
        try:
            import torch
            if torch.cuda.is_available():
                free, _ = torch.cuda.mem_get_info()
                return free >= MIN_FREE_VRAM_MB * 1024 * 1024
        except (ImportError, RuntimeError):
            pass
        return True  # If can't check, assume OK (VLM will fail gracefully anyway)

    def _update_sonar(self) -> None:
        """Read sonar distances and update WorldState."""
        try:
            if self._sonar is None:
                from repryntt.hardware.sonar import get_sonar, GPIO_AVAILABLE
                if not GPIO_AVAILABLE:
                    return
                self._sonar = get_sonar()

            readings = self._sonar.read_both()
            front = readings.get("front")
            rear = readings.get("rear")

            front_cm = front.distance_cm if front and front.valid else -1.0
            rear_cm = rear.distance_cm if rear and rear.valid else -1.0
            obstacle = (0 < front_cm < 30) or (0 < rear_cm < 20)

            self._world_state.update_proximity(ProximityData(
                front_cm=front_cm,
                rear_cm=rear_cm,
                obstacle_detected=obstacle,
                timestamp=time.time(),
            ))
        except Exception as e:
            logger.debug("Sonar read failed: %s", e)

    def _update_body(self) -> None:
        """Read system thermals/memory and update WorldState."""
        try:
            cpu_temp = self._read_thermal("cpu")
            gpu_temp = self._read_thermal("gpu")
            mem_free = 0.0
            if PSUTIL_AVAILABLE:
                mem_free = psutil.virtual_memory().available / (1024 * 1024)

            self._world_state.update_body(BodyState(
                cpu_temp_c=cpu_temp,
                gpu_temp_c=gpu_temp,
                memory_free_mb=mem_free,
                motor_state="idle",
                timestamp=time.time(),
            ))
        except Exception as e:
            logger.debug("Body state read failed: %s", e)

    def _update_vision(self) -> None:
        """Capture camera frame and update WorldState.

        The VLM is NOT loaded here — it stays cold until Andrew
        explicitly starts nav_explore, which loads it on-demand via
        nav_cortex.  This keeps ~1.5 GB of PyTorch/CUDA overhead
        out of steady-state memory.
        """
        try:
            import json

            from repryntt.tools.media import capture_camera

            brain_path = os.environ.get(
                "REPRYNTT_BRAIN",
                os.path.join(os.path.expanduser("~"), ".repryntt", "brain"),
            )
            raw = capture_camera(
                brain_path,
                camera_id=self._camera_index,
                analyze=False,
                save=False,
            )
            result = json.loads(raw) if isinstance(raw, str) else raw
            if not result or result.get("error"):
                logger.debug("Vision capture skipped: %s",
                             result.get("error") if isinstance(result, dict) else result)
                return

            resolution = result.get("resolution", "unknown")

            self._world_state.update_visual(SceneSummary(
                description=f"camera frame captured ({resolution})",
                detected_objects=[],
                scene_type="unknown",
                timestamp=time.time(),
            ))
        except Exception as e:
            logger.debug("Vision update failed: %s", e)

    def _extract_objects(self, vlm_result: dict) -> list:
        """Extract detected object labels from VLM perception result."""
        objects = []
        if vlm_result.get("people_detected"):
            objects.append("person")
        obstacles = vlm_result.get("obstacles", {})
        for direction, score in obstacles.items():
            if score > 0.5:
                objects.append(f"obstacle_{direction}")
        path = vlm_result.get("path", {})
        if path.get("best_direction") == "stop":
            objects.append("path_blocked")
        return objects

    def _read_thermal(self, zone: str) -> float:
        """Read Jetson thermal zone temperature."""
        thermal_paths = {
            "cpu": "/sys/devices/virtual/thermal/thermal_zone1/temp",
            "gpu": "/sys/devices/virtual/thermal/thermal_zone2/temp",
        }
        path = thermal_paths.get(zone, "")
        try:
            if os.path.exists(path):
                with open(path) as f:
                    return int(f.read().strip()) / 1000.0
        except (IOError, ValueError):
            pass
        return 0.0


_singleton: Optional[PerceptionLoop] = None
_singleton_lock = threading.Lock()


def get_perception_loop(
    world_state: Optional[WorldState] = None,
    brain_inferring: Optional[threading.Event] = None,
) -> PerceptionLoop:
    """Singleton accessor."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = PerceptionLoop(world_state, brain_inferring)
    return _singleton
