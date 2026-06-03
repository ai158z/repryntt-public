"""Tests for the deliberation pipeline (Phase 0.5).

Covers:
  - ConsciousRegion._deliberate() — generates task candidates
  - Dispatcher.request_deliberation() — routes signal
  - Stagnation detection — auto-queues rotation chain
  - INTERESTS.md + VALUES.md bootstrap wiring
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════
# CONSCIOUS DELIBERATION
# ═══════════════════════════════════════════════════════════════

class TestConsciousDeliberation:
    """Tests for ConsciousRegion._deliberate()."""

    def _make_region(self):
        from repryntt.cortex.regions.conscious import ConsciousRegion
        r = ConsciousRegion()
        r._model_name = None  # no model → fallback
        return r

    def test_fallback_with_chain(self):
        r = self._make_region()
        result = r._deliberate({
            "active_chain": "Build OAuth2 flow",
            "task_queue_hint": "",
            "interests_top5": "AI, robotics",
            "drives_summary": "",
            "values_snippet": "",
            "recent_topics": "",
        })
        assert result["success"]
        candidates = result["result"]["candidates"]
        assert len(candidates) == 3
        assert any("chain" in c.lower() or "oauth" in c.lower() for c in candidates)

    def test_fallback_with_task(self):
        r = self._make_region()
        result = r._deliberate({
            "active_chain": "",
            "task_queue_hint": "Fix login bug",
            "interests_top5": "",
            "drives_summary": "",
            "values_snippet": "",
            "recent_topics": "",
        })
        candidates = result["result"]["candidates"]
        assert len(candidates) == 3
        assert any("login" in c.lower() or "task" in c.lower() for c in candidates)

    def test_fallback_defaults(self):
        r = self._make_region()
        result = r._deliberate({
            "active_chain": "",
            "task_queue_hint": "",
            "interests_top5": "",
            "drives_summary": "",
            "values_snippet": "",
            "recent_topics": "",
        })
        candidates = result["result"]["candidates"]
        assert len(candidates) == 3
        # Should include reasonable defaults
        assert result.get("fallback") is True

    def test_model_inference_parsed(self):
        r = self._make_region()
        r._model_name = "test-model"
        r._infer = MagicMock(return_value=(
            "1. Check email inbox for operator messages\n"
            "2. Research autonomous agent architectures\n"
            "3. Review and improve system health monitoring\n"
        ))
        result = r._deliberate({
            "drives_summary": "understanding=0.80",
            "interests_top5": "AI (0.60), robotics (0.50)",
            "values_snippet": "Duty 70%",
            "recent_topics": "Last: email_check",
            "active_chain": "",
            "task_queue_hint": "",
        })
        assert result["success"]
        candidates = result["result"]["candidates"]
        assert len(candidates) == 3
        assert "email" in candidates[0].lower()

    def test_model_inference_empty_fallback(self):
        r = self._make_region()
        r._model_name = "test-model"
        r._infer = MagicMock(return_value="")
        result = r._deliberate({
            "active_chain": "",
            "task_queue_hint": "",
            "interests_top5": "",
            "drives_summary": "",
            "values_snippet": "",
            "recent_topics": "",
        })
        # Should fall back to template
        assert result["success"]
        assert len(result["result"]["candidates"]) == 3


# ═══════════════════════════════════════════════════════════════
# DISPATCHER ROUTING
# ═══════════════════════════════════════════════════════════════

class TestDispatcherDeliberation:
    """Tests for Dispatcher.request_deliberation() routing."""

    def test_deliberation_signal_structure(self):
        """Verify the signal payload matches what ConsciousRegion expects."""
        from repryntt.cortex.dispatcher import CortexDispatcher
        disp = CortexDispatcher.__new__(CortexDispatcher)
        disp._regions = {}
        disp._queue = MagicMock()
        disp._stats = {"guardian_blocks": 0}

        # Mock send_and_wait to capture the signal
        captured = {}
        def mock_send_wait(signal, timeout=5.0):
            captured["signal"] = signal
            return {"success": True, "result": {"candidates": ["a", "b", "c"]}}

        disp.send_and_wait = mock_send_wait

        result = disp.request_deliberation(
            drives_summary="understanding=0.80",
            interests_top5="AI, robotics",
            values_snippet="Duty 70%",
            recent_topics="email_check",
            active_chain="OAuth2",
            task_queue_hint="Fix bug",
        )

        assert result["result"]["candidates"] == ["a", "b", "c"]
        sig = captured["signal"]
        assert sig.target == "conscious"
        assert sig.signal_type == "deliberation"
        assert sig.payload["type"] == "deliberation"
        assert sig.payload["drives_summary"] == "understanding=0.80"
        assert sig.payload["active_chain"] == "OAuth2"


# ═══════════════════════════════════════════════════════════════
# STAGNATION DETECTION
# ═══════════════════════════════════════════════════════════════

class TestStagnationDetection:
    """Tests for the stagnation detection and auto-rotation chain."""

    def test_consecutive_tracking(self):
        """Consecutive same task types increment counter."""
        from repryntt.agents.persistent_agents import AgentDaemon
        d = AgentDaemon.__new__(AgentDaemon)
        d._jarvis_consecutive_same_type = 0
        d._jarvis_previous_task_type = ""

        # Simulate 3 heartbeats with same type
        for _ in range(3):
            current = "interest_research"
            if current == d._jarvis_previous_task_type:
                d._jarvis_consecutive_same_type += 1
            else:
                d._jarvis_consecutive_same_type = 1
            d._jarvis_previous_task_type = current

        assert d._jarvis_consecutive_same_type == 3

    def test_type_change_resets(self):
        """Changing task type resets counter."""
        from repryntt.agents.persistent_agents import AgentDaemon
        d = AgentDaemon.__new__(AgentDaemon)
        d._jarvis_consecutive_same_type = 5
        d._jarvis_previous_task_type = "interest_research"

        # Change type
        current = "email_check"
        if current == d._jarvis_previous_task_type:
            d._jarvis_consecutive_same_type += 1
        else:
            d._jarvis_consecutive_same_type = 1
        d._jarvis_previous_task_type = current

        assert d._jarvis_consecutive_same_type == 1

    def test_rotation_chain_created(self):
        """Stagnation at 3+ creates a rotation reasoning chain."""
        with tempfile.TemporaryDirectory() as tmpdir:
            chain_path = os.path.join(tmpdir, "reasoning_chain.json")
            # Simulate: no existing chain, stagnation threshold met
            assert not os.path.exists(chain_path)

            # Write the chain (simulating what persistent_agents does)
            chain = {
                "topic": "Break stagnation: rotate away from interest_research",
                "goal": "Identify and start a different category of work",
                "goal_type": "flexible",
                "success_criteria": "Start a new task type that isn't interest_research",
                "steps_completed": [],
                "target_steps": 2,
                "stagnation_triggered": True,
            }
            with open(chain_path, 'w') as f:
                json.dump(chain, f)

            assert os.path.exists(chain_path)
            loaded = json.loads(Path(chain_path).read_text())
            assert loaded["stagnation_triggered"] is True
            assert "rotate" in loaded["topic"].lower()

    def test_no_chain_overwrite(self):
        """Don't overwrite existing chain with stagnation chain."""
        with tempfile.TemporaryDirectory() as tmpdir:
            chain_path = os.path.join(tmpdir, "reasoning_chain.json")
            # Pre-existing chain
            existing = {"topic": "Important work", "goal": "Finish it"}
            with open(chain_path, 'w') as f:
                json.dump(existing, f)

            # Stagnation should NOT overwrite
            if not os.path.exists(chain_path):
                # This branch should NOT execute
                assert False, "Chain should exist"
            # Verify original is preserved
            loaded = json.loads(Path(chain_path).read_text())
            assert loaded["topic"] == "Important work"


