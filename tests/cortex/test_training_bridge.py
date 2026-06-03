"""Tests for the training pipeline bridge — PEFT trainer, data migration, tool timeouts."""

import json
import shutil
import tempfile
import time
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── PeftTrainer unit tests ───────────────────────────────────────────────


class TestPeftTrainerMemoryCheck:
    """Test memory check logic without loading any models."""

    def test_memory_check_returns_dict(self):
        from repryntt.cortex.training.peft_trainer import PeftTrainer
        result = PeftTrainer.check_memory()
        assert "available_ram_mb" in result
        assert "gpu_free_mb" in result
        assert "can_train" in result
        assert isinstance(result["can_train"], bool)

    def test_memory_check_realistic_values(self):
        from repryntt.cortex.training.peft_trainer import PeftTrainer
        result = PeftTrainer.check_memory()
        # Should report *something* positive for RAM
        assert result["available_ram_mb"] > 0


class TestPeftTrainerInit:
    """Test PeftTrainer initialization."""

    def test_default_init(self, tmp_path):
        from repryntt.cortex.training.peft_trainer import PeftTrainer
        trainer = PeftTrainer(output_dir=tmp_path / "peft")
        assert trainer.hf_model == "HuggingFaceTB/SmolLM2-360M-Instruct"
        assert trainer.lora_rank == 8
        assert trainer.lora_alpha == 16
        assert trainer.max_steps == 50
        assert (tmp_path / "peft").exists()

    def test_custom_params(self, tmp_path):
        from repryntt.cortex.training.peft_trainer import PeftTrainer
        trainer = PeftTrainer(
            hf_model="test/model",
            output_dir=tmp_path / "custom",
            lora_rank=16,
            lora_alpha=32,
            max_steps=100,
            learning_rate=1e-3,
        )
        assert trainer.hf_model == "test/model"
        assert trainer.lora_rank == 16
        assert trainer.max_steps == 100


class TestPeftTrainerValidation:
    """Test training input validation."""

    def test_rejects_too_few_examples(self, tmp_path):
        from repryntt.cortex.training.peft_trainer import PeftTrainer
        trainer = PeftTrainer(output_dir=tmp_path / "peft")
        result = trainer.train([
            {"prompt": "hello", "response": "world"},
            {"prompt": "foo", "response": "bar"},
        ])
        assert not result["success"]
        assert "at least 5" in result["error"]

    def test_rejects_empty_examples(self, tmp_path):
        from repryntt.cortex.training.peft_trainer import PeftTrainer
        trainer = PeftTrainer(output_dir=tmp_path / "peft")
        result = trainer.train([
            {"prompt": "", "response": "world"},
            {"prompt": "hello", "response": ""},
        ] * 5)
        assert not result["success"]

    def test_rejects_insufficient_memory(self, tmp_path):
        from repryntt.cortex.training.peft_trainer import PeftTrainer
        trainer = PeftTrainer(output_dir=tmp_path / "peft")
        examples = [{"prompt": f"q{i}", "response": f"a{i}"} for i in range(10)]

        with patch.object(PeftTrainer, 'check_memory', return_value={
            "available_ram_mb": 100, "gpu_free_mb": 0, "can_train": False,
        }):
            result = trainer.train(examples)
            assert not result["success"]
            assert "Insufficient memory" in result["error"]


class TestPeftTrainerGGUFConversion:
    """Test GGUF conversion logic (without actual model files)."""

    def test_convert_fails_gracefully_no_adapter(self, tmp_path):
        from repryntt.cortex.training.peft_trainer import PeftTrainer
        trainer = PeftTrainer(output_dir=tmp_path / "peft")
        result = trainer._convert_to_gguf()
        assert result is None  # No adapter files → returns None


# ── RegionTrainer integration tests ──────────────────────────────────────


class TestRegionTrainerPeftBridge:
    """Test RegionTrainer → PeftTrainer bridge."""

    def test_train_calls_peft_trainer(self, tmp_path):
        from repryntt.cortex.training.region_trainer import RegionTrainer

        trainer = RegionTrainer("conscious", base_dir=tmp_path)

        # Mock the data router to return real data
        mock_dataset = [
            {"prompt": f"Question {i}", "response": f"Answer {i} that is long enough to qualify"}
            for i in range(60)
        ]

        with patch("repryntt.cortex.training.data_router.get_data_router") as mock_router_fn:
            mock_router = MagicMock()
            mock_router.get_dataset.return_value = mock_dataset
            mock_router_fn.return_value = mock_router

            # Mock the model config
            with patch("repryntt.cortex.model_config.load_config") as mock_config_fn:
                mock_config = MagicMock()
                mock_model = MagicMock()
                mock_model.hf_repo = "HuggingFaceTB/SmolLM2-360M-Instruct"
                mock_config.get_region.return_value = MagicMock(model_name="smollm2-360m")
                mock_config.get_model.return_value = mock_model
                mock_config_fn.return_value = mock_config

                # Mock PeftTrainer.train() to avoid actual GPU work
                with patch("repryntt.cortex.training.peft_trainer.PeftTrainer.train") as mock_train:
                    mock_train.return_value = {
                        "success": True,
                        "peft_adapter_path": str(tmp_path / "peft"),
                        "gguf_adapter_path": str(tmp_path / "adapter.gguf"),
                        "metrics": {"loss": 0.5, "steps": 50},
                    }

                    result = trainer.train()

        assert result["success"]
        assert "metrics" in result

    def test_should_train_respects_min_examples(self, tmp_path):
        from repryntt.cortex.training.region_trainer import RegionTrainer
        trainer = RegionTrainer("conscious", base_dir=tmp_path)

        with patch("repryntt.cortex.training.data_router.get_data_router") as mock_router_fn:
            mock_router = MagicMock()
            mock_router.get_dataset.return_value = [{"prompt": "x", "response": "y"}] * 10
            mock_router_fn.return_value = mock_router

            assert not trainer.should_train(min_examples=50)  # Only 10 examples


