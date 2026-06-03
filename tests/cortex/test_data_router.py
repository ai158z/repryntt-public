"""Tests for repryntt.cortex.training.data_router — Training data pipeline."""

import json
import pytest
from pathlib import Path
from repryntt.cortex.training.data_router import DataRouter


@pytest.fixture
def router(tmp_path):
    return DataRouter(base_dir=tmp_path)


# ── Routing ──────────────────────────────────────────────────────────

class TestRouting:
    """Tests for route() — quality gates, classification, storage."""

    def test_routes_valid_example(self, router):
        result = router.route({
            "region": "conscious",
            "type": "self_reflection",
            "prompt": "Reflect on your day.",
            "response": "I learned about neural networks.",
            "quality": 4,
        })
        assert result is True
        dataset = router.get_dataset("conscious")
        assert len(dataset) == 1

    def test_rejects_low_quality(self, router):
        result = router.route({
            "region": "conscious",
            "type": "self_reflection",
            "prompt": "test",
            "response": "test",
            "quality": 2,
        })
        assert result is False
        assert len(router.get_dataset("conscious")) == 0

    def test_rejects_quality_3(self, router):
        result = router.route({
            "region": "conscious",
            "prompt": "test",
            "response": "test",
            "quality": 3,
        })
        assert result is False

    def test_auto_classifies_region(self, router):
        router.route({
            "type": "self_reflection",
            "prompt": "reflect on things",
            "response": "I feel good",
            "quality": 4,
        })
        assert len(router.get_dataset("conscious")) == 1

    def test_auto_classifies_motor(self, router):
        router.route({
            "type": "motor_command",
            "prompt": "move forward",
            "response": "ok",
            "quality": 4,
        })
        assert len(router.get_dataset("executor")) == 1

    def test_timestamps_added(self, router):
        router.route({
            "region": "conscious",
            "prompt": "test",
            "response": "test",
            "quality": 4,
        })
        dataset = router.get_dataset("conscious")
        assert "timestamp" in dataset[0]


# ── Dataset management ───────────────────────────────────────────────

class TestDatasetManagement:
    """Tests for dataset trimming and stats."""

    def test_trims_over_limit(self, router):
        from repryntt.cortex.training.data_router import MAX_EXAMPLES_PER_REGION
        # Route more than MAX examples
        for i in range(MAX_EXAMPLES_PER_REGION + 10):
            router.route({
                "region": "conscious",
                "prompt": f"p{i}",
                "response": f"r{i}",
                "quality": 5,
            })
        dataset = router.get_dataset("conscious")
        assert len(dataset) <= MAX_EXAMPLES_PER_REGION

    def test_dataset_stats(self, router):
        for i in range(5):
            router.route({
                "region": "conscious",
                "prompt": f"p{i}",
                "response": f"r{i}",
                "quality": 4,
            })
        stats = router.dataset_stats()
        assert "conscious" in stats
        assert stats["conscious"]["examples"] == 5

    def test_route_batch(self, router):
        examples = [
            {"region": "conscious", "prompt": f"p{i}", "response": f"r{i}", "quality": 4}
            for i in range(5)
        ]
        count = router.route_batch(examples)
        assert count == 5


# ── Classification heuristics ────────────────────────────────────────

class TestClassification:
    """Tests for _classify_region()."""

    def test_type_mapping(self):
        assert DataRouter._classify_region({"type": "self_reflection"}) == "conscious"
        assert DataRouter._classify_region({"type": "motor_command"}) == "executor"
        assert DataRouter._classify_region({"type": "camera_classification"}) == "perception"
        assert DataRouter._classify_region({"type": "safety_check"}) == "guardian"

    def test_keyword_fallback(self):
        assert DataRouter._classify_region({
            "type": "unknown",
            "prompt": "move the robot",
            "response": "navigate forward",
        }) == "executor"

    def test_default_conscious(self):
        assert DataRouter._classify_region({
            "type": "unknown",
            "prompt": "hello",
            "response": "hi",
        }) == "conscious"


# ── Deduplication ────────────────────────────────────────────────────

class TestDeduplication:
    """Tests for hash-based exact-match dedup."""

    def test_duplicate_prompt_rejected(self, router):
        ex = {"region": "conscious", "prompt": "same", "response": "resp1", "quality": 5}
        assert router.route(ex) is True
        ex2 = {"region": "conscious", "prompt": "same", "response": "resp2", "quality": 5}
        assert router.route(ex2) is False
        assert len(router.get_dataset("conscious")) == 1

    def test_different_prompts_accepted(self, router):
        assert router.route({"region": "conscious", "prompt": "a", "response": "r", "quality": 5}) is True
        assert router.route({"region": "conscious", "prompt": "b", "response": "r", "quality": 5}) is True
        assert len(router.get_dataset("conscious")) == 2
