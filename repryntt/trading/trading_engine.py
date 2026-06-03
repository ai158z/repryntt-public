"""
SAIGE Jarvis Trading Engine — Signal-Driven Autonomous Execution
================================================================

This module replaces the old "cold-call and hope" pattern with a real
trading engine that:

  1. QUEUES every scored signal — nothing is ever dropped.
  2. AUTO-EXECUTES STRONG BUY (score ≥ 8, no critical risk flags) immediately
     without waiting for the LLM. Speed wins on memecoins.
  3. QUEUES BUY/WEAK BUY signals for Jarvis to review on the next cold-call
     or heartbeat — with enough time to trade ALL of them.
  4. Tracks executed trades to avoid duplicates.
  5. Provides a queue drain function that the heartbeat can call.

Architecture:
  signal_scorer → trading_engine.ingest_signals()
                    ├─ STRONG BUY + no risk → auto_execute() → sim_buy()
                    ├─ BUY/WEAK BUY → pending_queue (for Jarvis review)
                    └─ deferred (lock held) → deferred_queue (retry next tick)

  heartbeat / cold-call → trading_engine.get_pending_prompt()
                            → returns focused prompt with ALL queued candidates
                            → Jarvis trades them all in one session

  cold-call blocked? → signal stays in queue, retried next tick (5 min)
"""

import json
import os
import time
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("saige.trading_engine")

BASE_DIR = Path(__file__).resolve().parent.parent
JARVIS_WORKSPACE = str(Path.home() / ".repryntt" / "workspace" / "agents" / "operator")
ENGINE_STATE_FILE = Path.home() / ".repryntt" / "workspace" / "agents" / "operator" / "trading_engine_state.json"

# ─── Configuration ───────────────────────────────────────────────────────────

# Push alerts master switch — controls ALL signal-push pipelines
# (trading cycle, whale monitor, ai72 hooks, signal scorer hooks)
PUSH_ALERTS_ENABLED = True           # Enabled — signals from token monitor + whale monitor push to Jarvis

# Auto-execute master switch — DISABLED: Andrew learns to trade manually first.
# Re-enable once wallet grows past a proven threshold.
AUTO_EXEC_ENABLED = False            # Off until Andrew proves profitability
AUTO_EXEC_MIN_SCORE = 8.0           # Minimum score for auto-execution
AUTO_EXEC_MAX_RISK_FLAGS = 1        # Max risk flags allowed for auto-exec
AUTO_EXEC_CRITICAL_FLAGS = {        # These flags BLOCK auto-execution
    "Sharp decline",
    "Heavy sell pressure",
    "Concentrated holders",
}
AUTO_EXEC_MAX_POSITION_USD = 16.0    # ~0.2 SOL per trade (auto-exec currently disabled)
AUTO_EXEC_MAX_PER_TICK = 1           # Max 1 auto-execution per 5-min tick (quality > quantity)
AUTO_EXEC_COOLDOWN_S = 600          # Don't re-buy same token within 10 min (patience)
AUTO_EXEC_MAX_PRICE_DROP_PCT = 5.0  # Reject if live price dropped >5% vs signal price

# Signal freshness
SIGNAL_MAX_AGE_S = 300              # Reject signals older than 5 min (one scoring cycle)

# Queue limits
MAX_PENDING_QUEUE = 20              # Max signals waiting for Jarvis review
MAX_RECENT_TRADES = 100             # Track recent trades to avoid duplicates
SIGNAL_EXPIRY_S = 1800              # Signals older than 30 min decay from queue

# ─── State ───────────────────────────────────────────────────────────────────

_lock = threading.Lock()

# Pending signals for Jarvis to review (BUY/WEAK BUY grade)
_pending_queue: deque = deque(maxlen=MAX_PENDING_QUEUE)

# Recently auto-executed addresses (address → timestamp) for cooldown
_recent_auto_trades: Dict[str, float] = {}

# Recently seen addresses this session (to avoid re-queuing duplicates)
_seen_addresses: Dict[str, float] = {}

# Stats
_stats = {
    "auto_executed": 0,
    "queued_for_review": 0,
    "dropped_duplicate": 0,
    "dropped_expired": 0,
    "jarvis_reviewed": 0,
}


# ─── Core: Ingest Scored Signals ────────────────────────────────────────────

