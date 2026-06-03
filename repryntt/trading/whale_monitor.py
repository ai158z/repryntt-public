"""
SAIGE Whale & KOL Wallet Monitor — Copy-Trade Signal Source
============================================================

Monitors a curated list of whale/KOL wallets on Solana for new token
swaps. When a tracked wallet buys a token, generates a high-priority
signal that feeds directly into the trading engine for auto-execution.

When a tracked wallet SELLS a token we already hold, generates an
urgent sell signal.

Architecture:
  whale_monitor.poll_wallets()
    → for each wallet: getSignaturesForAddress (recent txs)
    → for new txs: getTransaction (parsed)
    → parse swap instructions (Jupiter, Raydium)
    → on BUY: create WHALE_COPY signal → trading_engine.ingest_signals()
    → on SELL: if we hold it → trigger auto-sell via sim_sell()

Rate limiting:
  - Alchemy: 330 req/s — 0.12s delay between calls
  - Each poll: ~2 RPC calls per wallet (signatures + transaction)
  - 20 wallets × 2 calls × 4 polls/min = 160 calls/min = 9.6K/hr = 230K/day
  - Alchemy free tier: 300M CU/month → ~69M CU/month at this rate ✓

Storage:
  brain/tracked_wallets.json — wallet list with metadata + performance
  agent_workspaces/jarvis/whale_monitor_state.json — cursor/signature state
"""

import json
import os
import time
import logging
import threading
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Set

logger = logging.getLogger("saige.whale_monitor")

BASE_DIR = Path(__file__).resolve().parent.parent
JARVIS_WORKSPACE = str(BASE_DIR / "agent_workspaces" / "jarvis")
WALLETS_FILE = BASE_DIR / "brain" / "tracked_wallets.json"
STATE_FILE = BASE_DIR / "agent_workspaces" / "jarvis" / "whale_monitor_state.json"

# ─── Configuration ───────────────────────────────────────────────────────────

# RPC endpoint — use Alchemy (same key as solana_rpc_query tool) for reliable access
# Public Solana RPC rate-limits aggressively and causes constant 429 errors
SOLANA_RPC_URL = os.environ.get(
    "SOLANA_RPC_URL",
    "https://solana-mainnet.g.alchemy.com/v2/tRxgtGxhjC6y_yaW1W8phMk0yQTBUg73"
)

# Polling — monitor KOL/whale activity (KOLs hold tokens for hours/days, not seconds)
POLL_INTERVAL_S = 60          # Poll each wallet every 60s (activity monitoring, not copy-trading)
RPC_DELAY_S = 0.35            # Delay between RPC calls (rate-limited to avoid 429s)
MAX_SIGNATURES_PER_POLL = 5   # Recent txs to check per wallet per poll
TX_MAX_AGE_S = 900            # Include txs up to 15 min old (KOLs hold for days)

# Retry / backoff
RPC_MAX_RETRIES = 3           # Max retries per RPC call
RPC_BACKOFF_BASE = 1.0        # Base backoff in seconds (doubles each retry)
RPC_429_BACKOFF = 5.0         # Extra backoff on 429 rate-limit responses

# Signal generation — scores are BELOW auto-exec threshold; Andrew decides what to trade
WHALE_SIGNAL_SCORE = 6.5      # Score for whale signals (queued for Andrew review)
KOL_SIGNAL_SCORE = 7.5        # KOL wallets get slightly higher score (smart money premium)
MIN_SWAP_USD = 500            # Ignore swaps smaller than $500 (noise)
COPY_TRADE_ENABLED = False    # DISABLED — whale data is for intel only, not trade decisions

# Known DEX program IDs for swap detection
KNOWN_DEX_PROGRAMS = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4":  "Jupiter v6",
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcPX7r":  "Jupiter v4",
    "675kPX9MHTjS2zt1qfSwv7Fj4A3sPYMZLtW6jq1HRvT5":  "Raydium AMM v4",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK":  "Raydium CLMM",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc":   "Orca Whirlpool",
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP": "Orca v2",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P":  "Pump.fun",
    "PSwapMdSai8tjrEXcxFeQth87xC4rRsa4VA5mhGhXkP":   "Pump Swap",
}

