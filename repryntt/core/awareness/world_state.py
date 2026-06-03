"""
WorldState — Single Source of Truth for Current Awareness
=========================================================

Thread-safe buffer holding the system's real-time understanding of:
- Sensory input (vision, audio, proximity, body)
- Emotional state (canonical blend from hormone bridge)
- Contextual state (current task, relational mode, active memories)
- Perception context (accumulated observations for mesh firing)

Updated by: PerceptionLoop (1-5s), HormoneBridge (5-10s), consciousness beat (60s)
Read by: heartbeat prompt builder, consciousness daemon, nav cortex, tools

Follows codebase patterns:
- threading.Lock (same as PerceptionBuffer, MemoryMesh, task_system)
- Feature-flagged via self._world_state_enabled in AgentDaemon
- Graceful degradation: fields return defaults when stale/unavailable
"""

import copy
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger("repryntt.awareness.world_state")

_singleton: Optional["WorldState"] = None
_singleton_lock = threading.Lock()


class RelationalMode(Enum):
    SELF = "self"
    EMPLOYEE = "employee"
    FRIEND = "friend"
    FAMILY = "family"


@dataclass
class SceneSummary:
    """Visual scene from SmolVLM / camera."""
    description: str = ""
    detected_objects: List[str] = field(default_factory=list)
    scene_type: str = "unknown"
    timestamp: float = 0.0

    @property
    def is_fresh(self) -> bool:
        return (time.time() - self.timestamp) < 30.0


@dataclass
class AudioContext:
    """Audio environment from Whisper / mic."""
    last_transcript: str = ""
    speaker_detected: bool = False
    ambient_noise_level: float = 0.0
    timestamp: float = 0.0

    @property
    def is_fresh(self) -> bool:
        return (time.time() - self.timestamp) < 15.0


@dataclass
class ProximityData:
    """Distance readings from sonar / sensors."""
    front_cm: float = -1.0
    rear_cm: float = -1.0
    obstacle_detected: bool = False
    timestamp: float = 0.0

    @property
    def is_fresh(self) -> bool:
        return (time.time() - self.timestamp) < 10.0


@dataclass
class BodyState:
    """Physical state: motors, thermals, battery."""
    cpu_temp_c: float = 0.0
    gpu_temp_c: float = 0.0
    memory_free_mb: float = 0.0
    motor_state: str = "idle"
    timestamp: float = 0.0


@dataclass
class EmotionalState:
    """Canonical emotional blend from hormone bridge."""
    mood: str = "neutral"
    dominant_emotion: str = "curiosity"
    emotion_vector: Dict[str, float] = field(default_factory=dict)
    behavior_modifiers: Dict[str, float] = field(default_factory=dict)
    hormone_levels: Dict[str, float] = field(default_factory=dict)
    timestamp: float = 0.0


@dataclass
class DriveState:
    """Current drive levels and dominant drive."""
    dominant_drive: str = "understanding"
    drive_levels: Dict[str, float] = field(default_factory=dict)
    timestamp: float = 0.0


@dataclass
class ModeWeights:
    """Relational mode blend weights (sum to 1.0)."""
    weights: Dict[str, float] = field(default_factory=lambda: {
        "self": 0.50, "employee": 0.20, "friend": 0.15, "family": 0.15
    })
    primary_mode: str = "self"
    timestamp: float = 0.0

    @property
    def as_relational_mode(self) -> RelationalMode:
        return RelationalMode(self.primary_mode)


@dataclass
class PerceptionContext:
    """Accumulated perception observations for mesh firing (Strategy A).

    Between heartbeats, the perception loop accumulates what it detects here.
    fire_pre_heartbeat() reads this as additional fire_items, then clears it.
    """
    detected_objects: List[str] = field(default_factory=list)
    heard_speech: List[str] = field(default_factory=list)
    proximity_alerts: List[str] = field(default_factory=list)
    scene_types: List[str] = field(default_factory=list)

    def has_content(self) -> bool:
        return bool(self.detected_objects or self.heard_speech
                    or self.proximity_alerts or self.scene_types)

    def drain(self) -> "PerceptionContext":
        """Return copy and clear (called by fire_pre_heartbeat)."""
        snapshot = PerceptionContext(
            detected_objects=list(self.detected_objects),
            heard_speech=list(self.heard_speech),
            proximity_alerts=list(self.proximity_alerts),
            scene_types=list(self.scene_types),
        )
        self.detected_objects.clear()
        self.heard_speech.clear()
        self.proximity_alerts.clear()
        self.scene_types.clear()
        return snapshot


