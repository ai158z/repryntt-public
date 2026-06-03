"""
Andrew Gem Hunter — Long-Hold Research-Driven Trading
=======================================================

Replaces the old 5-minute scalp cold-calls with an HOURLY research cycle
that uses Andrew (Gemini API) to find gem tokens worth holding.

Strategy:
  - Andrew researches tokens: narrative, community, fundamentals, on-chain data
  - Buys gems and HOLDS for hours/days, not minutes
  - Profit targets: 50% (partial sell), 100%+ (take profit), -20% (stop loss)
  - Sells are triggered either algorithmically (auto_take_profit) or by
    Andrew cold-call when targets are near

Frequency:
  - Research + buy cycle: once per hour (saves API calls)
  - Profit-target sell check: every 15 minutes (lightweight, mostly algorithmic)

Integration:
  - Called from evolution loop on hourly timer
  - Uses call_jarvis bridge for Andrew research
  - Uses existing sim_buy/sim_sell/sim_portfolio for execution
  - Micro-chain trader (Qwen2.5) still handles quick scalps separately
"""

import json
import os
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("saige.gem_hunter")

BASE_DIR = Path(__file__).resolve().parent.parent
JARVIS_WORKSPACE = Path.home() / ".repryntt" / "workspace" / "agents" / "operator"
GEM_WATCHLIST = JARVIS_WORKSPACE / "gem_watchlist.json"
GEM_JOURNAL = JARVIS_WORKSPACE / "gem_journal.json"

# ── Gem Strategy Config ──
GEM_PROFIT_TARGET_PCT = 50.0       # Partial sell at 50% profit
GEM_MOON_TARGET_PCT = 100.0        # Full sell at 100%+ profit (2x)
GEM_STOP_LOSS_PCT = -20.0          # Cut at -20% (wider than scalps — give room)
GEM_PARTIAL_SELL_PCT = 50           # Sell 50% at first target, let rest ride
GEM_MAX_POSITIONS = 5               # Hold up to 5 gems at once
GEM_MAX_BUY_USD = 100.0            # Max per gem position
GEM_MIN_BUY_USD = 20.0             # Minimum gem position
GEM_MIN_MCAP = 50_000              # Don't buy below $50k mcap (rug risk)
GEM_MAX_MCAP = 10_000_000          # Don't buy above $10M (already pumped)
GEM_RESEARCH_INTERVAL_SEC = 3600   # 1 hour between research cycles
GEM_SELL_CHECK_INTERVAL_SEC = 900  # 15 min between profit-target checks


# ════════════════════════════════════════════════════════════════════
# WATCHLIST MANAGEMENT
# ════════════════════════════════════════════════════════════════════

def _load_watchlist() -> Dict:
    """Load the gem watchlist."""
    if GEM_WATCHLIST.exists():
        try:
            with open(GEM_WATCHLIST) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"gems": [], "last_research": None, "last_sell_check": None}


def _save_watchlist(data: Dict):
    """Persist the gem watchlist."""
    GEM_WATCHLIST.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(GEM_WATCHLIST) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, str(GEM_WATCHLIST))


def _log_gem_decision(action: str, details: Dict):
    """Log a gem hunting decision."""
    entries = []
    if GEM_JOURNAL.exists():
        try:
            with open(GEM_JOURNAL) as f:
                entries = json.load(f)
        except (json.JSONDecodeError, IOError):
            entries = []

    entries.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        **details,
    })

    # Keep last 200 entries
    entries = entries[-200:]
    tmp = str(GEM_JOURNAL) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=2)
    os.replace(tmp, str(GEM_JOURNAL))


# ════════════════════════════════════════════════════════════════════
# RESEARCH CYCLE — Uses Andrew (Gemini API) via call_jarvis
# ════════════════════════════════════════════════════════════════════