def ingest_signals(scored_signals: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Process scored signals from signal_scorer.

    For each signal:
      - STRONG BUY (≥8.0) + acceptable risk → auto-execute immediately
      - BUY/WEAK BUY (≥3.0) → queue for Jarvis review
      - Below threshold → ignore

    Returns summary of actions taken.
    """
    if not scored_signals:
        return {"auto_executed": [], "queued": 0, "skipped": 0}

    auto_results = []
    queued = 0
    skipped = 0
    stale = 0
    auto_count_this_tick = 0
    now = time.time()

    for signal in scored_signals:
        address = signal.get("address", "")
        score = signal.get("score", 0)
        grade = signal.get("grade", "")
        risk_flags = signal.get("risk_flags", [])

        if not address or score < 3.0:
            skipped += 1
            continue

        # ── Timestamp freshness: reject stale signals ──
        scored_at = signal.get("scored_at", "")
        if scored_at:
            try:
                signal_time = datetime.fromisoformat(scored_at)
                age_s = (datetime.now(timezone.utc) - signal_time).total_seconds()
                if age_s > SIGNAL_MAX_AGE_S:
                    logger.debug(
                        f"[TRADING ENGINE] Stale signal skipped: {address[:16]} "
                        f"age={age_s:.0f}s (max={SIGNAL_MAX_AGE_S}s)"
                    )
                    stale += 1
                    skipped += 1
                    continue
            except (ValueError, TypeError):
                pass  # Malformed timestamp — allow through, price check will guard

        # Check if we've recently seen/traded this token
        with _lock:
            if address in _recent_auto_trades:
                if now - _recent_auto_trades[address] < AUTO_EXEC_COOLDOWN_S:
                    _stats["dropped_duplicate"] += 1
                    skipped += 1
                    continue

        # STRONG BUY: auto-execute if enabled and safe
        if (AUTO_EXEC_ENABLED
                and score >= AUTO_EXEC_MIN_SCORE
                and auto_count_this_tick < AUTO_EXEC_MAX_PER_TICK
                and _is_safe_for_auto(risk_flags)):

            result = _auto_execute_buy(signal)
            if result and result.get("success"):
                auto_results.append(result)
                auto_count_this_tick += 1
                with _lock:
                    _recent_auto_trades[address] = now
                    _stats["auto_executed"] += 1
            elif result and result.get("queued"):
                # Portfolio full or insufficient cash — queue for Andrew
                _enqueue_signal(signal)
                queued += 1
            continue

        # ALL signals ≥ 3.0: queue for Andrew review (she decides what to trade)
        if score >= 3.0:
            _enqueue_signal(signal)
            queued += 1
        else:
            skipped += 1

    if stale > 0:
        logger.info(f"[TRADING ENGINE] Rejected {stale} stale signal(s) (>{SIGNAL_MAX_AGE_S}s old)")

    return {
        "auto_executed": auto_results,
        "queued": queued,
        "skipped": skipped,
        "stale_rejected": stale,
        "pending_queue_size": len(_pending_queue),
    }


def _is_safe_for_auto(risk_flags: List[str]) -> bool:
    """Check if risk flags allow auto-execution."""
    if len(risk_flags) > AUTO_EXEC_MAX_RISK_FLAGS:
        return False
    for flag in risk_flags:
        for critical in AUTO_EXEC_CRITICAL_FLAGS:
            if critical.lower() in flag.lower():
                return False
    return True


def _auto_execute_buy(signal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Auto-execute a sim_buy for a STRONG BUY signal.

    Uses conservative position sizing. Returns trade result or None.
    """
    try:
        from repryntt.trading.trading_simulator import sim_buy, _fetch_price, _load_portfolio, MAX_POSITIONS

        # ── Pre-check position limit before expensive price lookups ──
        portfolio = _load_portfolio(JARVIS_WORKSPACE)
        if len(portfolio.get("positions", {})) >= MAX_POSITIONS:
            logger.info(
                f"[TRADING ENGINE] Position limit reached ({MAX_POSITIONS}). "
                f"Queuing {signal.get('address', '?')[:16]} instead of auto-exec."
            )
            return {"success": False, "queued": True,
                    "error": f"Max {MAX_POSITIONS} positions — queued for review"}

        address = signal["address"]
        score = signal["score"]
        grade = signal["grade"]
        reasoning = signal.get("reasoning", "")
        signal_price = signal.get("latest_price", 0)

        # ── Live price validation: don't buy into a dump ──
        if signal_price and signal_price > 0:
            try:
                price_info = _fetch_price(address)
                if price_info and price_info["price_usd"] > 0:
                    live_price = price_info["price_usd"]
                    price_change_pct = ((live_price / signal_price) - 1) * 100

                    if price_change_pct < -AUTO_EXEC_MAX_PRICE_DROP_PCT:
                        logger.warning(
                            f"[TRADING ENGINE] Price check FAILED for {address[:16]}: "
                            f"signal=${signal_price:.8f} → live=${live_price:.8f} "
                            f"({price_change_pct:+.1f}%, threshold: -{AUTO_EXEC_MAX_PRICE_DROP_PCT}%)"
                        )
                        return {"success": False, "queued": False,
                                "error": f"Price dropped {price_change_pct:.1f}% since signal"}

                    logger.info(
                        f"[TRADING ENGINE] Price check OK for {address[:16]}: "
                        f"signal=${signal_price:.8f} → live=${live_price:.8f} ({price_change_pct:+.1f}%)"
                    )
            except Exception as e:
                logger.warning(f"[TRADING ENGINE] Price check failed (network?): {e} — proceeding cautiously")

        # Position sizing: fixed $50 until portfolio reaches $500
        # At $50, we need 3x returns — quality setups only
        size_usd = AUTO_EXEC_MAX_POSITION_USD  # $50 fixed

        reason = (
            f"[AUTO-EXEC] {grade} score={score:.1f} | {reasoning[:200]}"
        )

        result_json = sim_buy(
            workspace=JARVIS_WORKSPACE,
            token=address,
            amount_usd=size_usd,
            reason=reason,
        )
        result = json.loads(result_json)

        if "error" in result:
            error = result["error"]
            logger.warning(f"[TRADING ENGINE] Auto-exec failed for {address[:16]}: {error}")
            # If it's a cash/position limit issue, queue instead
            if "Insufficient" in error or "Max" in error:
                return {"success": False, "queued": True, "error": error}
            return {"success": False, "error": error}

        logger.info(
            f"⚡ [TRADING ENGINE] AUTO-EXECUTED: {result.get('symbol', '?')} "
            f"${size_usd:.2f} (score={score:.1f}, {grade})"
        )

        # Log to trade journal
        try:
            from repryntt.reference.jarvis_trading_cycle import log_trade_decision
            log_trade_decision("AUTO_BUY", {
                "symbol": result.get("symbol", ""),
                "address": address,
                "amount_usd": size_usd,
                "score": score,
                "grade": grade,
                "price": result.get("price_at_market", 0),
                "reasoning": reason[:300],
                "auto_executed": True,
            })
        except Exception:
            pass

        # Append to daily memory
        _append_daily_memory(
            f"⚡ **AUTO-TRADE**: Bought {result.get('symbol', '?')} — "
            f"${size_usd:.2f} at ${result.get('price_at_market', 0):.8f} "
            f"(score {score:.1f} {grade}). {reasoning[:150]}"
        )

        # Fire hook alert
        try:
            from repryntt.comms.hooks.trading_parsers import parse_trade_execution
            from repryntt.comms.hooks.router import get_hook_router
            hook = parse_trade_execution({
                "action": "AUTO_BUY",
                "symbol": result.get("symbol", ""),
                "amount_usd": size_usd,
                "price": result.get("price_at_market", 0),
                "score": score,
                "grade": grade,
                "reason": reason[:200],
            })
            if hook:
                get_hook_router().dispatch(hook)
        except Exception:
            pass

        return {
            "success": True,
            "symbol": result.get("symbol", ""),
            "address": address,
            "amount_usd": size_usd,
            "score": score,
            "grade": grade,
        }

    except Exception as e:
        logger.error(f"[TRADING ENGINE] Auto-exec exception: {e}", exc_info=True)
        return None


def _enqueue_signal(signal: Dict[str, Any]):
    """Add a signal to the pending queue for Jarvis review."""
    address = signal.get("address", "")
    now = time.time()

    with _lock:
        # Don't re-queue if already pending
        for existing in _pending_queue:
            if existing.get("address") == address:
                # Update score if newer signal is stronger
                if signal.get("score", 0) > existing.get("score", 0):
                    existing.update(signal)
                    existing["_queued_at"] = now
                return

        signal["_queued_at"] = now
        _pending_queue.append(signal)
        _stats["queued_for_review"] += 1


# ─── Queue Access for Jarvis ────────────────────────────────────────────────

def get_pending_count() -> int:
    """Return number of signals waiting for Jarvis review."""
    _expire_old_signals()
    return len(_pending_queue)


def get_pending_prompt() -> Optional[str]:
    """Build a trading prompt from all queued signals for Jarvis.

    Returns None if the queue is empty.
    Returns a focused prompt with ALL candidates so Jarvis can trade
    them in a single multi-trade session.
    """
    _expire_old_signals()

    with _lock:
        if not _pending_queue:
            return None

        candidates = list(_pending_queue)

    # Sort by score descending
    candidates.sort(key=lambda s: s.get("score", 0), reverse=True)

    # Load portfolio context
    try:
        from repryntt.reference.jarvis_trading_cycle import _load_sim_portfolio, _get_portfolio_summary
        portfolio = _load_sim_portfolio()
        pf = _get_portfolio_summary(portfolio)
    except Exception:
        pf = {"cash_balance": 0, "position_count": 0, "positions": []}

    parts = [
        "📊 **TRADING ENGINE — QUEUED SIGNALS FOR REVIEW**\n",
        f"You have **{len(candidates)} signal(s)** waiting for your decision.\n",
        f"**Cash: ${pf.get('cash_balance', 0):.2f}** | "
        f"Positions: {pf.get('position_count', 0)}/20\n",
    ]

    # Show auto-executed trades if any
    if _stats["auto_executed"] > 0:
        parts.append(
            f"_(The engine auto-executed {_stats['auto_executed']} STRONG BUY trade(s) "
            f"already — these are the remaining candidates that need your judgment.)_\n"
        )

    parts.append("**Candidates (ranked by score):**\n")

    # Load social profile data for each candidate from token profiles
    _social_cache = {}
    try:
        from pathlib import Path as _Path
        _profiles_dir = _Path(__file__).resolve().parent / "data" / "token_profiles"
        if _profiles_dir.exists():
            for c in candidates[:10]:
                addr = c.get("address", "")
                pfile = _profiles_dir / f"{addr}.json"
                if pfile.exists():
                    try:
                        with open(pfile) as _pf:
                            pd = json.load(_pf)
                        socials = []
                        raw = pd.get("raw_social_info", "")
                        if isinstance(raw, str) and raw:
                            raw = json.loads(raw)
                        if isinstance(raw, dict):
                            for s in raw.get("socials", []):
                                socials.append(s.get("url", ""))
                            for w in raw.get("websites", []):
                                if isinstance(w, dict):
                                    socials.append(w.get("url", ""))
                                elif isinstance(w, str):
                                    socials.append(w)
                        name = pd.get("token_name", "")
                        _social_cache[addr] = {"name": name, "socials": socials}
                    except Exception:
                        pass
    except Exception:
        pass

    for i, c in enumerate(candidates[:10], 1):
        score = c.get("score", 0)
        grade = c.get("grade", "?")
        address = c.get("address", "?")
        reasoning = c.get("reasoning", "")
        risk = c.get("risk_flags", [])
        mcap = c.get("market_cap", 0)
        p5m = c.get("price_change_5m", 0)

        risk_str = f" ⚠️ {', '.join(risk)}" if risk else ""
        social_info = _social_cache.get(address, {})
        name_str = f" ({social_info['name']})" if social_info.get("name") else ""
        socials_str = ""
        if social_info.get("socials"):
            socials_str = "\n     🔗 " + " | ".join(social_info["socials"][:3])
        parts.append(
            f"  **{i}. [{grade}] Score {score:.1f}** — `{address}`{name_str}\n"
            f"     MCap: ${mcap:,.0f} | 5m: {p5m:+.1f}%{risk_str}{socials_str}\n"
            f"     {reasoning[:120]}\n"
        )

    parts.append(
        "\n**INSTRUCTIONS — RESEARCH THEN TRADE:**\n"
        "1. For the top 2-3 candidates, use `trading_token_detail(address)` to see full profile\n"
        "2. Check the social links above — use `x_search_tweets` or `web_search` to gauge hype.\n"
        "   The #1 driver of memecoin price is MASS ATTENTION. Ask yourself:\n"
        "   - Is this token getting viral attention right now?\n"
        "   - Does the narrative grab people? (meme, celebrity, event-driven?)\n"
        "   - Is the community active and growing or dead?\n"
        "3. For tokens that pass your research: `sim_buy(token=address, amount_usd=X, reason='...')`\n"
        "4. Position sizing: ~$16 (0.2 SOL) per trade. You have limited capital — make it count.\n"
        "5. Also check existing positions — `sim_portfolio()` — sell losers and take profits\n"
        "6. Record your reasoning — WHY you think this token has attention momentum.\n"
        "   Remember what worked and what didn't. Your past trades teach you.\n\n"
        "**DO NOT** blindly buy on score alone. Research the narrative. "
        "Attention is everything in memecoins — find the tokens people can't stop talking about.\n"
    )

    return "\n".join(parts)


def mark_queue_reviewed():
    """Clear the pending queue after Jarvis has processed it."""
    with _lock:
        count = len(_pending_queue)
        _pending_queue.clear()
        _stats["jarvis_reviewed"] += count
        logger.info(f"[TRADING ENGINE] Queue cleared after Jarvis review ({count} signals)")


def _expire_old_signals():
    """Remove signals that have aged out of relevance."""
    now = time.time()
    with _lock:
        expired = []
        for sig in _pending_queue:
            age = now - sig.get("_queued_at", 0)
            if age > SIGNAL_EXPIRY_S:
                expired.append(sig)
        for sig in expired:
            _pending_queue.remove(sig)
            _stats["dropped_expired"] += 1

        # Also clean up old cooldown entries
        stale_keys = [k for k, v in _recent_auto_trades.items() if now - v > AUTO_EXEC_COOLDOWN_S * 2]
        for k in stale_keys:
            del _recent_auto_trades[k]


# ─── Stats ───────────────────────────────────────────────────────────────────

def get_engine_stats() -> Dict[str, Any]:
    """Return trading engine statistics."""
    return {
        **_stats,
        "pending_queue_size": len(_pending_queue),
        "recent_auto_cooldowns": len(_recent_auto_trades),
    }


# ─── Daily Memory Helper ────────────────────────────────────────────────────

def _append_daily_memory(text: str):
    """Append a note to Jarvis's daily memory file."""
    try:
        from datetime import date
        memory_dir = os.path.join(JARVIS_WORKSPACE, "memory")
        os.makedirs(memory_dir, exist_ok=True)
        daily_path = os.path.join(memory_dir, date.today().isoformat() + ".md")
        with open(daily_path, 'a') as f:
            f.write(f"\n{text}\n")
    except Exception:
        pass


# ─── Persistence (survive restarts) ─────────────────────────────────────────

def save_state():
    """Persist engine state to disk."""
    with _lock:
        state = {
            "pending_queue": list(_pending_queue),
            "recent_auto_trades": _recent_auto_trades.copy(),
            "stats": _stats.copy(),
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
    try:
        os.makedirs(ENGINE_STATE_FILE.parent, exist_ok=True)
        tmp = str(ENGINE_STATE_FILE) + ".tmp"
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, str(ENGINE_STATE_FILE))
    except Exception as e:
        logger.warning(f"[TRADING ENGINE] Failed to save state: {e}")


def load_state():
    """Restore engine state from disk."""
    global _stats
    if not ENGINE_STATE_FILE.exists():
        return

    try:
        with open(ENGINE_STATE_FILE) as f:
            state = json.load(f)

        with _lock:
            for sig in state.get("pending_queue", []):
                _pending_queue.append(sig)
            _recent_auto_trades.update(state.get("recent_auto_trades", {}))
            saved_stats = state.get("stats", {})
            for k in _stats:
                if k in saved_stats:
                    _stats[k] = saved_stats[k]

        logger.info(f"[TRADING ENGINE] State restored: {len(_pending_queue)} pending, "
                     f"{_stats['auto_executed']} auto-executed total")
    except Exception as e:
        logger.warning(f"[TRADING ENGINE] Failed to load state: {e}")
