"""
SAIGE Trading Bot Bridge — Connects Jarvis to the live trading_bot/ pipeline
==============================================================================

Gives Jarvis tools to:
- Start / stop the trading bot monitoring components (NOT execution bots)
- Read signals, predictions, hot tokens, and performance from the bot
- Get detailed token profiles with price history (for charts & analysis)

The trading_bot/ suite runs independently, writing data to:
  trading_bot/tokens.db              — SQLite with live token data
  trading_bot/data/token_profiles/   — per-token JSON with price history
  trading_bot/data/signal_tokens/    — pattern-detected trade signals
  trading_bot/data/predictions/      — AI-confirmed buy signals
  trading_bot/data/token_performance.json — win/loss record

This bridge reads that data non-destructively so Jarvis can make informed
sim trades without modifying the live pipeline.

Revenue target: $1,000 USD / 15 SOL per day.
"""

import json
import os
import glob
import time
import signal
import sqlite3
import logging
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

logger = logging.getLogger("saige.trading_bot_bridge")

# ─── Paths ────────────────────────────────────────────────────────────────────

_MODULE_DIR = Path(__file__).resolve().parent
TRADING_BOT_DIR = _MODULE_DIR
DATA_DIR = _MODULE_DIR / "data"
TOKENS_DB = DATA_DIR / "tokens.db"
TOKEN_PROFILES_DIR = DATA_DIR / "token_profiles"
SIGNAL_TOKENS_DIR = DATA_DIR / "signal_tokens"
PREDICTIONS_DIR = DATA_DIR / "predictions"
AI_INPUT_DIR = DATA_DIR / "ai_input"
WATCH_DIR = DATA_DIR / "watch_dir"
PERFORMANCE_FILE = DATA_DIR / "token_performance.json"
TRADE_SIGNALS_FILE = DATA_DIR / "trade_signals.json"
ACTIVE_TOKENS_FILE = DATA_DIR / "active_tokens.json"

# Python venv
from repryntt.platform_utils import get_venv_python as _get_venv_py
PYTHON_BIN = _get_venv_py(Path.home() / "saige_venv")
if not PYTHON_BIN.exists():
    import sys as _sys_bot
    PYTHON_BIN = Path(_sys_bot.executable)

# ─── Component Registry ──────────────────────────────────────────────────────

# Components that Jarvis can start/stop (NEVER the JS execution bots)
# "module" is used for `python -m` style launching (preferred for imports)
MANAGED_COMPONENTS = {
    "monitor": {
        "script": "token_monitor.py",
        "module": "repryntt.trading.token_monitor",
        "description": "Core token monitor — DexScreener polling, pattern detection, signal generation",
        "pidfile": "monitor.pid",
    },
    "trend_agent": {
        "script": "trend_agent.py",
        "module": "repryntt.trading.trend_agent",
        "description": "AI trend analysis — ATH breakout detection + LLM confirmation",
        "pidfile": "trend_agent.pid",
    },
    "dashboard": {
        "script": "dashboard_server.py",
        "module": "repryntt.trading.dashboard_server",
        "description": "Web dashboard on port 8888 — live charts, signals, performance",
        "pidfile": "dashboard.pid",
    },
    "token_fetcher": {
        "script": "token_fetcher.py",
        "module": "repryntt.trading.token_fetcher",
        "description": "Token discovery — watches PumpFun/Raydium/Boop wallets for new mints",
        "pidfile": "token_fetcher.pid",
    },
    "cleanup": {
        "script": "token_cleanup.py",
        "module": "repryntt.trading.token_cleanup",
        "description": "Profile archiver — removes stale/scam token profiles every 30s",
        "pidfile": "cleanup.pid",
    },
}

# Track PIDs of processes we started
_managed_pids: Dict[str, int] = {}


# ─── Bot Control ──────────────────────────────────────────────────────────────

def trading_bot_start(component: str = "all") -> str:
    """Start trading bot monitoring components. Does NOT start the JS execution bots (no real money).

    Components: monitor (core engine), trend_agent (AI analysis), dashboard (web UI on :8888),
    token_fetcher (wallet watcher), cleanup (profile archiver), or 'all' to start everything.

    Parameters:
        component: Which component to start — 'monitor', 'trend_agent', 'dashboard', 'token_fetcher', 'cleanup', or 'all'.

    Returns:
        JSON with start status for each component.
    """
    component = (component or "all").strip().lower()
    results = {}

    if component == "all":
        targets = list(MANAGED_COMPONENTS.keys())
    elif component in MANAGED_COMPONENTS:
        targets = [component]
    else:
        return json.dumps({
            "error": f"Unknown component '{component}'",
            "available": list(MANAGED_COMPONENTS.keys()) + ["all"],
        })

    for name in targets:
        info = MANAGED_COMPONENTS[name]
        script_path = TRADING_BOT_DIR / info["script"]

        if not script_path.exists():
            results[name] = {"status": "error", "reason": f"Script not found: {script_path}"}
            continue

        # Check if already running
        if name in _managed_pids:
            pid = _managed_pids[name]
            try:
                os.kill(pid, 0)  # Check if process is still alive
                results[name] = {"status": "already_running", "pid": pid}
                continue
            except OSError:
                # Process died, clean up
                del _managed_pids[name]

        # Also check if running externally (started outside Jarvis)
        if _is_script_running(info["script"]) or _is_script_running(info.get("module", "")):
            results[name] = {"status": "already_running_externally",
                             "note": "Started outside Jarvis"}
            continue

        try:
            log_file = TRADING_BOT_DIR / f"{name}.log"
            with open(log_file, 'a') as logf:
                logf.write(f"\n{'='*60}\n")
                logf.write(f"Started by Jarvis at {datetime.now(timezone.utc).isoformat()}\n")
                logf.write(f"{'='*60}\n")

            # Prefer module-style launch for reliable imports
            module_name = info.get("module")
            if module_name:
                cmd = [str(PYTHON_BIN), "-m", module_name]
                cwd = str(TRADING_BOT_DIR.parent.parent)
            else:
                cmd = [str(PYTHON_BIN), str(script_path)]
                cwd = str(TRADING_BOT_DIR)

            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=open(log_file, 'a'),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            _managed_pids[name] = proc.pid
            results[name] = {
                "status": "started",
                "pid": proc.pid,
                "log": str(log_file),
                "description": info["description"],
            }
            logger.info(f"[TRADING_BOT] Started {name} (PID {proc.pid})")
        except Exception as e:
            results[name] = {"status": "error", "reason": str(e)}
            logger.error(f"[TRADING_BOT] Failed to start {name}: {e}")

    return json.dumps({"components": results}, indent=2)