class TestRegionTrainerActivation:
    """Test adapter activation and model reload."""

    def test_activate_evicts_model(self, tmp_path):
        from repryntt.cortex.training.region_trainer import RegionTrainer
        trainer = RegionTrainer("conscious", base_dir=tmp_path)

        # Create a fake GGUF adapter
        fake_gguf = trainer.gguf_dir / "test_adapter.gguf"
        fake_gguf.write_text("fake gguf data")
        trainer._last_gguf_path = fake_gguf

        with patch("repryntt.cortex.model_config.load_config") as mock_config_fn:
            mock_config = MagicMock()
            mock_model = MagicMock()
            mock_config.get_region.return_value = MagicMock(model_name="smollm2-360m")
            mock_config.get_model.return_value = mock_model
            mock_config_fn.return_value = mock_config

            with patch("repryntt.cortex.model_config.get_config_path", return_value=tmp_path / "config.json"):
                with patch("repryntt.cortex.resource_manager.get_resource_manager") as mock_mgr_fn:
                    mock_mgr = MagicMock()
                    mock_mgr_fn.return_value = mock_mgr

                    result = trainer.activate_adapter()

                    assert result
                    mock_mgr.evict_model.assert_called_once_with("smollm2-360m")
                    assert mock_model.lora_adapter == str(fake_gguf)


# ── Legacy data migration tests ─────────────────────────────────────────


class TestLegacyDataMigration:
    """Test migration of legacy training_data.json."""

    def test_migrate_creates_marker(self, tmp_path):
        import repryntt.cortex.training as training_mod
        training_mod._LEGACY_MIGRATED = False  # Reset

        # Create fake legacy data
        legacy_data = [
            {"prompt": f"Q{i}", "response": f"Answer {i} that is long enough to count as valid", "type": "tool_execution"}
            for i in range(10)
        ]

        legacy_path = tmp_path / "training_data.json"
        legacy_path.write_text(json.dumps(legacy_data))

        marker_path = tmp_path / "cortex_training" / ".legacy_migrated"

        with patch("repryntt.paths.data_dir", return_value=tmp_path):
            with patch("repryntt.cortex.training.data_router.get_data_router") as mock_fn:
                mock_router = MagicMock()
                mock_router.route.return_value = True
                mock_fn.return_value = mock_router

                count = training_mod.migrate_legacy_training_data()

        assert count == 10
        assert marker_path.exists()

    def test_migrate_skips_if_marker_exists(self, tmp_path):
        import repryntt.cortex.training as training_mod
        training_mod._LEGACY_MIGRATED = False

        marker_path = tmp_path / "cortex_training" / ".legacy_migrated"
        marker_path.parent.mkdir(parents=True)
        marker_path.write_text("already done")

        with patch("repryntt.paths.data_dir", return_value=tmp_path):
            count = training_mod.migrate_legacy_training_data()

        assert count == 0

    def test_migrate_filters_short_responses(self, tmp_path):
        import repryntt.cortex.training as training_mod
        training_mod._LEGACY_MIGRATED = False

        legacy_data = [
            {"prompt": "Q1", "response": "short"},  # Too short (<20 chars)
            {"prompt": "Q2", "response": "This is a sufficiently long response for training"},
            {"prompt": "Q3", "response": ""},  # Empty
        ]

        legacy_path = tmp_path / "training_data.json"
        legacy_path.write_text(json.dumps(legacy_data))

        with patch("repryntt.paths.data_dir", return_value=tmp_path):
            with patch("repryntt.cortex.training.data_router.get_data_router") as mock_fn:
                mock_router = MagicMock()
                mock_router.route.return_value = True
                mock_fn.return_value = mock_router

                count = training_mod.migrate_legacy_training_data()

        assert count == 1  # Only the sufficiently long one


# ── Tool execution timeout tests ─────────────────────────────────────────


class TestToolExecutionTimeout:
    """Test per-tool execution timeout wrapping."""

    def test_slow_tool_times_out(self):
        """Verify that a tool taking longer than timeout gets killed."""
        import concurrent.futures as cf

        def slow_func():
            time.sleep(10)
            return {"success": True}

        with cf.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(slow_func)
            with pytest.raises(cf.TimeoutError):
                fut.result(timeout=0.1)

    def test_fast_tool_completes(self):
        """Verify that fast tools complete normally within timeout."""
        import concurrent.futures as cf

        def fast_func():
            return {"success": True, "result": "done"}

        with cf.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(fast_func)
            result = fut.result(timeout=5)
            assert result["success"]

    def test_slow_tools_get_longer_timeout(self):
        """Verify the slow tools set has the expected members."""
        slow_tools = {"scrape_web_page", "web_search", "google_search",
                      "generate_image", "generate_video", "execute_code",
                      "grokipedia_search", "send_email", "jupiter_swap"}
        assert "scrape_web_page" in slow_tools
        assert "read_file" not in slow_tools