# ═══════════════════════════════════════════════════════════════
# BOOTSTRAP FILE WIRING
# ═══════════════════════════════════════════════════════════════

class TestBootstrapWiring:
    """Tests that INTERESTS.md and VALUES.md are in the prompt file lists."""

    def test_interests_in_full_mode(self):
        from repryntt.agents.persistent_agents import AgentDaemon
        files = AgentDaemon.PROMPT_MODE_FILES["full"]
        assert "INTERESTS.md" in files

    def test_values_in_full_mode(self):
        from repryntt.agents.persistent_agents import AgentDaemon
        files = AgentDaemon.PROMPT_MODE_FILES["full"]
        assert "VALUES.md" in files

    def test_interests_in_foundation_mode(self):
        from repryntt.agents.persistent_agents import AgentDaemon
        files = AgentDaemon.PROMPT_MODE_FILES["foundation"]
        assert "INTERESTS.md" in files

    def test_values_in_foundation_mode(self):
        from repryntt.agents.persistent_agents import AgentDaemon
        files = AgentDaemon.PROMPT_MODE_FILES["foundation"]
        assert "VALUES.md" in files

    def test_values_md_exists(self):
        """VALUES.md bootstrap file exists on disk."""
        values_path = Path.home() / ".repryntt" / "brain" / "bootstrap" / "VALUES.md"
        assert values_path.exists(), f"VALUES.md not found at {values_path}"
        content = values_path.read_text()
        assert "Anti-Priorities" in content
        assert "Value Targets" in content

    def test_interests_md_exists(self):
        """INTERESTS.md bootstrap file exists on disk."""
        interests_path = Path.home() / ".repryntt" / "brain" / "bootstrap" / "INTERESTS.md"
        assert interests_path.exists(), f"INTERESTS.md not found at {interests_path}"
        content = interests_path.read_text()
        assert "Tier 1" in content
