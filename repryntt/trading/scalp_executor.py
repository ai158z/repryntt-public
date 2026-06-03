"""
SAIGE Scalp Executor — Real-Time Memecoin Scalp Engine
======================================================

Port of simtrade13.js into Python, integrated with the SAIGE trading pipeline.

Architecture:
  ai72_andahalf.py → signal_tokens/ → ScalpExecutor → sim_buy/sim_sell

  The executor:
  1. Watches signal_tokens/ for new signals in real-time (3s poll)
  2. Aggregates signals per-token — when a token accumulates enough
     bullish signals in a short window, it becomes a buy candidate
  3. Executes ONE trade at a time (focused monitoring, like simtrade13.js)
  4. Monitors the active position every 5s with tight TP/SL/timeout
  5. Auto-exits and moves to the next queued candidate
  6. Logs everything — Andrew reviews performance and adjusts parameters

Andrew's role: strategist. Sets parameters, reviews performance,
manually triggers buys/sells, adjusts risk.
The executor's role: fast hands. Enters and exits at machine speed.
"""

import json
import os
import time
import logging
import threading
import requests
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger("saige.scalp")

BASE_DIR = Path(__file__).resolve().parent
SIGNAL_DIR = BASE_DIR / "data" / "signal_tokens"
WORKSPACE = str(Path.home() / ".repryntt" / "workspace" / "agents" / "operator")
CONFIG_FILE = os.path.join(WORKSPACE, "scalp_config.json")
STATUS_FILE = os.path.join(WORKSPACE, "scalp_status.json")
TRADES_FILE = os.path.join(WORKSPACE, "scalp_trades.json")
PERF_FILE = os.path.join(WORKSPACE, "scalp_token_perf.json")
COMMAND_FILE = os.path.join(WORKSPACE, "scalp_command.json")

# ─── Default Configuration (Andrew can adjust all of these) ─────────────────

DEFAULT_CONFIG = {
    "take_profit_pct": 200.0,        # +200% auto-sell (3x target — patience, not scalps)
    "stop_loss_pct": -15.0,          # -15% auto-cut (capital preservation)
    "max_hold_seconds": 7200,        # 2 hours max hold — tokens need time to pump
    "position_size_usd": 16.0,      # ~0.2 SOL per trade — enough for meaningful profit
    "min_signals_to_buy": 5,        # Min bullish signals in window to trigger buy
    "signal_window_s": 120,         # 2 min rolling window for signal aggregation
    "min_buy_signal_types": 2,      # At least 2 different bullish signal types
    "max_consecutive_wins": 3,      # Block token after N consecutive wins (from simtrade13)
    "poll_interval_s": 3,           # Signal directory poll interval
    "monitor_interval_s": 30,       # Position price check interval (less frequent — patience)
    "enabled": True,                # Master switch
    "max_trades_per_hour": 3,       # Rate limit — quality over quantity
    "auto_execute": False,          # DISABLED — Andrew trades manually until proven profitable
}

BULLISH_TYPES = {"TP2 Buy", "Higher Low Buy", "TP1 Buy", "TP3 Buy", "Momentum"}
BEARISH_TYPES = {"Large Sell Detected"}

# ─── Module-level singleton ──────────────────────────────────────────────────

_executor: Optional['ScalpExecutor'] = None


def get_executor() -> 'ScalpExecutor':
    global _executor
    if _executor is None:
        _executor = ScalpExecutor()
    return _executor


def start():
    """Start the scalp executor daemon."""
    get_executor().start()


def stop():
    """Stop the scalp executor daemon."""
    if _executor:
        _executor.stop()


# ─── Fast Price Fetch ────────────────────────────────────────────────────────

