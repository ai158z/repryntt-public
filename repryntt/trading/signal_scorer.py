"""
SAIGE Signal Scorer — Turns 841+ raw signals into ranked trade opportunities
=============================================================================

The trading bot generates raw signals (Momentum, TP1 Buy, Higher Low Buy,
Large Buy Detected, Large Sell Detected, TP2 Buy) in data/signal_tokens/.

This module aggregates them per-token, scores each token based on:
  - Signal density (more signals in a short window = stronger conviction)
  - Signal diversity (multiple signal types = more confirmations)
  - Recency (recent signals weighted higher)
  - Price momentum (5m change, buy/sell ratio)
  - Market cap range (sweet spot for memecoins: $20k–$500k)
  - Volume activity (buy volume vs sell volume)
  - Risk flags (large sells, holder concentration)

Output: A ranked list of trade candidates with scores + reasoning,
ready for Jarvis to act on via sim_buy.
"""

import json
import os
import glob
import time
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("saige.signal_scorer")

# ─── Paths ────────────────────────────────────────────────────────────────────

_MODULE_DIR = Path(__file__).resolve().parent
SIGNAL_TOKENS_DIR = _MODULE_DIR / "data" / "signal_tokens"
TOKEN_PROFILES_DIR = _MODULE_DIR / "data" / "token_profiles"
SCORE_CACHE_FILE = _MODULE_DIR / "data" / "scored_signals.json"

# ─── Signal Weights ───────────────────────────────────────────────────────────

# Higher weight = more bullish conviction
SIGNAL_WEIGHTS = {
    "TP2 Buy":              3.0,   # Strongest buy signal — confirmed breakout
    "Higher Low Buy":       2.5,   # Solid uptrend confirmation
    "TP1 Buy":              2.0,   # First target hit — moderate confidence
    "Momentum":             1.5,   # Trending but not confirmed
    "Large Buy Detected":   0.0,   # Data only — NOT used for scoring decisions
    "Large Sell Detected":  0.0,   # Data only — NOT used for scoring decisions
}

# Recency decay — signals older than this get down-weighted
RECENCY_WINDOW_S = 900        # 15 minutes — full weight (swing timeframe)
DECAY_HALF_LIFE_S = 600       # Weight halves every 10 min after window

# Market cap scoring — sweet spot for memecoin scalping
MCAP_IDEAL_LOW = 30_000
MCAP_IDEAL_HIGH = 300_000
MCAP_MAX = 2_000_000          # Above this, less upside for memecoins

# Minimum score to be considered a trade candidate
MIN_TRADE_SCORE = 3.0

# Maximum age for signals to be included at all (1 hour — matches swing timeframes)
MAX_SIGNAL_AGE_S = 3600

# ─── Learning Engine Integration ──────────────────────────────────────────────

_learning_engine = None   # Lazy-loaded singleton


def _get_active_weights() -> Dict[str, float]:
    """Return adaptive weights from the learning engine if available,
    otherwise fall back to static SIGNAL_WEIGHTS."""
    global _learning_engine
    if _learning_engine is None:
        try:
            from repryntt.learning import LearningEngine, TradingLearner
            data_dir = _MODULE_DIR.parent / "learning" / "data"
            _learning_engine = LearningEngine(data_dir=data_dir)
        except Exception as e:
            logger.debug(f"[SCORER] Learning engine not available: {e}")
            return dict(SIGNAL_WEIGHTS)
    try:
        from repryntt.learning.trading import TradingLearner, BASE_SIGNAL_WEIGHTS
        learner = TradingLearner(_learning_engine)
        adapted = learner.get_adapted_signal_weights()
        if adapted and adapted != BASE_SIGNAL_WEIGHTS:
            logger.info(f"[SCORER] Using adaptive weights from learning engine")
        return adapted
    except Exception as e:
        logger.debug(f"[SCORER] Adaptive weights unavailable: {e}")
        return dict(SIGNAL_WEIGHTS)


# ─── Core Scorer ──────────────────────────────────────────────────────────────

def _parse_timestamp(ts_str: str) -> float:
    """Parse ISO timestamp to epoch seconds."""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0


def _recency_weight(signal_age_s: float) -> float:
    """Exponential decay weight based on signal age."""
    if signal_age_s <= RECENCY_WINDOW_S:
        return 1.0
    excess = signal_age_s - RECENCY_WINDOW_S
    return max(0.05, 0.5 ** (excess / DECAY_HALF_LIFE_S))


