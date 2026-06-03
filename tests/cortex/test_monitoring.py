"""Tests for cortex health/monitoring endpoint and telemetry structure."""

import pytest
from unittest.mock import patch, MagicMock, PropertyMock


class TestCortexHealth:
    """Tests for the cortex_health() function."""

    def test_uninitialized_returns_false(self):
        """When cortex is not initialized, returns initialized=False."""
        import repryntt.cortex as cortex_mod
        original = cortex_mod._cortex_instance
        try:
            cortex_mod._cortex_instance = None
            result = cortex_mod.cortex_health()
            assert result == {"initialized": False}
        finally:
            cortex_mod._cortex_instance = original

    def test_initialized_returns_structure(self):
        """When cortex is initialized, returns full health dict."""
        import repryntt.cortex as cortex_mod

        mock_dispatcher = MagicMock()
        mock_dispatcher.health.return_value = {
            "queue_depth": 0,
            "signals_processed": 42,
            "signals_dropped": 0,
            "guardian_blocks": 1,
            "worker_alive": True,
            "regions": {"guardian": {"state": "active", "model": None}},
        }

        mock_resource_mgr = MagicMock()
        mock_resource_mgr.status.return_value = {
            "budget_mb": 512,
            "used_mb": 200,
            "loaded": {},
        }

        mock_runtime = MagicMock()
        mock_runtime.dispatcher = mock_dispatcher
        mock_runtime.resource_manager = mock_resource_mgr

        original = cortex_mod._cortex_instance
        try:
            cortex_mod._cortex_instance = mock_runtime
            result = cortex_mod.cortex_health()

            assert result["initialized"] is True
            assert "dispatcher" in result
            assert result["dispatcher"]["signals_processed"] == 42
            assert result["dispatcher"]["worker_alive"] is True
            assert "resources" in result
            assert result["resources"]["budget_mb"] == 512
            assert "training" in result
        finally:
            cortex_mod._cortex_instance = original

    def test_health_includes_training_stats(self):
        """Training stats are included when data router is available."""
        import repryntt.cortex as cortex_mod

        mock_dispatcher = MagicMock()
        mock_dispatcher.health.return_value = {"queue_depth": 0, "regions": {}}
        mock_resource_mgr = MagicMock()
        mock_resource_mgr.status.return_value = {"loaded": {}}

        mock_runtime = MagicMock()
        mock_runtime.dispatcher = mock_dispatcher
        mock_runtime.resource_manager = mock_resource_mgr

        original = cortex_mod._cortex_instance
        try:
            cortex_mod._cortex_instance = mock_runtime
            with patch("repryntt.cortex.training.data_router.get_data_router") as mock_dr:
                mock_dr.return_value.dataset_stats.return_value = {
                    "conscious": {"count": 50, "last_added": "2026-04-12"},
                }
                result = cortex_mod.cortex_health()

            assert result["training"]["conscious"]["count"] == 50
        finally:
            cortex_mod._cortex_instance = original


class TestDispatcherHealth:
    """Tests for CortexDispatcher.health() structure."""

    def test_health_returns_required_fields(self):
        """health() output has all expected keys."""
        from repryntt.cortex.dispatcher import CortexDispatcher
        d = CortexDispatcher.__new__(CortexDispatcher)
        d._regions = {}
        d._stats = {
            "signals_processed": 10,
            "signals_dropped": 1,
            "guardian_blocks": 2,
        }
        import queue
        d._queue = queue.PriorityQueue()
        d._worker_thread = None

        result = d.health()
        assert "queue_depth" in result
        assert "signals_processed" in result
        assert "signals_dropped" in result
        assert "guardian_blocks" in result
        assert "worker_alive" in result
        assert "regions" in result
        assert result["signals_processed"] == 10
        assert result["worker_alive"] is False  # No worker thread

    def test_health_regions_include_state(self):
        """health() lists regions with state and model."""
        from repryntt.cortex.dispatcher import CortexDispatcher
        from repryntt.cortex.regions.guardian import GuardianRegion

        d = CortexDispatcher.__new__(CortexDispatcher)
        d._stats = {"signals_processed": 0, "signals_dropped": 0, "guardian_blocks": 0}
        import queue
        d._queue = queue.PriorityQueue()
        d._worker_thread = None

        guardian = GuardianRegion()
        guardian.initialize()
        d._regions = {"guardian": guardian}

        result = d.health()
        assert "guardian" in result["regions"]
        assert result["regions"]["guardian"]["state"] == "ready"


class TestResourceManagerStatus:
    """Tests for ResourceManager.status() structure."""

    def test_status_returns_budget_info(self):
        """status() includes budget and loaded model info."""
        from repryntt.cortex.resource_manager import ResourceManager
        import threading
        mgr = ResourceManager.__new__(ResourceManager)
        mgr._budget_mb = 512
        mgr._used_mb = 200
        mgr._loaded = {}
        mgr._lock = threading.RLock()  # RLock to allow reentrant access
        mgr._latency_history = {}
        mock_registry = MagicMock()
        mock_registry.all_models.return_value = []
        mock_registry.missing_models.return_value = []
        mgr.registry = mock_registry

        result = mgr.status()
        assert isinstance(result, dict)
        assert result["budget_mb"] == 512
        assert "loaded_models" in result
        assert "registered_models" in result