def _build_research_prompt(portfolio_summary: Dict, current_positions: List[Dict]) -> str:
    """Build the Andrew research prompt for gem hunting."""
    cash = portfolio_summary.get("cash_balance", 0)
    pos_count = len(current_positions)

    held_summary = "None" if not current_positions else "\n".join(
        f"  - {p.get('symbol','?')}: ${p.get('cost_basis',0):.0f} invested"
        for p in current_positions[:5]
    )

    prompt = f"""🔍 **GEM HUNTER RESEARCH CYCLE**

You are researching Solana memecoin gems to buy and HOLD (not scalp).
This is a long-hold strategy — we want tokens with real potential, not quick pumps.

**Portfolio Status:**
- Cash available: ${cash:.0f}
- Current gem positions ({pos_count}/{GEM_MAX_POSITIONS}): 
{held_summary}
- Budget per gem: ${GEM_MIN_BUY_USD}-${GEM_MAX_BUY_USD}

**Research Task:**
1. Use `trading_scan()` to see what the signal scanner found
2. For the top 3-5 candidates, use the web search tool to research:
   - What is the token's narrative/story? (AI, memes, utility?)
   - Does it have an active community? (Twitter/X, Telegram, Discord)
   - Is the dev team doxxed or is the contract renounced?
   - How old is the token? (brand new = risky, 1-7 days = sweet spot)
   - Check DexScreener for liquidity depth and holder distribution
3. Use `sim_price_check(token=ADDRESS)` to get live data

**What makes a GEM (buy criteria):**
- Strong narrative that hasn't peaked yet
- Market cap ${GEM_MIN_MCAP/1000:.0f}K - ${GEM_MAX_MCAP/1_000_000:.0f}M (room to grow)
- Growing community, not just bots
- Reasonable liquidity (>$10K) so we can exit
- Token age 1-14 days (early but not too early)

**Output Required:**
For each gem you want to buy, execute:
  `sim_buy(token=TOKEN_ADDRESS, amount_usd=AMOUNT, reason="GEM: [your research summary]")`

For any existing positions that look dead (community gone, dev rugged, narrative died):
  `sim_sell(token=SYMBOL, sell_pct=100, reason="GEM EXIT: [reason]")`

If nothing looks good, that's fine — we only buy when conviction is high.
Patience is key for gem hunting. Quality over quantity.

**Rules:**
- Max {GEM_MAX_POSITIONS} gem positions total
- ${GEM_MIN_BUY_USD}-${GEM_MAX_BUY_USD} per position
- MCap must be ${GEM_MIN_MCAP/1000:.0f}K-${GEM_MAX_MCAP/1_000_000:.0f}M
- We HOLD — profit target is +{GEM_PROFIT_TARGET_PCT:.0f}% (partial) / +{GEM_MOON_TARGET_PCT:.0f}% (full)
- Stop loss at {GEM_STOP_LOSS_PCT:.0f}%
"""
    return prompt


def run_gem_research(brain_system) -> Dict:
    """Run the hourly Andrew gem research cycle.

    Uses the call_jarvis bridge to have Andrew (Gemini) research
    tokens with web search, then buy the best ones.

    Args:
        brain_system: BrainSystem instance (needed for call_jarvis bridge)

    Returns:
        Summary dict with research results.
    """
    cycle_start = time.time()
    logger.info("🔍 Andrew gem hunter: Starting hourly research cycle")

    # Load portfolio
    from repryntt.trading.trading_simulator import sim_portfolio
    portfolio = json.loads(sim_portfolio(str(JARVIS_WORKSPACE)))
    summary = portfolio.get("summary", {})
    positions = portfolio.get("positions", [])
    cash = summary.get("cash_balance", 0)

    # Check if we have room for more gems
    gem_positions = [p for p in positions if p.get("reason", "").startswith("GEM:")]
    all_positions = positions  # Count all positions for max check

    if cash < GEM_MIN_BUY_USD and len(all_positions) >= GEM_MAX_POSITIONS:
        logger.info("🔍 No cash and max positions — skipping research")
        return {
            "action": "skipped",
            "reason": "No cash or at max positions",
            "cash": cash,
            "positions": len(all_positions),
        }

    # Build research prompt
    prompt = _build_research_prompt(summary, positions)

    # Cold-call Andrew via call_jarvis bridge
    try:
        result_json = brain_system._call_jarvis_bridge(prompt=prompt)
        result = json.loads(result_json)

        if result.get("success"):
            response = result.get("jarvis_response", "")
            tools_used = result.get("tools_used", 0)
            tool_names = result.get("tool_names", [])
            elapsed = result.get("elapsed_seconds", 0)

            # Count trades made (look for sim_buy/sim_sell in tool calls)
            buys_made = sum(1 for t in tool_names if "sim_buy" in t)
            sells_made = sum(1 for t in tool_names if "sim_sell" in t)

            logger.info(
                f"🔍 Andrew gem research complete: {buys_made} buys, "
                f"{sells_made} sells, {tools_used} tools, {elapsed:.0f}s"
            )

            _log_gem_decision("research_cycle", {
                "buys_made": buys_made,
                "sells_made": sells_made,
                "tools_used": tools_used,
                "tool_names": tool_names,
                "elapsed_sec": elapsed,
                "response_preview": response[:300],
            })

            # Update watchlist timestamp
            wl = _load_watchlist()
            wl["last_research"] = datetime.now(timezone.utc).isoformat()
            _save_watchlist(wl)

            return {
                "action": "researched",
                "buys_made": buys_made,
                "sells_made": sells_made,
                "tools_used": tools_used,
                "elapsed_sec": round(time.time() - cycle_start, 1),
            }
        else:
            error = result.get("error", "unknown")
            logger.warning(f"🔍 Andrew gem research failed: {error}")
            return {"action": "failed", "error": error}

    except Exception as e:
        logger.error(f"🔍 Gem research error: {e}", exc_info=True)
        return {"action": "error", "error": str(e)}