FRESHNESS_THRESHOLDS = {
    "visual_scene": 30.0,
    "audio_context": 15.0,
    "proximity": 10.0,
    "body_state": 60.0,
    "emotional_state": 30.0,
    "drive_state": 120.0,
    "mode_weights": 120.0,
}


class WorldState:
    """Single source of truth for current awareness.

    Thread-safe. All writes acquire the lock. Reads via snapshot() get a
    consistent frozen copy without blocking writers during prompt building.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._ready = threading.Event()

        self.visual_scene = SceneSummary()
        self.audio_context = AudioContext()
        self.proximity = ProximityData()
        self.body_state = BodyState()
        self.emotional_state = EmotionalState()
        self.drive_state = DriveState()
        self.mode_weights = ModeWeights()
        self.perception_context = PerceptionContext()
        self.active_memories: List[Dict[str, Any]] = []
        self.current_task: Optional[Dict[str, Any]] = None

        self._last_updated: Dict[str, float] = {}
        self._update_count: int = 0

    def is_ready(self) -> bool:
        """True after first successful sensory update."""
        return self._ready.is_set()

    def update_visual(self, scene: SceneSummary) -> None:
        with self._lock:
            self.visual_scene = scene
            self._last_updated["visual_scene"] = time.time()
            self._update_count += 1
            if scene.detected_objects:
                self.perception_context.detected_objects.extend(scene.detected_objects)
            if scene.scene_type and scene.scene_type != "unknown":
                self.perception_context.scene_types.append(scene.scene_type)
            self._ready.set()

    def update_audio(self, audio: AudioContext) -> None:
        with self._lock:
            self.audio_context = audio
            self._last_updated["audio_context"] = time.time()
            self._update_count += 1
            if audio.last_transcript.strip():
                self.perception_context.heard_speech.append(audio.last_transcript.strip())
            self._ready.set()

    def update_proximity(self, prox: ProximityData) -> None:
        with self._lock:
            self.proximity = prox
            self._last_updated["proximity"] = time.time()
            self._update_count += 1
            if prox.obstacle_detected:
                alert = f"obstacle_front_{int(prox.front_cm)}cm"
                self.perception_context.proximity_alerts.append(alert)
            self._ready.set()

    def update_body(self, body: BodyState) -> None:
        with self._lock:
            self.body_state = body
            self._last_updated["body_state"] = time.time()
            self._update_count += 1

    def update_emotional(self, state: EmotionalState) -> None:
        with self._lock:
            self.emotional_state = state
            self._last_updated["emotional_state"] = time.time()

    def update_drives(self, state: DriveState) -> None:
        with self._lock:
            self.drive_state = state
            self._last_updated["drive_state"] = time.time()

    def update_mode(self, weights: ModeWeights) -> None:
        with self._lock:
            self.mode_weights = weights
            self._last_updated["mode_weights"] = time.time()

    def update_active_memories(self, memories: List[Dict[str, Any]]) -> None:
        with self._lock:
            self.active_memories = memories
            self._last_updated["active_memories"] = time.time()

    def update_current_task(self, task: Optional[Dict[str, Any]]) -> None:
        with self._lock:
            self.current_task = task
            self._last_updated["current_task"] = time.time()

    def drain_perception_context(self) -> PerceptionContext:
        """Called by fire_pre_heartbeat to get accumulated perception and clear it."""
        with self._lock:
            return self.perception_context.drain()

    def snapshot(self) -> "WorldStateSnapshot":
        """Consistent frozen copy for heartbeat prompt building.

        Uses deepcopy so the heartbeat can read without blocking perception writes.
        """
        with self._lock:
            return WorldStateSnapshot(
                visual_scene=copy.deepcopy(self.visual_scene),
                audio_context=copy.deepcopy(self.audio_context),
                proximity=copy.deepcopy(self.proximity),
                body_state=copy.deepcopy(self.body_state),
                emotional_state=copy.deepcopy(self.emotional_state),
                drive_state=copy.deepcopy(self.drive_state),
                mode_weights=copy.deepcopy(self.mode_weights),
                active_memories=copy.deepcopy(self.active_memories),
                current_task=copy.deepcopy(self.current_task),
                last_updated=dict(self._last_updated),
                coherence_score=self._compute_coherence(),
            )

    def _compute_coherence(self) -> float:
        """Score 0.0-1.0 indicating how fresh/consistent the state is.

        1.0 = all fields recently updated. 0.0 = everything stale.
        """
        now = time.time()
        if not self._last_updated:
            return 0.0

        scores = []
        for field_name, threshold in FRESHNESS_THRESHOLDS.items():
            last = self._last_updated.get(field_name, 0.0)
            age = now - last
            score = max(0.0, 1.0 - (age / threshold))
            scores.append(score)

        return sum(scores) / len(scores) if scores else 0.0

    def get_stats(self) -> Dict[str, Any]:
        """For telemetry/health reporting."""
        with self._lock:
            return {
                "update_count": self._update_count,
                "coherence_score": round(self._compute_coherence(), 3),
                "last_updated": {k: round(time.time() - v, 1)
                                 for k, v in self._last_updated.items()},
                "ready": self._ready.is_set(),
                "perception_pending": self.perception_context.has_content(),
            }


@dataclass
class WorldStateSnapshot:
    """Frozen copy of WorldState for prompt building. Immutable after creation."""
    visual_scene: SceneSummary
    audio_context: AudioContext
    proximity: ProximityData
    body_state: BodyState
    emotional_state: EmotionalState
    drive_state: DriveState
    mode_weights: ModeWeights
    active_memories: List[Dict[str, Any]]
    current_task: Optional[Dict[str, Any]]
    last_updated: Dict[str, float]
    coherence_score: float

    def to_prompt_context(self) -> str:
        """Generate awareness section for heartbeat prompt injection."""
        parts = []

        # Emotional state
        es = self.emotional_state
        if es.emotion_vector:
            top_emotions = sorted(es.emotion_vector.items(), key=lambda x: -x[1])[:3]
            emo_str = ", ".join(f"{e} {v:.2f}" for e, v in top_emotions)
            parts.append(f"**Mood**: {es.mood.title()} | **Emotions**: {emo_str}")

        # Drives
        ds = self.drive_state
        if ds.drive_levels:
            top_drives = sorted(ds.drive_levels.items(), key=lambda x: -x[1])[:3]
            drive_str = ", ".join(f"{d}: {v:.2f}" for d, v in top_drives)
            parts.append(f"**Dominant Drive**: {ds.dominant_drive} | {drive_str}")

        # Relational mode
        mw = self.mode_weights
        if mw.weights:
            sorted_modes = sorted(mw.weights.items(), key=lambda x: -x[1])
            mode_str = " / ".join(f"{m.title()} {int(w*100)}%" for m, w in sorted_modes if w > 0.05)
            parts.append(f"**Mode**: {mode_str}")

        # Vision
        vs = self.visual_scene
        if vs.is_fresh and vs.description:
            parts.append(f"**Seeing**: {vs.description}")
        elif vs.detected_objects:
            parts.append(f"**Visible**: {', '.join(vs.detected_objects[:5])}")

        # Audio
        ac = self.audio_context
        if ac.is_fresh and ac.last_transcript:
            parts.append(f"**Heard**: \"{ac.last_transcript[:200]}\"")
        elif ac.speaker_detected:
            parts.append("**Audio**: Speaker detected nearby")

        # Proximity
        px = self.proximity
        if px.is_fresh and px.obstacle_detected:
            distances = []
            if px.front_cm > 0:
                distances.append(f"front: {px.front_cm:.0f}cm")
            if px.rear_cm > 0:
                distances.append(f"rear: {px.rear_cm:.0f}cm")
            parts.append(f"**Proximity**: Obstacle! {', '.join(distances)}")

        # Body
        bs = self.body_state
        if bs.cpu_temp_c > 70:
            parts.append(f"**Body**: CPU {bs.cpu_temp_c:.0f}°C (hot!), {bs.memory_free_mb:.0f}MB free")

        # Active memories (subconscious)
        if self.active_memories:
            mem_labels = [m.get("label", "") for m in self.active_memories[:5] if m.get("label")]
            if mem_labels:
                parts.append(f"**Subconscious**: {', '.join(mem_labels)}")

        # Coherence indicator
        if self.coherence_score < 0.3:
            parts.append("**⚠ Awareness**: Degraded (sensors stale)")

        if not parts:
            return ""

        return "**🧠 UNIFIED AWARENESS**\n" + "\n".join(parts)


def get_world_state() -> WorldState:
    """Singleton accessor (same pattern as get_memory_mesh)."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = WorldState()
                logger.info("WorldState initialized")
    return _singleton
