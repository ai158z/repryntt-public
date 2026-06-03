"""
repryntt.cortex.dispatcher — Signal routing between brain regions.

The "thalamus" — routes signals between brain regions with priority-based
ordering.  All inter-region communication goes through here.

Architecture:
  - In-process priority queue (no message broker needed — single machine)
  - Thread-safe, supports concurrent region access
  - Signals can be unicast (to one region) or broadcast
  - Guardian always validates actions before executor/external output
  - Conscious layer receives all signals as background context

Signal flow examples:
  Perception → Guardian (safety) → Cortex (reasoning)
  Cortex → Guardian (validate) → Executor (motor)
  Voice input → Conscious (pre-response) → Cortex (full answer)
  Any region → Conscious (all signals, for self-awareness)
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from repryntt.cortex.region_base import BrainRegion, RegionState

logger = logging.getLogger(__name__)


# ── Signal definition ────────────────────────────────────────────────────

@dataclass(order=True)
class CortexSignal:
    """A signal routed between brain regions."""

    # Fields used for priority queue ordering (lower = higher priority)
    priority: int = field(compare=True)
    timestamp: float = field(default_factory=time.time, compare=True)

    # Content fields (not used for ordering)
    source: str = field(default="", compare=False)        # Region that sent it
    target: str = field(default="", compare=False)        # Target region or "broadcast"
    signal_type: str = field(default="", compare=False)   # "perception", "action", "safety", etc.
    payload: Dict[str, Any] = field(default_factory=dict, compare=False)

    # Tracking
    signal_id: str = field(default="", compare=False)

    def __post_init__(self):
        if not self.signal_id:
            import uuid
            self.signal_id = uuid.uuid4().hex[:8]


# Signal priority levels
class Priority:
    CRITICAL = 0    # Safety / emergency stop
    HIGH = 1        # Motor commands, real-time perception
    NORMAL = 2      # Reasoning, planning
    LOW = 3         # Reflection, memory consolidation
    BACKGROUND = 4  # Logging, telemetry


# ── Cortex Dispatcher ────────────────────────────────────────────────────

class CortexDispatcher:
    """Routes signals between brain regions.

    Usage::

        dispatcher = CortexDispatcher()
        dispatcher.register_region(guardian)
        dispatcher.register_region(conscious)

        # Send a signal
        dispatcher.send(CortexSignal(
            priority=Priority.NORMAL,
            source="cortex",
            target="conscious",
            signal_type="self_reflection",
            payload={"last_action": "researched federated learning"},
        ))

        # Process all pending signals
        dispatcher.process_pending()
    """

    def __init__(self, *, max_queue_size: int = 1000) -> None:
        self._regions: Dict[str, BrainRegion] = {}
        self._queue: queue.PriorityQueue = queue.PriorityQueue(maxsize=max_queue_size)
        self._lock = threading.Lock()
        self._running = False
        self._worker_thread: Optional[threading.Thread] = None
        self._signal_log: List[Dict[str, Any]] = []  # Last N processed signals
        self._max_log = 100
        self._hooks: Dict[str, List[Callable]] = {}  # signal_type → callbacks

        # Stats
        self._stats = {
            "signals_sent": 0,
            "signals_processed": 0,
            "signals_dropped": 0,
            "guardian_blocks": 0,
        }

        # Reflection persistence path
        from repryntt.paths import brain_dir
        self._reflections_path = brain_dir() / "cortex_reflections.jsonl"
        self._max_reflections = 1000

    # ── Region management ────────────────────────────────────────────

    def register_region(self, region: BrainRegion) -> None:
        """Register a brain region with the dispatcher."""
        with self._lock:
            self._regions[region.name] = region
            logger.info("Dispatcher: registered region '%s' (state=%s)",
                         region.name, region.state.value)

    def unregister_region(self, name: str) -> None:
        with self._lock:
            self._regions.pop(name, None)

    def get_region(self, name: str) -> Optional[BrainRegion]:
        return self._regions.get(name)

    def all_regions(self) -> Dict[str, BrainRegion]:
        return dict(self._regions)

    # ── Signal sending ───────────────────────────────────────────────

    def send(self, signal: CortexSignal) -> bool:
        """Queue a signal for processing.  Returns False if queue is full."""
        try:
            self._queue.put_nowait(signal)
            self._stats["signals_sent"] += 1
            return True
        except queue.Full:
            self._stats["signals_dropped"] += 1
            logger.warning("Signal queue full — dropped signal from '%s' → '%s'",
                            signal.source, signal.target)
            return False

    def send_and_wait(
        self,
        signal: CortexSignal,
        *,
        timeout: float = 5.0,
    ) -> Optional[Dict[str, Any]]:
        """Send a signal and synchronously wait for the result.

        Use this for request-response patterns (e.g., guardian validation).
        Bypasses the queue — processes immediately in the calling thread.
        Enforces timeout to prevent heartbeat stalls from hung models.
        """
        target = self._regions.get(signal.target)
        if not target:
            logger.warning("No region '%s' registered for synchronous signal", signal.target)
            return None

        self._stats["signals_sent"] += 1
        self._stats["signals_processed"] += 1

        import concurrent.futures
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(target.safe_process, signal.payload)
            result = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "send_and_wait TIMED OUT for region '%s' (signal=%s, timeout=%.1fs)",
                signal.target, signal.signal_type, timeout,
            )
            self._stats.setdefault("timeouts", 0)
            self._stats["timeouts"] += 1
            return None
        except Exception as e:
            logger.error("send_and_wait error for region '%s': %s", signal.target, e)
            return None
        finally:
            pool.shutdown(wait=False)  # Don't block — let orphaned thread finish in background

        self._log_signal(signal, result)
        return result

    # ── Signal processing ────────────────────────────────────────────

    def process_pending(self, *, max_signals: int = 50) -> int:
        """Process up to max_signals from the queue.  Returns count processed."""
        processed = 0
        while processed < max_signals:
            try:
                signal: CortexSignal = self._queue.get_nowait()
            except queue.Empty:
                break

            self._process_signal(signal)
            processed += 1

        return processed

    def _process_signal(self, signal: CortexSignal) -> None:
        """Process a single signal."""
        self._stats["signals_processed"] += 1

        if signal.target == "broadcast":
            # Send to all regions
            for name, region in self._regions.items():
                if name != signal.source and region.health_check():
                    result = region.safe_process(signal.payload)
                    self._log_signal(signal, result, target_override=name)
        else:
            target = self._regions.get(signal.target)
            if target and target.health_check():
                result = target.safe_process(signal.payload)
                self._log_signal(signal, result)
            else:
                logger.debug("Signal target '%s' not available — dropping", signal.target)

        # Fire registered hooks
        for hook in self._hooks.get(signal.signal_type, []):
            try:
                hook(signal)
            except Exception as e:
                logger.warning("Signal hook error for '%s': %s", signal.signal_type, e)

    # ── Convenience methods for common signal patterns ────────────────

    def request_guardian_validation(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Ask the guardian to validate a tool call.  Synchronous."""
        signal = CortexSignal(
            priority=Priority.CRITICAL,
            source="dispatcher",
            target="guardian",
            signal_type="safety",
            payload={
                "type": "validate_action",
                "tool_name": tool_name,
                "arguments": arguments,
            },
        )
        result = self.send_and_wait(signal)
        if not result:
            self._stats["guardian_blocks"] += 1
            return {"success": False, "result": {"allowed": False, "reason": "guardian unavailable"}}
        inner = result.get("result") or {}
        if not inner.get("allowed", False):
            self._stats["guardian_blocks"] += 1
        return result

    def request_conscious_filter(
        self,
        context: str,
        pending_tasks: int = 0,
        recent_activity: str = "",
    ) -> Dict[str, Any]:
        """Ask the conscious layer to score this heartbeat.  Synchronous."""
        signal = CortexSignal(
            priority=Priority.NORMAL,
            source="dispatcher",
            target="conscious",
            signal_type="filter",
            payload={
                "type": "pre_heartbeat_filter",
                "context": context,
                "pending_tasks": pending_tasks,
                "recent_activity": recent_activity,
            },
        )
        return self.send_and_wait(signal) or {
            "success": True, "result": {"score": 0.7, "reason": "dispatcher default"}
        }

    def request_voice_preresponse(
        self,
        user_text: str,
        history: str = "",
    ) -> Dict[str, Any]:
        """Get an instant voice acknowledgment from the conscious layer."""
        signal = CortexSignal(
            priority=Priority.HIGH,
            source="dispatcher",
            target="conscious",
            signal_type="voice",
            payload={
                "type": "voice_preresponse",
                "user_text": user_text,
                "history": history,
            },
        )
        return self.send_and_wait(signal, timeout=2.0) or {
            "success": True, "result": {"text": "Let me think about that."}, "fallback": True
        }

    def request_self_reflection(
        self,
        last_action: str = "",
        last_result: str = "",
        current_goal: str = "",
    ) -> Dict[str, Any]:
        """Trigger a self-reflection in the conscious layer.  Async (queued)."""
        signal = CortexSignal(
            priority=Priority.LOW,
            source="dispatcher",
            target="conscious",
            signal_type="reflection",
            payload={
                "type": "self_reflection",
                "last_action": last_action,
                "last_result": last_result,
                "current_goal": current_goal,
            },
        )
        self.send(signal)
        return {"success": True, "queued": True}

    def request_memory_consolidation(self, raw_memory: str) -> Dict[str, Any]:
        """Ask the conscious layer to consolidate memory.  Synchronous."""
        signal = CortexSignal(
            priority=Priority.LOW,
            source="dispatcher",
            target="conscious",
            signal_type="memory",
            payload={
                "type": "memory_consolidation",
                "raw_memory": raw_memory,
            },
        )
        return self.send_and_wait(signal, timeout=10.0) or {
            "success": True, "result": {"consolidated": ""}
        }

    def request_deliberation(
        self,
        drives_summary: str,
        interests_top5: str,
        values_snippet: str,
        recent_topics: str,
        active_chain: str = "",
        task_queue_hint: str = "",
    ) -> Dict[str, Any]:
        """Ask the conscious layer to propose task candidates.  Synchronous.

        The local model (SmolLM2) reads the agent's whiteboard (drives,
        interests, values, recent work) and proposes 3 concrete task
        candidates.  The expensive API model then deliberates on these
        instead of the full context firehose.
        """
        signal = CortexSignal(
            priority=Priority.NORMAL,
            source="dispatcher",
            target="conscious",
            signal_type="deliberation",
            payload={
                "type": "deliberation",
                "drives_summary": drives_summary,
                "interests_top5": interests_top5,
                "values_snippet": values_snippet,
                "recent_topics": recent_topics,
                "active_chain": active_chain,
                "task_queue_hint": task_queue_hint,
            },
        )
        return self.send_and_wait(signal, timeout=8.0) or {
            "success": True,
            "result": {"candidates": []},
            "fallback": True,
        }

    def request_identity_query(self, question: str) -> Dict[str, Any]:
        """Ask the conscious layer an identity-related question.  Synchronous."""
        signal = CortexSignal(
            priority=Priority.NORMAL,
            source="dispatcher",
            target="conscious",
            signal_type="identity",
            payload={
                "type": "identity_query",
                "question": question,
            },
        )
        return self.send_and_wait(signal, timeout=5.0) or {
            "success": True, "result": {"answer": ""}, "fallback": True
        }

    def request_personality_rewrite(
        self, text: str, context: str = "written"
    ) -> Dict[str, Any]:
        """Rewrite text in Andrew's authentic voice.  Synchronous."""
        signal = CortexSignal(
            priority=Priority.NORMAL,
            source="dispatcher",
            target="conscious",
            signal_type="personality",
            payload={
                "type": "personality_rewrite",
                "text": text,
                "context": context,
            },
        )
        return self.send_and_wait(signal, timeout=5.0) or {
            "success": True, "result": {"text": text}, "fallback": True
        }

    # ── Hooks ────────────────────────────────────────────────────────

    def on_signal(self, signal_type: str, callback: Callable) -> None:
        """Register a callback for a signal type."""
        self._hooks.setdefault(signal_type, []).append(callback)

    # ── Background worker ────────────────────────────────────────────

    def start_background(self) -> None:
        """Start a background thread that continuously processes signals."""
        if self._running:
            return
        self._running = True
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="cortex-dispatcher",
            daemon=True,
        )
        self._worker_thread.start()
        logger.info("Cortex dispatcher background worker started")

    def stop_background(self) -> None:
        """Graceful shutdown: drain queue, stop worker, shut down regions."""
        if not self._running:
            return

        logger.info("Cortex dispatcher shutting down — draining queue...")

        # Stop accepting new signals
        self._running = False

        # Drain remaining signals (best effort, max 2 seconds)
        drain_deadline = time.time() + 2.0
        drained = 0
        while time.time() < drain_deadline:
            try:
                signal = self._queue.get_nowait()
                self._process_signal(signal)
                drained += 1
            except queue.Empty:
                break

        if drained:
            logger.info("Drained %d queued signals during shutdown", drained)

        # Wait for worker thread to finish
        if self._worker_thread:
            self._worker_thread.join(timeout=5)
            if self._worker_thread.is_alive():
                logger.warning("Dispatcher worker thread did not stop cleanly")
            self._worker_thread = None

        # Shut down all regions
        with self._lock:
            for name, region in self._regions.items():
                try:
                    region.shutdown()
                    logger.info("Region '%s' shut down cleanly", name)
                except Exception as e:
                    logger.warning("Region '%s' shutdown error: %s", name, e)

        logger.info("Cortex dispatcher stopped")

    def _worker_loop(self) -> None:
        """Background worker that processes queued signals."""
        while self._running:
            try:
                signal = self._queue.get(timeout=0.5)
                self._process_signal(signal)
            except queue.Empty:
                continue
            except Exception as e:
                logger.error("Dispatcher worker error: %s", e, exc_info=True)

    def restart_worker_if_dead(self) -> bool:
        """Restart background worker if it died. Returns True if restarted."""
        if self._worker_thread and self._worker_thread.is_alive():
            return False
        if not self._running:
            return False
        logger.warning("Cortex dispatcher worker thread died — restarting")
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="cortex-dispatcher",
            daemon=True,
        )
        self._worker_thread.start()
        return True

    def health(self) -> Dict[str, Any]:
        """Return cortex health summary for monitoring endpoints."""
        return {
            "queue_depth": self._queue.qsize(),
            "signals_processed": self._stats["signals_processed"],
            "signals_dropped": self._stats["signals_dropped"],
            "guardian_blocks": self._stats["guardian_blocks"],
            "worker_alive": bool(self._worker_thread and self._worker_thread.is_alive()),
            "regions": {
                name: {"state": r.state.value, "model": r.model_name}
                for name, r in self._regions.items()
            },
        }

    def persist_reflection(self, reflection: str, heartbeat: int = 0,
                           goal: str = "", action: str = "") -> None:
        """Append a self-reflection to the persistent JSONL file."""
        import json
        import fcntl
        from datetime import datetime
        entry = {
            "ts": datetime.now().isoformat(),
            "heartbeat": heartbeat,
            "reflection": reflection,
            "goal": goal,
            "action": action,
        }
        try:
            with open(self._reflections_path, "a") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    f.write(json.dumps(entry) + "\n")
                    f.flush()
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
            # Trim if over limit (less frequently — every 100 writes)
            self._stats.setdefault("reflections_written", 0)
            self._stats["reflections_written"] += 1
            if self._stats["reflections_written"] % 100 == 0:
                self._trim_reflections()
        except OSError as e:
            logger.warning("Failed to persist reflection (disk issue?): %s", e)

    def load_recent_reflections(self, n: int = 10) -> List[str]:
        """Load the last N reflections from disk."""
        import json
        if not self._reflections_path.exists():
            return []
        try:
            lines = self._reflections_path.read_text().strip().splitlines()
            results = []
            for line in lines[-n:]:
                try:
                    entry = json.loads(line)
                    results.append(entry.get("reflection", ""))
                except json.JSONDecodeError:
                    continue
            return results
        except Exception:
            return []

    def _trim_reflections(self) -> None:
        """Keep only the last _max_reflections entries."""
        try:
            if not self._reflections_path.exists():
                return
            lines = self._reflections_path.read_text().strip().splitlines()
            if len(lines) > self._max_reflections:
                trimmed = lines[-self._max_reflections:]
                self._reflections_path.write_text("\n".join(trimmed) + "\n")
        except Exception:
            pass

    # ── Logging / Status ─────────────────────────────────────────────

    def _log_signal(
        self,
        signal: CortexSignal,
        result: Optional[Dict[str, Any]],
        *,
        target_override: str = "",
    ) -> None:
        """Log a processed signal for monitoring."""
        entry = {
            "id": signal.signal_id,
            "source": signal.source,
            "target": target_override or signal.target,
            "type": signal.signal_type,
            "priority": signal.priority,
            "timestamp": signal.timestamp,
            "success": result.get("success", False) if result else False,
            "latency_ms": result.get("latency_ms", 0) if result else 0,
        }
        self._signal_log.append(entry)
        if len(self._signal_log) > self._max_log:
            self._signal_log = self._signal_log[-self._max_log:]

    def status(self) -> Dict[str, Any]:
        """Return dispatcher status for monitoring."""
        regions_status = {}
        for name, region in self._regions.items():
            regions_status[name] = region.get_stats()

        return {
            "queue_size": self._queue.qsize(),
            "regions": regions_status,
            "stats": dict(self._stats),
            "recent_signals": self._signal_log[-10:],
            "background_running": self._running,
        }


# ── Singleton ────────────────────────────────────────────────────────────

_instance: Optional[CortexDispatcher] = None
_init_lock = threading.Lock()


def get_dispatcher(*, force_refresh: bool = False) -> CortexDispatcher:
    """Return the singleton CortexDispatcher."""
    global _instance
    if _instance is not None and not force_refresh:
        return _instance
    with _init_lock:
        if _instance is None or force_refresh:
            _instance = CortexDispatcher()
    return _instance
