"""
HormoneBridge — Bidirectional Sync Between Hormone Systems
===========================================================

Connects the two independent hormone/emotional systems:
    1. AlgorithmicHormoneSystem (SAIGE/evolution loop — neuroscience-based)
    2. JarvisConsciousness (agent daemon — cloud-LLM operator state)

Neither system is replaced. The bridge:
    - Periodically reads state from both (every 5-10s)
    - Computes a canonical EmotionalState for WorldState
    - Propagates significant events bidirectionally
    - Resolves conflicts with configurable priority

Design rationale (from user): hormones and consciousness are separate systems
that influence each other through feedback loops — like humans "seeing red" when
angry. They stay separate but sync, just like biological neuromodulators and
the prefrontal cortex are different systems connected by pathways.

Follows codebase patterns:
    - threading.Lock + daemon thread
    - _stop_event for clean shutdown
    - Observer pattern for event propagation (minimal hooks into existing code)
    - Feature-flagged via self._hormone_bridge_enabled
"""

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from repryntt.core.awareness.world_state import (
    DriveState,
    EmotionalState,
    WorldState,
    get_world_state,
)

logger = logging.getLogger("repryntt.hormones.bridge")

SYNC_INTERVAL_S = 7.0

# Mapping between the two systems' emotion names
# AlgorithmicHormoneSystem uses Lovheim cube emotions
# JarvisConsciousness uses: curiosity, satisfaction, frustration, excitement, focus, empathy
JARVIS_TO_ALGO_MAP = {
    "curiosity": "interest",
    "satisfaction": "enjoyment",
    "frustration": "anger",
    "excitement": "surprise",
    "focus": None,  # No direct mapping — derived from acetylcholine
    "empathy": "empathy",  # Algo has a composite "empathy" field
}


