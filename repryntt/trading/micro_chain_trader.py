"""
Micro-Chain Trader — Local LLM Sequential Trading Pipeline
===========================================================

Runs Solana memecoin trading decisions entirely on the local 3B model
(Qwen2.5-3B-Instruct or kappa-3-phi) via llama.cpp at localhost:8080.

Architecture:
  signal_scorer (algorithmic) → micro-chain LLM decisions → sim_buy/sim_sell

Each trading decision is decomposed into tiny sequential chain links,
each fitting comfortably in ~600-800 tokens (well under 4096 context).

Chain Links:
  1. SCREEN  — Per-candidate: buy or skip? (300 tokens in, 50 out)
  2. VALIDATE — Confirm with live price check (200 tokens in, 30 out)
  3. EXECUTE  — Pure code: sim_buy() (no LLM needed)
  4. REVIEW   — Portfolio health check (400 tokens in, 100 out)

QLoRA Training Data:
  Every decision is recorded. After trades resolve (profit/loss),
  winning decisions become high-quality training examples that feed
  into the existing self-evolution pipeline.

Integration:
  - Plugs into existing signal_scorer.py (pure algorithmic scoring)
  - Uses existing trading_simulator.py (sim_buy, sim_sell, sim_portfolio)
  - Runs on MasterAIQueue (serialized localhost:8080 access)
  - Called from evolution loop or standalone cron
"""

import json
import os
import re
import time
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("micro_chain_trader")

# ── Paths ──
SAIGE_ROOT = Path(__file__).resolve().parent.parent
JARVIS_WORKSPACE = Path.home() / ".repryntt" / "workspace" / "agents" / "operator"
TRADING_DATA_DIR = SAIGE_ROOT / "data"
TRADE_DECISIONS_LOG = TRADING_DATA_DIR / "trade_decisions.jsonl"
TRAINING_DATA_FILE = TRADING_DATA_DIR / "training_data.json"

# ── LLM Config ──
from repryntt.paths import local_llm_endpoint as _llm_ep
LLM_ENDPOINT = _llm_ep()
LLM_MAX_TOKENS = 150  # Terse decision outputs
LLM_TEMPERATURE = 0.3  # Low creativity — we want consistent decisions
LLM_TIMEOUT = 30  # seconds

# ── Trading Config ──
MIN_SCORE_TO_SCREEN = 3.0          # signal_scorer minimum
MIN_SCORE_STRONG = 8.0             # skip LLM, auto-execute
MAX_CANDIDATES_PER_CYCLE = 5       # don't overwhelm the model
CYCLE_INTERVAL_SEC = 300           # 5 minutes between trading cycles
PRICE_DRIFT_REJECT_PCT = 10.0     # reject if price moved >10% since signal
PORTFOLIO_REVIEW_INTERVAL = 3      # review portfolio every N cycles

# ── Position Sizing ──
# Fixed $50 buy-ins until portfolio reaches $500.
# At $50, you need 3x returns to make meaningful profit.
# Quality > quantity. Patience > volume.
POSITION_SIZING = {
    "STRONG_BUY": (50, 50),   # $50 fixed — no variation until $500 portfolio
    "BUY": (50, 50),          # $50 fixed
    "WEAK_BUY": (50, 50),     # $50 fixed — don't trade weak buys at this size
}

# ── Module State ──
_cycle_count = 0
_lock = threading.Lock()


# ════════════════════════════════════════════════════════════════════
# LLM INTERFACE — Direct localhost:8080 calls
# ════════════════════════════════════════════════════════════════════

