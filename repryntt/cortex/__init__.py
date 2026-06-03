"""
repryntt.cortex — Multi-Model Neural Cortex
=============================================
Modular brain architecture with specialised regions, dynamic resource
management, and per-region self-evolution.

Brain Regions:
  Guardian   — safety constraints, e-stop, input sanitisation  (<1 ms)
  Conscious  — identity, personality, memory consolidation      (<500 ms)
  Executor   — ROS2 action selection, motor commands            (<10 ms)
  Perception — camera/audio classification, sensor fusion       (<50 ms)
  Cortex     — complex reasoning via cloud/local LLM API        (5-30 s)

The ``CortexDispatcher`` routes signals between regions.  The
``ResourceManager`` dynamically loads/unloads models based on hardware
capabilities and priority.
"""

from repryntt.cortex.model_config import CortexConfig, RegionConfig, ModelEntry
from repryntt.cortex.resource_manager import ResourceManager
from repryntt.cortex.dispatcher import CortexDispatcher, CortexSignal
from repryntt.cortex.region_base import BrainRegion, RegionState

__all__ = [
    "CortexConfig",
    "RegionConfig",
    "ModelEntry",
    "ResourceManager",
    "CortexDispatcher",
    "CortexSignal",
    "BrainRegion",
    "RegionState",
    "initialize_cortex",
    "get_cortex",
    "shutdown_cortex",
]

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_cortex_instance: Optional["CortexRuntime"] = None
_cortex_lock = threading.Lock()


class CortexRuntime:
    """Convenience wrapper that holds the initialised cortex components."""

    __slots__ = ("dispatcher", "resource_manager", "config", "initialized")

    def __init__(self, dispatcher, resource_manager, config):
        self.dispatcher: CortexDispatcher = dispatcher
        self.resource_manager: ResourceManager = resource_manager
        self.config: CortexConfig = config
        self.initialized: bool = True


def initialize_cortex() -> CortexRuntime:
    """Bootstrap the cortex — create regions, load models if available.

    Safe to call multiple times (idempotent).  Returns the runtime.
    """
    global _cortex_instance

    with _cortex_lock:
        # All checks inside the lock — no broken double-checked locking
        if _cortex_instance is not None:
            return _cortex_instance

        logger.info("🧠 Initialising Neural Cortex...")

        from repryntt.cortex.model_config import load_config, get_config_path
        from repryntt.cortex.model_registry import get_registry
        from repryntt.cortex.resource_manager import get_resource_manager
        from repryntt.cortex.dispatcher import get_dispatcher

        # 1. Load config (creates defaults if missing)
        config = load_config()
        config.ensure_default_models()
        config.save(get_config_path())

        # 2. Init registry and discover on-disk models
        registry = get_registry()
        from repryntt.paths import models_dir
        cortex_models_dir = models_dir() / "cortex"
        cortex_models_dir.mkdir(parents=True, exist_ok=True)
        discovered = registry.discover_models(cortex_models_dir)
        if discovered:
            registry.save()

        # Auto-download missing models (non-blocking best-effort)
        missing = registry.missing_models()
        if missing:
            names = ", ".join(m.name for m in missing[:3])
            logger.info("🧠 Missing models: %s — attempting auto-download", names)
            try:
                downloaded = registry.download_missing()
                if downloaded:
                    logger.info("🧠 Auto-downloaded %d model(s)", downloaded)
                    registry.save()
            except Exception as e:
                logger.warning("Model auto-download failed (non-fatal): %s", e)

        # 3. Init resource manager
        mgr = get_resource_manager()

        # 4. Init dispatcher and register regions
        dispatcher = get_dispatcher()

        # Guardian — always active (rule-based, no model needed)
        from repryntt.cortex.regions.guardian import GuardianRegion
        guardian = GuardianRegion()
        guardian.initialize()
        dispatcher.register_region(guardian)

        # Conscious — init if enabled, select best model for budget
        region_cfg = config.get_region("conscious")
        if region_cfg and region_cfg.enabled:
            from repryntt.cortex.regions.conscious import ConsciousRegion
            conscious = ConsciousRegion()

            # Auto-select model
            model_name = region_cfg.model_name
            if not model_name:
                best = registry.select_for_region("conscious", mgr.available_mb)
                if best:
                    model_name = best.name

            if model_name and registry.is_available_on_disk(model_name):
                conscious.initialize(model_name=model_name)
                logger.info("🧠 Conscious layer: %s", model_name)
            else:
                conscious.initialize(model_name=None)
                missing = registry.missing_models()
                if missing:
                    names = ", ".join(m.name for m in missing[:3])
                    logger.info("🧠 Conscious layer: fallback mode (models not downloaded: %s)", names)
                else:
                    logger.info("🧠 Conscious layer: fallback mode (no model available)")

            dispatcher.register_region(conscious)

        # Executor — init only if ROS2 present
        region_cfg = config.get_region("executor")
        if region_cfg and region_cfg.enabled:
            from repryntt.cortex.regions.executor import ExecutorRegion
            executor = ExecutorRegion()
            executor.initialize()
            if executor.state != RegionState.DISABLED:
                dispatcher.register_region(executor)

        # Perception — init only if sensors present
        region_cfg = config.get_region("perception")
        if region_cfg and region_cfg.enabled:
            from repryntt.cortex.regions.perception import PerceptionRegion
            perception = PerceptionRegion()
            perception.initialize()
            if perception.state != RegionState.DISABLED:
                dispatcher.register_region(perception)

        # Start background signal processing
        dispatcher.start_background()

        # Migrate legacy training data into cortex DataRouter on first boot
        try:
            from repryntt.cortex.training import migrate_legacy_training_data
            migrated = migrate_legacy_training_data()
            if migrated:
                logger.info("🧠 Migrated %d legacy training examples into cortex pipeline", migrated)
        except Exception as e:
            logger.debug("Legacy training data migration skipped: %s", e)

        runtime = CortexRuntime(dispatcher, mgr, config)
        _cortex_instance = runtime

        # Summary
        regions_str = ", ".join(
            f"{r.name}({r.state.value})"
            for r in dispatcher.all_regions().values()
        )
        logger.info(
            "🧠 Neural Cortex ready: budget=%dMB, regions=[%s]",
            mgr.budget_mb, regions_str,
        )

        return runtime


