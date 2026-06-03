"""Tests for repryntt.cortex.resource_manager — VRAM budget and model lifecycle."""

import pytest
from unittest.mock import patch, MagicMock
from repryntt.cortex.resource_manager import ResourceManager, LoadedModel
from repryntt.cortex.model_config import CortexConfig, ModelEntry, ModelFormat


# ── Fixtures ─────────────────────────────────────────────────────────

class MockRegistry:
    """Minimal model registry for testing."""

    def __init__(self, models=None):
        self._models = {m.name: m for m in (models or [])}

    def get(self, name):
        return self._models.get(name)

    def all_models(self):
        return list(self._models.values())

    def missing_models(self):
        return [m for m in self._models.values() if not m.resolved_path().exists()]

    def save(self):
        pass


def make_entry(name="test-model", vram=400, fmt=ModelFormat.GGUF, path="/tmp/test.gguf"):
    return ModelEntry(
        name=name,
        path=path,
        format=fmt.value if hasattr(fmt, 'value') else str(fmt),
        vram_mb=vram,
        param_count=360_000_000,
        context_length=2048,
        role="conscious",
    )


@pytest.fixture
def config():
    return CortexConfig(memory_budget_mb=1000)


@pytest.fixture
def mgr(config):
    registry = MockRegistry([make_entry()])
    return ResourceManager(config=config, registry=registry)


# ── Budget computation ───────────────────────────────────────────────

class TestBudget:

    def test_explicit_budget(self, config):
        config.memory_budget_mb = 500
        registry = MockRegistry()
        mgr = ResourceManager(config=config, registry=registry)
        assert mgr.budget_mb == 500

    def test_budget_properties(self, mgr):
        assert mgr.budget_mb == 1000
        assert mgr.used_mb == 0
        assert mgr.available_mb == 1000


# ── Latency tracking ────────────────────────────────────────────────

class TestLatencyTracking:

    def test_record_and_stats(self, mgr):
        for ms in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
            mgr._record_latency("test-model", ms)

        stats = mgr.latency_stats("test-model")
        assert stats["count"] == 10
        assert stats["p50"] > 0
        assert stats["p95"] >= stats["p50"]
        assert stats["p99"] >= stats["p95"]

    def test_empty_stats(self, mgr):
        stats = mgr.latency_stats("nonexistent")
        assert stats["count"] == 0

    def test_trims_history(self, mgr):
        mgr._max_latency_history = 10
        for i in range(20):
            mgr._record_latency("test-model", float(i))
        assert len(mgr._latency_history["test-model"]) == 10


# ── Status ───────────────────────────────────────────────────────────

class TestStatus:

    def test_status_structure(self, mgr):
        status = mgr.status()
        assert "budget_mb" in status
        assert "used_mb" in status
        assert "loaded_models" in status
        assert "registered_models" in status
        assert "missing_models" in status

    def test_unload_all(self, mgr):
        # Fake a loaded model
        entry = make_entry()
        mgr._loaded["test-model"] = LoadedModel(
            entry=entry, backend="llama_cpp", handle=None, vram_used_mb=400,
        )
        assert mgr.used_mb == 400
        mgr.unload_all()
        assert mgr.used_mb == 0

    def test_status_includes_latency(self, mgr):
        mgr._record_latency("test-model", 42.0)
        status = mgr.status()
        assert "latency" in status
        assert "test-model" in status["latency"]
        assert status["latency"]["test-model"]["count"] == 1


# ── Model download ───────────────────────────────────────────────────

class TestModelDownload:

    def test_download_unknown_model(self):
        from repryntt.cortex.model_registry import ModelRegistry
        from repryntt.cortex.model_config import CortexConfig
        config = CortexConfig()
        registry = ModelRegistry(config=config)
        assert registry.download_model("nonexistent") is False

    def test_download_model_already_exists(self, tmp_path):
        from repryntt.cortex.model_registry import ModelRegistry
        from repryntt.cortex.model_config import CortexConfig, ModelEntry
        model_file = tmp_path / "test.gguf"
        model_file.write_text("fake model data")
        config = CortexConfig(models=[
            ModelEntry(name="test", role="conscious", format="gguf",
                       path=str(model_file), hf_repo="test/repo")
        ])
        registry = ModelRegistry(config=config)
        assert registry.download_model("test") is True

    def test_download_no_hf_repo(self, tmp_path):
        from repryntt.cortex.model_registry import ModelRegistry
        from repryntt.cortex.model_config import CortexConfig, ModelEntry
        config = CortexConfig(models=[
            ModelEntry(name="test", role="conscious", format="gguf",
                       path=str(tmp_path / "test.gguf"), hf_repo="")
        ])
        registry = ModelRegistry(config=config)
        assert registry.download_model("test") is False

    def test_download_missing_returns_count(self):
        from repryntt.cortex.model_registry import ModelRegistry
        from repryntt.cortex.model_config import CortexConfig
        config = CortexConfig()
        registry = ModelRegistry(config=config)
        # No missing models when none registered
        assert registry.download_missing() == 0
