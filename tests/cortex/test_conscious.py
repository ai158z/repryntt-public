"""Tests for repryntt.cortex.regions.conscious — Conscious layer."""

import pytest
from unittest.mock import patch, MagicMock
from repryntt.cortex.regions.conscious import ConsciousRegion


@pytest.fixture
def conscious():
    c = ConsciousRegion()
    c._model_name = "test-model"
    c._identity_context = "I am Andrew, an autonomous AI."
    return c


@pytest.fixture
def conscious_no_model():
    c = ConsciousRegion()
    c._model_name = None
    return c


# ── Pre-heartbeat filter ─────────────────────────────────────────────

class TestPreHeartbeatFilter:
    """Tests for _pre_heartbeat_filter."""

    def test_returns_score_and_reason(self, conscious):
        with patch("repryntt.cortex.resource_manager.get_resource_manager") as mock_mgr:
            mock_mgr.return_value.classify_yes_no.return_value = 0.65
            result = conscious._pre_heartbeat_filter({
                "type": "pre_heartbeat_filter",
                "context": "Tasks pending",
                "pending_tasks": 3,
                "recent_activity": "research",
            })
        assert result["success"] is True
        assert 0 <= result["result"]["score"] <= 1
        assert isinstance(result["result"]["reason"], str)

    def test_no_model_returns_default(self, conscious_no_model):
        result = conscious_no_model._pre_heartbeat_filter({
            "type": "pre_heartbeat_filter",
            "context": "test",
        })
        assert result["result"]["score"] == 0.7
        assert result["result"]["reason"] == "no model"

    def test_classification_failure_returns_default(self, conscious):
        with patch("repryntt.cortex.resource_manager.get_resource_manager") as mock_mgr:
            mock_mgr.return_value.classify_yes_no.return_value = None
            result = conscious._pre_heartbeat_filter({
                "type": "pre_heartbeat_filter",
                "context": "test",
            })
        assert result["result"]["score"] == 0.7

    def test_reason_includes_task_count(self, conscious):
        with patch("repryntt.cortex.resource_manager.get_resource_manager") as mock_mgr:
            mock_mgr.return_value.classify_yes_no.return_value = 0.5
            result = conscious._pre_heartbeat_filter({
                "type": "pre_heartbeat_filter",
                "context": "stuff",
                "pending_tasks": 5,
            })
        assert "5" in result["result"]["reason"]


# ── Memory consolidation ─────────────────────────────────────────────

class TestMemoryConsolidation:
    """Tests for _consolidate_memory."""

    def test_empty_memory_returns_empty(self, conscious):
        result = conscious._consolidate_memory({
            "type": "memory_consolidation",
            "raw_memory": "",
        })
        assert result["result"]["consolidated"] == ""

    def test_consolidation_with_model(self, conscious):
        with patch.object(conscious, "_infer", return_value="- Learned about ML\n- Grew as a person"):
            result = conscious._consolidate_memory({
                "type": "memory_consolidation",
                "raw_memory": "Did some research about machine learning today.",
            })
        assert result["success"] is True
        assert len(result["result"]["consolidated"]) > 0

    def test_consolidation_fallback_no_model(self, conscious):
        with patch.object(conscious, "_infer", return_value=None):
            result = conscious._consolidate_memory({
                "type": "memory_consolidation",
                "raw_memory": "some memory",
            })
        assert result.get("fallback") is True


# ── Self-reflection ──────────────────────────────────────────────────

class TestSelfReflection:
    """Tests for _self_reflect."""

    def test_reflection_stored(self, conscious):
        with patch.object(conscious, "_infer", return_value="I'm growing."):
            with patch("repryntt.cortex.dispatcher.get_dispatcher") as _:
                result = conscious._self_reflect({
                    "type": "self_reflection",
                    "last_action": "researched ML",
                    "current_goal": "learn more",
                })
        assert result["result"]["reflection"] == "I'm growing."
        assert "I'm growing." in conscious._recent_reflections

    def test_keeps_max_10_reflections(self, conscious):
        conscious._recent_reflections = [f"r{i}" for i in range(10)]
        with patch.object(conscious, "_infer", return_value="new reflection"):
            with patch("repryntt.cortex.dispatcher.get_dispatcher"):
                conscious._self_reflect({"type": "self_reflection"})
        assert len(conscious._recent_reflections) == 10
        assert conscious._recent_reflections[-1] == "new reflection"

    def test_fallback_no_model(self, conscious):
        with patch.object(conscious, "_infer", return_value=None):
            result = conscious._self_reflect({"type": "self_reflection"})
        assert result.get("fallback") is True


