"""
SAIGE Trading Simulator — Paper Trading for Jarvis
===================================================

Gives Jarvis a virtual portfolio with real market prices from DexScreener.
All trades are simulated but use live price data, allowing Jarvis to prove
its trading thesis before real money is deployed.

Portfolio state persists in a JSON file between heartbeats.
"""

import json
import os
import re
import time
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger("repryntt.trading_sim")


def _try_real_execution(side: str, token_address: str, amount_usd: float = 0, sell_pct: float = 100.0):
    """Real on-chain execution hook (no-op).

    The previous implementation used the MoonPay CLI, which has been removed
    from this installation. If you want live trading from the simulator,
    wire this to an alternative execution path (e.g. jupiter_swap in
    repryntt.tools.jupiter_tools) — kept as a stub so callers continue to
    work and simulator behavior is unaffected.
    """
    return


# ─── Config ──────────────────────────────────────────────────────────────────
DEFAULT_STARTING_BALANCE = 23.00    # USD — matches real wallet (~0.28 SOL)
SLIPPAGE_PCT = 0.06                 # 6% slippage on every trade (memecoin reality)
MIN_TRADE_USD = 8.00                 # Minimum $8 (~0.1 SOL) — below this, fees eat profit
POSITION_SIZE_USD = 16.00            # ~0.2 SOL buy-ins — enough for meaningful profit
MAX_POSITIONS_BASE = 1              # Max 1 position at a time — patience, not volume
MAX_POSITIONS_SCALED = 3            # Up to 3 concurrent once wallet reaches $200+
POSITION_SCALE_THRESHOLD = 200.00   # Wallet balance that unlocks multi-position trading
DEXSCREENER_TIMEOUT = 15            # seconds
FAUCET_RELOAD_AMOUNT = 0            # NO faucet — this is real money, Andrew earns more by trading
DAILY_PROFIT_TARGET = 50.00         # $50/day target — realistic at 0.2 SOL buy-ins with 3x returns


def _max_positions(portfolio: Dict[str, Any]) -> int:
    """Dynamic position limit — earn the right to run multiple trades.

    Start with 1 position. $50 buy-ins need TIME to 3x.
    Running multiple positions at $50 = spread too thin.
    Once the wallet grows to $500+ through disciplined trading,
    unlock up to 3 concurrent positions.
    """
    total_value = portfolio.get("cash_balance", 0)
    for pos in portfolio.get("positions", {}).values():
        total_value += pos.get("total_cost", 0)
    if total_value >= POSITION_SCALE_THRESHOLD:
        return MAX_POSITIONS_SCALED
    return MAX_POSITIONS_BASE

# ─── Auto-Profit Config ─────────────────────────────────────────────────────
# ENABLED — Watchdog auto-sells when thresholds are hit.
# Strategy: $50 buy-ins targeting 3x. Let winners ride, cut losers.
AUTO_TAKE_PROFIT_PCT = 200.0         # Auto-sell 100% at +200% P&L (3x — the target)
AUTO_STOP_LOSS_PCT = -15.0           # Auto-sell 100% at -15% P&L (capital preservation)
AUTO_MOON_PARTIAL_PCT = 500.0        # Auto-sell 50% at +500% (5x moonshot — let rest ride)
AUTO_PROFIT_ENABLED = True           # ENABLED — watchdog executes auto TP/SL

# ─── Portfolio File ──────────────────────────────────────────────────────────

def _portfolio_path(workspace: str) -> str:
    return os.path.join(workspace, "sim_portfolio.json")


def _load_portfolio(workspace: str) -> Dict[str, Any]:
    """Load portfolio from disk, or create a fresh one."""
    path = _portfolio_path(workspace)
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Corrupt portfolio file, backing up and resetting: {e}")
            try:
                os.rename(path, path + ".corrupt")
            except OSError:
                pass

    # Fresh portfolio
    portfolio = {
        "starting_balance": DEFAULT_STARTING_BALANCE,
        "cash_balance": DEFAULT_STARTING_BALANCE,
        "positions": {},       # symbol -> {token_address, avg_entry, quantity, total_cost}
        "trade_history": [],   # list of trade records
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "metadata": {
            "slippage_pct": SLIPPAGE_PCT,
            "starting_capital": DEFAULT_STARTING_BALANCE,
            "version": 1,
        }
    }
    _save_portfolio(workspace, portfolio)
    return portfolio


def _save_portfolio(workspace: str, portfolio: Dict[str, Any]):
    """Persist portfolio to disk."""
    os.makedirs(workspace, exist_ok=True)
    portfolio["last_updated"] = datetime.now(timezone.utc).isoformat()
    path = _portfolio_path(workspace)
    tmp = path + ".tmp"
    with open(tmp, 'w') as f:
        json.dump(portfolio, f, indent=2)
    os.replace(tmp, path)


# ─── Price Fetching (Internal DB → DexScreener fallback) ─────────────────────

# Path to the degen terminal's SQLite database (updated ~1s by token_monitor)
_INTERNAL_DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "tokens.db"
)


