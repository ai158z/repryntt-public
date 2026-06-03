"""
SAIGE Jarvis Trading Cycle — Autonomous Sim Trading
=====================================================

This module is called every 5 minutes by the persistent_agents trading cron.
It provides:

  1. `trading_scan` — A Jarvis tool that scores signals, evaluates the
     portfolio, and returns a structured analysis + recommended actions.
     Jarvis reads this and decides what to do (sim_buy / sim_sell).

  2. `run_trading_cycle` — A standalone function called by the trading cron
     that cold-calls Jarvis with fresh market data and asks it to trade.

The key insight: Jarvis already has sim_buy, sim_sell, sim_portfolio, and
sim_price_check tools. This module doesn't trade FOR Jarvis — it gives
Jarvis the signal intelligence and portfolio context it needs to make its
own decisions, then lets it act via its existing tools.

Flow:
  trading cron (every 5 min)
    → run_trading_cycle()
      → score_signals() (from signal_scorer)
      → load sim portfolio + compute P/L
      → build cold-call prompt with market data
      → Jarvis gets prompt + all trading tools
      → Jarvis decides: buy / sell / hold
      → Jarvis acts via sim_buy / sim_sell
      → Log result to daily memory + trade journal
"""

import json
import os
import time
import logging
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger("saige.jarvis_trading")

BASE_DIR = Path(__file__).resolve().parent.parent
TRADING_BOT_DIR = BASE_DIR / "trading_bot"
JARVIS_WORKSPACE = BASE_DIR / "agent_workspaces" / "jarvis"
TRADE_JOURNAL = JARVIS_WORKSPACE / "trade_journal.json"


# ─── Portfolio Analysis ──────────────────────────────────────────────────────

def _load_sim_portfolio() -> Dict[str, Any]:
    """Load Jarvis's sim portfolio and compute current P/L."""
    portfolio_path = JARVIS_WORKSPACE / "sim_portfolio.json"
    if not portfolio_path.exists():
        return {"cash_balance": 300.0, "positions": {}, "trade_history": []}

    try:
        with open(portfolio_path) as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load portfolio: {e}")
        return {"cash_balance": 300.0, "positions": {}, "trade_history": []}


def _get_portfolio_summary(portfolio: Dict) -> Dict[str, Any]:
    """Build a concise portfolio summary with P/L calculations."""
    cash = portfolio.get("cash_balance", 0)
    positions = portfolio.get("positions", {})
    history = portfolio.get("trade_history", [])
    starting = portfolio.get("starting_balance", 300.0)

    total_invested = sum(p.get("total_cost", 0) for p in positions.values())
    position_count = len(positions)

    # Calculate realized P/L from trade history
    realized_pnl = 0
    wins = 0
    losses = 0
    for trade in history:
        pnl = trade.get("pnl_usd", 0)
        realized_pnl += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

    total_trades = wins + losses
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

    # Today's trades
    today_str = date.today().isoformat()
    today_trades = [t for t in history if t.get("timestamp", "").startswith(today_str)]
    today_pnl = sum(t.get("pnl_usd", 0) for t in today_trades)

    # Position details
    position_summaries = []
    for symbol, pos in positions.items():
        cost = pos.get("total_cost", 0)
        qty = pos.get("quantity", 0)
        entry = pos.get("avg_entry", 0)
        position_summaries.append({
            "symbol": symbol,
            "quantity": qty,
            "avg_entry": entry,
            "cost_basis": round(cost, 2),
            "token_address": pos.get("token_address", ""),
        })

    return {
        "cash_balance": round(cash, 2),
        "total_invested": round(total_invested, 2),
        "total_value": round(cash + total_invested, 2),  # Approximate (no live prices here)
        "starting_balance": starting,
        "position_count": position_count,
        "positions": position_summaries,
        "realized_pnl": round(realized_pnl, 2),
        "today_pnl": round(today_pnl, 2),
        "today_trade_count": len(today_trades),
        "all_time_trades": total_trades,
        "win_rate": round(win_rate, 1),
        "wins": wins,
        "losses": losses,
    }


# ─── Trade Journal ───────────────────────────────────────────────────────────