def trading_bot_stop(component: str = "all") -> str:
    """Stop trading bot monitoring components started by Jarvis.

    Parameters:
        component: Which component to stop — 'monitor', 'trend_agent', 'dashboard', 'token_fetcher', 'cleanup', or 'all'.
    """
    component = (component or "all").strip().lower()
    results = {}

    if component == "all":
        targets = list(MANAGED_COMPONENTS.keys())
    elif component in MANAGED_COMPONENTS:
        targets = [component]
    else:
        return json.dumps({"error": f"Unknown component '{component}'",
                           "available": list(MANAGED_COMPONENTS.keys()) + ["all"]})

    for name in targets:
        if name in _managed_pids:
            pid = _managed_pids[name]
            try:
                os.kill(pid, signal.SIGTERM)
                # Wait briefly for graceful shutdown
                for _ in range(10):
                    try:
                        os.kill(pid, 0)
                        time.sleep(0.5)
                    except (ProcessLookupError, OSError):
                        break
                else:
                    # Force kill if still alive
                    try:
                        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
                    except (ProcessLookupError, OSError):
                        pass
                del _managed_pids[name]
                results[name] = {"status": "stopped", "pid": pid}
                logger.info(f"[TRADING_BOT] Stopped {name} (PID {pid})")
            except OSError as e:
                del _managed_pids[name]
                results[name] = {"status": "already_dead", "pid": pid}
        else:
            # Try to find and kill externally-started process
            info = MANAGED_COMPONENTS[name]
            killed = _kill_script(info["script"])
            if killed:
                results[name] = {"status": "stopped_external", "note": "Was running outside Jarvis"}
            else:
                results[name] = {"status": "not_running"}

    return json.dumps({"components": results}, indent=2)


def _is_script_running(script_name: str) -> bool:
    """Check if a Python script/module is running."""
    if not script_name:
        return False
    from repryntt.platform_utils import is_process_running
    return is_process_running(script_name)


def _kill_script(script_name: str) -> bool:
    """Kill a running Python script by name."""
    from repryntt.platform_utils import kill_process_by_name
    return kill_process_by_name(script_name)


# ─── Bot Status ───────────────────────────────────────────────────────────────

def trading_bot_status() -> str:
    """Check the status of all trading bot components: which are running,
    token count in DB, data freshness, signal/prediction counts.
    Call this first to understand the bot's state before reading signals.

    Parameters:
        (none required)
    """
    status = {
        "components": {},
        "data": {},
        "summary": "",
    }

    # Check each component
    for name, info in MANAGED_COMPONENTS.items():
        running = False
        pid = None

        if name in _managed_pids:
            pid = _managed_pids[name]
            try:
                os.kill(pid, 0)
                running = True
            except OSError:
                del _managed_pids[name]
                pid = None

        if not running:
            running = _is_script_running(info["script"])

        status["components"][name] = {
            "running": running,
            "pid": pid,
            "description": info["description"],
        }

    # Check data state
    # Token count from DB
    db_count = 0
    if TOKENS_DB.exists():
        try:
            conn = sqlite3.connect(str(TOKENS_DB), timeout=5)
            cur = conn.execute("SELECT COUNT(*) FROM tokens")
            db_count = cur.fetchone()[0]
            conn.close()
        except Exception as e:
            db_count = f"error: {e}"

    # Count files in data dirs
    profile_count = len(list(TOKEN_PROFILES_DIR.glob("*.json"))) if TOKEN_PROFILES_DIR.exists() else 0
    signal_count = len(list(SIGNAL_TOKENS_DIR.glob("*.json"))) if SIGNAL_TOKENS_DIR.exists() else 0
    prediction_count = len(list(PREDICTIONS_DIR.glob("*.json"))) if PREDICTIONS_DIR.exists() else 0
    watch_count = len(list(WATCH_DIR.glob("*.json"))) if WATCH_DIR.exists() else 0

    # Check performance file
    performance = _load_performance()
    total_trades = 0
    wins = 0
    losses = 0
    if performance:
        for token_perf in performance.values():
            if isinstance(token_perf, dict):
                total_trades += token_perf.get("total_trades", 0)
                wins += token_perf.get("wins", 0)
                losses += token_perf.get("losses", 0)

    # Data freshness — most recent token profile modification
    latest_profile_age = "no profiles"
    if TOKEN_PROFILES_DIR.exists():
        profiles = list(TOKEN_PROFILES_DIR.glob("*.json"))
        if profiles:
            newest = max(profiles, key=lambda p: p.stat().st_mtime)
            age_secs = time.time() - newest.stat().st_mtime
            if age_secs < 60:
                latest_profile_age = f"{age_secs:.0f}s ago"
            elif age_secs < 3600:
                latest_profile_age = f"{age_secs/60:.0f}m ago"
            else:
                latest_profile_age = f"{age_secs/3600:.1f}h ago"

    status["data"] = {
        "tokens_in_db": db_count,
        "token_profiles": profile_count,
        "signal_tokens": signal_count,
        "predictions": prediction_count,
        "pending_watch_dir": watch_count,
        "latest_profile_update": latest_profile_age,
        "bot_trades": total_trades,
        "bot_wins": wins,
        "bot_losses": losses,
        "win_rate": f"{(wins / max(wins + losses, 1)) * 100:.0f}%" if (wins + losses) > 0 else "N/A",
    }

    running_count = sum(1 for c in status["components"].values() if c["running"])
    total_count = len(MANAGED_COMPONENTS)

    if running_count == 0:
        status["summary"] = (
            f"Trading bot is OFFLINE — 0/{total_count} components running. "
            f"Use trading_bot_start('all') to start monitoring. "
            f"Data dirs: {profile_count} profiles, {signal_count} signals, {prediction_count} predictions."
        )
    elif running_count < total_count:
        status["summary"] = (
            f"Trading bot PARTIAL — {running_count}/{total_count} running. "
            f"DB: {db_count} tokens, {profile_count} profiles, latest update: {latest_profile_age}."
        )
    else:
        status["summary"] = (
            f"Trading bot FULLY ONLINE — all {total_count} components running. "
            f"DB: {db_count} tokens, {profile_count} profiles, {signal_count} signals, "
            f"{prediction_count} predictions. Latest update: {latest_profile_age}. "
            f"Bot performance: {wins}W/{losses}L ({status['data']['win_rate']})."
        )

    return json.dumps(status, indent=2)


