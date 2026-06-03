"""
Structured event logging for agent operations.

Events are written to JSONL files for persistence and pushed to
an in-memory ring buffer for real-time SSE streaming to the dashboard.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── Event Types ──

class EventType:
    """Constants for telemetry event types."""
    HEARTBEAT_START = "heartbeat_start"
    HEARTBEAT_END = "heartbeat_end"
    PLAN = "plan"
    ACT_START = "act_start"
    ACT_END = "act_end"
    EVALUATE = "evaluate"
    RECOVERY = "recovery"
    API_CALL = "api_call"
    API_RESPONSE = "api_response"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    THOUGHT = "thought"
    CHAIN_LOAD = "chain_load"
    CHAIN_UPDATE = "chain_update"
    MEMORY_WRITE = "memory_write"
    POST = "post"
    ERROR = "error"
    DUTY_CYCLE = "duty_cycle"
    AGENT_CYCLE_START = "agent_cycle_start"
    AGENT_CYCLE_END = "agent_cycle_end"
    # Cortex events
    CORTEX_PREFILTER = "cortex_prefilter"
    CORTEX_REFLECTION = "cortex_reflection"
    CORTEX_CONSOLIDATION = "cortex_consolidation"
    CORTEX_TRAINING = "cortex_training"
    CORTEX_MODEL_LOAD = "cortex_model_load"
    CORTEX_GUARDIAN_BLOCK = "cortex_guardian_block"


class Phase:
    """Constants for execution phases."""
    GENESIS = "GENESIS"
    PLAN = "PLAN"
    ACT = "ACT"
    EVALUATE = "EVALUATE"
    RECOVERY = "RECOVERY"
    REFLECT = "REFLECT"
    IDLE = "IDLE"


# ── Event Dataclass ──

@dataclass
class TelemetryEvent:
    """A single telemetry event from the agent pipeline."""
    timestamp: float
    event_id: str
    agent_id: str
    event_type: str
    phase: str
    content: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    duration_ms: Optional[float] = None
    parent_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Convert None values to keep JSON clean
        return {k: v for k, v in d.items() if v is not None}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)

    @staticmethod
    def create(
        agent_id: str,
        event_type: str,
        phase: str = Phase.IDLE,
        content: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        duration_ms: Optional[float] = None,
        parent_id: Optional[str] = None,
    ) -> "TelemetryEvent":
        return TelemetryEvent(
            timestamp=time.time(),
            event_id=uuid.uuid4().hex[:12],
            agent_id=agent_id,
            event_type=event_type,
            phase=phase,
            content=content,
            metadata=metadata or {},
            duration_ms=duration_ms,
            parent_id=parent_id,
        )


# ── Ring Buffer for SSE ──

_MAX_BUFFER = 500  # Keep last 500 events in memory


class _RingBuffer:
    """Thread-safe ring buffer for recent events."""

    def __init__(self, maxsize: int = _MAX_BUFFER):
        self._buf: List[TelemetryEvent] = []
        self._maxsize = maxsize
        self._lock = threading.Lock()

    def push(self, event: TelemetryEvent):
        with self._lock:
            self._buf.append(event)
            if len(self._buf) > self._maxsize:
                self._buf = self._buf[-self._maxsize:]

    def recent(self, n: int = 100) -> List[TelemetryEvent]:
        with self._lock:
            return list(self._buf[-n:])

    def since(self, timestamp: float) -> List[TelemetryEvent]:
        with self._lock:
            return [e for e in self._buf if e.timestamp > timestamp]


# ── SSE Broadcaster ──

class _SSEBroadcaster:
    """Manages SSE client queues for live streaming."""

    def __init__(self):
        self._clients: List[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            try:
                self._clients.remove(q)
            except ValueError:
                pass

    def broadcast(self, event: TelemetryEvent):
        data = event.to_json()
        with self._lock:
            dead = []
            for q in self._clients:
                try:
                    q.put_nowait(data)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                try:
                    self._clients.remove(q)
                except ValueError:
                    pass


# ── OpsLogger (the main interface) ──

class OpsLogger:
    """
    Central telemetry logger. Writes events to JSONL files, maintains an
    in-memory ring buffer, and broadcasts via SSE to connected dashboards.

    Usage:
        ops = get_ops_logger()
        ops.log("JARVIS", EventType.PLAN, Phase.PLAN, content="Research AI chips...")
    """

    def __init__(self, telemetry_dir: Optional[Path] = None):
        if telemetry_dir is None:
            telemetry_dir = Path.home() / ".repryntt" / "workspace" / "telemetry"
        self._dir = telemetry_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._buffer = _RingBuffer()
        self._broadcaster = _SSEBroadcaster()
        self._file_lock = threading.Lock()
        self._current_date: Optional[str] = None
        self._current_file = None

    # ── Public API ──

    def log(
        self,
        agent_id: str,
        event_type: str,
        phase: str = Phase.IDLE,
        content: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        duration_ms: Optional[float] = None,
        parent_id: Optional[str] = None,
    ) -> TelemetryEvent:
        """Log a telemetry event. Returns the event for chaining."""
        event = TelemetryEvent.create(
            agent_id=agent_id,
            event_type=event_type,
            phase=phase,
            content=content,
            metadata=metadata,
            duration_ms=duration_ms,
            parent_id=parent_id,
        )
        self._buffer.push(event)
        self._broadcaster.broadcast(event)
        self._write_to_file(event)
        return event

    def recent(self, n: int = 100) -> List[Dict[str, Any]]:
        """Get the N most recent events as dicts."""
        return [e.to_dict() for e in self._buffer.recent(n)]

    def since(self, timestamp: float) -> List[Dict[str, Any]]:
        """Get events since a given timestamp."""
        return [e.to_dict() for e in self._buffer.since(timestamp)]

    def subscribe_sse(self) -> queue.Queue:
        """Subscribe to the live event stream. Returns a queue."""
        return self._broadcaster.subscribe()

    def unsubscribe_sse(self, q: queue.Queue):
        """Unsubscribe from the live event stream."""
        self._broadcaster.unsubscribe(q)

    def history(self, date: Optional[str] = None, limit: int = 500,
                offset: int = 0) -> List[Dict[str, Any]]:
        """Read historical events from JSONL files.
        
        Args:
            date: YYYY-MM-DD string, defaults to today
            limit: max events to return
            offset: skip this many events from the end
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        filepath = self._dir / f"{date}.jsonl"
        if not filepath.exists():
            return []
        events = []
        try:
            with open(filepath, "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError:
            return []
        # Return most recent events (tail)
        if offset > 0:
            events = events[:-offset] if offset < len(events) else []
        return events[-limit:]

    def available_dates(self) -> List[str]:
        """List dates that have telemetry data."""
        dates = []
        for f in sorted(self._dir.glob("*.jsonl")):
            dates.append(f.stem)
        return dates

    # ── File I/O ──

    def _write_to_file(self, event: TelemetryEvent):
        """Append event to today's JSONL file."""
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            with self._file_lock:
                if today != self._current_date:
                    if self._current_file is not None:
                        self._current_file.close()
                    filepath = self._dir / f"{today}.jsonl"
                    self._current_file = open(filepath, "a")
                    self._current_date = today
                self._current_file.write(event.to_json() + "\n")
                self._current_file.flush()
        except OSError:
            pass  # Non-fatal — don't crash agents over telemetry

    def close(self):
        """Flush and close the file handle."""
        with self._file_lock:
            if self._current_file is not None:
                self._current_file.close()
                self._current_file = None


# ── Singleton ──

_instance: Optional[OpsLogger] = None
_instance_lock = threading.Lock()


def get_ops_logger() -> OpsLogger:
    """Get the global OpsLogger singleton."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = OpsLogger()
    return _instance