def _load_journal() -> List[Dict]:
    """Load the AI trade journal (decisions log)."""
    if TRADE_JOURNAL.exists():
        try:
            with open(TRADE_JOURNAL) as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _save_journal(journal: List[Dict]):
    """Persist the trade journal."""
    os.makedirs(TRADE_JOURNAL.parent, exist_ok=True)
    tmp = str(TRADE_JOURNAL) + ".tmp"
    with open(tmp, 'w') as f:
        json.dump(journal[-500:], f, indent=2)  # Keep last 500 entries
    os.replace(tmp, str(TRADE_JOURNAL))


def log_trade_decision(action: str, details: Dict[str, Any]):
    """Record a trade decision (buy/sell/hold) to the journal."""
    journal = _load_journal()
    journal.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        **details,
    })
    _save_journal(journal)


# ─── Trading Scan Tool (for Jarvis) ─────────────────────────────────────────

def trading_scan(strategy: str = "balanced") -> str:
    """Scan trading signals, analyze your portfolio, and get AI-scored trade
    recommendations. This is your primary tool for autonomous trading decisions.

    Returns scored signals (ranked by conviction), portfolio P/L status,
    and specific action recommendations based on current market conditions.

    Use the output to decide what to sim_buy or sim_sell.

    Parameters:
        strategy: Trading style — 'aggressive' (lower thresholds, more trades),
                  'balanced' (default, moderate risk), or 'conservative' (high
                  conviction only, fewer trades).
    """
    from brain.signal_scorer import score_signals, MIN_TRADE_SCORE

    # Adjust thresholds by strategy
    strategy = (strategy or "balanced").lower().strip()
    if strategy == "aggressive":
        min_score = max(1.5, MIN_TRADE_SCORE * 0.5)
        max_position_pct = 25   # Max % of portfolio per position
        max_positions = 8
        label = "AGGRESSIVE"
    elif strategy == "conservative":
        min_score = MIN_TRADE_SCORE * 1.5
        max_position_pct = 12
        max_positions = 4
        label = "CONSERVATIVE"
    else:
        min_score = MIN_TRADE_SCORE
        max_position_pct = 18
        max_positions = 6
        label = "BALANCED"

    # 1. Score signals
    scored = score_signals(max_age_s=1800)  # 30 min window
    candidates = [t for t in scored if t["score"] >= min_score]

    # 2. Load portfolio
    portfolio = _load_sim_portfolio()
    pf_summary = _get_portfolio_summary(portfolio)

    # 3. Compute position sizing
    cash = pf_summary["cash_balance"]
    total_value = pf_summary["total_value"]
    max_trade_usd = total_value * (max_position_pct / 100) if total_value > 0 else 20

    # 4. Check existing positions against current signals
    held_addresses = set()
    for pos in pf_summary["positions"]:
        held_addresses.add(pos.get("token_address", ""))

    # 5. Build buy recommendations (skip tokens we already hold)
    buy_recs = []
    for token in candidates[:10]:
        addr = token["address"]
        if addr in held_addresses:
            continue

        # Size recommendation based on conviction
        score = token["score"]
        if score >= 8:
            size_pct = max_position_pct
            urgency = "NOW"
        elif score >= 5:
            size_pct = max_position_pct * 0.65
            urgency = "SOON"
        else:
            size_pct = max_position_pct * 0.35
            urgency = "WATCH"

        rec_amount = min(total_value * (size_pct / 100), cash * 0.9)  # Don't go all-in
        rec_amount = max(rec_amount, 1.0)  # At least $1

        buy_recs.append({
            "address": addr,
            "score": token["score"],
            "grade": token["grade"],
            "recommended_usd": round(rec_amount, 2),
            "urgency": urgency,
            "reasoning": token["reasoning"],
            "risk_flags": token["risk_flags"],
            "market_cap": token["market_cap"],
            "price_change_5m": token["price_change_5m"],
        })

    # 6. Build sell recommendations for holdings that are in danger
    sell_recs = []
    for pos in pf_summary["positions"]:
        addr = pos.get("token_address", "")
        # Check if this token has bearish signals
        for token in scored:
            if token["address"] == addr and token["score"] < 0:
                sell_recs.append({
                    "symbol": pos["symbol"],
                    "address": addr,
                    "reason": f"Bearish signals detected (score: {token['score']})",
                    "recommendation": "SELL 100%",
                })
                break

    # 7. Build final response
    result = {
        "strategy": label,
        "scan_time": datetime.now(timezone.utc).isoformat(),

        # Market conditions
        "market": {
            "total_signals_in_window": len(scored),
            "tradeable_candidates": len(candidates),
            "buy_candidates": len(buy_recs),
        },

        # Portfolio status
        "portfolio": pf_summary,

        # Recommendations
        "buy_recommendations": buy_recs[:5],
        "sell_recommendations": sell_recs,

        # Position sizing rules
        "rules": {
            "max_trade_usd": round(max_trade_usd, 2),
            "max_positions": max_positions,
            "available_cash": round(cash, 2),
            "can_buy": cash >= 1.0 and pf_summary["position_count"] < max_positions,
        },
    }

    return json.dumps(result, indent=2)


