"""Tests for memory consolidation in the conscious region and trigger wiring."""

import json
import pytest
from pathlib import Path
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


# ── Conscious region _consolidate_memory ─────────────────────────────

class TestConsolidateMemory:
    """Tests for ConsciousRegion._consolidate_memory."""

    def test_empty_raw_returns_empty(self, conscious):
        """No raw memory → empty consolidation (no model call)."""
        result = conscious._consolidate_memory({"raw_memory": ""})
        assert result["success"] is True
        assert result["result"]["consolidated"] == ""

    def test_missing_raw_returns_empty(self, conscious):
        """Missing raw_memory key → empty consolidation."""
        result = conscious._consolidate_memory({})
        assert result["success"] is True
        assert result["result"]["consolidated"] == ""

    def test_model_inference_produces_consolidated(self, conscious):
        """With model available, raw memory is distilled into insights."""
        mock_result = "- Learned to prioritize trading tasks\n- Built stronger debugging patterns"
        with patch.object(conscious, "_infer", return_value=mock_result):
            result = conscious._consolidate_memory({
                "raw_memory": "Did trading research. Fixed a bug in persistent_agents.py. "
                              "Reviewed blockchain data. Fixed another bug."
            })
        assert result["success"] is True
        assert "prioritize" in result["result"]["consolidated"]
        assert result.get("fallback") is None

    def test_model_returns_none_fallback(self, conscious):
        """Model inference returns None → fallback with empty consolidation."""
        with patch.object(conscious, "_infer", return_value=None):
            result = conscious._consolidate_memory({
                "raw_memory": "Some memory entries here"
            })
        assert result["success"] is True
        assert result["result"]["consolidated"] == ""
        assert result.get("fallback") is True

    def test_raw_memory_truncated_to_3000(self, conscious):
        """Raw memory over 3000 chars is truncated in prompt."""
        long_memory = "x" * 5000
        with patch.object(conscious, "_infer", return_value="ok") as mock_infer:
            conscious._consolidate_memory({"raw_memory": long_memory})
        prompt = mock_infer.call_args[0][0]
        # The prompt should contain at most 3000 chars of raw memory
        assert "x" * 3001 not in prompt
        assert "x" * 3000 in prompt

    def test_no_model_returns_empty(self, conscious_no_model):
        """No model loaded → inference returns None → fallback."""
        result = conscious_no_model._consolidate_memory({
            "raw_memory": "Some entries"
        })
        assert result["success"] is True
        # No model means _infer returns None
        assert result["result"]["consolidated"] == ""


class TestConsolidationRouting:
    """Tests that memory_consolidation signals route correctly."""

    def test_process_routes_to_consolidation(self, conscious):
        """process() with type=memory_consolidation calls _consolidate_memory."""
        with patch.object(conscious, "_consolidate_memory", return_value={"success": True}) as mock:
            conscious.process({"type": "memory_consolidation", "raw_memory": "test"})
        mock.assert_called_once()

    def test_consolidation_in_process_types(self):
        """memory_consolidation is a registered process type."""
        from repryntt.cortex.regions.conscious import PROCESS_TYPES
        assert "memory_consolidation" in PROCESS_TYPES


class TestDispatcherConsolidation:
    """Tests for dispatcher request_memory_consolidation."""

    def test_request_memory_consolidation_signal(self):
        """Dispatcher creates proper consolidation signal."""
        from repryntt.cortex.dispatcher import CortexDispatcher
        d = CortexDispatcher.__new__(CortexDispatcher)
        d._regions = {}

        # Mock the synchronous dispatch
        with patch.object(d, "send_and_wait") as mock_dispatch:
            mock_dispatch.return_value = {"success": True, "result": {"consolidated": "insights"}}
            result = d.request_memory_consolidation("raw memory text here")

        signal = mock_dispatch.call_args[0][0]
        assert signal.target == "conscious"
        assert signal.payload["type"] == "memory_consolidation"
        assert signal.payload["raw_memory"] == "raw memory text here"