def _fast_price(address: str) -> Optional[float]:
    """Fast price fetch via DexScreener direct token endpoint (skips text search)."""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{address}",
            timeout=8,
            headers={"Accept": "application/json", "User-Agent": "SAIGE/1.0"}
        )
        if r.status_code != 200:
            return None
        pairs = r.json().get("pairs", [])
        if not pairs:
            return None
        # Prefer Solana pair
        for p in pairs:
            if p.get("chainId", "").lower() == "solana":
                ps = p.get("priceUsd")
                if ps:
                    return float(ps)
        ps = pairs[0].get("priceUsd")
        return float(ps) if ps else None
    except Exception:
        return None


# ─── Scalp Executor ─────────────────────────────────────────────────────────

class ScalpExecutor:
    """Real-time memecoin scalp execution engine.

    Mirrors simtrade13.js architecture: file-watch → score → buy one →
    monitor tight → sell → next in queue.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Config (hot-reloaded every loop iteration)
        self.config = dict(DEFAULT_CONFIG)
        self._load_config()

        # Signal tracking
        self._processed_files: set = set()
        self._signal_window: Dict[str, list] = defaultdict(list)  # addr → [{time, type, price, mcap}]
        self._candidates_seen: set = set()  # Prevent re-evaluating same token in same window

        # Trade state
        self._active_trade: Optional[Dict[str, Any]] = None
        self._trade_queue: deque = deque(maxlen=10)
        self._token_perf: Dict[str, Any] = {}
        self._trade_history: list = []
        self._trades_this_hour = 0
        self._hour_start = time.time()
        self._last_monitor = 0.0
        self._last_status_write = 0.0

        # Stats
        self.stats = {
            "total_trades": 0,
            "winning_trades": 0,
            "total_pnl_usd": 0.0,
            "started_at": None,
            "last_trade_at": None,
            "avg_hold_s": 0.0,
            "best_trade_pnl": 0.0,
            "worst_trade_pnl": 0.0,
        }

        self._load_perf()
        self._load_trades()

    # ─── Config Persistence ──────────────────────────────────────────────

    def _load_config(self):
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE) as f:
                    saved = json.load(f)
                for k, v in saved.items():
                    if k in DEFAULT_CONFIG:
                        self.config[k] = v
        except Exception:
            pass

    def _save_config(self):
        try:
            os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
            with open(CONFIG_FILE, 'w') as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            logger.error(f"[SCALP] Config save failed: {e}")

    # ─── Performance / Trade History Persistence ─────────────────────────

    def _load_perf(self):
        try:
            if os.path.exists(PERF_FILE):
                with open(PERF_FILE) as f:
                    self._token_perf = json.load(f)
        except Exception:
            self._token_perf = {}

    def _save_perf(self):
        try:
            tmp = PERF_FILE + ".tmp"
            with open(tmp, 'w') as f:
                json.dump(self._token_perf, f, indent=2)
            os.replace(tmp, PERF_FILE)
        except Exception:
            pass

    def _load_trades(self):
        try:
            if os.path.exists(TRADES_FILE):
                with open(TRADES_FILE) as f:
                    data = json.load(f)
                self._trade_history = data.get("trades", [])
                saved_stats = data.get("stats", {})
                for k, v in saved_stats.items():
                    if k in self.stats:
                        self.stats[k] = v
        except Exception:
            pass

    def _save_trades(self):
        try:
            tmp = TRADES_FILE + ".tmp"
            with open(tmp, 'w') as f:
                json.dump({
                    "trades": self._trade_history[-200:],
                    "stats": self.stats,
                }, f, indent=2)
            os.replace(tmp, TRADES_FILE)
        except Exception:
            pass

    # ─── Lifecycle ───────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self.stats["started_at"] = datetime.now(timezone.utc).isoformat()

        # Mark all existing signal files as processed — don't replay history
        self._init_processed_files()

        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="scalp-executor"
        )
        self._thread.start()
        logger.info("[SCALP] Scalp executor started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        self._write_status()
        logger.info("[SCALP] Scalp executor stopped")

    def _init_processed_files(self):
        """Mark all existing signal files as processed on startup."""
        try:
            if SIGNAL_DIR.exists():
                for entry in os.scandir(SIGNAL_DIR):
                    if entry.name.endswith('.json'):
                        self._processed_files.add(entry.name)
                logger.info(
                    f"[SCALP] Marked {len(self._processed_files)} existing signals as processed"
                )
        except Exception as e:
            logger.error(f"[SCALP] Init processed files failed: {e}")

    # ─── Main Loop ───────────────────────────────────────────────────────

    def _run_loop(self):
        """Main executor loop — scan, evaluate, monitor, repeat."""
        while self._running:
            try:
                self._load_config()  # Hot-reload config

                if not self.config.get("enabled", True):
                    time.sleep(5)
                    continue

                # 1. Check for Andrew commands (force buy/sell)
                self._check_commands()

                # 2. Scan for new signal files
                self._scan_new_signals()

                # 3. Prune old signals from rolling window
                self._prune_signal_window()

                # 4. Evaluate candidates — buy if threshold met
                self._evaluate_candidates()

                # 5. Monitor active trade — TP/SL/timeout
                now = time.time()
                monitor_interval = self.config["monitor_interval_s"]
                if self._active_trade and (now - self._last_monitor >= monitor_interval):
                    self._monitor_active_trade()
                    self._last_monitor = now

                # 6. Write status file (every 10s)
                if now - self._last_status_write >= 10:
                    self._write_status()
                    self._last_status_write = now

                # Reset hourly trade counter
                if now - self._hour_start >= 3600:
                    self._trades_this_hour = 0
                    self._hour_start = now

                time.sleep(self.config["poll_interval_s"])

            except Exception as e:
                logger.error(f"[SCALP] Loop error: {e}", exc_info=True)
                time.sleep(5)

    # ─── Signal Scanning ─────────────────────────────────────────────────

    def _scan_new_signals(self):
        """Check signal_tokens/ for new files and aggregate by token."""
        if not SIGNAL_DIR.exists():
            return

        try:
            current_files = set()
            for entry in os.scandir(SIGNAL_DIR):
                if entry.name.endswith('.json'):
                    current_files.add(entry.name)
        except Exception:
            return

        new_files = current_files - self._processed_files
        if not new_files:
            return

        now = time.time()
        window = self.config["signal_window_s"]

        for fname in new_files:
            self._processed_files.add(fname)
            fpath = SIGNAL_DIR / fname

            try:
                with open(fpath) as f:
                    sig = json.load(f)

                addr = sig.get("address", "")
                sig_type = sig.get("signal_type", "")
                price = sig.get("current_price", 0) or 0
                mcap = sig.get("market_cap", 0) or 0

                if not addr or not sig_type:
                    continue

                # Parse detection timestamp
                ts_str = sig.get("detection_timestamp", "")
                try:
                    ts = datetime.fromisoformat(
                        ts_str.replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    ts = now

                # Only add if within aggregation window
                if now - ts <= window:
                    self._signal_window[addr].append({
                        "time": ts,
                        "type": sig_type,
                        "price": float(price) if price else 0,
                        "mcap": float(mcap) if mcap else 0,
                    })
            except Exception:
                continue

        # Prune processed_files set to prevent unbounded growth
        if len(self._processed_files) > 10000:
            self._processed_files = self._processed_files & current_files

    def _prune_signal_window(self):
        """Remove signals older than the window from memory."""
        now = time.time()
        window = self.config["signal_window_s"]

        to_remove = []
        for addr, signals in self._signal_window.items():
            self._signal_window[addr] = [
                s for s in signals if now - s["time"] <= window
            ]
            if not self._signal_window[addr]:
                to_remove.append(addr)

        for addr in to_remove:
            del self._signal_window[addr]
            self._candidates_seen.discard(addr)

    # ─── Candidate Evaluation ────────────────────────────────────────────

    def _evaluate_candidates(self):
        """Check if any token in the signal window meets buy threshold."""
        if self._active_trade:
            return  # One trade at a time (simtrade13 pattern)

        if self._trades_this_hour >= self.config["max_trades_per_hour"]:
            return

        min_signals = self.config["min_signals_to_buy"]
        min_types = self.config["min_buy_signal_types"]

        best_candidate = None
        best_score = 0

        for addr, signals in self._signal_window.items():
            if addr in self._candidates_seen:
                continue

            # Count bullish signals
            bullish = [s for s in signals if s["type"] in BULLISH_TYPES]
            if len(bullish) < min_signals:
                continue

            # Check signal type diversity
            types = set(s["type"] for s in bullish)
            if len(types) < min_types:
                continue

            # Consecutive wins filter (from simtrade13)
            if not self._is_token_eligible(addr):
                self._candidates_seen.add(addr)
                continue

            # Already in portfolio?
            if self._is_already_held(addr):
                self._candidates_seen.add(addr)
                continue

            # Quick score: signal_count * type_diversity - bearish penalty
            score = len(bullish) * len(types)
            bearish_count = sum(1 for s in signals if s["type"] in BEARISH_TYPES)
            score -= bearish_count * 3

            if score > best_score:
                best_score = score
                latest = max(bullish, key=lambda s: s["time"])
                best_candidate = {
                    "address": addr,
                    "signal_count": len(bullish),
                    "signal_types": list(types),
                    "latest_price": latest["price"],
                    "mcap": latest["mcap"],
                    "score": score,
                }

        if best_candidate:
            self._candidates_seen.add(best_candidate["address"])

            if self.config.get("auto_execute", True):
                self._execute_buy(best_candidate)
            else:
                self._trade_queue.append(best_candidate)
                logger.info(
                    f"[SCALP] Queued: {best_candidate['address'][:16]}... "
                    f"({best_candidate['signal_count']} signals, "
                    f"types: {best_candidate['signal_types']})"
                )

    def _is_token_eligible(self, address: str) -> bool:
        """Check if token isn't blocked by consecutive wins filter."""
        perf = self._token_perf.get(address, {})
        history = perf.get("history", [])
        max_wins = self.config["max_consecutive_wins"]

        consecutive = 0
        for entry in reversed(history):
            if entry.get("result") == "win":
                consecutive += 1
            else:
                break

        return consecutive < max_wins

    def _is_already_held(self, address: str) -> bool:
        """Check if this token is already in the sim portfolio."""
        try:
            portfolio_path = os.path.join(WORKSPACE, "sim_portfolio.json")
            if not os.path.exists(portfolio_path):
                return False
            with open(portfolio_path) as f:
                portfolio = json.load(f)
            for _sym, pos in portfolio.get("positions", {}).items():
                if pos.get("token_address", "").lower() == address.lower():
                    return True
            return False
        except Exception:
            return False

    # ─── Trade Execution ─────────────────────────────────────────────────

    def _execute_buy(self, candidate: dict):
        """Execute a buy for a candidate token via sim_buy."""
        try:
            from repryntt.trading.trading_simulator import sim_buy

            address = candidate["address"]
            size_usd = self.config["position_size_usd"]

            reason = (
                f"[SCALP-EXEC] {candidate['signal_count']} signals "
                f"({', '.join(candidate['signal_types'][:4])}), "
                f"score={candidate.get('score', 0)}"
            )

            result_json = sim_buy(
                workspace=WORKSPACE,
                token=address,
                amount_usd=size_usd,
                reason=reason,
            )
            result = json.loads(result_json)

            if "error" in result:
                logger.warning(f"[SCALP] Buy failed for {address[:16]}: {result['error']}")
                return

            symbol = result.get("symbol", "???")
            entry_price = result.get("price_at_market", 0)

            with self._lock:
                self._active_trade = {
                    "address": address,
                    "symbol": symbol,
                    "entry_price": entry_price,
                    "entry_time": time.time(),
                    "size_usd": size_usd,
                    "signals": candidate["signal_count"],
                    "signal_types": candidate["signal_types"],
                }
                self._trades_this_hour += 1

            logger.info(
                f"⚡ [SCALP] BUY: {symbol} ${size_usd:.0f} at "
                f"${entry_price:.8f} ({candidate['signal_count']} signals)"
            )

            self._append_daily_memory(
                f"⚡ **SCALP-BUY**: {symbol} — ${size_usd:.0f} at "
                f"${entry_price:.8f} ({candidate['signal_count']} signals: "
                f"{', '.join(candidate['signal_types'][:4])})"
            )

        except Exception as e:
            logger.error(f"[SCALP] Buy execution error: {e}", exc_info=True)

    def _monitor_active_trade(self):
        """Check active trade price, apply TP/SL/timeout."""
        with self._lock:
            trade = self._active_trade

        if not trade:
            self._process_queue()
            return

        try:
            current_price = _fast_price(trade["address"])
            if current_price is None or current_price <= 0:
                logger.debug(f"[SCALP] Price fetch failed for {trade['symbol']}")
                return

            entry_price = trade["entry_price"]
            if entry_price <= 0:
                return

            pnl_pct = ((current_price / entry_price) - 1) * 100
            time_held = time.time() - trade["entry_time"]

            tp = self.config["take_profit_pct"]
            sl = self.config["stop_loss_pct"]
            max_hold = self.config["max_hold_seconds"]

            logger.debug(
                f"[SCALP] {trade['symbol']}: ${current_price:.8f} "
                f"({pnl_pct:+.2f}%) hold={time_held:.0f}s"
            )

            action = None
            reason = ""

            if pnl_pct >= tp:
                action = "TAKE_PROFIT"
                reason = (
                    f"[SCALP-TP] {trade['symbol']} +{pnl_pct:.1f}% "
                    f"(target: +{tp}%)"
                )
            elif pnl_pct <= sl:
                action = "STOP_LOSS"
                reason = (
                    f"[SCALP-SL] {trade['symbol']} {pnl_pct:.1f}% "
                    f"(limit: {sl}%)"
                )
            elif time_held >= max_hold:
                action = "TIMEOUT"
                reason = (
                    f"[SCALP-TIMEOUT] {trade['symbol']} held {time_held:.0f}s "
                    f"(max: {max_hold}s), P/L: {pnl_pct:+.1f}%"
                )

            if action:
                self._execute_sell(reason, action, pnl_pct, current_price)

        except Exception as e:
            logger.error(f"[SCALP] Monitor error: {e}", exc_info=True)

    def _execute_sell(self, reason: str, action: str = "MANUAL",
                      pnl_pct: float = 0.0, current_price: float = 0.0):
        """Execute a sell of the active trade."""
        with self._lock:
            trade = self._active_trade

        if not trade:
            return

        try:
            from repryntt.trading.trading_simulator import sim_sell

            result_json = sim_sell(
                workspace=WORKSPACE,
                token=trade["symbol"],
                sell_pct=100,
                reason=reason,
            )
            result = json.loads(result_json)

            if "error" in result:
                logger.warning(
                    f"[SCALP] Sell failed for {trade['symbol']}: {result['error']}"
                )
                # Position already gone (sold by watchdog or Andrew) — clear state
                if "No position" in result.get("error", ""):
                    with self._lock:
                        self._active_trade = None
                    self._process_queue()
                return

            pnl_usd = result.get("pnl", 0)
            hold_time = time.time() - trade["entry_time"]
            is_win = pnl_usd > 0

            # Update stats
            with self._lock:
                self.stats["total_trades"] += 1
                if is_win:
                    self.stats["winning_trades"] += 1
                self.stats["total_pnl_usd"] = round(
                    self.stats["total_pnl_usd"] + pnl_usd, 2
                )
                self.stats["last_trade_at"] = datetime.now(timezone.utc).isoformat()
                if pnl_usd > self.stats.get("best_trade_pnl", 0):
                    self.stats["best_trade_pnl"] = round(pnl_usd, 2)
                if pnl_usd < self.stats.get("worst_trade_pnl", 0):
                    self.stats["worst_trade_pnl"] = round(pnl_usd, 2)

                # Running average hold time
                total = self.stats["total_trades"]
                prev_avg = self.stats.get("avg_hold_s", 0)
                self.stats["avg_hold_s"] = round(
                    ((prev_avg * (total - 1)) + hold_time) / total, 1
                )

                self._active_trade = None

            # Update token performance (consecutive wins filter)
            addr = trade["address"]
            if addr not in self._token_perf:
                self._token_perf[addr] = {"wins": 0, "losses": 0, "history": []}
            tp = self._token_perf[addr]
            if is_win:
                tp["wins"] += 1
            else:
                tp["losses"] += 1
            tp["history"].append({
                "result": "win" if is_win else "loss",
                "pnl_usd": round(pnl_usd, 2),
                "pnl_pct": round(pnl_pct, 2),
                "hold_s": round(hold_time, 1),
                "action": action,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            tp["history"] = tp["history"][-50:]  # Cap per-token history
            self._save_perf()

            # Add to trade history
            self._trade_history.append({
                "symbol": trade["symbol"],
                "address": trade["address"],
                "entry_price": trade["entry_price"],
                "exit_price": current_price or result.get("price_at_market", 0),
                "pnl_usd": round(pnl_usd, 2),
                "pnl_pct": round(pnl_pct, 2),
                "hold_seconds": round(hold_time, 1),
                "action": action,
                "size_usd": trade["size_usd"],
                "signals": trade["signals"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            self._save_trades()

            emoji = "✅" if is_win else "❌"
            logger.info(
                f"{emoji} [SCALP] {action}: {trade['symbol']} — "
                f"P/L {pnl_pct:+.1f}% (${pnl_usd:+.2f}) — held {hold_time:.0f}s"
            )

            self._append_daily_memory(
                f"{emoji} **SCALP-{action}**: {trade['symbol']} — "
                f"P/L {pnl_pct:+.1f}% (${pnl_usd:+.2f}), held {hold_time:.0f}s"
            )

            # Move to next in queue
            self._process_queue()

        except Exception as e:
            logger.error(f"[SCALP] Sell error: {e}", exc_info=True)

    def _process_queue(self):
        """Try to start the next trade from the queue."""
        while self._trade_queue and not self._active_trade:
            candidate = self._trade_queue.popleft()
            if (self._is_token_eligible(candidate["address"])
                    and not self._is_already_held(candidate["address"])):
                self._execute_buy(candidate)
                break

    # ─── Commands from Andrew ───────────────────────────────────────────

    def _check_commands(self):
        """Check for command file from Andrew (force buy/sell/config)."""
        if not os.path.exists(COMMAND_FILE):
            return

        try:
            with open(COMMAND_FILE) as f:
                cmd = json.load(f)
            os.remove(COMMAND_FILE)

            action = cmd.get("action", "")

            if action == "force_buy":
                address = cmd.get("address", "")
                if address:
                    candidate = {
                        "address": address,
                        "signal_count": 0,
                        "signal_types": ["MANUAL"],
                        "latest_price": 0,
                        "mcap": 0,
                        "score": 99,
                    }
                    if self._active_trade:
                        self._trade_queue.appendleft(candidate)
                        logger.info(f"[SCALP] Force buy queued: {address[:16]}")
                    else:
                        self._execute_buy(candidate)

            elif action == "force_sell":
                reason = cmd.get("reason", "Manual sell by Andrew")
                if self._active_trade:
                    self._execute_sell(reason, "MANUAL")

            elif action == "clear_queue":
                self._trade_queue.clear()
                logger.info("[SCALP] Queue cleared by Andrew")

        except Exception as e:
            logger.error(f"[SCALP] Command error: {e}")
            try:
                os.remove(COMMAND_FILE)
            except Exception:
                pass

    # ─── Status / IO ─────────────────────────────────────────────────────

    def _write_status(self):
        """Write current status to file for Andrew to read."""
        try:
            with self._lock:
                active = None
                if self._active_trade:
                    t = self._active_trade
                    active = {
                        "symbol": t["symbol"],
                        "address": t["address"],
                        "entry_price": t["entry_price"],
                        "time_in_trade_s": round(time.time() - t["entry_time"], 1),
                        "size_usd": t["size_usd"],
                        "signals": t["signals"],
                    }

                total = self.stats["total_trades"]
                status = {
                    "enabled": self.config.get("enabled", True),
                    "running": self._running,
                    "active_trade": active,
                    "queue_size": len(self._trade_queue),
                    "queue": [dict(c) for c in list(self._trade_queue)[:5]],
                    "config": dict(self.config),
                    "stats": dict(self.stats),
                    "win_rate": (
                        round(self.stats["winning_trades"] / total * 100, 1)
                        if total > 0 else 0
                    ),
                    "signals_tracked": len(self._signal_window),
                    "processed_files": len(self._processed_files),
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                }

            tmp = STATUS_FILE + ".tmp"
            with open(tmp, 'w') as f:
                json.dump(status, f, indent=2)
            os.replace(tmp, STATUS_FILE)

        except Exception:
            pass

    def _append_daily_memory(self, text: str):
        """Append a note to Jarvis's daily memory."""
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            daily_path = os.path.join(WORKSPACE, "memory", f"{today}.md")
            os.makedirs(os.path.dirname(daily_path), exist_ok=True)
            with open(daily_path, 'a') as f:
                f.write(f"\n{text}\n")
        except Exception:
            pass

    # ─── Public API (for Andrew tools) ──────────────────────────────────

    def get_status(self) -> dict:
        """Return current executor status."""
        try:
            if os.path.exists(STATUS_FILE):
                with open(STATUS_FILE) as f:
                    return json.load(f)
        except Exception:
            pass
        return {"running": self._running, "enabled": self.config.get("enabled")}

    def get_history(self, limit: int = 20) -> list:
        """Return recent scalp trade history."""
        return self._trade_history[-limit:]

    def set_param(self, param: str, value) -> str:
        """Update a config parameter. Returns confirmation or error."""
        if param not in DEFAULT_CONFIG:
            return f"Unknown param '{param}'. Valid: {sorted(DEFAULT_CONFIG.keys())}"

        expected_type = type(DEFAULT_CONFIG[param])
        try:
            if expected_type == bool:
                value = str(value).lower() in ("true", "1", "yes")
            else:
                value = expected_type(value)
        except (ValueError, TypeError):
            return f"Bad value for {param}: expected {expected_type.__name__}"

        self.config[param] = value
        self._save_config()
        return f"✅ {param} = {value}"

    def force_buy(self, address: str, reason: str = "") -> str:
        """Queue a manual buy command."""
        try:
            os.makedirs(os.path.dirname(COMMAND_FILE), exist_ok=True)
            with open(COMMAND_FILE, 'w') as f:
                json.dump({
                    "action": "force_buy",
                    "address": address,
                    "reason": reason or "Manual buy by Andrew",
                }, f)
            return f"✅ Force buy queued for {address[:16]}..."
        except Exception as e:
            return f"Error: {e}"

    def force_sell(self, reason: str = "") -> str:
        """Queue a manual sell command."""
        try:
            os.makedirs(os.path.dirname(COMMAND_FILE), exist_ok=True)
            with open(COMMAND_FILE, 'w') as f:
                json.dump({
                    "action": "force_sell",
                    "reason": reason or "Manual sell by Andrew",
                }, f)
            return "✅ Force sell queued"
        except Exception as e:
            return f"Error: {e}"