def get_cortex() -> Optional[CortexRuntime]:
    """Get the cortex runtime if initialised, else None."""
    return _cortex_instance


def cortex_health() -> dict:
    """Return cortex health summary for monitoring endpoints."""
    runtime = _cortex_instance
    if not runtime:
        return {"initialized": False}
    try:
        health = {
            "initialized": True,
            "dispatcher": runtime.dispatcher.health(),
            "resources": runtime.resource_manager.status(),
        }
        # Add training stats
        try:
            from repryntt.cortex.training.data_router import get_data_router
            health["training"] = get_data_router().dataset_stats()
        except Exception:
            health["training"] = {}
        # Add pre-filter accuracy metrics
        try:
            from repryntt.cortex.training.data_router import get_cortex_metrics
            health["prefilter_accuracy"] = get_cortex_metrics().accuracy()
        except Exception:
            health["prefilter_accuracy"] = {}
        # Add per-model inference latency
        try:
            latencies = {}
            for name in runtime.resource_manager.status().get("loaded", {}):
                latencies[name] = runtime.resource_manager.latency_stats(name)
            if latencies:
                health["latency"] = latencies
        except Exception:
            pass
        return health
    except Exception as e:
        return {"initialized": True, "error": str(e)}


def shutdown_cortex() -> None:
    """Gracefully shut down the cortex — drain signals, unload models, stop worker."""
    global _cortex_instance
    with _cortex_lock:
        runtime = _cortex_instance
        if not runtime:
            return
        logger.info("🧠 Shutting down Neural Cortex...")
        try:
            runtime.dispatcher.stop_background()
        except Exception as e:
            logger.warning("Dispatcher shutdown error: %s", e)
        try:
            runtime.resource_manager.unload_all()
        except Exception as e:
            logger.warning("Resource manager shutdown error: %s", e)
        runtime.initialized = False
        _cortex_instance = None
        logger.info("🧠 Neural Cortex shut down")