def _fetch_price_internal(address: str) -> Optional[Dict[str, Any]]:
    """Try to fetch price from the internal degen terminal DB (near real-time).

    The token_monitor refreshes prices every ~1 second from DexScreener in bulk.
    Reading from the local DB is instant and avoids per-call API rate limits.
    Returns None if the token isn't in the internal DB.
    """
    if not address or not _is_contract_address(address):
        return None
    try:
        import sqlite3
        if not os.path.exists(_INTERNAL_DB_PATH):
            return None
        conn = sqlite3.connect(_INTERNAL_DB_PATH, timeout=2)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT address, base_token_symbol, token_name, current_price, "
            "current_market_cap, volume_5m, buy_volume_5m, sell_volume_5m, "
            "price_change_5m, price_change_1m, top_20_holders_percentage, "
            "last_updated FROM tokens WHERE address = ? LIMIT 1",
            (address,)
        )
        row = cur.fetchone()
        conn.close()
        if not row or not row["current_price"] or row["current_price"] <= 0:
            return None
        # Check staleness — if last_updated is older than 5 min, fall back to DexScreener
        try:
            from datetime import timezone as _tz
            updated = datetime.fromisoformat(row["last_updated"].replace("Z", "+00:00"))
            age_s = (datetime.now(_tz.utc) - updated).total_seconds()
            if age_s > 300:
                logger.debug(f"Internal price for {address} is {age_s:.0f}s old, using DexScreener")
                return None
        except Exception:
            pass
        return {
            "symbol": row["base_token_symbol"] or "",
            "name": row["token_name"] or "",
            "chain": "solana",
            "price_usd": float(row["current_price"]),
            "price_change_5m": float(row["price_change_5m"] or 0),
            "price_change_1m": float(row["price_change_1m"] or 0),
            "volume_5m": float(row["volume_5m"] or 0),
            "market_cap": float(row["current_market_cap"] or 0),
            "token_address": row["address"],
            "holder_concentration": float(row["top_20_holders_percentage"] or 0),
            "source": "internal_db",
            # Compat fields for existing code
            "price_change_24h": 0,
            "volume_24h": 0,
            "liquidity_usd": 0,
            "pair_address": "",
            "dex": "",
        }
    except Exception as e:
        logger.debug(f"Internal price lookup failed for {address}: {e}")
        return None


def _fetch_price_internal_stale(address: str) -> Optional[Dict[str, Any]]:
    """Same as _fetch_price_internal but accepts stale data (no 300s cutoff).

    Used as a last-resort fallback for Pump.fun tokens that DexScreener doesn't list.
    A stale price is better than no price when all live sources fail.
    """
    if not address or not _is_contract_address(address):
        return None
    try:
        import sqlite3
        if not os.path.exists(_INTERNAL_DB_PATH):
            return None
        conn = sqlite3.connect(_INTERNAL_DB_PATH, timeout=2)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT address, base_token_symbol, token_name, current_price, "
            "current_market_cap, volume_5m, buy_volume_5m, sell_volume_5m, "
            "price_change_5m, price_change_1m, top_20_holders_percentage, "
            "last_updated FROM tokens WHERE address = ? LIMIT 1",
            (address,)
        )
        row = cur.fetchone()
        conn.close()
        if not row or not row["current_price"] or row["current_price"] <= 0:
            return None
        return {
            "symbol": row["base_token_symbol"] or "",
            "name": row["token_name"] or "",
            "chain": "solana",
            "price_usd": float(row["current_price"]),
            "price_change_5m": float(row["price_change_5m"] or 0),
            "price_change_1m": float(row["price_change_1m"] or 0),
            "volume_5m": float(row["volume_5m"] or 0),
            "market_cap": float(row["current_market_cap"] or 0),
            "token_address": row["address"],
            "holder_concentration": float(row["top_20_holders_percentage"] or 0),
            "source": "internal_db_stale",
            "price_change_24h": 0,
            "volume_24h": 0,
            "liquidity_usd": 0,
            "pair_address": "",
            "dex": "",
        }
    except Exception as e:
        logger.debug(f"Stale internal price lookup failed for {address}: {e}")
        return None


def _is_contract_address(query: str) -> bool:
    """Heuristic: Solana addresses are 32-44 base58 chars, no spaces."""
    q = query.strip()
    if len(q) < 30 or " " in q:
        return False
    return bool(re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", q))


def _normalize_token_query(query: str) -> str:
    """Normalize token identifiers — strip Pump.fun suffixes, clean whitespace."""
    q = query.strip()
    # Pump.fun addresses end with 'pump' making them 44+4=48 chars
    # e.g. 'n3ShrNZRCoMrw5Gww7rPMxVbDq3to3YwsGkDz19pump'
    # Strip the suffix and check if the base is a valid Solana address
    if q.lower().endswith("pump") and len(q) > 44:
        base = q[:-4]
        if _is_contract_address(base):
            return base
    return q


def _fetch_price(query: str) -> Optional[Dict[str, Any]]:
    """Fetch current price for a token.

    Resolution order:
      1. Internal degen terminal DB (near real-time, ~1s refresh) — instant, no API call
      2. DexScreener API (fallback for tokens not in internal DB)
      3. Jupiter Price API (catches Pump.fun bonding-curve tokens DexScreener misses)
      4. Stale internal DB data (last resort — a stale price beats no price)

    Args:
        query: Token symbol (e.g. 'BONK'), name, or contract address.

    Returns:
        Dict with price info or None on failure.
    """
    # Normalize pump.fun-style addresses (strip trailing 'pump' suffix)
    query = _normalize_token_query(query)

    # Try internal DB first (only works for contract addresses the monitor is tracking)
    if _is_contract_address(query):
        internal = _fetch_price_internal(query)
        if internal:
            return internal

    try:
        # Use the direct token endpoint for contract addresses (exact match,
        # no risk of returning a different token with the same ticker)
        if _is_contract_address(query):
            resp = requests.get(
                f"https://api.dexscreener.com/tokens/v1/solana/{query}",
                timeout=DEXSCREENER_TIMEOUT,
                headers={"Accept": "application/json", "User-Agent": "SAIGE/1.0"}
            )
            resp.raise_for_status()
            pairs = resp.json()
            if isinstance(pairs, dict):
                pairs = pairs.get("pairs", pairs.get("data", []))
        else:
            resp = requests.get(
                f"https://api.dexscreener.com/latest/dex/search/?q={query}",
                timeout=DEXSCREENER_TIMEOUT,
                headers={"Accept": "application/json", "User-Agent": "SAIGE/1.0"}
            )
            resp.raise_for_status()
            data = resp.json()
            pairs = data.get("pairs", [])

        if not pairs:
            return None

        # Prefer Solana pairs, sorted by liquidity (highest first) to
        # deterministically pick the "real" token when multiple share a ticker
        solana_pairs = [p for p in pairs if p.get("chainId", "").lower() == "solana"]
        candidates = solana_pairs if solana_pairs else pairs
        candidates.sort(
            key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0),
            reverse=True,
        )
        best = candidates[0]

        base = best.get("baseToken", {})
        price_str = best.get("priceUsd", "0")
        try:
            price = float(price_str) if price_str else 0.0
        except (ValueError, TypeError):
            price = 0.0

        return {
            "symbol": base.get("symbol", "").strip(),
            "name": base.get("name", "").strip(),
            "chain": best.get("chainId", ""),
            "price_usd": price,
            "price_change_24h": best.get("priceChange", {}).get("h24", 0),
            "volume_24h": best.get("volume", {}).get("h24", 0),
            "liquidity_usd": best.get("liquidity", {}).get("usd", 0),
            "market_cap": best.get("marketCap", 0),
            "pair_address": best.get("pairAddress", ""),
            "token_address": base.get("address", ""),
            "dex": best.get("dexId", ""),
        }
    except Exception as e:
        logger.warning(f"DexScreener price fetch failed for '{query}': {e}")

    # ── Fallback: Jupiter Price API (catches Pump.fun bonding-curve tokens) ──
    if _is_contract_address(query):
        try:
            resp = requests.get(
                f"https://api.jup.ag/price/v2?ids={query}",
                timeout=8,
                headers={"Accept": "application/json"},
            )
            if resp.status_code == 200:
                jdata = resp.json().get("data", {}).get(query, {})
                jprice = float(jdata.get("price", 0))
                if jprice > 0:
                    logger.info(f"Jupiter fallback price for {query}: ${jprice}")
                    return {
                        "symbol": jdata.get("mintSymbol", query[:8]),
                        "name": jdata.get("mintSymbol", ""),
                        "chain": "solana",
                        "price_usd": jprice,
                        "price_change_24h": 0,
                        "volume_24h": 0,
                        "liquidity_usd": 0,
                        "market_cap": 0,
                        "pair_address": "",
                        "token_address": query,
                        "dex": "jupiter",
                    }
        except Exception as e:
            logger.debug(f"Jupiter price fallback failed for '{query}': {e}")

    # ── Last resort: use stale internal DB data (better than nothing) ──
    if _is_contract_address(query):
        stale = _fetch_price_internal_stale(query)
        if stale:
            logger.info(f"Using stale internal price for {query}: ${stale['price_usd']}")
            return stale

    return None