def _mcap_score(mcap: float) -> float:
    """Score market cap — sweet spot gets 1.0, outside ranges get penalized."""
    if mcap <= 0:
        return 0.1
    if MCAP_IDEAL_LOW <= mcap <= MCAP_IDEAL_HIGH:
        return 1.0
    if mcap < MCAP_IDEAL_LOW:
        # Too tiny — might be a scam, but still tradeable
        return max(0.3, mcap / MCAP_IDEAL_LOW)
    if mcap <= MCAP_MAX:
        # Above sweet spot but still viable
        return max(0.4, 1.0 - (mcap - MCAP_IDEAL_HIGH) / (MCAP_MAX - MCAP_IDEAL_HIGH) * 0.6)
    # Above $2M — low upside for memecoin scalping
    return 0.2


def _volume_score(buy_vol: float, sell_vol: float) -> float:
    """Score based on buy vs sell volume ratio."""
    total = buy_vol + sell_vol
    if total <= 0:
        return 0.5  # No data
    ratio = buy_vol / total
    # ratio > 0.6 = bullish, < 0.4 = bearish
    if ratio >= 0.65:
        return 1.2
    elif ratio >= 0.55:
        return 1.0
    elif ratio >= 0.45:
        return 0.7
    elif ratio >= 0.35:
        return 0.4
    else:
        return 0.2  # Heavy selling


def _momentum_score(price_change_5m: float, price_change_15m: float = 0.0,
                     price_change_30m: float = 0.0, price_change_1h: float = 0.0) -> float:
    """Score based on swing-timeframe price changes (5m/15m/30m/1h).
    
    Uses the best available longer-TF data for conviction.
    Multi-timeframe alignment (all positive) gets a bonus.
    """
    # Use the best longer-TF signal available
    best_long = max(price_change_15m, price_change_30m, price_change_1h)
    
    # Multi-timeframe alignment bonus
    aligned = sum(1 for pc in [price_change_5m, price_change_15m, price_change_30m, price_change_1h] if pc > 0)
    alignment_bonus = 0.0
    if aligned >= 3:
        alignment_bonus = 0.2  # 3+ TFs positive = stronger conviction
    if aligned == 4:
        alignment_bonus = 0.4  # All 4 TFs positive = peak conviction
    
    # Base score from 5m (fastest swing TF)
    if price_change_5m >= 30:
        base = 1.3
    elif price_change_5m >= 15:
        base = 1.1
    elif price_change_5m >= 5:
        base = 0.9
    elif price_change_5m >= 0:
        base = 0.7
    elif price_change_5m >= -10:
        base = 0.4
    else:
        base = 0.2
    
    # Boost from longer TF trend (if 15m or 30m shows strong uptrend)
    if best_long >= 20:
        base += 0.3
    elif best_long >= 10:
        base += 0.2
    elif best_long >= 5:
        base += 0.1
    
    return base + alignment_bonus


