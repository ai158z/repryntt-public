#!/usr/bin/env python3
"""
SAIGE Hook Router — Central dispatcher that receives HookMessages and
routes them to agents, then sends responses back to the originating channel.

Architecture:
    Any source → parse_hook() → HookMessage → HookRouter.dispatch() →
        AgentDaemon.invoke_jarvis() → response back to reply_channel

Deduplication via session_key prevents processing the same event twice.
Thread-safe: all dispatch runs through the thread pool.
"""

from __future__ import annotations
import logging
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from repryntt.comms.hooks.message import HookMessage

logger = logging.getLogger("hooks.router")

# Maximum size of dedup cache (LRU)
MAX_DEDUP_CACHE = 2000
# TTL for dedup entries (seconds) — skip events seen within this window
DEDUP_TTL = 3600  # 1 hour
# Maximum number of events in the event log ring buffer
MAX_EVENT_LOG = 200


class HookRouter:
    """Central hook dispatch engine.

    Usage:
        router = get_hook_router()
        router.dispatch(hook_message)   # async (returns immediately)
        result = router.dispatch_sync(hook_message)  # blocking
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._dedup_cache: OrderedDict[str, float] = OrderedDict()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hook")
        self._reply_handlers: Dict[str, Callable] = {}
        self._daemon = None
        self._stats = {
            "dispatched": 0,
            "deduped": 0,
            "errors": 0,
            "last_dispatch": 0.0,
        }
        self._event_log: deque[dict] = deque(maxlen=MAX_EVENT_LOG)
        self._running = False

    # ──────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────

    def start(self):
        self._running = True
        logger.info("HookRouter started")

    def stop(self):
        self._running = False
        self._executor.shutdown(wait=False)
        logger.info("HookRouter stopped")

    # ──────────────────────────────────────────────
    # Reply handler registration
    # ──────────────────────────────────────────────

    def register_reply_handler(self, channel: str, handler: Callable):
        """Register a callback for sending responses back to a channel.

        handler signature: handler(reply_to: str, text: str) -> None
        """
        self._reply_handlers[channel] = handler
        logger.info(f"Registered reply handler for channel: {channel}")

    # ──────────────────────────────────────────────
    # Dedup
    # ──────────────────────────────────────────────

    def _is_duplicate(self, hook: HookMessage) -> bool:
        if not hook.session_key:
            return False
        now = time.time()
        with self._lock:
            if hook.session_key in self._dedup_cache:
                ts = self._dedup_cache[hook.session_key]
                if now - ts < DEDUP_TTL:
                    self._stats["deduped"] += 1
                    return True
            # Record and prune
            self._dedup_cache[hook.session_key] = now
            while len(self._dedup_cache) > MAX_DEDUP_CACHE:
                self._dedup_cache.popitem(last=False)
        return False

    # ──────────────────────────────────────────────
    # Agent dispatch
    # ──────────────────────────────────────────────

    def _get_daemon(self):
        if self._daemon is None:
            try:
                from repryntt.agents.persistent_agents import get_agent_daemon
                self._daemon = get_agent_daemon(auto_start=False)
            except Exception as e:
                logger.error(f"Failed to get AgentDaemon: {e}")
        return self._daemon

    def _dispatch_now(self, hook: HookMessage) -> Optional[Dict[str, Any]]:
        """Synchronously invoke the agent and route the response back."""
        daemon = self._get_daemon()
        if not daemon:
            logger.error("No AgentDaemon available, cannot dispatch hook")
            return None

        prompt = hook.to_prompt()
        logger.info(
            f"Dispatching hook [{hook.source}/{hook.event}] "
            f"from={hook.sender} agent={hook.agent_id}"
        )

        try:
            if hook.agent_id == "jarvis" or not hook.agent_id:
                result = daemon.invoke_jarvis(prompt, max_tokens=6000)
            else:
                result = daemon.invoke_agent(hook.agent_id, prompt)
        except Exception as e:
            logger.error(f"Hook dispatch error: {e}")
            self._stats["errors"] += 1
            self._event_log.append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "source": hook.source,
                "event": hook.event,
                "sender": hook.sender or "",
                "subject": hook.subject or "",
                "agent_id": hook.agent_id or "jarvis",
                "priority": hook.priority,
                "success": False,
                "error": str(e)[:200],
            })
            return {"success": False, "error": str(e)}

        self._stats["dispatched"] += 1
        self._stats["last_dispatch"] = time.time()

        # Log event for dashboard feed
        self._event_log.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "source": hook.source,
            "event": hook.event,
            "sender": hook.sender or "",
            "subject": hook.subject or "",
            "agent_id": hook.agent_id or "jarvis",
            "priority": hook.priority,
            "success": True,
            "snippet": (hook.body or "")[:200],
        })

        # Route response back to source channel
        response_text = ""
        if isinstance(result, dict):
            response_text = result.get("response", "")
        elif isinstance(result, str):
            response_text = result

        if response_text and hook.reply_channel and hook.reply_to:
            self._send_reply(hook.reply_channel, hook.reply_to, response_text)

        return result

    def _send_reply(self, channel: str, reply_to: str, text: str):
        """Send a response back through the originating channel."""
        handler = self._reply_handlers.get(channel)
        if handler:
            try:
                handler(reply_to, text)
            except Exception as e:
                logger.error(f"Reply handler error ({channel}): {e}")
        else:
            logger.debug(
                f"No reply handler for channel '{channel}', response not sent back"
            )

    # ──────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────

    def dispatch(self, hook: HookMessage) -> None:
        """Async dispatch — fire and forget. Runs in thread pool."""
        if not self._running:
            logger.warning("HookRouter not running, ignoring hook")
            return
        if self._is_duplicate(hook):
            logger.debug(f"Duplicate hook skipped: {hook.session_key}")
            return
        self._executor.submit(self._safe_dispatch, hook)

    def dispatch_sync(self, hook: HookMessage) -> Optional[Dict[str, Any]]:
        """Synchronous dispatch — blocks until agent responds. For HTTP endpoints."""
        if self._is_duplicate(hook):
            logger.debug(f"Duplicate hook skipped: {hook.session_key}")
            return {"success": False, "reason": "duplicate"}
        return self._dispatch_now(hook)

    def _safe_dispatch(self, hook: HookMessage):
        """Wrapper that catches all exceptions for async dispatch."""
        try:
            self._dispatch_now(hook)
        except Exception as e:
            logger.error(f"Unhandled hook dispatch error: {e}")
            self._stats["errors"] += 1

    def get_event_log(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return the most recent hook events (newest first) for the dashboard feed."""
        events = list(self._event_log)
        events.reverse()
        return events[:limit]

    def status(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "reply_channels": list(self._reply_handlers.keys()),
            "dedup_cache_size": len(self._dedup_cache),
            "event_log_size": len(self._event_log),
            **self._stats,
        }


# ──────────────────────────────────────────────────
# Module-level singleton
# ──────────────────────────────────────────────────

_router: Optional[HookRouter] = None
_router_lock = threading.Lock()


def get_hook_router() -> HookRouter:
    global _router
    if _router is None:
        with _router_lock:
            if _router is None:
                _router = HookRouter()
                _router.start()
    return _router