def _fetch_prices_batch(symbols: list) -> Dict[str, Dict]:
    """Fetch prices for multiple tokens. Returns {symbol: price_info}."""
    results = {}
    for sym in symbols:
        info = _fetch_price(sym)
        if info:
            results[sym] = info
        time.sleep(0.3)  # Be nice to DexScreener
    return results


def _sync_token_profile(workspace: str, symbol: str, token_address: str):
    """Sync degen terminal data into a position after buying.

    Enriches the position with holder data, signals, price history,
    and narrative from the internal monitoring system. This gives
    Artemis all relevant data in one place when reviewing positions.
    """
    if not token_address:
        return
    try:
        portfolio = _load_portfolio(workspace)
        pos = portfolio.get("positions", {}).get(symbol)
        if not pos:
            return

        # 1. Read from internal DB for real-time metrics
        import sqlite3
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tokens.db")
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path, timeout=2)
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT base_token_symbol, token_name, current_market_cap, "
                "top_20_holders_percentage, is_bundled, bundling_reason, "
                "is_uptrend, ath_price, ath_timestamp, volume_5m, volume_1h, "
                "buys_5m, sells_5m, price_change_5m "
                "FROM tokens WHERE address = ? LIMIT 1",
                (token_address,)
            )
            row = cur.fetchone()
            conn.close()
            if row:
                pos["_terminal"] = {
                    "name": row["token_name"] or "",
                    "mcap_at_buy": float(row["current_market_cap"] or 0),
                    "holder_pct": float(row["top_20_holders_percentage"] or 0),
                    "bundled": bool(row["is_bundled"]),
                    "bundling_reason": row["bundling_reason"] or "",
                    "uptrend": bool(row["is_uptrend"]),
                    "ath_price": float(row["ath_price"] or 0),
                    "volume_5m_at_buy": float(row["volume_5m"] or 0),
                    "volume_1h_at_buy": float(row["volume_1h"] or 0),
                    "synced_at": datetime.now(timezone.utc).isoformat(),
                }

        # 2. Read token profile for price history if available
        profile_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "data", "token_profiles", f"{token_address}.json"
        )
        if os.path.exists(profile_path):
            with open(profile_path) as f:
                profile = json.load(f)
            terminal = pos.setdefault("_terminal", {})
            terminal["initial_price"] = profile.get("initial_price", 0)
            terminal["initial_mcap"] = profile.get("initial_market_cap", 0)
            terminal["signal_history"] = profile.get("signal_history", [])[-10:]
            terminal["profile_synced"] = True

        _save_portfolio(workspace, portfolio)
        logger.info(f"[SYNC] Token profile synced for {symbol} ({token_address[:12]}...)")
    except Exception as e:
        logger.debug(f"Token profile sync failed for {symbol}: {e}")


# ─── Core Trading Functions ──────────────────────────────────────────────────

