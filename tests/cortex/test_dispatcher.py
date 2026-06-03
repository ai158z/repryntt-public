"""Tests for repryntt.cortex.dispatcher — Signal routing and persistence."""

import time
import pytest
from repryntt.cortex.dispatcher import (
    CortexDispatcher,
    CortexSignal,
    Priority,
    get_dispatcher,
)
from repryntt.cortex.region_base import BrainRegion, RegionState


# ── Test helpers ─────────────────────────────────────────────────────

class MockRegion(BrainRegion):
    """Minimal brain region for testing."""

    def __init__(self, name: str = "mock"):
        super().__init__()
        self._name = name
        self._state = RegionState.READY
        self.last_input = None
        self.call_count = 0

    @property
    def name(self) -> str:
        return self._name

    def process(self, input_data):
        self.last_input = input_data
        self.call_count += 1
        return {"success": True, "result": {"echo": input_data.get("value", "ok")}}


class BlockingRegion(BrainRegion):
    """Region that always blocks."""

    @property
    def name(self):
        return "blocker"

    def process(self, input_data):
        return {"success": True, "result": {"allowed": False, "reason": "blocked by test"}}


@pytest.fixture
def dispatcher():
    return CortexDispatcher(max_queue_size=100)


@pytest.fixture
def mock_region():
    return MockRegion("test")


# ── Queue operations ─────────────────────────────────────────────────

class TestQueueOperations:
    """Tests for send, process_pending, and send_and_wait."""

    def test_send_and_wait_returns_result(self, dispatcher, mock_region):
        dispatcher.register_region(mock_region)
        signal = CortexSignal(
            priority=Priority.NORMAL,
            source="test",
            target="test",
            signal_type="test",
            payload={"type": "echo", "value": "hello"},
        )
        result = dispatcher.send_and_wait(signal)
        assert result["success"] is True
        assert result["result"]["echo"] == "hello"
        assert mock_region.call_count == 1

    def test_send_queues_signal(self, dispatcher):
        signal = CortexSignal(
            priority=Priority.NORMAL,
            source="test",
            target="test",
            signal_type="test",
            payload={},
        )
        assert dispatcher.send(signal) is True
        assert dispatcher._queue.qsize() == 1

    def test_process_pending_drains_queue(self, dispatcher, mock_region):
        dispatcher.register_region(mock_region)
        for i in range(5):
            dispatcher.send(CortexSignal(
                priority=Priority.NORMAL,
                source="test",
                target="test",
                signal_type="test",
                payload={"type": "echo", "value": str(i)},
            ))
        processed = dispatcher.process_pending(max_signals=10)
        assert processed == 5
        assert mock_region.call_count == 5

    def test_queue_full_drops_signal(self):
        d = CortexDispatcher(max_queue_size=2)
        for _ in range(2):
            d.send(CortexSignal(priority=Priority.NORMAL, source="test", target="x", payload={}))
        result = d.send(CortexSignal(priority=Priority.NORMAL, source="test", target="x", payload={}))
        assert result is False
        assert d._stats["signals_dropped"] == 1

    def test_priority_ordering(self, dispatcher, mock_region):
        dispatcher.register_region(mock_region)
        # Send LOW first, then CRITICAL
        dispatcher.send(CortexSignal(priority=Priority.LOW, source="test", target="test",
                                      signal_type="low", payload={"type": "echo", "value": "low"}))
        dispatcher.send(CortexSignal(priority=Priority.CRITICAL, source="test", target="test",
                                      signal_type="critical", payload={"type": "echo", "value": "critical"}))
        # Process one — should be CRITICAL (lower number = higher priority)
        dispatcher.process_pending(max_signals=1)
        assert mock_region.last_input["value"] == "critical"

    def test_send_to_missing_region(self, dispatcher):
        signal = CortexSignal(
            priority=Priority.NORMAL,
            source="test",
            target="nonexistent",
            signal_type="test",
            payload={},
        )
        result = dispatcher.send_and_wait(signal)
        assert result is None


# ── Health ───────────────────────────────────────────────────────────

class TestDispatcherHealth:
    """Tests for health() method."""

    def test_health_returns_structure(self, dispatcher, mock_region):
        dispatcher.register_region(mock_region)
        health = dispatcher.health()
        assert "queue_depth" in health
        assert "signals_processed" in health
        assert "worker_alive" in health
        assert "regions" in health
        assert "test" in health["regions"]

    def test_health_tracks_processed(self, dispatcher, mock_region):
        dispatcher.register_region(mock_region)
        dispatcher.send_and_wait(CortexSignal(
            priority=Priority.NORMAL, source="test", target="test",
            signal_type="test", payload={"type": "echo"},
        ))
        health = dispatcher.health()
        assert health["signals_processed"] >= 1