def score_signals(max_age_s: int = MAX_SIGNAL_AGE_S) -> List[Dict[str, Any]]:
    """
    Aggregate all raw signals, score each unique token, return ranked list.

    Returns:
        List of scored token dicts sorted by score descending:
        [{
            "address": "...",
            "score": 8.5,
            "signal_count": 12,
            "signal_types": {"Momentum": 5, "TP1 Buy": 3, ...},
            "latest_price": 0.00012,
            "market_cap": 120000,
            "buy_volume_5m": 5000,
            "sell_volume_5m": 3000,
            "price_change_5m": 15.2,
            "reasoning": "Strong buy: 12 signals (3 types), 15% 5m gain, buy-heavy volume...",
            "risk_flags": ["Large sells detected"],
            "scored_at": "2026-03-04T22:10:00+00:00"
        }, ...]
    """
    if not SIGNAL_TOKENS_DIR.exists():
        logger.warning("Signal tokens directory not found")
        return []

    now = time.time()
    token_signals: Dict[str, List[Dict]] = defaultdict(list)

    # 1. Load all signals within time window
    signal_files = glob.glob(str(SIGNAL_TOKENS_DIR / "*.json"))
    loaded = 0
    for fpath in signal_files:
        try:
            with open(fpath) as f:
                sig = json.load(f)

            # Check age
            ts = _parse_timestamp(sig.get("detection_timestamp", ""))
            if ts <= 0:
                # Fallback: use file mtime
                ts = os.path.getmtime(fpath)
            age = now - ts
            if age > max_age_s:
                continue

            sig["_age_s"] = age
            sig["_timestamp"] = ts
            sig["_file"] = os.path.basename(fpath)
            addr = sig.get("address", "")
            if addr:
                token_signals[addr].append(sig)
                loaded += 1
        except Exception:
            pass

    logger.info(f"[SCORER] Loaded {loaded} signals for {len(token_signals)} tokens (window: {max_age_s}s)")

    # 2. Score each token
    active_weights = _get_active_weights()
    scored = []
    for addr, signals in token_signals.items():
        # Count signal types
        type_counts = defaultdict(int)
        for s in signals:
            type_counts[s.get("signal_type", "unknown")] += 1

        # Use the most recent signal for current market data
        signals.sort(key=lambda s: s.get("_timestamp", 0), reverse=True)
        latest = signals[0]

        # --- Compute component scores ---

        # A) Signal conviction score (weighted sum with recency decay)
        signal_score = 0.0
        for s in signals:
            stype = s.get("signal_type", "unknown")
            weight = active_weights.get(stype, 0.5)
            recency = _recency_weight(s.get("_age_s", 9999))
            signal_score += weight * recency

        # B) Signal diversity bonus (multiple signal types = more confirmation)
        buy_types = [t for t in type_counts if t != "Large Sell Detected"]
        diversity_bonus = min(len(buy_types) * 0.5, 2.0)  # Max +2.0

        # C) Market cap score
        mcap = latest.get("market_cap", 0) or 0
        mcap_mult = _mcap_score(mcap)

        # D) Volume score
        buy_vol = latest.get("buy_volume_5m", 0) or 0
        sell_vol = latest.get("sell_volume_5m", 0) or 0
        vol_mult = _volume_score(buy_vol, sell_vol)

        # E) Momentum score — uses swing timeframes
        p5m = latest.get("price_change_5m", 0) or 0
        p15m = latest.get("price_change_15m", 0) or 0
        p30m = latest.get("price_change_30m", 0) or 0
        p1h = latest.get("price_change_1h", 0) or 0
        mom_mult = _momentum_score(p5m, p15m, p30m, p1h)

        # --- Final composite score ---
        raw_score = signal_score + diversity_bonus
        final_score = raw_score * mcap_mult * vol_mult * mom_mult

        # --- Risk flags ---
        risk_flags = []
        sell_count = type_counts.get("Large Sell Detected", 0)
        if sell_count > 0:
            risk_flags.append(f"{sell_count} large sell(s) detected")
        if sell_vol > buy_vol * 1.5:
            risk_flags.append("Heavy sell pressure (sell > 1.5x buy volume)")
        if mcap > MCAP_MAX:
            risk_flags.append(f"High mcap (${mcap:,.0f}) — limited upside")
        holders = latest.get("top_20_holders_percentage", 0) or 0
        if holders > 50:
            risk_flags.append(f"Concentrated holders ({holders:.0f}%)")
        if p5m < -15:
            risk_flags.append(f"Sharp decline ({p5m:.1f}% in 5m)")
        if p1h < -20:
            risk_flags.append(f"Hourly dump ({p1h:.1f}% in 1h)")

        # --- Build reasoning ---
        type_summary = ", ".join(f"{c}x {t}" for t, c in sorted(type_counts.items(), key=lambda x: -x[1]))
        reasoning_parts = [
            f"{len(signals)} signals ({type_summary})",
        ]
        # Show all available timeframe changes
        tf_parts = []
        if p5m != 0:
            tf_parts.append(f"5m:{p5m:+.1f}%")
        if p15m != 0:
            tf_parts.append(f"15m:{p15m:+.1f}%")
        if p30m != 0:
            tf_parts.append(f"30m:{p30m:+.1f}%")
        if p1h != 0:
            tf_parts.append(f"1h:{p1h:+.1f}%")
        if tf_parts:
            reasoning_parts.append(" ".join(tf_parts))
        if buy_vol > sell_vol:
            reasoning_parts.append(f"buy-heavy volume (${buy_vol:,.0f} vs ${sell_vol:,.0f})")
        elif sell_vol > buy_vol:
            reasoning_parts.append(f"sell-heavy volume (${sell_vol:,.0f} vs ${buy_vol:,.0f})")
        if mcap > 0:
            reasoning_parts.append(f"mcap ${mcap:,.0f}")

        if final_score >= 8:
            grade = "STRONG BUY"
        elif final_score >= 5:
            grade = "BUY"
        elif final_score >= MIN_TRADE_SCORE:
            grade = "WEAK BUY"
        elif final_score >= 0:
            grade = "HOLD/WATCH"
        else:
            grade = "AVOID"

        scored.append({
            "address": addr,
            "score": round(final_score, 2),
            "grade": grade,
            "signal_count": len(signals),
            "signal_types": dict(type_counts),
            "latest_price": latest.get("current_price", 0),
            "market_cap": mcap,
            "buy_volume_5m": buy_vol,
            "sell_volume_5m": sell_vol,
            "price_change_5m": p5m,
            "price_change_15m": p15m,
            "price_change_30m": p30m,
            "price_change_1h": p1h,
            "pool_buys_5m": latest.get("pool_buys_5m", 0),
            "pool_sells_5m": latest.get("pool_sells_5m", 0),
            "reasoning": f"{grade}: {'; '.join(reasoning_parts)}",
            "risk_flags": risk_flags,
            "scored_at": datetime.now(timezone.utc).isoformat(),
            # Extra data for decision-making
            "_signal_score": round(signal_score, 2),
            "_diversity_bonus": round(diversity_bonus, 2),
            "_mcap_mult": round(mcap_mult, 2),
            "_vol_mult": round(vol_mult, 2),
            "_mom_mult": round(mom_mult, 2),
        })

    # Sort by score descending
    scored.sort(key=lambda t: t["score"], reverse=True)

    # Fire hook alerts for actionable signals
    # Toggle: PUSH_ALERTS_ENABLED in brain/jarvis_trading_engine.py
    try:
        from repryntt.trading.trading_engine import PUSH_ALERTS_ENABLED
        if PUSH_ALERTS_ENABLED:
            from repryntt.comms.hooks.trading_parsers import parse_trade_signal
            from repryntt.comms.hooks.router import get_hook_router
            router = get_hook_router()
            if router._running:
                for sig in scored:
                    if sig["score"] >= MIN_TRADE_SCORE:
                        hook = parse_trade_signal(sig)
                        if hook:
                            router.dispatch(hook)
    except Exception:
        pass

    # Cache results
    try:
        os.makedirs(SCORE_CACHE_FILE.parent, exist_ok=True)
        with open(SCORE_CACHE_FILE, 'w') as f:
            json.dump({
                "scored_tokens": scored,
                "count": len(scored),
                "signals_processed": loaded,
                "unique_tokens": len(token_signals),
                "scored_at": datetime.now(timezone.utc).isoformat(),
                "window_seconds": max_age_s,
            }, f, indent=2)
    except Exception as e:
        logger.warning(f"[SCORER] Failed to cache scores: {e}")

    return scored