# ── Voice pre-response ───────────────────────────────────────────────

class TestVoicePreresponse:

    def test_generates_acknowledgment(self, conscious):
        with patch.object(conscious, "_infer", return_value="On it!"):
            result = conscious._voice_preresponse({
                "type": "voice_preresponse",
                "user_text": "What's the weather?",
                "history": "",
            })
        assert result["result"]["text"] == "On it!"

    def test_fallback_returns_canned(self, conscious):
        with patch.object(conscious, "_infer", return_value=None):
            result = conscious._voice_preresponse({
                "type": "voice_preresponse",
                "user_text": "Hello",
            })
        assert result.get("fallback") is True
        assert len(result["result"]["text"]) > 0


# ── Personality rewrite ──────────────────────────────────────────────

class TestPersonalityRewrite:

    def test_rewrites_text(self, conscious):
        with patch.object(conscious, "_infer", return_value="Yo, that's cool."):
            result = conscious._personality_rewrite({
                "type": "personality_rewrite",
                "text": "The analysis is complete.",
                "context": "spoken",
            })
        assert result["result"]["text"] == "Yo, that's cool."

    def test_empty_text_passthrough(self, conscious):
        result = conscious._personality_rewrite({
            "type": "personality_rewrite",
            "text": "",
        })
        assert result["result"]["text"] == ""


# ── Identity query ───────────────────────────────────────────────────

class TestIdentityQuery:

    def test_answers_identity(self, conscious):
        with patch.object(conscious, "_infer", return_value="I am Andrew."):
            result = conscious._identity_query({
                "type": "identity_query",
                "question": "Who are you?",
            })
        assert result["result"]["answer"] == "I am Andrew."


# ── Process dispatch ─────────────────────────────────────────────────

class TestProcessDispatch:

    def test_unknown_type_returns_error(self, conscious):
        result = conscious.process({"type": "garbage"})
        assert result["success"] is False

    def test_routes_to_correct_handler(self, conscious):
        with patch.object(conscious, "_pre_heartbeat_filter") as mock:
            mock.return_value = {"success": True, "result": {"score": 0.5}}
            conscious.process({"type": "pre_heartbeat_filter"})
            mock.assert_called_once()


# ── Fallback ─────────────────────────────────────────────────────────

class TestFallback:

    def test_fallback_filter(self, conscious_no_model):
        result = conscious_no_model.fallback({"type": "pre_heartbeat_filter"})
        assert result["result"]["score"] == 0.7

    def test_fallback_voice(self, conscious_no_model):
        result = conscious_no_model.fallback({"type": "voice_preresponse"})
        assert result.get("fallback") is True

    def test_fallback_identity(self, conscious_no_model):
        result = conscious_no_model.fallback({"type": "identity_query"})
        assert "Andrew" in result["result"]["answer"]


# ── _parse_score ─────────────────────────────────────────────────────

class TestParseScore:

    def test_parse_normal(self):
        score, reason = ConsciousRegion._parse_score("SCORE=0.8 REASON=active tasks")
        assert abs(score - 0.8) < 0.01
        assert "active" in reason

    def test_parse_1_10_scale(self):
        score, _ = ConsciousRegion._parse_score("SCORE=7 REASON=busy")
        assert abs(score - 0.7) < 0.01

    def test_parse_no_match(self):
        score, _ = ConsciousRegion._parse_score("garbage output")
        assert score == 0.5


# ── Identity hot-reload ──────────────────────────────────────────────

class TestIdentityHotReload:

    def test_maybe_reload_updates_on_change(self, conscious, tmp_path):
        import time
        with patch("repryntt.paths.brain_dir", return_value=tmp_path):
            bootstrap = tmp_path / "bootstrap"
            bootstrap.mkdir()
            pulse = bootstrap / "PULSE.md"
            pulse.write_text("Original identity")
            conscious._identity_mtime = pulse.stat().st_mtime

            # Modify the file
            time.sleep(0.1)
            pulse.write_text("Updated identity")

            conscious.maybe_reload_identity()
            assert "Updated" in conscious._identity_context


# ── Training data generation ─────────────────────────────────────────

class TestTrainingDataGeneration:

    def test_generates_from_reflections(self, conscious):
        conscious._recent_reflections = [
            "I'm getting better at research.",
            "Short.",  # Too short (< 20 chars) — should be skipped
            "I notice I've been more methodical about breaking down complex problems.",
        ]
        data = conscious.generate_training_data()
        assert len(data) == 2  # Only the two long-enough reflections
        assert data[0]["region"] == "conscious"