# ════════════════════════════════════════════════════════════════════
# PROFIT-TARGET SELL CHECK — Mostly algorithmic, Andrew for edge cases
# ════════════════════════════════════════════════════════════════════

def check_gem_profits(brain_system=None) -> Dict:
    """Check gem positions against profit targets. Runs every 15 minutes.

    Algorithmic first — auto-sells at targets.
    If a position is near a target (within 5%), optionally cold-calls
    Andrew for a nuanced sell/hold decision.

    Args:
        brain_system: Optional BrainSystem for Andrew cold-calls on edge cases.

    Returns:
        Summary of actions taken.
    """
    from repryntt.trading.trading_simulator import sim_portfolio, sim_sell, sim_price_check

    logger.info("💎 Gem profit check: Scanning positions")

    portfolio = json.loads(sim_portfolio(str(JARVIS_WORKSPACE)))
    positions = portfolio.get("positions", [])

    if not positions:
        return {"action": "no_positions", "sells": 0}

    actions_taken = []

    for pos in positions:
        symbol = pos.get("symbol", "?")
        address = pos.get("token_address", "")
        entry_price = pos.get("avg_entry", 0)
        cost = pos.get("total_cost", 0)
        pnl_pct = pos.get("pnl_pct", 0)

        if entry_price <= 0:
            continue

        # Get live price for accurate P/L
        try:
            price_data = json.loads(sim_price_check(str(JARVIS_WORKSPACE), address or symbol))
            live_price = price_data.get("price_usd", 0)
            if live_price > 0:
                pnl_pct = ((live_price / entry_price) - 1) * 100
        except Exception:
            pass  # Use portfolio's cached P/L

        action = None
        sell_pct = 0
        reason = ""

        # ── Hard stop loss ──
        if pnl_pct <= GEM_STOP_LOSS_PCT:
            action = "STOP_LOSS"
            sell_pct = 100
            reason = f"GEM STOP-LOSS: {symbol} down {pnl_pct:.1f}% (limit: {GEM_STOP_LOSS_PCT}%)"

        # ── Moon target: sell all at 100%+ ──
        elif pnl_pct >= GEM_MOON_TARGET_PCT:
            action = "MOON_EXIT"
            sell_pct = 100
            reason = f"GEM MOON: {symbol} up {pnl_pct:.1f}%! Taking full profit (target: {GEM_MOON_TARGET_PCT}%)"

        # ── First target: partial sell at 50% ──
        elif pnl_pct >= GEM_PROFIT_TARGET_PCT:
            action = "PARTIAL_PROFIT"
            sell_pct = GEM_PARTIAL_SELL_PCT
            reason = f"GEM PROFIT: {symbol} up {pnl_pct:.1f}%. Selling {GEM_PARTIAL_SELL_PCT}% (target: {GEM_PROFIT_TARGET_PCT}%)"

        # ── Near target (within 5% of profit target) — ask Andrew ──
        elif pnl_pct >= (GEM_PROFIT_TARGET_PCT - 10) and brain_system:
            # Edge case: close to profit target. Ask Andrew whether to hold or sell.
            andrew_decision = _ask_andrew_sell_decision(brain_system, pos, pnl_pct)
            if andrew_decision.get("sell"):
                action = "ANDREW_SELL"
                sell_pct = andrew_decision.get("sell_pct", GEM_PARTIAL_SELL_PCT)
                reason = f"GEM ANDREW SELL: {andrew_decision.get('reason', 'Andrew recommended exit')}"

        # Execute sell if triggered
        if action and sell_pct > 0:
            logger.info(f"💎 {action}: {symbol} ({pnl_pct:+.1f}%) — selling {sell_pct}%")
            try:
                sell_result = json.loads(
                    sim_sell(str(JARVIS_WORKSPACE), symbol, sell_pct, reason)
                )
                actions_taken.append({
                    "symbol": symbol,
                    "action": action,
                    "sell_pct": sell_pct,
                    "pnl_pct": round(pnl_pct, 1),
                    "reason": reason,
                    "result": sell_result.get("status", "unknown"),
                })
                _log_gem_decision(action.lower(), {
                    "symbol": symbol,
                    "pnl_pct": round(pnl_pct, 1),
                    "sell_pct": sell_pct,
                    "reason": reason,
                })
            except Exception as e:
                logger.error(f"💎 Sell failed for {symbol}: {e}")
        else:
            logger.debug(f"💎 HOLD: {symbol} at {pnl_pct:+.1f}%")

    # Update last check time
    wl = _load_watchlist()
    wl["last_sell_check"] = datetime.now(timezone.utc).isoformat()
    _save_watchlist(wl)

    return {
        "action": "checked",
        "positions_checked": len(positions),
        "sells": len(actions_taken),
        "actions": actions_taken,
    }


