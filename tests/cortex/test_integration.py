"""Integration tests for repryntt.cortex — End-to-end flows."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from repryntt.cortex.dispatcher import CortexDispatcher, CortexSignal, Priority
from repryntt.cortex.regions.guardian import GuardianRegion
from repryntt.cortex.regions.conscious import ConsciousRegion


@pytest.fixture
def full_dispatcher():
    """Dispatcher with guardian + conscious regions registered."""
    d = CortexDispatcher(max_queue_size=100)
    guardian = GuardianRegion()
    guardian.initialize()
    d.register_region(guardian)

    conscious = ConsciousRegion()
    conscious.initialize(model_name=None)  # Fallback mode
    d.register_region(conscious)

    return d


# ── Guardian → Tool execution flow ───────────────────────────────────

class TestGuardianToolValidation:
    """Guardian blocks dangerous tools before execution."""

    def test_guardian_blocks_rm_rf(self, full_dispatcher):
        result = full_dispatcher.request_guardian_validation(
            "execute_shell", {"command": "rm -rf /"}
        )
        assert result["result"]["allowed"] is False

    def test_guardian_allows_safe_tool(self, full_dispatcher):
        result = full_dispatcher.request_guardian_validation(
            "read_file", {"path": str(Path.cwd() / "README.md")}
        )
        assert result["result"]["allowed"] is True

    def test_guardian_blocks_count_tracks(self, full_dispatcher):
        full_dispatcher.request_guardian_validation(
            "execute_shell", {"command": "rm -rf /"}
        )
        assert full_dispatcher._stats["guardian_blocks"] >= 1


# ── Conscious filter flow ────────────────────────────────────────────

class TestConsciousFilterFlow:
    """Conscious pre-filter via dispatcher (fallback mode)."""

    def test_filter_returns_valid_score(self, full_dispatcher):
        result = full_dispatcher.request_conscious_filter(
            context="Tasks pending",
            pending_tasks=3,
        )
        assert result["success"] is True
        score = result["result"]["score"]
        assert 0 <= score <= 1

    def test_filter_fallback_mode(self, full_dispatcher):
        result = full_dispatcher.request_conscious_filter(
            context="idle",
            pending_tasks=0,
        )
        # In fallback mode (no model), should return 0.7 default
        assert result["result"]["score"] == 0.7


# ── Memory consolidation flow ────────────────────────────────────────

class TestConsolidationFlow:

    def test_consolidation_returns_empty_in_fallback(self, full_dispatcher):
        result = full_dispatcher.request_memory_consolidation(
            "Today I researched machine learning."
        )
        assert result["success"] is True
        # In fallback mode, consolidated is empty
        assert result.get("fallback") is True or result["result"]["consolidated"] == ""


# ── Training data → dataset ──────────────────────────────────────────

class TestTrainingCollectToDataset:
    """Heartbeat data flows into training datasets."""

    def test_training_example_stored(self, tmp_path):
        from repryntt.cortex.training.data_router import DataRouter
        router = DataRouter(base_dir=tmp_path)
        router.route({
            "region": "conscious",
            "type": "heartbeat_plan",
            "prompt": "Research federated learning",
            "response": "Completed research, found 5 papers.",
            "quality": 4,
            "heartbeat": 42,
        })
        dataset = router.get_dataset("conscious")
        assert len(dataset) == 1
        assert dataset[0]["heartbeat"] == 42
        assert dataset[0]["quality"] == 4


# ── Voice pre-response flow ──────────────────────────────────────────

class TestVoiceFlow:

    def test_voice_fallback(self, full_dispatcher):
        result = full_dispatcher.request_voice_preresponse(
            user_text="What's the weather?",
            history="",
        )
        assert result["success"] is True
        assert len(result["result"]["text"]) > 0


# ── Self-reflection flow ─────────────────────────────────────────────

class TestSelfReflectionFlow:

    def test_reflection_queued(self, full_dispatcher):
        result = full_dispatcher.request_self_reflection(
            last_action="Researched ML",
            current_goal="Learn more",
        )
        assert result["queued"] is True


# ── Full heartbeat cycle simulation ──────────────────────────────────

class TestHeartbeatCycle:
    """Simulate a mini heartbeat cycle through the cortex."""

    def test_full_cycle(self, full_dispatcher, tmp_path):
        # 1. Pre-filter
        filter_result = full_dispatcher.request_conscious_filter(
            context="Chain active: research ML",
            pending_tasks=2,
        )
        assert filter_result["result"]["score"] >= 0

        # 2. Guardian validates a tool
        guard_result = full_dispatcher.request_guardian_validation(
            "google_web_search", {"query": "federated learning"}
        )
        assert guard_result["result"]["allowed"] is True

        # 3. Post-heartbeat reflection
        reflect_result = full_dispatcher.request_self_reflection(
            last_action="Searched for federated learning",
            last_result="Found 5 papers",
            current_goal="Build federated training",
        )
        assert reflect_result["queued"] is True

        # 4. Route training data
        from repryntt.cortex.training.data_router import DataRouter
        router = DataRouter(base_dir=tmp_path)
        routed = router.route({
            "region": "conscious",
            "type": "heartbeat_plan",
            "prompt": "Research federated learning approaches",
            "response": "Found and summarized 5 key papers.",
            "quality": 4,
        })
        assert routed is True

        # 5. Memory consolidation
        consol_result = full_dispatcher.request_memory_consolidation(
            "Researched ML. Found 5 papers. Built summary."
        )
        assert consol_result["success"] is True


# ── Identity query integration ───────────────────────────────────────

class TestIdentityQueryIntegration:
    """Test identity query through the full dispatch pipeline."""

    def test_identity_query_returns_answer(self, full_dispatcher):
        result = full_dispatcher.request_identity_query("What do I value?")
        # Falls back since no model loaded, but should still return structure
        assert result["success"] is True
        assert "answer" in result.get("result", {})

    def test_personality_rewrite_preserves_text_in_fallback(self, full_dispatcher):
        result = full_dispatcher.request_personality_rewrite("Hello world")
        assert result["success"] is True
        # In fallback mode, should return original text
        assert result["result"]["text"] == "Hello world"


# ── Voice pre-response integration ──────────────────────────────────

class TestVoicePreresponseIntegration:

    def test_voice_preresponse_returns_text(self, full_dispatcher):
        result = full_dispatcher.request_voice_preresponse("Hey, what's up?")
        assert result["success"] is True
        assert "text" in result.get("result", {})