# ─── Cold-Call Prompt Builder ────────────────────────────────────────────────

def build_trading_coldcall_prompt() -> Optional[str]:
    """Build a focused trading prompt for the 5-min cron cold-call.

    Returns None if there's nothing actionable (no signals, quiet market).
    Returns a prompt string if there are candidates worth trading.
    """
    from brain.signal_scorer import score_signals, MIN_TRADE_SCORE

    scored = score_signals(max_age_s=1800)
    candidates = [t for t in scored if t["score"] >= MIN_TRADE_SCORE]

    portfolio = _load_sim_portfolio()
    pf = _get_portfolio_summary(portfolio)
    cash = pf["cash_balance"]
    position_count = pf["position_count"]

    # Decide if this cold-call is worth Jarvis's time
    has_buy_candidates = len(candidates) > 0 and cash >= 1.0 and position_count < 8
    has_positions_to_check = position_count > 0

    if not has_buy_candidates and not has_positions_to_check:
        logger.info("[TRADING] No actionable signals and no positions — skipping cold-call")
        return None

    # Build concise prompt
    parts = ["📊 **TRADING CYCLE — 5-Minute Scan**\n"]

    if has_buy_candidates:
        parts.append(f"**{len(candidates)} tradeable signal(s)** detected. Top picks:\n")
        for i, c in enumerate(candidates[:3], 1):
            flags = f" ⚠️ {', '.join(c['risk_flags'])}" if c['risk_flags'] else ""
            parts.append(
                f"  {i}. `{c['address'][:16]}...` — Score: {c['score']:.1f} "
                f"({c['grade']}) — MCap: ${c['market_cap']:,.0f} — "
                f"5m: {c['price_change_5m']:+.1f}%{flags}"
            )
        parts.append("")

    if has_positions_to_check:
        parts.append(f"**{position_count} open position(s)** to monitor.\n")
        for pos in pf["positions"][:5]:
            parts.append(f"  • {pos['symbol']} — ${pos['cost_basis']:.2f} invested")
        parts.append("")

    parts.append(f"**Cash: ${cash:.2f}** | Positions: {position_count}/8")
    parts.append(f"Today: {pf['today_trade_count']} trades, ${pf['today_pnl']:+.2f} P/L\n")

    parts.append(
        "**Your job this cycle:**\n"
        "1. Call `trading_scan()` to get full scored analysis\n"
        "2. Check prices with `sim_price_check(token=...)` for any candidates\n"
        "3. Execute trades: `sim_buy(...)` or `sim_sell(...)`\n"
        "4. Check `sim_portfolio()` to verify\n"
        "5. Record your decisions and reasoning in daily memory\n\n"
        "Think like a degen, analyze like a quant. "
        "Cut losers fast, let winners run. Target 5-15% gains per position."
    )

    return "\n".join(parts)


# ─── CLI Test Entry Point ────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("\n=== Trading Scan Output ===\n")
    result = trading_scan()
    parsed = json.loads(result)
    print(json.dumps(parsed, indent=2))

    print("\n=== Cold-Call Prompt ===\n")
    prompt = build_trading_coldcall_prompt()
    if prompt:
        print(prompt)
    else:
        print("(No actionable prompt — market quiet)")