def _ask_andrew_sell_decision(brain_system, position: Dict, pnl_pct: float) -> Dict:
    """Ask Andrew whether to sell a position that's near the profit target.

    Returns: {"sell": bool, "sell_pct": int, "reason": str}
    """
    symbol = position.get("symbol", "?")
    address = position.get("token_address", "")
    cost = position.get("total_cost", 0)
    current_value = position.get("current_value", 0)

    prompt = f"""💎 **GEM SELL DECISION** — Quick check needed

{symbol} is up {pnl_pct:+.1f}% (close to our {GEM_PROFIT_TARGET_PCT}% target).
Invested: ${cost:.0f} | Current value: ${current_value:.0f}

Quick research:
1. Check the token's current momentum with `sim_price_check(token="{address}")`
2. Is the narrative still alive? Quick web search if needed.

Then decide:
- If momentum is still strong and narrative growing → reply "HOLD"
- If momentum fading or volume dropping → execute `sim_sell(token="{symbol}", sell_pct={GEM_PARTIAL_SELL_PCT}, reason="GEM: Taking partial profit at {pnl_pct:+.1f}%")`

Be brief — this is a quick check, not deep research."""

    try:
        result_json = brain_system._call_jarvis_bridge(prompt=prompt)
        result = json.loads(result_json)

        if result.get("success"):
            tool_names = result.get("tool_names", [])
            # If Andrew executed a sim_sell, it already sold
            if any("sim_sell" in t for t in tool_names):
                return {"sell": True, "sell_pct": GEM_PARTIAL_SELL_PCT, "reason": "Andrew executed sell directly"}
            # Otherwise Andrew chose to hold
            return {"sell": False, "reason": "Andrew chose to hold"}
        else:
            return {"sell": False, "reason": f"Andrew unavailable: {result.get('error', '')}"}
    except Exception as e:
        logger.warning(f"Andrew sell decision failed: {e}")
        return {"sell": False, "reason": f"Error: {e}"}


# ════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT — Called from evolution loop
# ════════════════════════════════════════════════════════════════════

def run_gem_cycle(brain_system, force_research: bool = False) -> Dict:
    """Run the gem hunter cycle. Called from the evolution loop.

    - Research (buys): once per hour
    - Profit checks (sells): every 15 minutes

    Args:
        brain_system: BrainSystem instance for Andrew bridge access.
        force_research: Skip the hourly timer and research now.

    Returns:
        Summary of what happened.
    """
    wl = _load_watchlist()
    now = time.time()
    results = {}

    # ── Sell check: every 15 minutes ──
    last_sell = wl.get("last_sell_check")
    sell_due = True
    if last_sell:
        try:
            last_sell_time = datetime.fromisoformat(last_sell).timestamp()
            sell_due = (now - last_sell_time) >= GEM_SELL_CHECK_INTERVAL_SEC
        except (ValueError, TypeError):
            sell_due = True

    if sell_due:
        results["sell_check"] = check_gem_profits(brain_system)
    else:
        results["sell_check"] = {"action": "skipped", "reason": "Not due yet"}

    # ── Research cycle: once per hour ──
    last_research = wl.get("last_research")
    research_due = force_research
    if not research_due:
        if last_research:
            try:
                last_research_time = datetime.fromisoformat(last_research).timestamp()
                research_due = (now - last_research_time) >= GEM_RESEARCH_INTERVAL_SEC
            except (ValueError, TypeError):
                research_due = True
        else:
            research_due = True  # Never researched

    if research_due:
        results["research"] = run_gem_research(brain_system)
    else:
        results["research"] = {"action": "skipped", "reason": "Not due yet (hourly)"}

    return results


# ════════════════════════════════════════════════════════════════════
# STANDALONE ENTRY POINT
# ════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    print("Gem hunter requires BrainSystem for Andrew access.")
    print("Use: from repryntt.trading.gem_hunter import run_gem_cycle")
    print("     run_gem_cycle(brain_system, force_research=True)")