def get_trade_candidates(min_score: float = MIN_TRADE_SCORE,
                         max_results: int = 10) -> List[Dict[str, Any]]:
    """
    Get the top trade candidates — scored, filtered, ready for Jarvis to act on.

    Returns only tokens above min_score threshold, limited to max_results.
    """
    scored = score_signals()
    candidates = [t for t in scored if t["score"] >= min_score]
    return candidates[:max_results]


def get_scored_signals_summary() -> str:
    """
    Return a JSON summary of current scored signals — for Jarvis tool use.
    """
    scored = score_signals()

    if not scored:
        return json.dumps({
            "candidates": [],
            "count": 0,
            "note": "No tradeable signals in the current window. "
                    "Either the bot isn't running or market is quiet.",
        })

    # Split into tiers
    strong = [t for t in scored if t["score"] >= 8]
    buy = [t for t in scored if 5 <= t["score"] < 8]
    weak = [t for t in scored if MIN_TRADE_SCORE <= t["score"] < 5]
    avoid = [t for t in scored if t["score"] < MIN_TRADE_SCORE]

    return json.dumps({
        "candidates": scored[:10],
        "total_scored": len(scored),
        "tiers": {
            "STRONG_BUY": len(strong),
            "BUY": len(buy),
            "WEAK_BUY": len(weak),
            "AVOID": len(avoid),
        },
        "top_pick": scored[0] if scored else None,
        "tip": "Use sim_buy(token=address, amount_usd=X, reason='...') to trade. "
               "Check sim_portfolio() first to see available cash.",
    }, indent=2)


# ─── CLI Entry Point ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scored = score_signals()
    if not scored:
        print("No signals in window")
    else:
        print(f"\n{'='*70}")
        print(f"  SIGNAL SCORER — {len(scored)} tokens scored")
        print(f"{'='*70}\n")
        for i, t in enumerate(scored, 1):
            flags = f" ⚠️  {', '.join(t['risk_flags'])}" if t['risk_flags'] else ""
            print(f"  #{i}  {t['address'][:12]}...  "
                  f"Score: {t['score']:6.2f}  "
                  f"Grade: {t['grade']:<12s}  "
                  f"Signals: {t['signal_count']:3d}  "
                  f"MCap: ${t['market_cap']:>10,.0f}  "
                  f"5m: {t['price_change_5m']:+.1f}%"
                  f"{flags}")
        print()