# Wrapped SOL mint
WRAPPED_SOL = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# ─── State ───────────────────────────────────────────────────────────────────

_lock = threading.Lock()

# Last processed signature per wallet (cursor)
_wallet_cursors: Dict[str, str] = {}

# Stats
_stats = {
    "polls": 0,
    "swaps_detected": 0,
    "buy_signals_generated": 0,
    "sell_signals_generated": 0,
    "rpc_errors": 0,
}

# Recent signals ring buffer (last 50 for dashboard display)
_recent_signals: List[Dict[str, Any]] = []
_RECENT_SIGNALS_MAX = 50


# ─── Wallet List Management ─────────────────────────────────────────────────

def load_tracked_wallets() -> List[Dict[str, Any]]:
    """Load the tracked wallets list from disk."""
    if not WALLETS_FILE.exists():
        return []
    try:
        with open(WALLETS_FILE, "r") as f:
            data = json.load(f)
        return data.get("wallets", [])
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"[WHALE] Failed to load wallets file: {e}")
        return []


def save_tracked_wallets(wallets: List[Dict[str, Any]]):
    """Save the wallet list to disk."""
    data = {
        "wallets": wallets,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(WALLETS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except IOError as e:
        logger.error(f"[WHALE] Failed to save wallets: {e}")


def add_wallet(address: str, label: str = "", tier: str = "whale",
               notes: str = "") -> Dict[str, Any]:
    """Add a wallet to the tracking list.

    Args:
        address: Solana wallet address
        label: Human-readable name (e.g. "ansem", "whale_0x3f")
        tier: "whale" or "kol" — KOLs get higher signal score
        notes: Optional notes about the wallet
    """
    wallets = load_tracked_wallets()

    # Check for duplicates
    for w in wallets:
        if w["address"] == address:
            return {"error": f"Wallet {address[:8]}... already tracked as '{w.get('label', '')}'"}

    wallet = {
        "address": address,
        "label": label or f"wallet_{address[:8]}",
        "tier": tier,  # "whale" or "kol"
        "notes": notes,
        "added_at": datetime.now(timezone.utc).isoformat(),
        "enabled": True,
        "stats": {
            "trades_copied": 0,
            "profitable_copies": 0,
            "total_pnl_usd": 0.0,
        }
    }
    wallets.append(wallet)
    save_tracked_wallets(wallets)
    logger.info(f"[WHALE] Added {tier} wallet: {label} ({address[:16]}...)")
    return {"success": True, "wallet": wallet}


def remove_wallet(address: str) -> Dict[str, Any]:
    """Remove a wallet from tracking."""
    wallets = load_tracked_wallets()
    original_len = len(wallets)
    wallets = [w for w in wallets if w["address"] != address]
    if len(wallets) == original_len:
        return {"error": f"Wallet {address[:8]}... not found"}
    save_tracked_wallets(wallets)
    return {"success": True, "removed": address}


def list_wallets() -> List[Dict[str, Any]]:
    """Return all tracked wallets with stats."""
    return load_tracked_wallets()


# ─── Solana RPC Helpers ──────────────────────────────────────────────────────

def _rpc_call(method: str, params: list) -> Optional[Dict]:
    """Make a Solana JSON-RPC call with retry + exponential backoff."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }
    for attempt in range(RPC_MAX_RETRIES):
        try:
            resp = requests.post(SOLANA_RPC_URL, json=payload, timeout=10)

            # Handle 429 rate limit explicitly
            if resp.status_code == 429:
                backoff = RPC_429_BACKOFF * (2 ** attempt)
                logger.warning(
                    f"[WHALE] 429 rate-limited ({method}), "
                    f"backing off {backoff:.1f}s (attempt {attempt + 1}/{RPC_MAX_RETRIES})"
                )
                _stats["rpc_errors"] += 1
                time.sleep(backoff)
                continue

            # Handle other server errors (500, 502, 503) with retry
            if resp.status_code >= 500:
                backoff = RPC_BACKOFF_BASE * (2 ** attempt)
                logger.warning(
                    f"[WHALE] Server error {resp.status_code} ({method}), "
                    f"retrying in {backoff:.1f}s (attempt {attempt + 1}/{RPC_MAX_RETRIES})"
                )
                _stats["rpc_errors"] += 1
                time.sleep(backoff)
                continue

            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                err = data['error']
                err_code = err.get('code', 0) if isinstance(err, dict) else 0
                # Retry on RPC-level rate limits (code -32429 or -32005)
                if err_code in (-32429, -32005):
                    backoff = RPC_429_BACKOFF * (2 ** attempt)
                    logger.warning(
                        f"[WHALE] RPC rate-limit code {err_code} ({method}), "
                        f"backing off {backoff:.1f}s (attempt {attempt + 1}/{RPC_MAX_RETRIES})"
                    )
                    _stats["rpc_errors"] += 1
                    time.sleep(backoff)
                    continue
                logger.warning(f"[WHALE] RPC error ({method}): {err}")
                _stats["rpc_errors"] += 1
                return None
            return data.get("result")
        except requests.Timeout:
            backoff = RPC_BACKOFF_BASE * (2 ** attempt)
            logger.warning(
                f"[WHALE] RPC timeout ({method}), "
                f"retrying in {backoff:.1f}s (attempt {attempt + 1}/{RPC_MAX_RETRIES})"
            )
            _stats["rpc_errors"] += 1
            time.sleep(backoff)
            continue
        except requests.RequestException as e:
            logger.warning(f"[WHALE] RPC request failed ({method}): {e}")
            _stats["rpc_errors"] += 1
            return None

    logger.error(f"[WHALE] RPC call failed after {RPC_MAX_RETRIES} retries ({method})")
    return None


def _get_recent_signatures(wallet_address: str, limit: int = 5,
                           until_sig: str = None) -> List[Dict]:
    """Get recent transaction signatures for a wallet."""
    params = [wallet_address, {"limit": limit}]
    if until_sig:
        params[1]["until"] = until_sig
    result = _rpc_call("getSignaturesForAddress", params)
    return result or []


def _get_parsed_transaction(signature: str) -> Optional[Dict]:
    """Get a fully parsed transaction by signature."""
    result = _rpc_call("getTransaction", [
        signature,
        {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
    ])
    return result


# ─── Swap Detection ─────────────────────────────────────────────────────────

def _parse_swap_from_tx(tx_data: Dict, wallet_address: str) -> Optional[Dict[str, Any]]:
    """Parse a transaction to detect token swaps.

    Detects buys and sells by analyzing token balance changes:
    - SOL decreased + token increased = BUY
    - SOL increased + token decreased = SELL

    Returns swap info dict or None if not a swap.
    """
    if not tx_data:
        return None

    meta = tx_data.get("meta", {})
    if not meta or meta.get("err"):
        return None  # Failed tx

    # Check if any known DEX program was involved
    tx_msg = tx_data.get("transaction", {}).get("message", {})
    account_keys = []

    # Handle both legacy and versioned tx formats
    for key_entry in tx_msg.get("accountKeys", []):
        if isinstance(key_entry, str):
            account_keys.append(key_entry)
        elif isinstance(key_entry, dict):
            account_keys.append(key_entry.get("pubkey", ""))

    dex_used = None
    for program_id, dex_name in KNOWN_DEX_PROGRAMS.items():
        if program_id in account_keys:
            dex_used = dex_name
            break

    # Also check log messages for DEX program mentions
    if not dex_used:
        log_msgs = meta.get("logMessages", [])
        log_text = " ".join(log_msgs).lower() if log_msgs else ""
        if "jupiter" in log_text or "jup" in log_text:
            dex_used = "Jupiter"
        elif "raydium" in log_text:
            dex_used = "Raydium"
        elif "pump" in log_text:
            dex_used = "Pump.fun"

    if not dex_used:
        return None  # Not a DEX swap

    # Analyze token balance changes for the wallet
    pre_balances = meta.get("preTokenBalances", []) or []
    post_balances = meta.get("postTokenBalances", []) or []

    # Find wallet's account index
    wallet_indices = set()
    for i, key in enumerate(account_keys):
        if key == wallet_address:
            wallet_indices.add(i)

    # Also match by owner field in token balances
    pre_by_mint = {}
    for bal in pre_balances:
        owner = bal.get("owner", "")
        if owner == wallet_address:
            mint = bal.get("mint", "")
            amount = float(bal.get("uiTokenAmount", {}).get("uiAmount") or 0)
            pre_by_mint[mint] = amount

    post_by_mint = {}
    for bal in post_balances:
        owner = bal.get("owner", "")
        if owner == wallet_address:
            mint = bal.get("mint", "")
            amount = float(bal.get("uiTokenAmount", {}).get("uiAmount") or 0)
            post_by_mint[mint] = amount

    # Calculate SOL change from lamport balances
    pre_sol = meta.get("preBalances", [])
    post_sol = meta.get("postBalances", [])
    sol_change = 0
    for idx in wallet_indices:
        if idx < len(pre_sol) and idx < len(post_sol):
            sol_change += (post_sol[idx] - pre_sol[idx]) / 1e9  # lamports to SOL

    # Find the non-SOL token that changed
    all_mints = set(list(pre_by_mint.keys()) + list(post_by_mint.keys()))
    all_mints.discard(WRAPPED_SOL)

    token_changes = {}
    for mint in all_mints:
        pre_amount = pre_by_mint.get(mint, 0)
        post_amount = post_by_mint.get(mint, 0)
        change = post_amount - pre_amount
        if abs(change) > 0:
            token_changes[mint] = {
                "mint": mint,
                "pre": pre_amount,
                "post": post_amount,
                "change": change,
            }

    if not token_changes:
        return None

    # Determine the primary swap: biggest token change
    primary = max(token_changes.values(), key=lambda t: abs(t["change"]))
    token_mint = primary["mint"]
    token_change = primary["change"]

    # Determine direction
    if token_change > 0 and sol_change < -0.001:
        direction = "BUY"
        sol_amount = abs(sol_change)
    elif token_change < 0 and sol_change > 0.001:
        direction = "SELL"
        sol_amount = abs(sol_change)
    elif token_change > 0:
        direction = "BUY"
        sol_amount = 0
    elif token_change < 0:
        direction = "SELL"
        sol_amount = 0
    else:
        return None

    # Block timestamp
    block_time = tx_data.get("blockTime")
    tx_time = datetime.fromtimestamp(block_time, tz=timezone.utc) if block_time else datetime.now(timezone.utc)

    return {
        "direction": direction,
        "token_mint": token_mint,
        "token_change": abs(token_change),
        "sol_amount": sol_amount,
        "dex": dex_used,
        "timestamp": tx_time.isoformat(),
        "block_time": block_time,
    }


# ─── DexScreener Price Enrichment ───────────────────────────────────────────

def _get_token_info(token_address: str) -> Optional[Dict[str, Any]]:
    """Get token info from DexScreener for a swap (with retry on 429)."""
    for attempt in range(2):
        try:
            url = f"https://api.dexscreener.com/tokens/v1/solana/{token_address}"
            resp = requests.get(url, timeout=8)
            if resp.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            if resp.status_code != 200:
                return None
            pairs = resp.json()
            if not pairs or not isinstance(pairs, list):
                return None
            # Use the highest-liquidity pair
            best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
            return {
                "symbol": best.get("baseToken", {}).get("symbol", "???"),
                "price_usd": float(best.get("priceUsd", 0) or 0),
                "market_cap": float(best.get("marketCap", 0) or 0),
                "liquidity_usd": float(best.get("liquidity", {}).get("usd", 0) or 0),
                "price_change_5m": float(best.get("priceChange", {}).get("m5", 0) or 0),
                "volume_24h": float(best.get("volume", {}).get("h24", 0) or 0),
                "pair_address": best.get("pairAddress", ""),
            }
        except Exception as e:
            logger.debug(f"[WHALE] DexScreener lookup failed for {token_address[:16]}: {e}")
            return None
    return None  # All retries exhausted


# ─── Core: Poll Wallets ─────────────────────────────────────────────────────

def poll_wallets() -> Dict[str, Any]:
    """Poll all tracked wallets for new swaps.

    Returns summary of detected swaps and generated signals.
    Call this on a timer (every 60s recommended).
    """
    if not COPY_TRADE_ENABLED:
        return {"enabled": False}

    wallets = load_tracked_wallets()
    if not wallets:
        return {"wallets": 0, "message": "No wallets tracked"}

    enabled_wallets = [w for w in wallets if w.get("enabled", True)]
    if not enabled_wallets:
        return {"wallets": 0, "message": "All wallets disabled"}

    _stats["polls"] += 1
    now = time.time()
    buy_signals = []
    sell_signals = []

    for wallet in enabled_wallets:
        addr = wallet["address"]
        label = wallet.get("label", addr[:8])
        tier = wallet.get("tier", "whale")

        try:
            # Get recent transaction signatures
            cursor = _wallet_cursors.get(addr)
            sigs = _get_recent_signatures(addr, limit=MAX_SIGNATURES_PER_POLL,
                                          until_sig=cursor)
            time.sleep(RPC_DELAY_S)

            if not sigs:
                continue

            # Update cursor to newest signature (first in list)
            with _lock:
                _wallet_cursors[addr] = sigs[0]["signature"]

            # Process each transaction
            for sig_info in sigs:
                sig = sig_info["signature"]
                block_time = sig_info.get("blockTime", 0)

                # Skip old transactions
                if block_time and (now - block_time) > TX_MAX_AGE_S:
                    continue

                # Skip failed txs
                if sig_info.get("err"):
                    continue

                # Fetch and parse the transaction
                tx_data = _get_parsed_transaction(sig)
                time.sleep(RPC_DELAY_S)

                if not tx_data:
                    continue

                swap = _parse_swap_from_tx(tx_data, addr)
                if not swap:
                    continue

                _stats["swaps_detected"] += 1

                # Enrich with DexScreener data
                token_info = _get_token_info(swap["token_mint"])
                time.sleep(RPC_DELAY_S)

                symbol = token_info["symbol"] if token_info else "???"
                price_usd = token_info["price_usd"] if token_info else 0
                market_cap = token_info["market_cap"] if token_info else 0
                liquidity = token_info["liquidity_usd"] if token_info else 0
                swap_usd = swap["sol_amount"] * _get_sol_price() if swap["sol_amount"] > 0 else 0

                # Skip tiny swaps (noise — KOLs do dust-amount test buys)
                if 0 < swap_usd < MIN_SWAP_USD:
                    continue

                if swap["direction"] == "BUY":
                    score = KOL_SIGNAL_SCORE if tier == "kol" else WHALE_SIGNAL_SCORE

                    signal = {
                        "address": swap["token_mint"],
                        "score": score,
                        "grade": "STRONG BUY",
                        "signal_count": 1,
                        "signal_types": {"Whale Copy": 1},
                        "latest_price": price_usd,
                        "market_cap": market_cap,
                        "liquidity_usd": liquidity,
                        "reasoning": (
                            f"WHALE COPY: {tier.upper()} '{label}' bought {symbol} "
                            f"(${swap_usd:.0f} swap via {swap['dex']}). "
                            f"MCap ${market_cap:,.0f}, Liq ${liquidity:,.0f}"
                        ),
                        "risk_flags": _assess_risk(token_info, swap),
                        "scored_at": datetime.now(timezone.utc).isoformat(),
                        "source": "whale_monitor",
                        "whale_wallet": addr,
                        "whale_label": label,
                        "whale_tier": tier,
                        "whale_swap_usd": swap_usd,
                    }
                    buy_signals.append(signal)
                    _stats["buy_signals_generated"] += 1

                    logger.info(
                        f"🐋 [WHALE] {tier.upper()} '{label}' BOUGHT {symbol} "
                        f"(${swap_usd:,.0f} via {swap['dex']}) — "
                        f"generating copy signal (score={score})"
                    )

                    # Fire hook alert
                    # Toggle: PUSH_ALERTS_ENABLED in brain/jarvis_trading_engine.py
                    try:
                        from repryntt.trading.trading_engine import PUSH_ALERTS_ENABLED
                        if PUSH_ALERTS_ENABLED:
                            from repryntt.comms.hooks.trading_parsers import parse_whale_alert
                            from repryntt.comms.hooks.router import get_hook_router
                            hook = parse_whale_alert({**signal, "direction": "BUY", "symbol": symbol})
                            if hook:
                                get_hook_router().dispatch(hook)
                    except Exception:
                        pass

                elif swap["direction"] == "SELL":
                    sell_signals.append({
                        "token_mint": swap["token_mint"],
                        "symbol": symbol,
                        "wallet": addr,
                        "label": label,
                        "tier": tier,
                        "sol_amount": swap["sol_amount"],
                        "usd_amount": swap_usd,
                        "dex": swap["dex"],
                        "timestamp": swap["timestamp"],
                    })
                    _stats["sell_signals_generated"] += 1

                    logger.info(
                        f"🐋 [WHALE] {tier.upper()} '{label}' SOLD {symbol} "
                        f"(${swap_usd:,.0f} via {swap['dex']})"
                    )

                    # Fire hook alert
                    # Toggle: PUSH_ALERTS_ENABLED in brain/jarvis_trading_engine.py
                    try:
                        from repryntt.trading.trading_engine import PUSH_ALERTS_ENABLED
                        if PUSH_ALERTS_ENABLED:
                            from repryntt.comms.hooks.trading_parsers import parse_whale_alert
                            from repryntt.comms.hooks.router import get_hook_router
                            hook = parse_whale_alert({
                                "direction": "SELL", "symbol": symbol,
                                "whale_label": label, "whale_tier": tier,
                                "whale_swap_usd": swap_usd, "wallet": addr,
                                "token_mint": swap["token_mint"], "dex": swap["dex"],
                            })
                            if hook:
                                get_hook_router().dispatch(hook)
                    except Exception:
                        pass

        except Exception as e:
            logger.error(f"[WHALE] Error polling {label} ({addr[:16]}): {e}")
            continue

    # Feed buy signals to trading engine
    # DISABLED — whale signals are DATA ONLY, not fed to trading decisions
    # KOL/whale data is still logged and shown in dashboard for reference.
    # To re-enable, set COPY_TRADE_ENABLED = True above.
    try:
        if False:  # was: PUSH_ALERTS_ENABLED and buy_signals
            from repryntt.trading.trading_engine import ingest_signals
            result = ingest_signals(buy_signals)
            logger.info(
                f"🐋 [WHALE] Fed {len(buy_signals)} copy signal(s) to engine: "
                f"auto={len(result.get('auto_executed', []))}, queued={result.get('queued', 0)}"
            )
        if False:  # was: PUSH_ALERTS_ENABLED and sell_signals
            _handle_whale_sells(sell_signals)
    except Exception as e:
        logger.error(f"[WHALE] Failed to feed signals to engine: {e}")

    # Buffer signals for dashboard display
    with _lock:
        for sig in buy_signals:
            _recent_signals.insert(0, sig)
        for sig in sell_signals:
            _recent_signals.insert(0, {
                "direction": "SELL",
                "symbol": sig.get("symbol", "???"),
                "address": sig.get("token_mint", ""),
                "whale_label": sig.get("label", ""),
                "whale_tier": sig.get("tier", "whale"),
                "whale_swap_usd": sig.get("usd_amount", 0),
                "reasoning": f"{sig.get('tier','whale').upper()} '{sig.get('label','')}' sold {sig.get('symbol','???')} (${sig.get('usd_amount',0):,.0f} via {sig.get('dex','?')})",
                "scored_at": datetime.now(timezone.utc).isoformat(),
            })
        del _recent_signals[_RECENT_SIGNALS_MAX:]

    # Persist state
    _save_state()

    return {
        "wallets_polled": len(enabled_wallets),
        "buy_signals": len(buy_signals),
        "sell_signals": len(sell_signals),
        "stats": dict(_stats),
    }


def _handle_whale_sells(sell_signals: List[Dict]):
    """If a whale sells a token we hold, auto-sell our position too.

    Smart money exiting = our exit signal.
    """
    try:
        from repryntt.trading.trading_simulator import sim_sell, _load_portfolio

        portfolio = _load_portfolio(JARVIS_WORKSPACE)
        positions = portfolio.get("positions", {})
        if not positions:
            return

        # Build lookup: token_address → symbol
        held_addresses = {}
        for symbol, pos in positions.items():
            token_addr = pos.get("token_address", "")
            if token_addr:
                held_addresses[token_addr] = symbol

        for sell in sell_signals:
            token_mint = sell["token_mint"]
            if token_mint in held_addresses:
                symbol = held_addresses[token_mint]
                label = sell["label"]
                tier = sell["tier"]

                reason = (
                    f"[WHALE-EXIT] {tier.upper()} '{label}' sold {symbol} "
                    f"(${sell.get('usd_amount', 0):,.0f} via {sell['dex']}). "
                    f"Smart money exiting — following."
                )
                result_json = sim_sell(JARVIS_WORKSPACE, symbol, sell_pct=100, reason=reason)
                result = json.loads(result_json)

                if "error" not in result:
                    logger.info(
                        f"🐋 [WHALE-EXIT] Auto-sold {symbol}: whale '{label}' exited — "
                        f"P/L ${result.get('pnl', 0):+.2f}"
                    )
                else:
                    logger.warning(f"[WHALE-EXIT] Sell failed for {symbol}: {result.get('error')}")

    except Exception as e:
        logger.error(f"[WHALE] Error handling sell signals: {e}", exc_info=True)


# ─── Risk Assessment ────────────────────────────────────────────────────────

def _assess_risk(token_info: Optional[Dict], swap: Dict) -> List[str]:
    """Quick risk check for a whale copy signal."""
    flags = []
    if not token_info:
        flags.append("No DexScreener data")
        return flags

    liq = token_info.get("liquidity_usd", 0)
    mcap = token_info.get("market_cap", 0)

    if liq < 5000:
        flags.append("Very low liquidity (<$5K)")
    elif liq < 20000:
        flags.append("Low liquidity (<$20K)")

    if mcap > 0 and mcap < 10000:
        flags.append("Micro cap (<$10K)")

    if token_info.get("price_change_5m", 0) < -20:
        flags.append("Sharp decline")

    return flags


# ─── SOL Price Cache ────────────────────────────────────────────────────────

_sol_price_cache = {"price": 0.0, "fetched_at": 0.0}

def _get_sol_price() -> float:
    """Get current SOL price (cached for 60s)."""
    now = time.time()
    if now - _sol_price_cache["fetched_at"] < 60 and _sol_price_cache["price"] > 0:
        return _sol_price_cache["price"]

    try:
        resp = requests.get(
            "https://api.dexscreener.com/tokens/v1/solana/So11111111111111111111111111111111111111112",
            timeout=5
        )
        if resp.status_code == 200:
            pairs = resp.json()
            if pairs and isinstance(pairs, list):
                price = float(pairs[0].get("priceUsd", 0) or 0)
                if price > 0:
                    _sol_price_cache["price"] = price
                    _sol_price_cache["fetched_at"] = now
                    return price
    except Exception:
        pass

    # Fallback
    return _sol_price_cache["price"] if _sol_price_cache["price"] > 0 else 130.0


# ─── State Persistence ───────────────────────────────────────────────────────

def _save_state():
    """Save monitor state (cursors + recent signals) to disk."""
    try:
        with _lock:
            signals_snapshot = list(_recent_signals)
        state = {
            "wallet_cursors": dict(_wallet_cursors),
            "stats": dict(_stats),
            "recent_signals": signals_snapshot[:_RECENT_SIGNALS_MAX],
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except IOError as e:
        logger.error(f"[WHALE] State save failed: {e}")


def load_state():
    """Load monitor state from disk (call at startup)."""
    global _wallet_cursors
    if not STATE_FILE.exists():
        return
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        _wallet_cursors.update(state.get("wallet_cursors", {}))
        _stats.update(state.get("stats", {}))
        saved_signals = state.get("recent_signals", [])
        if saved_signals:
            with _lock:
                _recent_signals.clear()
                _recent_signals.extend(saved_signals[:_RECENT_SIGNALS_MAX])
            logger.info(f"[WHALE] Restored {len(saved_signals)} recent signal(s)")
        logger.info(f"[WHALE] Loaded state: tracking {len(_wallet_cursors)} wallet cursor(s)")
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"[WHALE] State load failed: {e}")


def get_stats() -> Dict[str, Any]:
    """Return monitor stats."""
    wallets = load_tracked_wallets()
    return {
        "enabled": COPY_TRADE_ENABLED,
        "tracked_wallets": len(wallets),
        "enabled_wallets": sum(1 for w in wallets if w.get("enabled", True)),
        "poll_interval_s": POLL_INTERVAL_S,
        "rpc_url": SOLANA_RPC_URL[:40] + "...",
        **_stats,
    }


def get_recent_signals() -> List[Dict[str, Any]]:
    """Return recent whale signals for dashboard display."""
    with _lock:
        return list(_recent_signals)


def get_intelligence_summary(max_age_hours: float = 2.0) -> Optional[str]:
    """Build a human-readable intelligence summary of recent KOL/whale activity.

    Returns a concise summary of who bought/sold what recently,
    suitable for injecting into Artemis's heartbeat prompt.
    Returns None if no recent activity.
    """
    with _lock:
        signals = list(_recent_signals)

    if not signals:
        return None

    now = datetime.now(timezone.utc)
    cutoff_s = max_age_hours * 3600

    # Filter to recent signals only
    recent_buys = []
    recent_sells = []
    for sig in signals:
        scored_at = sig.get("scored_at", "")
        if not scored_at:
            continue
        try:
            sig_time = datetime.fromisoformat(scored_at)
            age_s = (now - sig_time).total_seconds()
            if age_s > cutoff_s:
                continue
        except (ValueError, TypeError):
            continue

        direction = sig.get("direction", "BUY")
        if direction == "SELL":
            recent_sells.append(sig)
        else:
            recent_buys.append(sig)

    if not recent_buys and not recent_sells:
        return None

    # Group buys by wallet label
    buys_by_wallet: Dict[str, List[Dict]] = {}
    for sig in recent_buys:
        label = sig.get("whale_label", sig.get("label", "unknown"))
        buys_by_wallet.setdefault(label, []).append(sig)

    parts = [f"\U0001f40b **KOL/Whale Intelligence** (last {max_age_hours:.0f}h):"]

    for label, buys in buys_by_wallet.items():
        tier = buys[0].get("whale_tier", "whale").upper()
        tokens = []
        for b in buys:
            sym = b.get("reasoning", "").split("bought ")[1].split(" ")[0] if "bought " in b.get("reasoning", "") else "???"
            usd = b.get("whale_swap_usd", 0)
            addr = b.get("address", "")[:12]
            tokens.append(f"{sym} (${usd:,.0f}, `{addr}...`)")
        parts.append(f"- **{tier} '{label}'** bought: {', '.join(tokens[:5])}")

    if recent_sells:
        sell_labels = set()
        for s in recent_sells:
            lbl = s.get("whale_label", s.get("label", ""))
            sym = s.get("symbol", "???")
            sell_labels.add(f"{lbl} sold {sym}")
        parts.append(f"- **Sells**: {'; '.join(list(sell_labels)[:5])}")

    parts.append(
        f"- Total: {len(recent_buys)} buy(s), {len(recent_sells)} sell(s)\n"
        f"- *KOLs scalp Pump.fun in minutes — treat as sector intelligence, not copy signals.*\n"
        f"- If a token appears in MULTIPLE KOL buys, investigate it with `dexscreener_token_search()`."
    )

    return "\n".join(parts)
