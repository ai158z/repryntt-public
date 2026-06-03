#!/usr/bin/env python3
"""
SAIGE Trading Hook Rate Limiter — Prevents alert storms from real-time
token detection so AI agents can actually evaluate each signal.

Two layers:
    1. Per-token cooldown: max 1 alert per token per COOLDOWN window (default 5 min)
    2. Global burst cap: max N total alerts per BURST window (default 10 per 5 min)

Usage:
    limiter = get_trading_rate_limiter()
    if limiter.allow(token_address):
        router.dispatch(hook)
"""

from __future__ import annotations
import logging
import threading
import time
from collections import OrderedDict
from typing import Dict

logger = logging.getLogger("hooks.rate_limiter")

# --- Defaults (tunable) ---
PER_TOKEN_COOLDOWN = 300       # seconds — 1 alert per token per 5 min
GLOBAL_BURST_LIMIT = 10        # max alerts in the burst window
GLOBAL_BURST_WINDOW = 300      # seconds — 5 min window for burst cap
MAX_TOKEN_CACHE = 500          # LRU cap on per-token timestamps


class TradingRateLimiter:
    """Rate limiter for real-time trading hooks."""

    def __init__(
        self,
        per_token_cooldown: int = PER_TOKEN_COOLDOWN,
        burst_limit: int = GLOBAL_BURST_LIMIT,
        burst_window: int = GLOBAL_BURST_WINDOW,
    ):
        self._lock = threading.Lock()
        self._per_token_cooldown = per_token_cooldown
        self._burst_limit = burst_limit
        self._burst_window = burst_window
        # token_address → last_alert_timestamp
        self._token_last: OrderedDict[str, float] = OrderedDict()
        # global alert timestamps (ring of recent dispatches)
        self._global_times: list[float] = []

    def allow(self, token_address: str) -> bool:
        """Return True if this token alert should be dispatched."""
        now = time.time()
        with self._lock:
            # --- Per-token cooldown ---
            last = self._token_last.get(token_address)
            if last is not None and (now - last) < self._per_token_cooldown:
                logger.debug(
                    f"Rate limit: token {token_address[:12]} on cooldown "
                    f"({now - last:.0f}s < {self._per_token_cooldown}s)"
                )
                return False

            # --- Global burst cap ---
            cutoff = now - self._burst_window
            self._global_times = [t for t in self._global_times if t > cutoff]
            if len(self._global_times) >= self._burst_limit:
                logger.debug(
                    f"Rate limit: global burst cap hit "
                    f"({len(self._global_times)}/{self._burst_limit} in {self._burst_window}s)"
                )
                return False

            # --- Allow it ---
            self._token_last[token_address] = now
            self._token_last.move_to_end(token_address)
            while len(self._token_last) > MAX_TOKEN_CACHE:
                self._token_last.popitem(last=False)
            self._global_times.append(now)
            return True

    def status(self) -> Dict:
        now = time.time()
        with self._lock:
            cutoff = now - self._burst_window
            recent = [t for t in self._global_times if t > cutoff]
            return {
                "per_token_cooldown": self._per_token_cooldown,
                "burst_limit": self._burst_limit,
                "burst_window": self._burst_window,
                "tracked_tokens": len(self._token_last),
                "alerts_in_window": len(recent),
                "burst_remaining": max(0, self._burst_limit - len(recent)),
            }


# ── Singleton ──
_limiter: TradingRateLimiter | None = None
_limiter_lock = threading.Lock()


def get_trading_rate_limiter() -> TradingRateLimiter:
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                _limiter = TradingRateLimiter()
                logger.info(
                    f"TradingRateLimiter created: "
                    f"token_cooldown={PER_TOKEN_COOLDOWN}s, "
                    f"burst={GLOBAL_BURST_LIMIT}/{GLOBAL_BURST_WINDOW}s"
                )
    return _limiter
