"""
repryntt.cortex.region_base — Abstract base for all brain regions.

Every brain region (Guardian, Conscious, Executor, Perception) implements
this interface.  The ResourceManager handles loading/unloading; the region
provides domain-specific inference, training-data generation, and health checks.
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class RegionState(enum.Enum):
    """Lifecycle state of a brain region."""
    UNINITIALIZED = "uninitialized"
    READY = "ready"            # Model loaded (or rule-based, always ready)
    ACTIVE = "active"          # Currently processing
    DEGRADED = "degraded"      # Fallback mode (model unavailable, using rules)
    TRAINING = "training"      # Offline for LoRA training
    ERROR = "error"
    DISABLED = "disabled"
    SHUTDOWN = "shutdown"       # Graceful shutdown in progress


class BrainRegion(ABC):
    """Abstract base class for a brain region.

    Subclasses must implement:
      - ``name``           (property)  — unique region identifier
      - ``process()``      — main inference entry point
      - ``health_check()`` — returns True if the region is functional

    Optional overrides:
      - ``on_load()`` / ``on_unload()`` — lifecycle hooks
      - ``generate_training_data()``    — produce training examples for evolution
      - ``fallback()``                  — rule-based alternative when model is unavailable
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = RegionState.UNINITIALIZED
        self._model_name: Optional[str] = None
        self._stats = {
            "calls": 0,
            "errors": 0,
            "fallbacks": 0,
            "total_latency_ms": 0.0,
        }

    # ── Identity ─────────────────────────────────────────────────────

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique region identifier (e.g. 'conscious', 'guardian')."""
        ...

    @property
    def state(self) -> RegionState:
        return self._state

    @property
    def model_name(self) -> Optional[str]:
        return self._model_name

    # ── Lifecycle ────────────────────────────────────────────────────

    def initialize(self, model_name: Optional[str] = None) -> None:
        """Prepare the region.  Called once at startup or when model changes."""
        self._model_name = model_name
        if model_name:
            self._state = RegionState.READY
        else:
            # No model — use rule-based fallback if available
            self._state = RegionState.DEGRADED
        self.on_load()

    def on_load(self) -> None:
        """Hook called after model is loaded / region initialized."""
        pass

    def on_unload(self) -> None:
        """Hook called before model is unloaded."""
        pass

    def shutdown(self) -> None:
        """Graceful shutdown — mark state before cleanup."""
        self._state = RegionState.SHUTDOWN
        self.on_unload()
        self._state = RegionState.DISABLED

    # ── Core interface ───────────────────────────────────────────────

    @abstractmethod
    def process(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Run the region's primary function.

        Args:
            input_data: Region-specific input.  Always has a ``"type"`` key.

        Returns:
            Region-specific output.  Always has ``"success"`` (bool) and
            ``"result"`` keys.
        """
        ...

    def safe_process(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Process with timing, error handling, and automatic fallback."""
        with self._lock:
            self._stats["calls"] += 1
        t0 = time.monotonic()

        try:
            if self._state == RegionState.ERROR:
                # Attempt recovery: try fallback first, then process
                logger.info("Region '%s' in ERROR state — attempting recovery via fallback", self.name)
                result = self.fallback(input_data)
                with self._lock:
                    self._stats["fallbacks"] += 1
                # If fallback succeeded, transition back to DEGRADED
                self._state = RegionState.DEGRADED
            elif self._state in (RegionState.READY, RegionState.ACTIVE):
                self._state = RegionState.ACTIVE
                result = self.process(input_data)
                self._state = RegionState.READY
            elif self._state == RegionState.DEGRADED:
                # Model not available — try fallback
                result = self.fallback(input_data)
                with self._lock:
                    self._stats["fallbacks"] += 1
            else:
                return {
                    "success": False,
                    "result": None,
                    "error": f"Region '{self.name}' is in state {self._state.value}",
                }
        except Exception as e:
            with self._lock:
                self._stats["errors"] += 1
            logger.error("Region '%s' error: %s", self.name, e, exc_info=True)
            # Try fallback on error
            try:
                result = self.fallback(input_data)
                with self._lock:
                    self._stats["fallbacks"] += 1
            except Exception:
                self._state = RegionState.ERROR
                return {"success": False, "result": None, "error": str(e)}

        elapsed_ms = (time.monotonic() - t0) * 1000
        with self._lock:
            self._stats["total_latency_ms"] += elapsed_ms
        result["latency_ms"] = round(elapsed_ms, 1)
        return result

    def fallback(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Rule-based fallback when model is unavailable.

        Override in subclasses.  Default: return a neutral "no-op" result.
        """
        return {
            "success": True,
            "result": None,
            "fallback": True,
            "note": f"Region '{self.name}' used rule-based fallback",
        }

    # ── Health ───────────────────────────────────────────────────────

    def health_check(self) -> bool:
        """Return True if the region is operational."""
        return self._state in (RegionState.READY, RegionState.DEGRADED)

    def get_stats(self) -> Dict[str, Any]:
        """Return region statistics."""
        with self._lock:
            stats_copy = dict(self._stats)
        avg_latency = 0.0
        if stats_copy["calls"] > 0:
            avg_latency = stats_copy["total_latency_ms"] / stats_copy["calls"]
        return {
            "name": self.name,
            "state": self._state.value,
            "model": self._model_name,
            "calls": stats_copy["calls"],
            "errors": stats_copy["errors"],
            "fallbacks": stats_copy["fallbacks"],
            "avg_latency_ms": round(avg_latency, 1),
        }

    # ── Training data generation ─────────────────────────────────────

    def generate_training_data(self) -> List[Dict[str, Any]]:
        """Produce training examples from this region's recent activity.

        Override in subclasses that support self-evolution.
        Returns a list of training examples suitable for the region trainer.
        """
        return []