class HormoneBridge:
    """Bidirectional sync between AlgorithmicHormoneSystem and JarvisConsciousness.

    Produces a canonical EmotionalState that WorldState exposes to all consumers.
    """

    def __init__(self,
                 world_state: Optional[WorldState] = None,
                 algo_system=None,
                 jarvis_consciousness=None):
        self._world_state = world_state or get_world_state()
        self._algo = algo_system
        self._jarvis = jarvis_consciousness
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._observers: List[Callable] = []

        # Last known states for delta detection
        self._last_algo_mood: str = ""
        self._last_jarvis_mood: str = ""

    def set_algo_system(self, system) -> None:
        """Set/update the AlgorithmicHormoneSystem reference."""
        with self._lock:
            self._algo = system

    def set_jarvis_consciousness(self, consciousness) -> None:
        """Set/update the JarvisConsciousness reference."""
        with self._lock:
            self._jarvis = consciousness

    def add_observer(self, callback: Callable[[EmotionalState], None]) -> None:
        """Register a callback for emotional state changes."""
        self._observers.append(callback)

    def start(self) -> None:
        """Start the bridge sync thread."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="HormoneBridge",
            daemon=True,
        )
        self._thread.start()
        logger.info("HormoneBridge started (interval=%.1fs)", SYNC_INTERVAL_S)

    def stop(self) -> None:
        """Stop the bridge."""
        self._stop_event.set()
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("HormoneBridge stopped")

    def _run(self) -> None:
        """Sync loop."""
        while not self._stop_event.is_set():
            try:
                self._sync()
            except Exception as e:
                logger.error("HormoneBridge sync error: %s", e, exc_info=True)
            self._stop_event.wait(timeout=SYNC_INTERVAL_S)

    def _sync(self) -> None:
        """One sync cycle: read both systems, compute canonical state, update WorldState."""
        with self._lock:
            algo = self._algo
            jarvis = self._jarvis

        if algo is None and jarvis is None:
            return

        # Read from both systems
        algo_state = self._read_algo(algo) if algo else {}
        jarvis_state = self._read_jarvis(jarvis) if jarvis else {}

        # Compute canonical emotional state
        canonical = self._compute_canonical(algo_state, jarvis_state)
        self._world_state.update_emotional(canonical)

        # Compute drive state from whichever system has drives
        drive_state = self._compute_drives(algo_state, jarvis_state)
        if drive_state:
            self._world_state.update_drives(drive_state)

        # Propagate significant changes bidirectionally
        self._propagate_events(algo, jarvis, algo_state, jarvis_state)

        # Notify observers
        for observer in self._observers:
            try:
                observer(canonical)
            except Exception as e:
                logger.debug("Observer error: %s", e)

    def _read_algo(self, algo) -> Dict[str, Any]:
        """Read state from AlgorithmicHormoneSystem."""
        try:
            return {
                "emotions": algo.get_emotional_state(),
                "modifiers": algo.get_behavior_modifiers(),
                "levels": dict(algo.levels) if hasattr(algo, "levels") else {},
                "dominant_circuit": algo.get_dominant_circuit() if hasattr(algo, "get_dominant_circuit") else ("SEEKING", 0.5),
            }
        except Exception as e:
            logger.debug("Failed to read algo system: %s", e)
            return {}

    def _read_jarvis(self, jarvis) -> Dict[str, Any]:
        """Read state from JarvisConsciousness."""
        try:
            result = {
                "emotions": dict(jarvis.emotions) if hasattr(jarvis, "emotions") else {},
                "mood": jarvis.mood if hasattr(jarvis, "mood") else "neutral",
                "drives": dict(jarvis.drives) if hasattr(jarvis, "drives") else {},
            }
            return result
        except Exception as e:
            logger.debug("Failed to read jarvis consciousness: %s", e)
            return {}

    def _compute_canonical(self, algo_state: Dict, jarvis_state: Dict) -> EmotionalState:
        """Merge both systems into one canonical EmotionalState.

        Priority: agent-facing mood comes from Jarvis (it drives prompt tone).
        Behavior modifiers come from Algo (neuroscience-grounded).
        Emotion vector blends both with Algo dominant (more granular).
        """
        # Mood: prefer Jarvis mood (it's what the agent "feels")
        mood = jarvis_state.get("mood", "neutral")
        if not mood or mood == "neutral":
            # Fall back to algo dominant circuit as mood indicator
            circuit = algo_state.get("dominant_circuit", ("SEEKING", 0.5))
            mood = circuit[0].lower() if circuit else "neutral"

        # Emotion vector: primarily from algo (8+ emotions via Lovheim)
        # with Jarvis emotions as overlay for cloud-agent-specific feelings
        emotion_vector = dict(algo_state.get("emotions", {}))
        jarvis_emotions = jarvis_state.get("emotions", {})
        for j_emotion, j_value in jarvis_emotions.items():
            algo_key = JARVIS_TO_ALGO_MAP.get(j_emotion)
            if algo_key and algo_key in emotion_vector:
                # Blend: 60% algo + 40% jarvis for mapped emotions
                emotion_vector[algo_key] = 0.6 * emotion_vector[algo_key] + 0.4 * j_value
            elif j_emotion not in emotion_vector:
                # Jarvis-only emotions (focus, etc.) pass through
                emotion_vector[j_emotion] = j_value

        # Dominant emotion: highest activation
        dominant = "curiosity"
        if emotion_vector:
            dominant = max(emotion_vector, key=emotion_vector.get)

        # Behavior modifiers: from algo (neuroscience-grounded)
        modifiers = algo_state.get("modifiers", {})

        # Hormone levels: from algo
        levels = algo_state.get("levels", {})

        return EmotionalState(
            mood=mood,
            dominant_emotion=dominant,
            emotion_vector=emotion_vector,
            behavior_modifiers=modifiers,
            hormone_levels=levels,
            timestamp=time.time(),
        )

    def _compute_drives(self, algo_state: Dict, jarvis_state: Dict) -> Optional[DriveState]:
        """Compute unified drive state from Jarvis drives (it has the 5-drive model)."""
        jarvis_drives = jarvis_state.get("drives", {})
        if not jarvis_drives:
            return None

        dominant = max(jarvis_drives, key=jarvis_drives.get) if jarvis_drives else "understanding"
        return DriveState(
            dominant_drive=dominant,
            drive_levels=jarvis_drives,
            timestamp=time.time(),
        )

    def _propagate_events(self, algo, jarvis, algo_state: Dict, jarvis_state: Dict) -> None:
        """Propagate significant changes between systems.

        Algo dopamine spike → Jarvis excitement boost.
        Jarvis frustration spike → Algo cortisol bump.
        """
        if algo is None or jarvis is None:
            return

        # Algo → Jarvis propagation
        algo_emotions = algo_state.get("emotions", {})
        jarvis_emotions = jarvis_state.get("emotions", {})

        # High algo enjoyment (dopamine success) → boost Jarvis satisfaction
        if algo_emotions.get("enjoyment", 0) > 0.7:
            if jarvis_emotions.get("satisfaction", 0.5) < 0.7:
                try:
                    jarvis.emotions["satisfaction"] = min(1.0,
                        jarvis.emotions.get("satisfaction", 0.5) + 0.05)
                except (AttributeError, KeyError):
                    pass

        # High algo fear/distress (cortisol spike) → boost Jarvis frustration slightly
        if algo_emotions.get("distress", 0) > 0.6 or algo_emotions.get("fear", 0) > 0.6:
            try:
                jarvis.emotions["frustration"] = min(1.0,
                    jarvis.emotions.get("frustration", 0.1) + 0.03)
            except (AttributeError, KeyError):
                pass

        # Jarvis → Algo propagation
        # Jarvis frustration spike → algo cortisol event
        jarvis_frustration = jarvis_emotions.get("frustration", 0)
        if jarvis_frustration > 0.6 and hasattr(algo, "process_event"):
            try:
                algo.process_event("stress", details={"source": "jarvis_frustration"})
            except Exception:
                pass

    def get_canonical_state(self) -> EmotionalState:
        """Get the current canonical emotional state (from WorldState)."""
        return self._world_state.emotional_state

    def force_sync(self) -> None:
        """Force an immediate sync (useful during initialization)."""
        try:
            self._sync()
        except Exception as e:
            logger.debug("Force sync failed: %s", e)


_singleton: Optional[HormoneBridge] = None
_singleton_lock = threading.Lock()


def get_hormone_bridge(
    world_state: Optional[WorldState] = None,
) -> HormoneBridge:
    """Singleton accessor."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = HormoneBridge(world_state)
    return _singleton