# ─── Signal & Prediction Reader ──────────────────────────────────────────────

def trading_signals(signal_type: str = "all", limit: int = 20) -> str:
    """Read recent trade signals and AI predictions from the trading bot pipeline.
    These are the tokens the bot has flagged as potential buys based on pattern
    detection (Higher Low, TP1/TP2/TP3, Momentum) and AI trend analysis.

    Use this to find promising tokens for sim trades. The bot's signals are
    generated from real-time DexScreener data and validated by AI trend analysis.

    Revenue target: $1,000/day. Use these signals wisely.

    Parameters:
        signal_type: Filter by type — 'all', 'predictions' (AI-confirmed buys), 'signals' (pattern detections), or a specific pattern like 'higher_low', 'momentum'.
        limit: Max number of signals to return (default 20, max 100).
    """
    limit = min(max(1, int(limit or 20)), 100)
    signal_type = (signal_type or "all").strip().lower()
    results = []

    # Read predictions (AI-confirmed buy signals)
    if signal_type in ("all", "predictions"):
        if PREDICTIONS_DIR.exists():
            pred_files = sorted(
                PREDICTIONS_DIR.glob("*.json"),
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )[:limit]
            for f in pred_files:
                try:
                    data = json.loads(f.read_text())
                    data["_source"] = "prediction"
                    data["_file"] = f.name
                    data["_age_seconds"] = int(time.time() - f.stat().st_mtime)
                    results.append(data)
                except Exception:
                    pass

    # Read signal tokens (pattern detections)
    if signal_type in ("all", "signals") or signal_type not in ("all", "predictions", "signals"):
        if SIGNAL_TOKENS_DIR.exists():
            sig_files = sorted(
                SIGNAL_TOKENS_DIR.glob("*.json"),
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )[:limit]
            for f in sig_files:
                try:
                    data = json.loads(f.read_text())
                    data["_source"] = "signal"
                    data["_file"] = f.name
                    data["_age_seconds"] = int(time.time() - f.stat().st_mtime)
                    # Filter by specific signal type if requested
                    if signal_type not in ("all", "predictions", "signals"):
                        sig_name = data.get("signal_type", "").lower()
                        if signal_type not in sig_name:
                            continue
                    results.append(data)
                except Exception:
                    pass

    # Sort by recency
    results.sort(key=lambda r: r.get("_age_seconds", 999999))
    results = results[:limit]

    # Strip verbose internal fields to save tokens
    _KEEP_FIELDS = {
        "address", "signal_type", "current_price", "market_cap",
        "detection_timestamp", "price_change_5s", "price_change_15s",
        "price_change_30s", "price_change_1m", "price_change_5m",
        "pool_buys_5m", "pool_sells_5m", "buy_volume_5m", "sell_volume_5m",
        "top_20_holders_percentage", "_source", "_age_seconds",
        "prediction", "confidence", "symbol", "name",
    }
    compact = []
    for sig in results:
        compact.append({k: v for k, v in sig.items() if k in _KEEP_FIELDS})

    return json.dumps({
        "signals": compact,
        "count": len(compact),
        "signal_type_filter": signal_type,
    }, indent=2)


# ─── Hot Tokens ───────────────────────────────────────────────────────────────