def sim_buy(workspace: str, token: str, amount_usd: float,
            reason: str = "") -> str:
    """Buy a token with simulated USD.

    Args:
        workspace: Jarvis workspace path
        token: Token symbol or contract address to buy (looked up on DexScreener)
        amount_usd: USD amount to spend on this buy
        reason: Your reasoning for this trade (recorded in trade log)

    Returns:
        JSON string with trade result or error.
    """
    portfolio = _load_portfolio(workspace)

    # Validate amount
    try:
        amount_usd = float(amount_usd)
    except (ValueError, TypeError):
        return json.dumps({"error": "amount_usd must be a number"})

    if amount_usd < MIN_TRADE_USD:
        return json.dumps({"error": f"Minimum trade is ${MIN_TRADE_USD}"})

    # Auto-faucet: if wallet is depleted and no positions open, inject small amount
    if portfolio["cash_balance"] < MIN_TRADE_USD and len(portfolio["positions"]) == 0:
        logger.info(f"⚠️ [AUTO-FAUCET] Wallet depleted (${portfolio['cash_balance']:.2f}), "
                    f"no positions open — emergency injection of ${FAUCET_RELOAD_AMOUNT:.0f}")
        portfolio["cash_balance"] += FAUCET_RELOAD_AMOUNT
        portfolio["starting_balance"] += FAUCET_RELOAD_AMOUNT
        portfolio["trade_history"].append({
            "type": "FAUCET",
            "amount_usd": round(FAUCET_RELOAD_AMOUNT, 2),
            "old_balance": 0,
            "new_balance": round(FAUCET_RELOAD_AMOUNT, 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": "Auto-faucet: wallet depleted, no positions open",
        })
        _save_portfolio(workspace, portfolio)

    if amount_usd > portfolio["cash_balance"]:
        return json.dumps({
            "error": f"Insufficient balance. You have ${portfolio['cash_balance']:.2f}, "
                     f"tried to spend ${amount_usd:.2f}"
        })

    max_pos = _max_positions(portfolio)
    if len(portfolio["positions"]) >= max_pos:
        total_value = portfolio["cash_balance"] + sum(p.get("total_cost", 0) for p in portfolio["positions"].values())
        if total_value < POSITION_SCALE_THRESHOLD:
            return json.dumps({
                "error": f"Max {max_pos} position until wallet reaches ${POSITION_SCALE_THRESHOLD:.0f}. "
                         f"Current portfolio value: ${total_value:.2f}. Sell or grow first."
            })
        return json.dumps({
            "error": f"Max {max_pos} positions reached. Sell something first."
        })

    # ── Safety net: reject bundled tokens ─────────────────────────────
    # Try BOTH the raw address and the normalized form — DB stores full
    # pump.fun addresses (with "pump" suffix) but _normalize strips it.
    _raw_address = token.strip()
    _norm_address = _normalize_token_query(_raw_address)
    try:
        import sqlite3 as _sq
        _tdb = Path(__file__).resolve().parent / "data" / "tokens.db"
        if _tdb.exists():
            _conn = _sq.connect(str(_tdb), timeout=3)
            _brow = _conn.execute(
                "SELECT is_bundled, bundling_reason FROM tokens "
                "WHERE address IN (?, ?) AND is_bundled = 1 LIMIT 1",
                (_raw_address, _norm_address)
            ).fetchone()
            _conn.close()
            if _brow and _brow[0]:
                return json.dumps({
                    "error": f"BLOCKED — token is bundled: {_brow[1] or 'bundling detected'}. "
                             f"Bundled tokens are not safe to trade."
                })
    except Exception:
        pass  # Non-fatal — rely on pipeline gate

    # Fetch real price
    price_info = _fetch_price(token)
    if not price_info or price_info["price_usd"] <= 0:
        return json.dumps({"error": f"Could not fetch price for '{token}'. Check the symbol/address."})

    price = price_info["price_usd"]
    symbol = price_info["symbol"]

    # Apply slippage (buy = worse price = pay more)
    effective_price = price * (1 + SLIPPAGE_PCT)
    quantity = amount_usd / effective_price

    # Update position (average in if already holding)
    key = symbol.upper()
    new_address = price_info.get("token_address", "")
    if key in portfolio["positions"]:
        pos = portfolio["positions"][key]
        existing_address = pos.get("token_address", "")
        # Guard against symbol collision: reject if contract addresses differ
        # Normalize both for comparison (Solana base58 is case-sensitive,
        # but DexScreener can return addresses with inconsistent casing)
        if existing_address and new_address and existing_address.strip() != new_address.strip():
            return json.dumps({
                "error": f"Symbol collision: already holding {key} at address "
                         f"{existing_address[:12]}... but DexScreener returned "
                         f"a DIFFERENT token at {new_address[:12]}... — "
                         f"pass the contract address directly to buy the right one."
            })
        old_qty = pos["quantity"]
        old_cost = pos["total_cost"]
        new_qty = old_qty + quantity
        new_cost = old_cost + amount_usd
        pos["quantity"] = new_qty
        pos["total_cost"] = new_cost
        pos["avg_entry"] = new_cost / new_qty
    else:
        portfolio["positions"][key] = {
            "token_address": new_address,
            "chain": price_info.get("chain", "solana"),
            "avg_entry": effective_price,
            "quantity": quantity,
            "total_cost": amount_usd,
            "bought_at": datetime.now(timezone.utc).isoformat(),
        }

    # Deduct cash
    portfolio["cash_balance"] -= amount_usd

    # Record trade
    trade = {
        "type": "BUY",
        "symbol": key,
        "token_address": price_info.get("token_address", ""),
        "amount_usd": round(amount_usd, 2),
        "price": price,
        "effective_price": round(effective_price, 8),
        "quantity": quantity,
        "slippage_pct": SLIPPAGE_PCT,
        "reason": reason[:500] if reason else "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_cap": price_info.get("market_cap", 0),
        "liquidity": price_info.get("liquidity_usd", 0),
    }
    portfolio["trade_history"].append(trade)
    _save_portfolio(workspace, portfolio)

    buy_result = {
        "status": "BUY FILLED",
        "symbol": key,
        "quantity": quantity,
        "price_at_market": price,
        "effective_price": round(effective_price, 8),
        "total_spent": round(amount_usd, 2),
        "remaining_cash": round(portfolio["cash_balance"], 2),
        "position_total_cost": round(portfolio["positions"][key]["total_cost"], 2),
        "reason": reason[:200],
    }

    _try_real_execution("BUY", price_info.get("token_address", ""), amount_usd=amount_usd)

    # Sync degen terminal data into the position
    _sync_token_profile(workspace, key, new_address)

    return json.dumps(buy_result, indent=2)


def sim_sell(workspace: str, token: str, sell_pct: float = 100.0,
             reason: str = "") -> str:
    """Sell a token position (or partial).

    Args:
        workspace: Jarvis workspace path
        token: Token symbol to sell (must match a held position)
        sell_pct: Percentage of position to sell (1-100). Default: 100 (sell all).
        reason: Your reasoning for this trade (recorded in trade log)

    Returns:
        JSON string with trade result or error.
    """
    portfolio = _load_portfolio(workspace)

    key = token.strip().upper()

    # Direct symbol match
    if key not in portfolio["positions"]:
        # Fallback: match by token_address (agent may pass address instead of symbol)
        for sym, pos in portfolio["positions"].items():
            addr = pos.get("token_address", "")
            if addr and (key == addr.upper() or key == addr
                         or token.strip() == addr):
                key = sym
                break

    if key not in portfolio["positions"]:
        # Fallback: case-insensitive partial symbol match (e.g. "tsuki" vs "TSUKI")
        for sym in portfolio["positions"]:
            if sym.upper() == key:
                key = sym
                break

    if key not in portfolio["positions"]:
        held = list(portfolio["positions"].keys())
        return json.dumps({
            "error": f"No position in {token.strip()}. Current positions: {held or 'none'}"
        })

    try:
        sell_pct = float(sell_pct)
    except (ValueError, TypeError):
        sell_pct = 100.0
    sell_pct = max(1.0, min(100.0, sell_pct))

    pos = portfolio["positions"][key]

    # Fetch real current price
    lookup = pos.get("token_address") or key
    price_info = _fetch_price(lookup)
    if not price_info or price_info["price_usd"] <= 0:
        return json.dumps({"error": f"Could not fetch current price for {key}. Try again."})

    current_price = price_info["price_usd"]

    # Apply slippage (sell = worse price = get less)
    effective_price = current_price * (1 - SLIPPAGE_PCT)

    sell_quantity = pos["quantity"] * (sell_pct / 100.0)
    proceeds = sell_quantity * effective_price

    # Calculate P&L for this sale
    cost_basis = pos["avg_entry"] * sell_quantity
    pnl = proceeds - cost_basis
    pnl_pct = ((effective_price / pos["avg_entry"]) - 1) * 100 if pos["avg_entry"] > 0 else 0

    # Update position
    remaining_qty = pos["quantity"] - sell_quantity
    if remaining_qty < 1e-12 or sell_pct >= 99.9:
        # Full close
        del portfolio["positions"][key]
        remaining_qty = 0
    else:
        pos["quantity"] = remaining_qty
        pos["total_cost"] = pos["avg_entry"] * remaining_qty

    # Add proceeds to cash
    portfolio["cash_balance"] += proceeds

    # Record trade
    trade = {
        "type": "SELL",
        "symbol": key,
        "token_address": price_info.get("token_address", ""),
        "sell_pct": round(sell_pct, 1),
        "quantity_sold": sell_quantity,
        "price": current_price,
        "effective_price": round(effective_price, 8),
        "proceeds": round(proceeds, 2),
        "cost_basis": round(cost_basis, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "slippage_pct": SLIPPAGE_PCT,
        "reason": reason[:500] if reason else "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "remaining_quantity": remaining_qty,
    }
    portfolio["trade_history"].append(trade)
    _save_portfolio(workspace, portfolio)

    # Feed learning engine with trade outcome
    try:
        from repryntt.learning.engine import LearningEngine
        from repryntt.learning.trading import TradingLearner
        _data_dir = Path(__file__).resolve().parent.parent / "learning" / "data"
        _eng = LearningEngine(data_dir=_data_dir)
        _tl = TradingLearner(_eng)
        _tl.on_trade_exit_by_address(
            address=price_info.get("token_address", key),
            pnl_pct=round(pnl_pct, 2),
            exit_price=current_price,
            hold_seconds=0,
            reason=reason[:200] if reason else "sim_sell",
        )
    except Exception:
        pass  # Learning engine hook is best-effort

    _try_real_execution("SELL", price_info.get("token_address", ""), sell_pct=sell_pct)

    return json.dumps({
        "status": "SELL FILLED",
        "symbol": key,
        "sold": f"{sell_pct:.0f}%",
        "quantity_sold": sell_quantity,
        "price_at_market": current_price,
        "effective_price": round(effective_price, 8),
        "proceeds": round(proceeds, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "remaining_position": remaining_qty,
        "cash_balance": round(portfolio["cash_balance"], 2),
        "reason": reason[:200],
    }, indent=2)


def sim_portfolio(workspace: str, **kwargs) -> str:
    """Get full portfolio status with live prices, P&L, and trade history summary.

    Args:
        workspace: Jarvis workspace path

    Returns:
        JSON string with full portfolio status.
    """
    portfolio = _load_portfolio(workspace)

    # Fetch live prices for all held positions
    positions_with_prices = []
    total_position_value = 0.0
    total_unrealized_pnl = 0.0

    for symbol, pos in portfolio["positions"].items():
        lookup = pos.get("token_address") or symbol
        price_info = _fetch_price(lookup)

        if price_info and price_info["price_usd"] > 0:
            current_price = price_info["price_usd"]
            current_value = pos["quantity"] * current_price
            unrealized_pnl = current_value - pos["total_cost"]
            pnl_pct = ((current_price / pos["avg_entry"]) - 1) * 100 if pos["avg_entry"] > 0 else 0

            # Persist live price data into position for Artemis visibility
            pos["current_price"] = current_price
            pos["current_value"] = round(current_value, 2)
            pos["unrealized_pnl"] = round(unrealized_pnl, 2)
            pos["pnl_pct"] = round(pnl_pct, 2)
            pos["last_price_update"] = datetime.now(timezone.utc).isoformat()
        else:
            current_price = 0
            current_value = 0
            unrealized_pnl = -pos["total_cost"]
            pnl_pct = -100

        total_position_value += current_value
        total_unrealized_pnl += unrealized_pnl

        positions_with_prices.append({
            "symbol": symbol,
            "quantity": pos["quantity"],
            "avg_entry": round(pos["avg_entry"], 8),
            "current_price": current_price,
            "cost_basis": round(pos["total_cost"], 2),
            "current_value": round(current_value, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "held_since": pos.get("bought_at", "?"),
        })
        time.sleep(0.3)  # DexScreener rate limiting

    # Persist updated prices to disk so Artemis sees P/L between tool calls
    _save_portfolio(workspace, portfolio)

    # Calculate realized P&L from trade history
    realized_pnl = sum(
        t.get("pnl", 0) for t in portfolio["trade_history"]
        if t.get("type") == "SELL"
    )

    total_trades = len(portfolio["trade_history"])
    winning_sells = [t for t in portfolio["trade_history"]
                     if t.get("type") == "SELL" and t.get("pnl", 0) > 0]
    losing_sells = [t for t in portfolio["trade_history"]
                    if t.get("type") == "SELL" and t.get("pnl", 0) < 0]

    total_portfolio_value = portfolio["cash_balance"] + total_position_value
    total_pnl = total_portfolio_value - portfolio["starting_balance"]
    total_return_pct = (total_pnl / portfolio["starting_balance"]) * 100

    result = {
        "summary": {
            "starting_balance": portfolio["starting_balance"],
            "cash_balance": round(portfolio["cash_balance"], 2),
            "positions_value": round(total_position_value, 2),
            "total_portfolio_value": round(total_portfolio_value, 2),
            "total_pnl": round(total_pnl, 2),
            "total_return_pct": round(total_return_pct, 2),
            "realized_pnl": round(realized_pnl, 2),
            "unrealized_pnl": round(total_unrealized_pnl, 2),
        },
        "stats": {
            "total_trades": total_trades,
            "winning_trades": len(winning_sells),
            "losing_trades": len(losing_sells),
            "win_rate": f"{(len(winning_sells) / max(len(winning_sells)+len(losing_sells), 1)) * 100:.0f}%",
        },
        "positions": positions_with_prices,
        "recent_trades": [
            {"type": t.get("type"), "symbol": t.get("symbol"),
             "amount_usd": round(t.get("amount_usd", 0), 2),
             "pnl": round(t.get("pnl", 0), 2) if t.get("type") == "SELL" else None,
             "timestamp": t.get("timestamp", "")}
            for t in portfolio["trade_history"][-3:]
        ],
        "last_updated": portfolio.get("last_updated", "?"),
    }

    return json.dumps(result, indent=2)


def sim_price_check(workspace: str = "", token: str = "", **kwargs) -> str:
    """Check live price for a token without trading. Use this to research before buying.

    Args:
        token: Token symbol, name, or contract address to look up.

    Returns:
        JSON string with price data from DexScreener.
    """
    if not token:
        return json.dumps({"error": "token parameter is required — provide a symbol, name, or address"})

    price_info = _fetch_price(token)
    if not price_info:
        return json.dumps({"error": f"No price data found for '{token}'"})

    return json.dumps({
        "symbol": price_info["symbol"],
        "name": price_info["name"],
        "chain": price_info["chain"],
        "dex": price_info["dex"],
        "price_usd": price_info["price_usd"],
        "price_change_24h_pct": price_info["price_change_24h"],
        "volume_24h": price_info["volume_24h"],
        "liquidity_usd": price_info["liquidity_usd"],
        "market_cap": price_info["market_cap"],
        "token_address": price_info["token_address"],
    }, indent=2)


# ─── Sim Wallet Faucet ───────────────────────────────────────────────────────

def sim_faucet(workspace: str, amount: float = 0, **kwargs) -> str:
    """Request additional capital from the operator.

    This is NOT free money. Each reload represents Nate extending more trust.
    Default injection is $25 — small and deliberate. If you're hitting the
    faucet frequently, you're trading poorly. Fix the strategy first.

    Args:
        workspace: Jarvis workspace path
        amount: USD to add. If 0 or omitted, adds $25 (not a full reset).

    Returns:
        JSON string confirming the reload.
    """
    portfolio = _load_portfolio(workspace)

    try:
        amount = float(amount) if amount else 0
    except (ValueError, TypeError):
        amount = 0

    old_balance = portfolio["cash_balance"]

    if amount <= 0:
        reload_amount = FAUCET_RELOAD_AMOUNT
    else:
        reload_amount = amount
    # Always ADD to existing balance, never reset
    portfolio["cash_balance"] += reload_amount
    portfolio["starting_balance"] += reload_amount

    portfolio["trade_history"].append({
        "type": "FAUCET",
        "amount_usd": round(reload_amount, 2),
        "old_balance": round(old_balance, 2),
        "new_balance": round(portfolio["cash_balance"], 2),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": "Sim wallet faucet reload",
    })

    _save_portfolio(workspace, portfolio)

    logger.info(
        f"💰 [SIM-FAUCET] Wallet reloaded: ${old_balance:.2f} → "
        f"${portfolio['cash_balance']:.2f} (+${reload_amount:.2f})"
    )

    return json.dumps({
        "status": "FAUCET_OK",
        "old_balance": round(old_balance, 2),
        "added": round(reload_amount, 2),
        "new_balance": round(portfolio["cash_balance"], 2),
        "starting_balance": round(portfolio["starting_balance"], 2),
    }, indent=2)


# ─── Auto Take-Profit / Stop-Loss (runs BEFORE watchdog alerts) ─────────────

def auto_take_profit(workspace: str) -> List[Dict[str, Any]]:
    """Automatically sell positions that hit profit targets or stop-loss.

    This runs every watchdog tick BEFORE alert generation. No LLM needed —
    executes instantly based on price vs entry.

    Rules:
      - Profit ≥ AUTO_TAKE_PROFIT_PCT (12%) → sell 100% (lock the scalp)
      - Loss ≤ AUTO_STOP_LOSS_PCT (-15%) → sell 100% (cut fast)
      - Profit ≥ AUTO_MOON_PARTIAL_PCT (30%) → sell 50% (lock gains, let rest ride)

    Returns list of executed trades.
    """
    if not AUTO_PROFIT_ENABLED:
        return []

    portfolio = _load_portfolio(workspace)
    positions = portfolio.get("positions", {})
    if not positions:
        return []

    executed = []
    symbols_to_process = list(positions.keys())

    for symbol in symbols_to_process:
        if symbol not in portfolio["positions"]:
            continue  # Already sold in this loop

        pos = portfolio["positions"][symbol]
        avg_entry = pos.get("avg_entry", 0)
        if avg_entry <= 0:
            continue

        # Fetch current price
        lookup = pos.get("token_address") or symbol
        try:
            price_info = _fetch_price(lookup)
        except Exception:
            continue

        if not price_info or price_info["price_usd"] <= 0:
            continue

        current_price = price_info["price_usd"]
        pnl_pct = ((current_price / avg_entry) - 1) * 100
        current_value = pos["quantity"] * current_price
        cost_basis = pos["total_cost"]

        action = None
        sell_pct = 0
        reason = ""

        # ── Auto-sell bundled tokens immediately ─────────────────────
        terminal = pos.get("_terminal", {})
        if terminal.get("bundled"):
            reason = (f"[AUTO-BUNDLED-EXIT] {symbol} flagged as bundled: "
                      f"{terminal.get('bundling_reason', 'bundling detected')}. "
                      f"Selling 100% to avoid rug risk.")
            result_json = sim_sell(workspace, symbol, sell_pct=100, reason=reason)
            result = json.loads(result_json)
            if "error" not in result:
                executed.append({
                    "action": "BUNDLED_EXIT",
                    "symbol": symbol,
                    "sell_pct": 100,
                    "pnl_pct": round(pnl_pct, 2),
                    "reason": reason,
                })
                logger.warning(f"🚨 [BUNDLED-EXIT] Auto-sold {symbol} — bundled token")
            continue

        # Skip dust positions — no point trading fractions of a penny
        if current_value < 0.01:
            # Auto-close dust: remove the position entirely
            if current_value < 0.001 and cost_basis < 0.01:
                del portfolio["positions"][symbol]
                _save_portfolio(workspace, portfolio)
                logger.info(f"🧹 [DUST-CLEANUP] Removed {symbol} (value=${current_value:.6f})")
            continue

        # Stop-loss: cut immediately
        if pnl_pct <= AUTO_STOP_LOSS_PCT:
            action = "STOP_LOSS"
            sell_pct = 100
            reason = (f"[AUTO-STOP-LOSS] {symbol} down {pnl_pct:.1f}% "
                      f"(threshold: {AUTO_STOP_LOSS_PCT}%). Cutting losses.")

        # Moon: sell half to lock gains — BUT only once per position.
        # After the first 50% moon sell, the position is marked so it doesn't
        # keep halving into dust on every heartbeat cycle.
        elif pnl_pct >= AUTO_MOON_PARTIAL_PCT and not pos.get("moon_sold"):
            action = "MOON_PARTIAL"
            sell_pct = 50
            reason = (f"[AUTO-MOON] {symbol} up {pnl_pct:.1f}%! "
                      f"Selling 50% to lock gains, letting rest ride.")
            # Mark this position so we only moon-sell ONCE
            pos["moon_sold"] = True
            _save_portfolio(workspace, portfolio)

        # Take profit: sell all
        elif pnl_pct >= AUTO_TAKE_PROFIT_PCT and not pos.get("moon_sold"):
            action = "TAKE_PROFIT"
            sell_pct = 100
            reason = (f"[AUTO-TAKE-PROFIT] {symbol} up {pnl_pct:.1f}% "
                      f"(target: {AUTO_TAKE_PROFIT_PCT}%). Scalp complete.")

        if action and sell_pct > 0:
            result_json = sim_sell(workspace, symbol, sell_pct=sell_pct, reason=reason)
            result = json.loads(result_json)

            if "error" not in result:
                trade_info = {
                    "action": action,
                    "symbol": symbol,
                    "sell_pct": sell_pct,
                    "pnl_pct": round(pnl_pct, 2),
                    "pnl_usd": round(result.get("pnl", 0), 2),
                    "proceeds": round(result.get("proceeds", 0), 2),
                    "reason": reason,
                }
                executed.append(trade_info)
                logger.info(
                    f"⚡ [AUTO-PROFIT] {action}: {symbol} — "
                    f"P/L {pnl_pct:+.1f}% (${result.get('pnl', 0):+.2f}) — "
                    f"sold {sell_pct}% for ${result.get('proceeds', 0):.2f}"
                )

                # Fire hook alert
                try:
                    from repryntt.comms.hooks.trading_parsers import parse_trade_execution
                    from repryntt.comms.hooks.router import get_hook_router
                    hook = parse_trade_execution(trade_info)
                    if hook:
                        get_hook_router().dispatch(hook)
                except Exception:
                    pass
            else:
                logger.warning(f"[AUTO-PROFIT] Sell failed for {symbol}: {result.get('error')}")

        time.sleep(0.3)  # DexScreener rate limiting

    return executed


# ─── Portfolio Watchdog (runs independently of heartbeat) ────────────────────

# Alert thresholds — when to wake Artemis for sell/hold decisions
ALERT_PROFIT_PCT = 5.0       # Position up 5%+ → nudge Artemis to consider taking profits
ALERT_LOSS_PCT = -8.0        # Position down 8%+ → nudge Artemis to consider cutting
ALERT_SPIKE_PCT = 20.0       # Position up 20%+ → urgent moon alert
ALERT_CRASH_PCT = -20.0      # Position down 20%+ → urgent dump alert
WATCHDOG_INTERVAL = 180      # Watchdog runs every 3 min — must catch moves in volatile memecoins
ALERT_COOLDOWN = 300         # Re-alert same symbol after 5 min (tighter for fast markets)

# Track last alert times per symbol to avoid spam
_last_alert_times: Dict[str, float] = {}


def check_portfolio_alerts(workspace: str) -> Optional[Dict[str, Any]]:
    """Check all positions for significant price movements.

    Returns alert dict if action needed, None if everything is calm.
    This is designed to be called frequently (every 10 min) with minimal cost.
    """
    portfolio = _load_portfolio(workspace)
    positions = portfolio.get("positions", {})

    if not positions:
        return None

    alerts = []
    now = time.time()
    prices_updated = False

    for symbol, pos in positions.items():
        # Always fetch current price — persist to position for Artemis visibility
        # _fetch_price checks internal DB first (instant), falls back to DexScreener
        lookup = pos.get("token_address") or symbol
        try:
            price_info = _fetch_price(lookup)
        except Exception:
            continue

        if not price_info or price_info["price_usd"] <= 0:
            # Only sleep between DexScreener API calls, not internal DB reads
            if not price_info or price_info.get("source") != "internal_db":
                time.sleep(0.3)
            continue

        current_price = price_info["price_usd"]
        avg_entry = pos.get("avg_entry", 0)
        if avg_entry <= 0:
            time.sleep(0.3)
            continue

        pnl_pct = ((current_price / avg_entry) - 1) * 100
        current_value = pos["quantity"] * current_price
        cost_basis = pos["total_cost"]
        pnl_usd = current_value - cost_basis

        # Persist live price data into position so Artemis sees P/L
        pos["current_price"] = current_price
        pos["current_value"] = round(current_value, 2)
        pos["unrealized_pnl"] = round(pnl_usd, 2)
        pos["pnl_pct"] = round(pnl_pct, 2)
        pos["last_price_update"] = datetime.now(timezone.utc).isoformat()
        prices_updated = True

        # Check cooldown — only gate ALERTS, not price updates
        last_alert = _last_alert_times.get(symbol, 0)
        if now - last_alert < ALERT_COOLDOWN:
            if price_info.get("source") != "internal_db":
                time.sleep(0.3)
            continue

        # Classify alert level
        alert_level = None
        action_hint = ""

        if pnl_pct >= ALERT_SPIKE_PCT:
            alert_level = "URGENT_MOON"
            action_hint = f"🚀 {symbol} is MOONING — up {pnl_pct:.1f}%! Consider taking profits on some or all."
        elif pnl_pct >= ALERT_PROFIT_PCT:
            alert_level = "TAKE_PROFIT"
            action_hint = f"📈 {symbol} is up {pnl_pct:.1f}% — consider taking partial profits."
        elif pnl_pct <= ALERT_CRASH_PCT:
            alert_level = "URGENT_DUMP"
            action_hint = f"🔴 {symbol} is CRASHING — down {pnl_pct:.1f}%! Consider cutting losses NOW."
        elif pnl_pct <= ALERT_LOSS_PCT:
            alert_level = "STOP_LOSS"
            action_hint = f"⚠️ {symbol} is down {pnl_pct:.1f}% — consider a stop-loss exit."

        if alert_level:
            _last_alert_times[symbol] = now
            alerts.append({
                "symbol": symbol,
                "alert_level": alert_level,
                "action_hint": action_hint,
                "pnl_pct": round(pnl_pct, 2),
                "pnl_usd": round(pnl_usd, 2),
                "current_price": current_price,
                "avg_entry": avg_entry,
                "current_value": round(current_value, 2),
                "cost_basis": round(cost_basis, 2),
                "quantity": pos["quantity"],
            })

        # Only sleep between DexScreener API calls — internal DB reads are instant
        if price_info.get("source") != "internal_db":
            time.sleep(0.3)

    # Persist updated prices to disk
    if prices_updated:
        _save_portfolio(workspace, portfolio)

    if not alerts:
        return None

    # Sort by urgency (urgent first)
    urgency_order = {"URGENT_DUMP": 0, "URGENT_MOON": 1, "STOP_LOSS": 2, "TAKE_PROFIT": 3}
    alerts.sort(key=lambda a: urgency_order.get(a["alert_level"], 99))

    # Build the cold-call prompt for Jarvis
    alert_lines = []
    for a in alerts:
        alert_lines.append(a["action_hint"])

    cash = portfolio.get("cash_balance", 0)

    # ── Dispatch alerts through hook system for external notifications ──
    try:
        from repryntt.comms.hooks.router import get_hook_router
        from repryntt.comms.hooks.message import HookMessage
        router = get_hook_router()
        for a in alerts:
            emoji = {"URGENT_MOON": "🚀", "TAKE_PROFIT": "📈",
                     "URGENT_DUMP": "🔴", "STOP_LOSS": "⚠️"}.get(a["alert_level"], "📊")
            hook = HookMessage(
                source="portfolio_alert",
                event=a["alert_level"].lower(),
                sender="watchdog",
                subject=f"{emoji} {a['symbol']} {a['alert_level']} ({a['pnl_pct']:+.1f}%)",
                body=a["action_hint"],
                priority=1 if "URGENT" in a["alert_level"] else 3,
                session_key=f"portfolio:{a['symbol']}:{a['alert_level']}:{int(now//300)}",
                reply_channel="",
                metadata={
                    "symbol": a["symbol"],
                    "alert_level": a["alert_level"],
                    "pnl_pct": a["pnl_pct"],
                    "pnl_usd": a["pnl_usd"],
                    "current_price": a["current_price"],
                },
            )
            router.dispatch(hook)
        logger.info("Dispatched %d portfolio alert hooks", len(alerts))
    except Exception as e:
        logger.warning("Hook dispatch for portfolio alerts failed: %s", e)

    return {
        "alerts": alerts,
        "alert_count": len(alerts),
        "prompt": (
            f"⚡ PORTFOLIO ALERT — IMMEDIATE ACTION REQUIRED ⚡\n\n"
            f"Your trading simulator positions need attention:\n\n"
            + "\n".join(alert_lines) + "\n\n"
            f"Cash available: ${cash:.2f}\n\n"
            f"Use `sim_portfolio()` to see full state, then `sim_sell()` or `sim_buy()` as needed.\n"
            f"Act on this NOW — don't defer to next heartbeat. This is time-sensitive."
        ),
    }