# ── Reflection persistence ───────────────────────────────────────────

class TestReflectionPersistence:
    """Tests for persist_reflection and load_recent_reflections."""

    def test_persist_and_load(self, dispatcher, tmp_path):
        dispatcher._reflections_path = tmp_path / "reflections.jsonl"
        dispatcher.persist_reflection("I learned something", heartbeat=5, goal="grow")
        dispatcher.persist_reflection("Another thought", heartbeat=6)

        reflections = dispatcher.load_recent_reflections(10)
        assert len(reflections) == 2
        assert "learned something" in reflections[0]

    def test_load_returns_last_n(self, dispatcher, tmp_path):
        dispatcher._reflections_path = tmp_path / "reflections.jsonl"
        for i in range(20):
            dispatcher.persist_reflection(f"reflection {i}", heartbeat=i)

        reflections = dispatcher.load_recent_reflections(5)
        assert len(reflections) == 5
        assert "reflection 19" in reflections[-1]

    def test_trim_keeps_max(self, dispatcher, tmp_path):
        dispatcher._reflections_path = tmp_path / "reflections.jsonl"
        dispatcher._max_reflections = 10
        for i in range(15):
            dispatcher.persist_reflection(f"r{i}", heartbeat=i)
        dispatcher._trim_reflections()

        lines = dispatcher._reflections_path.read_text().strip().splitlines()
        assert len(lines) == 10

    def test_load_empty_returns_empty(self, dispatcher, tmp_path):
        dispatcher._reflections_path = tmp_path / "nonexistent.jsonl"
        assert dispatcher.load_recent_reflections(5) == []


# ── Background worker ────────────────────────────────────────────────

class TestBackgroundWorker:
    """Tests for background worker lifecycle."""

    def test_start_and_stop(self, dispatcher):
        dispatcher.start_background()
        assert dispatcher._worker_thread is not None
        assert dispatcher._worker_thread.is_alive()
        dispatcher.stop_background()
        time.sleep(1)
        assert not dispatcher._running

    def test_restart_dead_worker(self, dispatcher):
        dispatcher._running = True
        dispatcher._worker_thread = None  # Simulate dead worker
        assert dispatcher.restart_worker_if_dead() is True
        assert dispatcher._worker_thread.is_alive()
        dispatcher.stop_background()

    def test_restart_not_needed(self, dispatcher):
        dispatcher.start_background()
        assert dispatcher.restart_worker_if_dead() is False
        dispatcher.stop_background()


# ── Region management ────────────────────────────────────────────────

class TestRegionManagement:

    def test_register_and_get(self, dispatcher, mock_region):
        dispatcher.register_region(mock_region)
        assert dispatcher.get_region("test") is mock_region

    def test_unregister(self, dispatcher, mock_region):
        dispatcher.register_region(mock_region)
        dispatcher.unregister_region("test")
        assert dispatcher.get_region("test") is None

    def test_all_regions(self, dispatcher):
        r1 = MockRegion("a")
        r2 = MockRegion("b")
        dispatcher.register_region(r1)
        dispatcher.register_region(r2)
        assert len(dispatcher.all_regions()) == 2


# ── Convenience request methods ──────────────────────────────────────

class TestConvenienceMethods:
    """Tests for request_identity_query and request_personality_rewrite."""

    def test_identity_query_routes_to_conscious(self, dispatcher):
        conscious = MockRegion("conscious")
        dispatcher.register_region(conscious)
        result = dispatcher.request_identity_query("Who am I?")
        assert result["success"] is True
        assert conscious.last_input["type"] == "identity_query"
        assert conscious.last_input["question"] == "Who am I?"

    def test_personality_rewrite_routes_to_conscious(self, dispatcher):
        conscious = MockRegion("conscious")
        dispatcher.register_region(conscious)
        result = dispatcher.request_personality_rewrite("Hello world", context="spoken")
        assert result["success"] is True
        assert conscious.last_input["type"] == "personality_rewrite"
        assert conscious.last_input["text"] == "Hello world"
        assert conscious.last_input["context"] == "spoken"

    def test_identity_query_fallback_when_no_region(self, dispatcher):
        result = dispatcher.request_identity_query("Who am I?")
        assert result.get("fallback") is True

    def test_personality_rewrite_fallback_when_no_region(self, dispatcher):
        result = dispatcher.request_personality_rewrite("test text")
        assert result.get("fallback") is True
        assert result["result"]["text"] == "test text"