def trading_hot_tokens(limit: int = 15) -> str:
    """Get the trading bot's hottest tokens — uptrending, high volume, recently
    active. These are the bot's top picks based on real-time DexScreener data.

    Parameters:
        limit: Max tokens to return (default 15, max 50).
    """
    limit = min(max(1, int(limit or 15)), 50)

    tokens = []

    # Try SQLite first (most complete data)
    if TOKENS_DB.exists():
        try:
            conn = sqlite3.connect(str(TOKENS_DB), timeout=5)
            conn.row_factory = sqlite3.Row
            cur = conn.execute("""
                SELECT * FROM tokens
                WHERE is_bundled = 0
                  AND current_market_cap > 20000
                ORDER BY
                    CASE WHEN is_uptrend = 1 THEN 0 ELSE 1 END,
                    price_change_5m DESC,
                    volume_5m DESC
                LIMIT ?
            """, (limit,))
            rows = cur.fetchall()
            for row in rows:
                keys = row.keys()
                tokens.append({
                    "symbol": row["base_token_symbol"] if "base_token_symbol" in keys else "?",
                    "name": row["base_token_name"] if "base_token_name" in keys else (row["token_name"] if "token_name" in keys else "?"),
                    "address": row["address"] if "address" in keys else "?",
                    "price_usd": row["current_price"] if "current_price" in keys else 0,
                    "market_cap": row["current_market_cap"] if "current_market_cap" in keys else 0,
                    "price_change_5m": row["price_change_5m"] if "price_change_5m" in keys else 0,
                    "volume_5m": row["volume_5m"] if "volume_5m" in keys else 0,
                    "trend": "uptrend" if (row["is_uptrend"] if "is_uptrend" in keys else 0) else ("downtrend" if (row["is_downtrend"] if "is_downtrend" in keys else 0) else "flat"),
                    "is_new": bool(row["is_new"]) if "is_new" in keys else False,
                    "buys_5m": row["buys_5m"] if "buys_5m" in keys else 0,
                    "sells_5m": row["sells_5m"] if "sells_5m" in keys else 0,
                    "top_20_holders_pct": row["top_20_holders_percentage"] if "top_20_holders_percentage" in keys else 0,
                    "volume_1h": row["volume_1h"] if "volume_1h" in keys else 0,
                    "pair_address": row["pair_address"] if "pair_address" in keys else "",
                    "dex_id": row["dex_id"] if "dex_id" in keys else "",
                })
            conn.close()
        except Exception as e:
            logger.warning(f"[TRADING_BOT] SQLite query failed: {e}")

    # Fallback / supplement: read token profiles
    if not tokens and TOKEN_PROFILES_DIR.exists():
        profiles = sorted(
            TOKEN_PROFILES_DIR.glob("*.json"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )[:limit * 2]  # Read more, then sort

        for pf in profiles:
            try:
                data = json.loads(pf.read_text())
                price_change = data.get("price_change_5m", 0) or 0
                mcap = data.get("current_market_cap", data.get("market_cap", 0)) or 0
                if mcap < 20000:
                    continue
                tokens.append({
                    "symbol": data.get("base_token_symbol", data.get("symbol", "?")),
                    "name": data.get("base_token_name", data.get("name", "?")),
                    "address": data.get("address", pf.stem),
                    "price_usd": data.get("current_price", data.get("price_usd", 0)),
                    "market_cap": mcap,
                    "price_change_5m": price_change,
                    "volume_5m": data.get("volume_5m", 0),
                    "trend": "uptrend" if data.get("is_uptrend") else ("downtrend" if data.get("is_downtrend") else "flat"),
                    "is_new": data.get("is_new", False),
                    "buys_5m": data.get("buys_5m", 0),
                    "sells_5m": data.get("sells_5m", 0),
                    "top_20_holders_pct": data.get("top_20_holders_percentage", 0),
                    "pair_address": data.get("pair_address", ""),
                    "source": "profile",
                })
            except Exception:
                pass

        # Sort by 5m change descending
        tokens.sort(key=lambda t: t.get("price_change_5m", 0), reverse=True)
        tokens = tokens[:limit]

    if not tokens:
        # Check if bot is running
        any_running = any(_is_script_running(c["script"]) for c in MANAGED_COMPONENTS.values())
        if not any_running:
            return json.dumps({
                "hot_tokens": [],
                "count": 0,
                "note": "Trading bot is OFFLINE — no live data available. "
                        "Use trading_bot_start('all') to start monitoring.",
            })
        return json.dumps({
            "hot_tokens": [],
            "count": 0,
            "note": "Bot is running but no tokens match criteria yet. "
                    "It may still be scanning — check again in a few minutes.",
        })

    return json.dumps({
        "hot_tokens": tokens,
        "count": len(tokens),
        "note": "Sorted by uptrend status + 5-minute price change. "
                "Use trading_token_detail(address) for full price history on any token.",
    }, indent=2)


# ─── Token Browser ────────────────────────────────────────────────────────────

def trading_browse_tokens(sort_by: str = "last_updated", order: str = "desc",
                          limit: int = 30, min_mcap: float = 0,
                          search: str = "") -> str:
    """Browse ALL tokens currently tracked in the trading database.
    Unlike trading_hot_tokens which filters aggressively, this shows every token
    the bot is monitoring — including ones that got flagged as bundled, downtrending,
    or low-cap. Use this for a complete overview.

    Parameters:
        sort_by: Column to sort by — 'last_updated', 'current_market_cap', 'current_price', 'volume_5m', 'price_change_5m', 'top_20_holders_percentage', 'first_seen'. Default: 'last_updated'.
        order: 'asc' or 'desc' (default: 'desc').
        limit: Max tokens to return (default 30, max 100).
        search: Optional text to filter by token name or symbol (case-insensitive).
        min_mcap: Minimum market cap filter (default 0 = no filter).
    """
    limit = min(max(1, int(limit or 30)), 100)
    order = "DESC" if (order or "desc").strip().upper() == "DESC" else "ASC"

    # Whitelist sortable columns to prevent injection
    _sortable = {
        "last_updated", "current_market_cap", "current_price", "volume_5m",
        "price_change_5m", "top_20_holders_percentage", "first_seen",
        "buys_5m", "sells_5m", "volume_1h", "ath_price",
    }
    if sort_by not in _sortable:
        sort_by = "last_updated"

    if not TOKENS_DB.exists():
        return json.dumps({"tokens": [], "count": 0, "note": "No tokens.db found. Is the trading bot running?"})

    try:
        conn = sqlite3.connect(str(TOKENS_DB), timeout=5)
        conn.row_factory = sqlite3.Row

        where_clauses = []
        params = []
        if min_mcap > 0:
            where_clauses.append("current_market_cap >= ?")
            params.append(min_mcap)
        if search:
            where_clauses.append("(base_token_symbol LIKE ? OR base_token_name LIKE ? OR token_name LIKE ?)")
            params.extend([f"%{search}%"] * 3)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        query = f"SELECT * FROM tokens {where_sql} ORDER BY {sort_by} {order} LIMIT ?"
        params.append(limit)

        cur = conn.execute(query, params)
        rows = cur.fetchall()
        total = conn.execute(f"SELECT COUNT(*) FROM tokens {where_sql}", params[:-1]).fetchone()[0]

        tokens = []
        for row in rows:
            keys = row.keys()
            tokens.append({
                "symbol": row["base_token_symbol"] if "base_token_symbol" in keys else "?",
                "name": row["base_token_name"] if "base_token_name" in keys else (row["token_name"] if "token_name" in keys else "?"),
                "address": row["address"],
                "price_usd": row["current_price"] if "current_price" in keys else 0,
                "market_cap": row["current_market_cap"] if "current_market_cap" in keys else 0,
                "ath_price": row["ath_price"] if "ath_price" in keys else 0,
                "price_change_5m": row["price_change_5m"] if "price_change_5m" in keys else 0,
                "volume_5m": row["volume_5m"] if "volume_5m" in keys else 0,
                "volume_1h": row["volume_1h"] if "volume_1h" in keys else 0,
                "buys_5m": row["buys_5m"] if "buys_5m" in keys else 0,
                "sells_5m": row["sells_5m"] if "sells_5m" in keys else 0,
                "top_20_holders_pct": row["top_20_holders_percentage"] if "top_20_holders_percentage" in keys else 0,
                "is_bundled": bool(row["is_bundled"]) if "is_bundled" in keys else False,
                "bundling_reason": row["bundling_reason"] if "bundling_reason" in keys else "",
                "trend": "uptrend" if (row["is_uptrend"] if "is_uptrend" in keys else 0) else ("downtrend" if (row["is_downtrend"] if "is_downtrend" in keys else 0) else "flat"),
                "is_new": bool(row["is_new"]) if "is_new" in keys else False,
                "pair_address": row["pair_address"] if "pair_address" in keys else "",
                "dex_id": row["dex_id"] if "dex_id" in keys else "",
                "last_updated": row["last_updated"] if "last_updated" in keys else "",
                "first_seen": row["first_seen"] if "first_seen" in keys else "",
            })
        conn.close()

        return json.dumps({
            "tokens": tokens,
            "showing": len(tokens),
            "total_in_db": total,
            "sorted_by": sort_by,
            "order": order.lower(),
            "note": "Use trading_token_detail(address) for full profile on any token.",
        }, indent=2)
    except Exception as e:
        return json.dumps({"tokens": [], "count": 0, "error": str(e)})


# ─── Performance Reader ──────────────────────────────────────────────────────

def _load_performance() -> Dict:
    """Load the bot's performance file."""
    if PERFORMANCE_FILE.exists():
        try:
            return json.loads(PERFORMANCE_FILE.read_text())
        except Exception:
            pass
    return {}


def trading_performance() -> str:
    """Get the trading bot's real win/loss performance record.
    Shows per-token results, overall stats, and recent trade history.
    Use this to calibrate your sim trading strategy against ground truth.

    Parameters:
        (none required)
    """
    performance = _load_performance()

    if not performance:
        return json.dumps({
            "performance": {},
            "summary": "No performance data yet. The bot hasn't completed any trades, "
                       "or the performance file doesn't exist.",
            "file": str(PERFORMANCE_FILE),
        })

    # Aggregate stats
    total_wins = 0
    total_losses = 0
    total_pnl_sol = 0.0
    tokens_traded = 0
    per_token = []

    for token_addr, data in performance.items():
        if not isinstance(data, dict):
            continue
        tokens_traded += 1
        wins = data.get("wins", 0)
        losses = data.get("losses", 0)
        total_wins += wins
        total_losses += losses

        # Calculate P&L if history is present
        pnl = 0
        history = data.get("history", [])
        for h in history:
            if isinstance(h, dict):
                pnl += h.get("pnl_sol", 0)
        total_pnl_sol += pnl

        per_token.append({
            "address": token_addr,
            "symbol": data.get("symbol", "?"),
            "wins": wins,
            "losses": losses,
            "pnl_sol": round(pnl, 4),
        })

    # Sort by P&L
    per_token.sort(key=lambda t: t["pnl_sol"], reverse=True)

    return json.dumps({
        "summary": {
            "total_trades": total_wins + total_losses,
            "wins": total_wins,
            "losses": total_losses,
            "win_rate": f"{(total_wins / max(total_wins + total_losses, 1)) * 100:.1f}%",
            "total_pnl_sol": round(total_pnl_sol, 4),
            "tokens_traded": tokens_traded,
        },
        "per_token": per_token[:10],  # Top 10 tokens by P&L
    }, indent=2)


# ─── Token Detail ─────────────────────────────────────────────────────────────

def trading_token_detail(address: str = "") -> str:
    """Get full details for a specific token from the trading bot's data:
    price history (for charts), ATH, holder concentration, trend state,
    volume breakdown, and any active signals.

    Parameters:
        address: The Solana token address (mint) to look up. Required.
    """
    if not address:
        return json.dumps({"error": "address parameter required — provide the Solana token mint address"})

    address = address.strip()
    result = {"address": address}

    # 1. Try token profile (extract key fields only — skip raw dump to save tokens)
    profile_path = TOKEN_PROFILES_DIR / f"{address}.json"
    if profile_path.exists():
        try:
            profile = json.loads(profile_path.read_text())

            # Extract key fields for quick view
            result["quick"] = {
                "symbol": profile.get("base_token_symbol", "") or profile.get("symbol", "?"),
                "name": profile.get("token_name", "") or profile.get("base_token_name", "") or profile.get("name", "?"),
                "price_usd": profile.get("current_price", 0) or profile.get("price_usd", 0),
                "market_cap": profile.get("current_market_cap", 0) or profile.get("market_cap", 0),
                "ath_price": profile.get("ath_price") or profile.get("ath_data", {}).get("price", 0),
                "dex_id": profile.get("dex_id", ""),
                "pair_address": profile.get("pair_address", ""),
                "holder_top20_pct": profile.get("top_20_holders_percentage", 0),
                "is_bundled": profile.get("is_bundled", False),
                "bundling_reason": profile.get("bundling_reason", ""),
                "is_uptrend": profile.get("is_uptrend", 0),
                "is_downtrend": profile.get("is_downtrend", 0),
                "price_change_5m": profile.get("price_change_5m", 0),
                "volume_5m": profile.get("volume_5m", 0),
                "volume_1h": profile.get("volume_1h", 0),
                "volume_24h": profile.get("volume_24h", 0),
                "buys_5m": profile.get("buys_5m", 0),
                "sells_5m": profile.get("sells_5m", 0),
                "buys_1h": profile.get("buys_1h", 0),
                "sells_1h": profile.get("sells_1h", 0),
            }

            # Extract social links from DexScreener profile info
            raw_social = profile.get("raw_social_info", "{}")
            if isinstance(raw_social, str):
                try:
                    social_info = json.loads(raw_social)
                except Exception:
                    social_info = {}
            else:
                social_info = raw_social if isinstance(raw_social, dict) else {}
            social_links = []
            for s in social_info.get("socials", []):
                social_links.append({"type": s.get("type", ""), "url": s.get("url", "")})
            for w in social_info.get("websites", []):
                social_links.append({"type": "website", "url": w.get("url", ""), "label": w.get("label", "")})
            if social_links:
                result["social_links"] = social_links
                result["has_social_presence"] = True
            else:
                result["social_links"] = []
                result["has_social_presence"] = False

            # Top 20 holders detail (parsed from JSON string)
            raw_holders = profile.get("top_20_holders", "[]")
            if isinstance(raw_holders, str):
                try:
                    holders_list = json.loads(raw_holders)
                except Exception:
                    holders_list = []
            else:
                holders_list = raw_holders if isinstance(raw_holders, list) else []
            if holders_list:
                # Label any holders that are likely LP vaults
                # The pair_address owns the LP vault token accounts — if we can
                # check cheaply, do so.  Otherwise trust the upstream exclusion.
                pair = profile.get("pair_address", "")
                labeled = []
                for h in holders_list[:10]:
                    entry = dict(h)
                    # Token accounts whose address matches pair_address are pool PDAs (rare),
                    # but we also flag if the token_monitor already excluded LP upstream.
                    if pair and h.get("address") == pair:
                        entry["label"] = "LP_POOL"
                    labeled.append(entry)
                result["top_holders"] = labeled
                result["holder_note"] = (
                    "LP pool vaults are excluded from holder_top20_pct when pair_address "
                    "is available. If the top holder looks disproportionately large, it may "
                    "be an LP vault that wasn't excluded yet — check pair_address."
                )

            # Price history summary (don't dump entire array, just stats)
            ph = profile.get("price_history", [])
            if ph:
                prices = [p[1] if isinstance(p, list) and len(p) > 1 else p.get("price", 0)
                          for p in ph if isinstance(p, (list, dict))]
                if prices:
                    result["price_history_stats"] = {
                        "data_points": len(prices),
                        "current": prices[-1] if prices else 0,
                        "high": max(prices),
                        "low": min(prices),
                        "oldest_price": prices[0] if prices else 0,
                        "timespan_seconds": (
                            (ph[-1][0] if isinstance(ph[-1], list) else ph[-1].get("timestamp", 0)) -
                            (ph[0][0] if isinstance(ph[0], list) else ph[0].get("timestamp", 0))
                        ) if len(ph) > 1 else 0,
                    }
                    # Include last 30 data points for charting
                    result["recent_price_points"] = ph[-30:]
        except Exception as e:
            result["profile_error"] = str(e)

    # 2. Try SQLite for key fields (skip raw row dump to save tokens)
    if TOKENS_DB.exists():
        try:
            conn = sqlite3.connect(str(TOKENS_DB), timeout=5)
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM tokens WHERE address = ?", (address,))
            row = cur.fetchone()
            if row:
                r = dict(row)
                result["db"] = {
                    "symbol": r.get("base_token_symbol", ""),
                    "price": r.get("current_price", 0),
                    "market_cap": r.get("current_market_cap", 0),
                    "ath_price": r.get("ath_price", 0),
                    "is_uptrend": r.get("is_uptrend", 0),
                    "is_bundled": r.get("is_bundled", 0),
                    "buys_5m": r.get("buys_5m", 0),
                    "sells_5m": r.get("sells_5m", 0),
                    "volume_5m": r.get("volume_5m", 0),
                    "top_20_holders_percentage": r.get("top_20_holders_percentage", 0),
                }
            conn.close()
        except Exception:
            pass

    # 3. Check for active signals for this token (compact — only key fields)
    active_signals = []
    _SIG_KEEP = {"signal_type", "current_price", "market_cap", "detection_timestamp",
                 "price_change_1m", "price_change_5m", "prediction", "confidence"}
    if SIGNAL_TOKENS_DIR.exists():
        sig_files = sorted(SIGNAL_TOKENS_DIR.glob(f"*{address}*"),
                           key=lambda f: f.stat().st_mtime, reverse=True)[:5]
        for sf in sig_files:
            try:
                sig = json.loads(sf.read_text())
                active_signals.append({k: v for k, v in sig.items() if k in _SIG_KEEP})
            except Exception:
                pass
    if PREDICTIONS_DIR.exists():
        pred_files = sorted(PREDICTIONS_DIR.glob(f"*{address}*"),
                            key=lambda f: f.stat().st_mtime, reverse=True)[:3]
        for pf_file in pred_files:
            try:
                pred = json.loads(pf_file.read_text())
                compact = {k: v for k, v in pred.items() if k in _SIG_KEEP}
                compact["_source"] = "prediction"
                active_signals.append(compact)
            except Exception:
                pass

    result["active_signals"] = active_signals

    if "profile" not in result and "db" not in result:
        return json.dumps({
            "error": f"Token {address} not found in bot's data. "
                     "It may not be tracked yet, or the bot may be offline.",
            "tip": "Use trading_hot_tokens() to see what's being tracked, "
                   "or sim_price_check(address) to look it up on DexScreener directly.",
        })

    return json.dumps(result, indent=2, default=str)


# ─── Bot's Cached Price Lookup ────────────────────────────────────────────────

def get_cached_price(address: str) -> Optional[float]:
    """Get a token's cached price from the bot's data (tokens.db or profiles).
    Returns None if not available — caller should fall back to DexScreener.

    This is for internal use by the sim trading tools to reduce API calls.
    """
    # Try profile first (updated every ~5s when bot is running)
    profile_path = TOKEN_PROFILES_DIR / f"{address}.json"
    if profile_path.exists():
        try:
            # Only use if fresh (< 30 seconds old)
            if time.time() - profile_path.stat().st_mtime < 30:
                data = json.loads(profile_path.read_text())
                price = data.get("price_usd", 0)
                if price and price > 0:
                    return float(price)
        except Exception:
            pass

    # Try SQLite
    if TOKENS_DB.exists():
        try:
            conn = sqlite3.connect(str(TOKENS_DB), timeout=3)
            cur = conn.execute(
                "SELECT price_usd, last_updated FROM tokens WHERE address = ?",
                (address,),
            )
            row = cur.fetchone()
            conn.close()
            if row and row[0] and row[0] > 0:
                return float(row[0])
        except Exception:
            pass

    return None


# ─── Token Price History (for Andrew analysis) ──────────────────────────────

def token_price_history(address: str = "", points: int = 100) -> str:
    """Get a token's full price history for chart analysis and pattern recognition.

    Returns timestamped price points, computed indicators (high/low/avg over windows),
    and trend direction. Use this to analyze price action before making trading decisions.

    Parameters:
        address: Solana token mint address. Required.
        points: Max data points to return (default 100, max 300).
    """
    if not address:
        return json.dumps({"error": "address parameter required"})

    address = address.strip()
    points = max(10, min(300, int(points or 100)))

    profile_path = TOKEN_PROFILES_DIR / f"{address}.json"
    if not profile_path.exists():
        return json.dumps({
            "error": f"No profile for {address}. Token may not be tracked by the trading bot.",
            "tip": "Use trading_hot_tokens() to see tracked tokens."
        })

    try:
        data = json.loads(profile_path.read_text())
    except Exception as e:
        return json.dumps({"error": f"Failed to read profile: {e}"})

    ph = data.get("price_history", [])
    if not ph:
        return json.dumps({"error": "No price history available for this token"})

    # Normalize to (timestamp, price) tuples
    normalized = []
    for p in ph:
        if isinstance(p, list) and len(p) >= 2:
            normalized.append((float(p[0]), float(p[1])))
        elif isinstance(p, dict):
            normalized.append((float(p.get("timestamp", 0)), float(p.get("price", 0))))

    # Take the most recent N points
    normalized = normalized[-points:]
    if not normalized:
        return json.dumps({"error": "Price history is empty after filtering"})

    prices = [p[1] for p in normalized]
    timestamps = [p[0] for p in normalized]

    # Compute windowed indicators
    def window_stats(price_list, window):
        if len(price_list) < window:
            return None
        segment = price_list[-window:]
        return {
            "high": round(max(segment), 10),
            "low": round(min(segment), 10),
            "avg": round(sum(segment) / len(segment), 10),
            "change_pct": round(((segment[-1] - segment[0]) / segment[0]) * 100, 2) if segment[0] else 0,
        }

    # Detect simple trend: compare last 20% vs first 20%
    split = max(1, len(prices) // 5)
    recent_avg = sum(prices[-split:]) / split
    early_avg = sum(prices[:split]) / split
    if early_avg > 0:
        trend_change = ((recent_avg - early_avg) / early_avg) * 100
    else:
        trend_change = 0
    if trend_change > 5:
        trend = "uptrend"
    elif trend_change < -5:
        trend = "downtrend"
    else:
        trend = "sideways"

    result = {
        "address": address,
        "symbol": data.get("symbol", data.get("base_token_symbol", "?")),
        "name": data.get("name", data.get("token_name", "?")),
        "data_points": len(normalized),
        "timespan_seconds": round(timestamps[-1] - timestamps[0]) if len(timestamps) > 1 else 0,

        "current_price": prices[-1],
        "ath_price": data.get("ath_price", max(prices)),
        "pct_from_ath": round(((prices[-1] - max(prices)) / max(prices)) * 100, 2) if max(prices) else 0,

        "trend": trend,
        "trend_change_pct": round(trend_change, 2),

        "indicators": {
            "last_10": window_stats(prices, 10),
            "last_30": window_stats(prices, 30),
            "last_60": window_stats(prices, 60),
            "last_100": window_stats(prices, 100),
        },

        # Last 50 price points for the LLM to visually inspect pattern
        "recent_prices": [
            {"t": round(t), "p": round(p, 10)}
            for t, p in normalized[-50:]
        ],

        "NEXT_STEP": (
            "Analyze the trend, indicators, and recent_prices to decide: "
            "BUY (uptrend + momentum), HOLD (sideways/uncertain), or SELL (downtrend/exhaustion). "
            "Cross-reference with web_search(token_address) for social sentiment and narrative."
        ),
    }

    return json.dumps(result, indent=2)


# ─── Trade Outcome Journal (learning system) ─────────────────────────────────

TRADE_JOURNAL_FILE = DATA_DIR / "trade_journal.json"


def _load_trade_journal() -> list:
    if TRADE_JOURNAL_FILE.exists():
        try:
            return json.loads(TRADE_JOURNAL_FILE.read_text())
        except Exception:
            return []
    return []


def _save_trade_journal(journal: list):
    TRADE_JOURNAL_FILE.write_text(json.dumps(journal, indent=2, default=str))


def log_trade_outcome(address: str = "", symbol: str = "", action: str = "",
                      entry_price: float = 0, exit_price: float = 0,
                      hold_seconds: int = 0, pnl_pct: float = 0,
                      reason: str = "", market_conditions: str = "",
                      lessons: str = "", **kwargs) -> str:
    """Log a completed trade outcome for Andrew's learning system.

    Call this AFTER every sim_sell or position close to build a training journal.
    Over time, this creates a dataset of what works and what doesn't.

    Parameters:
        address: Token mint address.
        symbol: Token symbol (e.g. 'PATTY').
        action: 'buy_and_profit', 'buy_and_loss', 'stopped_out', 'timed_out', 'manual_sell'.
        entry_price: Price when bought.
        exit_price: Price when sold.
        hold_seconds: How long the position was held.
        pnl_pct: Profit/loss percentage.
        reason: Why you entered (signal type, trending, whale copy, etc.).
        market_conditions: Brief note on market state at entry (e.g. 'high volume, multiple signals').
        lessons: What you learned from this trade (fill in honestly).
    """
    journal = _load_trade_journal()
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "address": address,
        "symbol": symbol,
        "action": action,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "hold_seconds": hold_seconds,
        "pnl_pct": round(pnl_pct, 2),
        "reason": reason,
        "market_conditions": market_conditions,
        "lessons": lessons,
    }
    journal.append(entry)

    # Keep last 500 entries
    if len(journal) > 500:
        journal = journal[-500:]

    _save_trade_journal(journal)

    # Also feed the recursive learning engine
    try:
        from repryntt.learning.engine import LearningEngine
        from repryntt.learning.trading import TradingLearner
        _data_dir = Path(__file__).resolve().parent.parent / "learning" / "data"
        _eng = LearningEngine(data_dir=_data_dir)
        _tl = TradingLearner(_eng)
        # Log entry event + immediate outcome
        signal_type = reason if reason else "manual"
        eid = _tl.on_trade_entry(
            address=address, symbol=symbol,
            amount_usd=entry_price,  # approximate
            entry_price=entry_price,
            reason=reason,
            signal_types=[signal_type],
        )
        _tl.on_trade_exit(
            event_id=eid,
            pnl_pct=pnl_pct,
            exit_price=exit_price,
            hold_seconds=hold_seconds,
            reason=action,
        )
    except Exception:
        pass  # Learning engine hook is best-effort

    return json.dumps({"success": True, "total_journal_entries": len(journal), "entry": entry})


def review_trade_journal(last_n: int = 30, winners_only: bool = False,
                         losers_only: bool = False, **kwargs) -> str:
    """Review past trade outcomes to identify patterns and improve strategy.

    This is your trading education tool. Study what worked and what didn't.

    Parameters:
        last_n: Number of recent trades to review (default 30, max 100).
        winners_only: If True, show only profitable trades.
        losers_only: If True, show only losing trades.
    """
    journal = _load_trade_journal()
    if not journal:
        return json.dumps({
            "message": "No trade journal entries yet. Start logging trades with log_trade_outcome() after each sim_sell.",
            "total_entries": 0,
        })

    last_n = max(1, min(100, int(last_n or 30)))

    entries = journal[-last_n * 3:]  # Get more to filter from
    if winners_only:
        entries = [e for e in entries if e.get("pnl_pct", 0) > 0]
    elif losers_only:
        entries = [e for e in entries if e.get("pnl_pct", 0) <= 0]

    entries = entries[-last_n:]

    # Compute aggregate stats
    pnls = [e.get("pnl_pct", 0) for e in entries]
    holds = [e.get("hold_seconds", 0) for e in entries]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p <= 0]

    # Group by reason to see which entry signals produce best results
    reason_stats = {}
    for e in entries:
        r = e.get("reason", "unknown") or "unknown"
        if r not in reason_stats:
            reason_stats[r] = {"count": 0, "wins": 0, "total_pnl": 0}
        reason_stats[r]["count"] += 1
        reason_stats[r]["total_pnl"] += e.get("pnl_pct", 0)
        if e.get("pnl_pct", 0) > 0:
            reason_stats[r]["wins"] += 1

    for r in reason_stats:
        s = reason_stats[r]
        s["win_rate"] = f"{(s['wins'] / max(s['count'], 1)) * 100:.0f}%"
        s["avg_pnl"] = round(s["total_pnl"] / max(s["count"], 1), 2)

    result = {
        "total_journal_entries": len(journal),
        "reviewing": len(entries),

        "aggregate": {
            "avg_pnl_pct": round(sum(pnls) / max(len(pnls), 1), 2),
            "best_trade": round(max(pnls), 2) if pnls else 0,
            "worst_trade": round(min(pnls), 2) if pnls else 0,
            "win_count": len(winners),
            "loss_count": len(losers),
            "win_rate": f"{(len(winners) / max(len(pnls), 1)) * 100:.0f}%",
            "avg_hold_seconds": round(sum(holds) / max(len(holds), 1)),
        },

        "by_signal_type": reason_stats,

        "recent_trades": entries[-15:],

        "NEXT_STEP": (
            "Study the 'by_signal_type' stats — which entry signals have the best win rate? "
            "Increase position sizes on high-win-rate signals. "
            "Stop trading signal types with <30% win rate. "
            "Log 'lessons' field on each trade to build intuition over time."
        ),
    }

    return json.dumps(result, indent=2)