def _llm_call(prompt: str, max_tokens: int = LLM_MAX_TOKENS,
              temperature: float = LLM_TEMPERATURE) -> Optional[str]:
    """Send a single prompt to localhost:8080 and return the text response.

    Uses a minimal system message — no identity, no consciousness,
    no bootstrap files. Just a 1-line trading persona.
    """
    try:
        resp = requests.post(
            LLM_ENDPOINT,
            json={
                "messages": [
                    {"role": "system", "content": "You are a veteran Solana memecoin scalp trader. Evaluate tokens by mcap, volume, buy/sell ratio, holder risk, and momentum. Answer concisely in the exact format requested."},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
                "top_p": 0.9,
                "stop": ["\n\n\n"],  # prevent rambling
            },
            timeout=LLM_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return content.strip() if content else None
    except requests.exceptions.ConnectionError:
        logger.warning("LLM server not reachable at localhost:8080")
        return None
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        return None


def _llm_healthy() -> bool:
    """Quick health check for localhost:8080."""
    try:
        r = requests.get("http://localhost:8080/health", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


# ════════════════════════════════════════════════════════════════════
# CHAIN LINK 1: SCREEN — Should we buy this candidate?
# ════════════════════════════════════════════════════════════════════

def _chain_screen(candidate: Dict, cash: float, positions: List[Dict]) -> Dict:
    """Ask the LLM whether to buy a scored candidate.

    Returns: {"decision": "BUY"|"SKIP", "amount": float, "reason": str}
    """
    score = candidate.get("score", 0)
    grade = candidate.get("grade", "HOLD")
    addr = candidate.get("address", "???")[:16]
    mcap = candidate.get("market_cap", 0)
    change_5m = candidate.get("price_change_5m", 0)
    risk_flags = candidate.get("risk_flags", [])
    sig_count = candidate.get("signal_count", 0)
    reasoning = candidate.get("reasoning", "")[:120]

    # ── Extra data for veteran-trader evaluation ──
    buy_vol = candidate.get("buy_volume_5m", 0)
    sell_vol = candidate.get("sell_volume_5m", 0)
    pool_buys = candidate.get("pool_buys_5m", 0)
    pool_sells = candidate.get("pool_sells_5m", 0)
    sig_types = candidate.get("signal_types", {})

    # Buy/sell ratio (txn count)
    bs_ratio = round(pool_buys / max(pool_sells, 1), 1)
    # Volume as % of mcap
    total_vol = buy_vol + sell_vol
    vol_mcap_pct = round((total_vol / max(mcap, 1)) * 100, 1) if mcap else 0
    # Compact signal-type summary (e.g. "Momentum:5 TP1:3 HigherLow:2")
    sig_type_str = " ".join(f"{k.replace(' ','')[:8]}:{v}" for k, v in
                            sorted(sig_types.items(), key=lambda x: -x[1])[:4])

    # Determine budget range for this tier
    if score >= 8:
        lo, hi = POSITION_SIZING["STRONG_BUY"]
    elif score >= 5:
        lo, hi = POSITION_SIZING["BUY"]
    else:
        lo, hi = POSITION_SIZING["WEAK_BUY"]

    pos_summary = "None" if not positions else ", ".join(
        f"{p.get('symbol','?')}: ${p.get('current_value',0):.0f} ({p.get('pnl_pct',0):+.1f}%)"
        for p in positions[:3]
    )
    risk_str = ", ".join(risk_flags) if risk_flags else "none"

    prompt = f"""Cash: ${cash:.0f} | Positions: {pos_summary}

Token: {addr}...
Score: {score:.1f} ({grade}) | Signals: {sig_count} [{sig_type_str}]
MCap: ${mcap:,.0f} | 5m: {change_5m:+.1f}% | Vol/MCap: {vol_mcap_pct}%
Buy/Sell ratio: {bs_ratio} ({pool_buys}b/{pool_sells}s) | BuyVol: ${buy_vol:,.0f}
Risk: {risk_str}
Analysis: {reasoning}

RULES: SKIP if mcap<100K, concentrated holders, bundled supply, vol/mcap<5%, or buy/sell<0.7.
Prefer: higher lows signals, ratio>1.5, vol/mcap>20%, no risk flags.
Budget: ${lo}-${hi}
Reply ONE line: BUY <amt> <reason 15 words> OR SKIP <reason 15 words>"""

    response = _llm_call(prompt)
    if not response:
        return {"decision": "SKIP", "amount": 0, "reason": "LLM unreachable"}

    # Parse response
    response_clean = response.strip().split("\n")[0].strip()

    if response_clean.upper().startswith("BUY"):
        # Extract amount
        amount_match = re.search(r'BUY\s+\$?(\d+(?:\.\d+)?)', response_clean, re.IGNORECASE)
        amount = float(amount_match.group(1)) if amount_match else (lo + hi) / 2
        amount = max(lo, min(hi, amount))  # clamp to tier range
        reason_part = re.sub(r'^BUY\s+\$?\d+(?:\.\d+)?\s*', '', response_clean, flags=re.IGNORECASE).strip()
        return {
            "decision": "BUY",
            "amount": round(amount, 2),
            "reason": reason_part[:100] or f"Score {score:.1f} {grade}",
            "llm_raw": response_clean,
        }
    else:
        reason_part = re.sub(r'^SKIP\s*', '', response_clean, flags=re.IGNORECASE).strip()
        return {
            "decision": "SKIP",
            "amount": 0,
            "reason": reason_part[:100] or "Model chose to skip",
            "llm_raw": response_clean,
        }


# ════════════════════════════════════════════════════════════════════
# CHAIN LINK 2: VALIDATE — Confirm with live price
# ════════════════════════════════════════════════════════════════════

def _chain_validate(candidate: Dict, buy_amount: float) -> Dict:
    """Check live price drift and ask LLM to confirm or reject.

    Returns: {"confirmed": bool, "live_price": float, "reason": str}
    """
    from repryntt.trading.trading_simulator import sim_price_check

    workspace = str(JARVIS_WORKSPACE)
    address = candidate.get("address", "")
    signal_price = candidate.get("latest_price", 0)

    # Get live price
    price_result = json.loads(sim_price_check(workspace=workspace, token=address))
    if "error" in price_result:
        return {"confirmed": False, "live_price": 0, "reason": f"Price check failed: {price_result['error']}"}

    live_price = float(price_result.get("price_usd", 0))
    if live_price <= 0 or signal_price <= 0:
        return {"confirmed": False, "live_price": live_price, "reason": "Invalid price data"}

    drift_pct = ((live_price - signal_price) / signal_price) * 100
    volume_24h = price_result.get("volume_24h", 0)
    liquidity = price_result.get("liquidity_usd", 0)

    # Hard reject if price drifted too much
    if abs(drift_pct) > PRICE_DRIFT_REJECT_PCT:
        return {
            "confirmed": False,
            "live_price": live_price,
            "drift_pct": drift_pct,
            "reason": f"Price drifted {drift_pct:+.1f}% since signal — too risky",
        }

    mcap = candidate.get("market_cap", 0)
    vol_mcap_pct = round((volume_24h / max(mcap, 1)) * 100, 1) if mcap else 0

    prompt = f"""Confirm trade: BUY ${buy_amount:.0f} of token.
Signal: ${signal_price:.8f} → Live: ${live_price:.8f} (drift: {drift_pct:+.1f}%)
24h vol: ${volume_24h:,.0f} | Liq: ${liquidity:,.0f} | Vol/MCap: {vol_mcap_pct}%
REJECT if: drift>8% (chasing), liq<$5K (rug risk), vol/mcap<5% (dead).
Reply EXACTLY: CONFIRM or REJECT <reason in 10 words>"""

    response = _llm_call(prompt, max_tokens=50)
    if not response:
        # If LLM is down, use algorithmic fallback
        if abs(drift_pct) < 5 and liquidity > 5000:
            return {"confirmed": True, "live_price": live_price, "drift_pct": drift_pct, "reason": "Auto-confirmed (LLM down, price stable)"}
        return {"confirmed": False, "live_price": live_price, "reason": "LLM down, price unstable"}

    confirmed = response.strip().upper().startswith("CONFIRM")
    reason = re.sub(r'^(CONFIRM|REJECT)\s*', '', response.strip(), flags=re.IGNORECASE).strip()

    return {
        "confirmed": confirmed,
        "live_price": live_price,
        "drift_pct": drift_pct,
        "reason": reason[:100] or ("Confirmed" if confirmed else "Rejected by LLM"),
        "llm_raw": response.strip(),
    }


# ════════════════════════════════════════════════════════════════════
# CHAIN LINK 3: EXECUTE — Pure code, no LLM
# ════════════════════════════════════════════════════════════════════

def _chain_execute(address: str, amount_usd: float, reason: str) -> Dict:
    """Execute a sim_buy trade. Pure code — no LLM needed."""
    from repryntt.trading.trading_simulator import sim_buy

    workspace = str(JARVIS_WORKSPACE)
    result = json.loads(sim_buy(workspace, address, amount_usd, reason))

    return {
        "success": result.get("status", "").startswith("BUY"),
        "result": result,
    }


# ════════════════════════════════════════════════════════════════════
# CHAIN LINK 4: REVIEW — Portfolio health check
# ════════════════════════════════════════════════════════════════════

def _chain_review(portfolio_data: Dict) -> List[Dict]:
    """Ask the LLM to review open positions and recommend actions.

    Returns: list of {"symbol": str, "action": "HOLD"|"SELL", "reason": str}
    """
    positions = portfolio_data.get("positions", [])
    if not positions:
        return []

    cash = portfolio_data.get("summary", {}).get("cash_balance", 0)
    total_pnl = portfolio_data.get("summary", {}).get("total_pnl", 0)

    pos_lines = []
    for p in positions[:5]:  # max 5 to keep prompt small
        sym = p.get("symbol", "?")
        invested = p.get("total_cost", 0)
        current = p.get("current_value", invested)
        pnl_pct = p.get("pnl_pct", 0)
        hold_time = p.get("hold_time_min", "?")
        pos_lines.append(f"  {sym}: ${invested:.0f} invested → ${current:.0f} ({pnl_pct:+.1f}%) held {hold_time}min")

    prompt = f"""Portfolio review. Cash: ${cash:.0f} | Total P/L: ${total_pnl:+.1f}
Positions:
{chr(10).join(pos_lines)}

EXIT RULES:
- Loss ≥-15%: SELL 100% immediately (cut losers fast, don't marry memecoins)
- Profit +6-12%: SELL 100% (take profit, don't get greedy on scalps)
- Profit +12-30%: SELL 50% (lock gains, let rest ride)
- Moonshot +30%+: SELL 50% to cover cost basis, let house money ride
- Held >45min with <+3%: SELL 100% (dead momentum, free up capital)
For each position, reply on one line:
SYMBOL: HOLD <reason> or SELL <pct>% <reason>"""

    response = _llm_call(prompt, max_tokens=200)
    if not response:
        return []

    actions = []
    for line in response.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        # Parse "BONK: SELL 100% approaching stop loss"
        sell_match = re.match(r'(\w+):\s*SELL\s+(\d+)%?\s*(.*)', line, re.IGNORECASE)
        hold_match = re.match(r'(\w+):\s*HOLD\s*(.*)', line, re.IGNORECASE)

        if sell_match:
            actions.append({
                "symbol": sell_match.group(1).upper(),
                "action": "SELL",
                "sell_pct": int(sell_match.group(2)),
                "reason": sell_match.group(3).strip()[:80],
            })
        elif hold_match:
            actions.append({
                "symbol": hold_match.group(1).upper(),
                "action": "HOLD",
                "sell_pct": 0,
                "reason": hold_match.group(2).strip()[:80],
            })

    return actions


# ════════════════════════════════════════════════════════════════════
# PORTFOLIO HELPERS
# ════════════════════════════════════════════════════════════════════

def _get_portfolio() -> Dict:
    """Load portfolio state with live prices."""
    from repryntt.trading.trading_simulator import sim_portfolio
    return json.loads(sim_portfolio(str(JARVIS_WORKSPACE)))


def _run_auto_tp_sl() -> List[Dict]:
    """Run algorithmic take-profit / stop-loss. No LLM needed."""
    from repryntt.trading.trading_simulator import auto_take_profit
    return auto_take_profit(str(JARVIS_WORKSPACE))


def _execute_sell(symbol: str, sell_pct: float, reason: str) -> Dict:
    """Execute a sim_sell. Pure code."""
    from repryntt.trading.trading_simulator import sim_sell
    return json.loads(sim_sell(str(JARVIS_WORKSPACE), symbol, sell_pct, reason))


# ════════════════════════════════════════════════════════════════════
# QLORA TRAINING DATA COLLECTION
# ════════════════════════════════════════════════════════════════════

def _log_decision(decision_type: str, prompt_text: str, response_text: str,
                  candidate: Dict = None, outcome: Dict = None):
    """Log a trading decision for QLoRA training.

    Records prompt→response pairs with metadata. After trades resolve,
    _score_resolved_trades() marks them as winning/losing examples.
    """
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "decision_type": decision_type,  # screen, validate, review
        "prompt": prompt_text,
        "response": response_text,
        "candidate_address": (candidate or {}).get("address", ""),
        "candidate_score": (candidate or {}).get("score", 0),
        "outcome": outcome,  # Filled later by _score_resolved_trades
        "resolved": False,
    }

    TRADING_DATA_DIR.mkdir(parents=True, exist_ok=True)

    with _lock:
        with open(TRADE_DECISIONS_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")


def score_resolved_trades():
    """Score resolved trades and promote winning decisions to QLoRA training data.

    Called periodically. Checks trade journal for resolved P/L,
    matches back to decision prompts, and writes high-quality
    training examples to the main training_data.json.
    """
    if not TRADE_DECISIONS_LOG.exists():
        return {"scored": 0, "promoted": 0}

    # Load decision log
    decisions = []
    with open(TRADE_DECISIONS_LOG) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    decisions.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    if not decisions:
        return {"scored": 0, "promoted": 0}

    # Load trade journal for realized P/L
    journal_file = JARVIS_WORKSPACE / "trade_journal.json"
    if not journal_file.exists():
        return {"scored": 0, "promoted": 0}

    with open(journal_file) as f:
        journal = json.load(f)

    # Handle both formats: root-level list or dict with "trades" key
    if isinstance(journal, list):
        trades = journal
    else:
        trades = journal.get("trades", [])
    # Build map of address → latest resolved trade
    resolved_map = {}
    for t in trades:
        if t.get("type") == "SELL" and t.get("pnl") is not None:
            addr = t.get("token_address", t.get("symbol", "")).upper()
            resolved_map[addr] = t

    scored = 0
    promoted = 0
    updated_decisions = []

    for dec in decisions:
        if dec.get("resolved"):
            updated_decisions.append(dec)
            continue

        addr = dec.get("candidate_address", "").upper()
        if addr in resolved_map:
            trade = resolved_map[addr]
            pnl_pct = trade.get("pnl_pct", 0)
            pnl_usd = trade.get("pnl", 0)

            dec["resolved"] = True
            dec["outcome"] = {
                "pnl_pct": pnl_pct,
                "pnl_usd": pnl_usd,
                "profitable": pnl_pct > 0,
            }
            scored += 1

            # Promote winning decisions to QLoRA training data
            if pnl_pct > 2.0 and dec.get("decision_type") == "screen":
                _promote_to_training(dec, quality="very_high")
                promoted += 1
            elif pnl_pct > 0 and dec.get("decision_type") == "screen":
                _promote_to_training(dec, quality="high")
                promoted += 1
            elif pnl_pct < -5 and dec.get("decision_type") == "screen":
                # Losing trades: create a CORRECTED training example
                _promote_corrected_example(dec)
                promoted += 1

        updated_decisions.append(dec)

    # Rewrite decisions file
    with _lock:
        with open(TRADE_DECISIONS_LOG, "w") as f:
            for dec in updated_decisions:
                f.write(json.dumps(dec) + "\n")

    return {"scored": scored, "promoted": promoted}


def _promote_to_training(decision: Dict, quality: str = "high"):
    """Write a winning trade decision to the main QLoRA training_data.json."""
    training_record = {
        "prompt": decision.get("prompt", ""),
        "response": decision.get("response", ""),
        "type": "trading_decision",
        "cycle": 0,
        "timestamp": decision.get("timestamp", datetime.now(timezone.utc).isoformat()),
        "topic": f"trade_{decision.get('decision_type', 'unknown')}_{decision.get('candidate_address', '')[:8]}",
        "quality": quality,
        "source": "micro_chain_trader",
        "domain": "trading",
        "hormone_context": {},
        "trade_outcome": decision.get("outcome", {}),
    }
    _append_training_record(training_record)


def _promote_corrected_example(decision: Dict):
    """For losing trades, create a corrected training example.

    If the model said BUY and lost money, the corrected example
    teaches it to SKIP in similar situations.
    """
    prompt = decision.get("prompt", "")
    original_response = decision.get("response", "")
    pnl_pct = decision.get("outcome", {}).get("pnl_pct", 0)

    # Build a corrected response
    corrected_response = f"SKIP risky setup, similar pattern lost {pnl_pct:.1f}% previously"

    training_record = {
        "prompt": prompt,
        "response": corrected_response,
        "type": "trading_correction",
        "cycle": 0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "topic": f"trade_correction_{decision.get('candidate_address', '')[:8]}",
        "quality": "very_high",  # Corrections are highest value
        "source": "micro_chain_trader",
        "domain": "trading",
        "hormone_context": {},
        "trade_outcome": decision.get("outcome", {}),
    }
    _append_training_record(training_record)


def _append_training_record(record: Dict):
    """Safely append a record to the shared training_data.json."""
    TRADING_DATA_DIR.mkdir(parents=True, exist_ok=True)

    with _lock:
        data = []
        if TRAINING_DATA_FILE.exists():
            try:
                with open(TRAINING_DATA_FILE) as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError):
                data = []

        data.append(record)

        # Cap at 5000 records (match existing limit)
        if len(data) > 5000:
            # Keep trading data + newest general data
            trading = [d for d in data if d.get("domain") == "trading"]
            general = [d for d in data if d.get("domain") != "trading"]
            # Keep all trading (up to 1000) + fill rest with newest general
            trading = trading[-1000:]
            general = general[-(5000 - len(trading)):]
            data = general + trading

        with open(TRAINING_DATA_FILE, "w") as f:
            json.dump(data, f, indent=2)


# ════════════════════════════════════════════════════════════════════
# MAIN TRADING CYCLE
# ════════════════════════════════════════════════════════════════════

def run_trading_cycle(force: bool = False) -> Dict:
    """Run one complete micro-chain trading cycle.

    This is the main entry point. Call every 5 minutes.

    Steps:
      1. Score signals (algorithmic — no LLM)
      2. Run auto take-profit/stop-loss (algorithmic — no LLM)
      3. Get portfolio state
      4. For each candidate: SCREEN → VALIDATE → EXECUTE
      5. Periodically: REVIEW portfolio
      6. Log all decisions for QLoRA training

    Returns: summary dict with actions taken.
    """
    global _cycle_count
    _cycle_count += 1
    cycle_start = time.time()

    logger.info(f"🔗 Micro-chain trading cycle #{_cycle_count} starting")

    # ── Pre-check: LLM health ──
    if not _llm_healthy():
        logger.warning("⚠️ Local LLM not available — running algorithmic-only cycle")
        # Still run auto TP/SL even without LLM
        tp_actions = _run_auto_tp_sl()
        return {
            "cycle": _cycle_count,
            "llm_available": False,
            "auto_tp_actions": len(tp_actions),
            "trades": [],
            "elapsed_sec": round(time.time() - cycle_start, 1),
        }

    # ── Step 1: Score signals (pure code) ──
    from repryntt.trading.signal_scorer import score_signals
    scored = score_signals(max_age_s=1800)
    candidates = [s for s in scored if s.get("score", 0) >= MIN_SCORE_TO_SCREEN]
    candidates = candidates[:MAX_CANDIDATES_PER_CYCLE]

    logger.info(f"📊 {len(scored)} signals scored, {len(candidates)} candidates above threshold")

    # ── Step 2: Auto TP/SL (pure code) ──
    tp_actions = _run_auto_tp_sl()
    if tp_actions:
        logger.info(f"💰 Auto TP/SL fired: {len(tp_actions)} actions")
        for a in tp_actions:
            logger.info(f"  {a.get('action')}: {a.get('symbol')} ({a.get('pnl_pct', 0):+.1f}%)")

    # ── Step 3: Get portfolio ──
    portfolio = _get_portfolio()
    summary = portfolio.get("summary", {})
    cash = summary.get("cash_balance", 0)
    positions = portfolio.get("positions", [])

    logger.info(f"💰 Portfolio: ${cash:.0f} cash, {len(positions)} positions, "
                f"${summary.get('total_portfolio_value', 0):.0f} total")

    # ── Step 4: Screen & Execute candidates ──
    trades_made = []
    skipped = []

    for candidate in candidates:
        addr = candidate.get("address", "")
        score = candidate.get("score", 0)
        grade = candidate.get("grade", "")

        # 4a: SCREEN
        screen = _chain_screen(candidate, cash, positions)

        # Log the decision for QLoRA
        _log_decision(
            "screen",
            f"Score:{score:.1f} Grade:{grade} MCap:{candidate.get('market_cap',0)} 5m:{candidate.get('price_change_5m',0):+.1f}%",
            screen.get("llm_raw", screen.get("reason", "")),
            candidate=candidate,
        )

        if screen["decision"] != "BUY":
            logger.info(f"  ⏭️ SKIP {addr[:12]}... — {screen['reason']}")
            skipped.append({"address": addr, "reason": screen["reason"]})
            continue

        buy_amount = screen["amount"]
        if buy_amount > cash:
            logger.info(f"  ⏭️ SKIP {addr[:12]}... — insufficient cash (${cash:.0f} < ${buy_amount:.0f})")
            skipped.append({"address": addr, "reason": "insufficient cash"})
            continue

        # 4b: VALIDATE
        validate = _chain_validate(candidate, buy_amount)

        _log_decision(
            "validate",
            f"BUY ${buy_amount:.0f} of {addr[:12]}... drift:{validate.get('drift_pct',0):+.1f}%",
            validate.get("llm_raw", validate.get("reason", "")),
            candidate=candidate,
        )

        if not validate["confirmed"]:
            logger.info(f"  ❌ REJECT {addr[:12]}... — {validate['reason']}")
            skipped.append({"address": addr, "reason": validate["reason"]})
            continue

        # 4c: EXECUTE (pure code)
        logger.info(f"  🔥 BUYING ${buy_amount:.0f} of {addr[:12]}... (score {score:.1f})")
        exec_result = _chain_execute(addr, buy_amount, screen["reason"])

        if exec_result["success"]:
            trade_info = exec_result["result"]
            cash = trade_info.get("remaining_cash", cash - buy_amount)
            trades_made.append({
                "address": addr,
                "amount_usd": buy_amount,
                "score": score,
                "grade": grade,
                "screen_reason": screen["reason"],
                "result": trade_info,
            })
            logger.info(f"  ✅ BOUGHT: {trade_info.get('symbol','?')} @ ${trade_info.get('effective_price',0):.8f}")
        else:
            logger.warning(f"  ⚠️ BUY FAILED: {exec_result['result'].get('error', 'unknown')}")
            skipped.append({"address": addr, "reason": f"Execution failed: {exec_result['result'].get('error', '')[:60]}"})

        # Rate limit between executions
        time.sleep(1)

    # ── Step 5: Portfolio Review (every N cycles) ──
    review_actions = []
    if _cycle_count % PORTFOLIO_REVIEW_INTERVAL == 0 and positions:
        logger.info(f"📋 Running portfolio review (cycle #{_cycle_count})")
        portfolio = _get_portfolio()  # Refresh after any trades
        review_actions = _chain_review(portfolio)

        for action in review_actions:
            if action["action"] == "SELL" and action.get("sell_pct", 0) > 0:
                sym = action["symbol"]
                pct = action["sell_pct"]
                reason = action["reason"]
                logger.info(f"  📤 LLM SELL: {sym} {pct}% — {reason}")
                sell_result = _execute_sell(sym, pct, f"LLM review: {reason}")
                action["executed"] = True
                action["sell_result"] = sell_result
            else:
                logger.info(f"  ✊ HOLD: {action['symbol']} — {action['reason']}")

    # ── Step 6: Score resolved trades for QLoRA ──
    qlora_stats = score_resolved_trades()

    elapsed = round(time.time() - cycle_start, 1)
    result = {
        "cycle": _cycle_count,
        "llm_available": True,
        "signals_scored": len(scored),
        "candidates_screened": len(candidates),
        "trades_made": len(trades_made),
        "trades_skipped": len(skipped),
        "auto_tp_actions": len(tp_actions),
        "review_actions": len(review_actions),
        "qlora_scored": qlora_stats.get("scored", 0),
        "qlora_promoted": qlora_stats.get("promoted", 0),
        "cash_remaining": cash,
        "elapsed_sec": elapsed,
        "trades": trades_made,
        "skipped": skipped,
        "tp_actions": tp_actions,
        "review": review_actions,
    }

    logger.info(f"🔗 Cycle #{_cycle_count} complete: {len(trades_made)} trades, "
                f"{len(skipped)} skipped, {len(tp_actions)} auto-TP/SL, "
                f"${cash:.0f} cash, {elapsed}s")

    return result


# ════════════════════════════════════════════════════════════════════
# DAEMON MODE — Runs as a background loop
# ════════════════════════════════════════════════════════════════════

_daemon_running = False
_daemon_thread = None


def start_daemon(interval_sec: int = CYCLE_INTERVAL_SEC):
    """Start the micro-chain trader as a background daemon.

    Runs trading cycles every `interval_sec` seconds.
    Integrates with the evolution loop — call from a task or cron.
    """
    global _daemon_running, _daemon_thread

    if _daemon_running:
        logger.info("Micro-chain trader daemon already running")
        return

    _daemon_running = True

    def _daemon_loop():
        global _daemon_running
        logger.info(f"🚀 Micro-chain trader daemon started (interval={interval_sec}s)")
        while _daemon_running:
            try:
                result = run_trading_cycle()
                logger.info(f"Cycle result: {result.get('trades_made', 0)} trades, "
                            f"${result.get('cash_remaining', 0):.0f} cash")
            except Exception as e:
                logger.error(f"Trading cycle error: {e}", exc_info=True)
            time.sleep(interval_sec)
        logger.info("Micro-chain trader daemon stopped")

    _daemon_thread = threading.Thread(target=_daemon_loop, daemon=True, name="micro-chain-trader")
    _daemon_thread.start()


def stop_daemon():
    """Stop the background trader daemon."""
    global _daemon_running
    _daemon_running = False
    logger.info("Micro-chain trader daemon stop requested")


def daemon_status() -> Dict:
    """Return current daemon status."""
    return {
        "running": _daemon_running,
        "cycle_count": _cycle_count,
        "llm_healthy": _llm_healthy(),
        "decisions_logged": _count_decisions(),
    }


def _count_decisions() -> int:
    if not TRADE_DECISIONS_LOG.exists():
        return 0
    with open(TRADE_DECISIONS_LOG) as f:
        return sum(1 for line in f if line.strip())


# ════════════════════════════════════════════════════════════════════
# STANDALONE ENTRY POINT
# ════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    import sys

    if "--daemon" in sys.argv:
        interval = CYCLE_INTERVAL_SEC
        for arg in sys.argv:
            if arg.startswith("--interval="):
                interval = int(arg.split("=")[1])
        start_daemon(interval)
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            stop_daemon()
    else:
        # Single cycle
        result = run_trading_cycle()
        print(json.dumps(result, indent=2, default=str))
