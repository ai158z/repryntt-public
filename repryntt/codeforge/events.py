"""
repryntt.codeforge.events — Live event bus for forge pipeline visibility.

Captures every codeforge.* log record, parses out the project_id and a coarse
event kind, and delivers them to subscribed SSE clients via per-subscriber
queues. Also keeps a small ring buffer of recent events so a freshly-connected
client gets the immediate context.

Wire-up:
  - register_forge_log_handler() — call once at app startup
  - get_bus() — singleton accessor
  - bus.subscribe(project_id=None) -> queue.Queue (events)
  - bus.unsubscribe(q)

Event shape (one JSON object per SSE line):
  {
    "ts": 1715800000.123,
    "project_id": "forge-...-2add" | None,
    "kind": "stage" | "module" | "api_call" | "test" | "error" | "log",
    "stage": "specifying" | "architecting" | "generating" | ...,  # if parseable
    "model": "qwen/qwen3-coder-480b-a35b-instruct",                 # if parseable
    "message": "raw log message"
  }
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional


# ─── Event bus ───────────────────────────────────────────────────────────

_PROJECT_ID_RE = re.compile(r"\[([A-Za-z0-9_\-]+(?:_[A-Za-z0-9_\-]+)*)\]")
_STAGE_HINT_RE = re.compile(
    r"(specifying|architecting|generating|testing|fix.?iterating|validating|packaging|complete|stage \d+)",
    re.IGNORECASE,
)
_MODEL_HINT_RE = re.compile(
    r"(coding model|model|provider):\s*([A-Za-z0-9_\-/.]+)",
    re.IGNORECASE,
)

# emoji → semantic event kind
_EMOJI_KIND = {
    "📋": ("stage", "specifying"),
    "🏗️": ("stage", "architecting"),
    "⚡": ("module", None),
    "🧪": ("test", None),
    "🔧": ("test", "fix_iterating"),
    "✅": ("stage", "validating"),
    "📦": ("stage", "packaging"),
    "🔨": ("stage", "queued"),
}


def _classify(message: str, name: str) -> Dict[str, Optional[str]]:
    """Best-effort parse of one log message into an event payload."""
    out: Dict[str, Optional[str]] = {"kind": "log", "stage": None, "model": None}

    # Logger-name-based hints first (more reliable than message text)
    if name == "codeforge.generator" and "calling" in message.lower():
        out["kind"] = "api_call"
    elif name == "codeforge.governance":
        out["kind"] = "deliberation"
    elif "error" in message.lower() or "failed" in message.lower() or "traceback" in message.lower():
        out["kind"] = "error"

    # Stage hint from prefix emoji
    for emoji, (k, stage) in _EMOJI_KIND.items():
        if message.startswith(emoji):
            out["kind"] = out["kind"] if out["kind"] != "log" else k
            if stage and not out["stage"]:
                out["stage"] = stage
            break

    # Generic stage-word extraction
    m = _STAGE_HINT_RE.search(message)
    if m and not out["stage"]:
        s = m.group(1).lower().replace(" ", "_").replace("-", "_")
        if s.startswith("stage_"):
            out["stage"] = None  # numeric stage is noise
        else:
            out["stage"] = s

    mm = _MODEL_HINT_RE.search(message)
    if mm:
        out["model"] = mm.group(2)
        if out["kind"] == "log":
            out["kind"] = "api_call"

    if message.startswith("Deliberation"):
        out["kind"] = "deliberation"
    elif message.startswith("Second opinion"):
        out["kind"] = "deliberation"
    elif "module" in message.lower() and out["kind"] == "log":
        out["kind"] = "module"

    return out


def _extract_project_id(message: str) -> Optional[str]:
    """Find `[project_id]` style brackets. Skip noisy single-word tokens like [JARVIS]."""
    for m in _PROJECT_ID_RE.finditer(message):
        token = m.group(1)
        if token.startswith("forge") or token.startswith("prop_"):
            return token
        # Some logs use the short hash form — fall back to it if nothing else
    return None


class ForgeEventBus:
    """Singleton — captures forge log events, fans out to subscribers."""

    _instance: Optional["ForgeEventBus"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._buffers: Dict[str, Deque[dict]] = defaultdict(lambda: deque(maxlen=500))
        self._buffer_global: Deque[dict] = deque(maxlen=500)
        self._subscribers: List[Dict] = []  # [{queue, project_id (or None)}]
        self._mu = threading.Lock()

    @classmethod
    def get(cls) -> "ForgeEventBus":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def publish(self, event: dict) -> None:
        pid = event.get("project_id")
        with self._mu:
            self._buffer_global.append(event)
            if pid:
                self._buffers[pid].append(event)
            # Fan out
            for sub in self._subscribers:
                if sub["project_id"] is None or sub["project_id"] == pid:
                    try:
                        sub["queue"].put_nowait(event)
                    except queue.Full:
                        pass

    def subscribe(self, project_id: Optional[str] = None,
                  replay: int = 50) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._mu:
            self._subscribers.append({"queue": q, "project_id": project_id})
            # Replay recent history so the UI gets context immediately
            buf = self._buffers.get(project_id) if project_id else self._buffer_global
            if buf:
                for ev in list(buf)[-replay:]:
                    try:
                        q.put_nowait(ev)
                    except queue.Full:
                        break
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._mu:
            self._subscribers = [s for s in self._subscribers if s["queue"] is not q]

    def recent(self, project_id: Optional[str] = None, limit: int = 100) -> List[dict]:
        with self._mu:
            buf = self._buffers.get(project_id) if project_id else self._buffer_global
            return list(buf)[-limit:] if buf else []


class ForgeLogHandler(logging.Handler):
    """Captures codeforge.* logger records and republishes to ForgeEventBus."""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self._bus = ForgeEventBus.get()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            classified = _classify(msg, record.name)
            event = {
                "ts": time.time(),
                "project_id": _extract_project_id(msg),
                "kind": classified["kind"],
                "stage": classified["stage"],
                "model": classified["model"],
                "message": msg,
                "logger": record.name,
                "level": record.levelname.lower(),
            }
            self._bus.publish(event)
        except Exception:
            # Never let a log-pipeline error break the producer
            self.handleError(record)


_HANDLER_INSTALLED = False
_HANDLER_LOCK = threading.Lock()


def register_forge_log_handler() -> None:
    """Idempotent — install once. Attaches the handler only to the top-level
    `repryntt.codeforge` and `codeforge` loggers; child loggers propagate up
    so we capture every codeforge.* record once without duplicates.
    """
    global _HANDLER_INSTALLED
    with _HANDLER_LOCK:
        if _HANDLER_INSTALLED:
            return
        handler = ForgeLogHandler()
        for name in ("repryntt.codeforge", "codeforge"):
            lg = logging.getLogger(name)
            # Guard against double-attach across imports
            if not any(isinstance(h, ForgeLogHandler) for h in lg.handlers):
                lg.addHandler(handler)
        _HANDLER_INSTALLED = True


def get_bus() -> ForgeEventBus:
    return ForgeEventBus.get()
