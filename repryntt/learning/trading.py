"""
repryntt.learning.trading — Trading Domain Adapter
====================================================
Maps trading events (signal scores, sim_buy, sim_sell, position outcomes)
into the generic LearningEngine, and exposes trading-specific queries.

Lifecycle:
  1. on_signal_scored()  → logs a "signal_seen" event per scored token
  2. on_trade_entry()    → logs a "buy" event when Jarvis buys
  3. on_trade_exit()     → records outcome (PnL) against the buy event
  4. backfill_journal()  → one-shot import from existing trade_journal.json
  5. get_adapted_signal_weights() → returns SIGNAL_WEIGHTS blended with learning
  6. get_trading_brief()  → context string for cold-call prompt injection
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from repryntt.learning.engine import LearningEngine

logger = logging.getLogger(__name__)

DOMAIN = "trading"

# Base weights — the starting point before learning shifts them.
# Matches repryntt/trading/signal_scorer.py SIGNAL_WEIGHTS exactly.
BASE_SIGNAL_WEIGHTS: Dict[str, float] = {
    "TP2 Buy":              3.0,
    "Higher Low Buy":       2.5,
    "TP1 Buy":              2.0,
    "Momentum":             1.5,
    "Large Buy Detected":   1.2,
    "Large Sell Detected":  -2.0,
}


class TradingLearner:
    """Thin adapter that translates trading events into LearningEngine calls."""

    def __init__(self, engine: LearningEngine):
        self.engine = engine

    # ── Event Capture ─────────────────────────────────────────────────

    def on_signal_scored(self, address: str, score: float,
                         signal_types: Dict[str, int],
                         market_cap: float = 0, price_change_5m: float = 0,
                         risk_flags: List[str] = None) -> List[str]:
        """Log one event per signal type when a token is scored.

        Returns list of event IDs (one per signal type present).
        """
        ids = []
        for stype, count in signal_types.items():
            eid = self.engine.log_event(
                domain=DOMAIN,
                category=stype,
                action="signal_scored",
                context={
                    "address": address,
                    "composite_score": score,
                    "signal_count": count,
                    "market_cap": market_cap,
                    "price_change_5m": price_change_5m,
                    "risk_flags": risk_flags or [],
                },
                tags=["auto"],
            )
            ids.append(eid)
        return ids

    def on_trade_entry(self, address: str, symbol: str, amount_usd: float,
                       entry_price: float, reason: str = "",
                       signal_types: List[str] = None,
                       composite_score: float = 0) -> str:
        """Log a buy event. Returns event_id to match with exit later."""
        primary_signal = signal_types[0] if signal_types else "manual"
        eid = self.engine.log_event(
            domain=DOMAIN,
            category=primary_signal,
            action="buy",
            context={
                "address": address,
                "symbol": symbol,
                "amount_usd": amount_usd,
                "entry_price": entry_price,
                "reason": reason,
                "signal_types": signal_types or [],
                "composite_score": composite_score,
            },
            tags=["trade"],
        )
        logger.info(f"[TRADE-LEARN] Logged buy: {symbol} via {primary_signal} → {eid}")
        return eid

    def on_trade_exit(self, event_id: str, pnl_pct: float,
                      exit_price: float = 0, hold_seconds: int = 0,
                      reason: str = "") -> bool:
        """Record the outcome of a previously logged buy.

        Args:
            event_id: The ID returned from on_trade_entry().
            pnl_pct: Profit/loss percentage (e.g. 15.0 for +15%).
            exit_price: Price at sell time.
            hold_seconds: Duration of position.
            reason: Why selling (TP, SL, manual, etc.).

        Returns True if matched.
        """
        # Normalize pnl_pct to -1..+1 score.
        # +100% → +1.0, 0% → 0.0, -100% → -1.0
        score = max(-1.0, min(1.0, pnl_pct / 100.0))
        return self.engine.record_outcome(
            event_id, score=score,
            details={
                "pnl_pct": pnl_pct,
                "exit_price": exit_price,
                "hold_seconds": hold_seconds,
                "reason": reason,
            },
        )

    def on_trade_exit_by_address(self, address: str, pnl_pct: float,
                                 exit_price: float = 0,
                                 hold_seconds: int = 0,
                                 reason: str = "") -> int:
        """Record outcome for all pending buy events matching an address.

        Useful when event_id wasn't stored (e.g. backfill from portfolio).
        Returns number of events matched.
        """
        score = max(-1.0, min(1.0, pnl_pct / 100.0))

        def match(evt):
            return (evt.action == "buy"
                    and evt.context.get("address") == address)

        return self.engine.record_outcome_by_context(
            DOMAIN, match, score=score,
            details={
                "pnl_pct": pnl_pct,
                "exit_price": exit_price,
                "hold_seconds": hold_seconds,
                "reason": reason,
            },
        )

    # ── Backfill from existing trade_journal.json ─────────────────────

    def backfill_journal(self, journal_path: Path = None) -> Dict[str, int]:
        """One-shot import from trade_journal.json into the engine.

        Reads the journal, creates event+outcome pairs for each entry.
        Skips entries already imported (idempotent by timestamp+address).
        """
        if journal_path is None:
            journal_path = (Path(__file__).resolve().parent.parent
                            / "trading" / "data" / "trade_journal.json")
        if not journal_path.exists():
            return {"imported": 0, "skipped": 0, "error": "journal not found"}

        try:
            journal = json.loads(journal_path.read_text())
        except Exception as e:
            return {"imported": 0, "skipped": 0, "error": str(e)}

        existing_addresses = set()
        for evt in self.engine._events.get(DOMAIN, []):
            addr = evt.context.get("address", "")
            if addr:
                existing_addresses.add(f"{addr}_{evt.timestamp:.0f}")

        imported = 0
        skipped = 0
        for entry in journal:
            addr = entry.get("address", "")
            reason = entry.get("reason", "manual")
            pnl_pct = entry.get("pnl_pct", 0)
            ts = entry.get("timestamp", "")

            # Rough dedup key
            dedup = f"{addr}_{hash(ts) % 10**9}"
            if dedup in existing_addresses:
                skipped += 1
                continue

            # Log event + immediate outcome
            eid = self.engine.log_event(
                domain=DOMAIN,
                category=reason if reason else "manual",
                action=entry.get("action", "buy"),
                context={
                    "address": addr,
                    "symbol": entry.get("symbol", ""),
                    "entry_price": entry.get("entry_price", 0),
                    "market_conditions": entry.get("market_conditions", ""),
                },
                tags=["backfill"],
            )
            score = max(-1.0, min(1.0, pnl_pct / 100.0))
            self.engine.record_outcome(
                eid, score=score,
                details={
                    "pnl_pct": pnl_pct,
                    "exit_price": entry.get("exit_price", 0),
                    "hold_seconds": entry.get("hold_seconds", 0),
                    "lessons": entry.get("lessons", ""),
                },
            )
            imported += 1

        if imported:
            self.engine.analyze(DOMAIN)
        return {"imported": imported, "skipped": skipped}

    # ── Query Helpers ─────────────────────────────────────────────────

    def get_adapted_signal_weights(self) -> Dict[str, float]:
        """Return SIGNAL_WEIGHTS adjusted by learned performance."""
        return self.engine.get_adaptive_weights(DOMAIN, BASE_SIGNAL_WEIGHTS)

    def get_trading_brief(self, max_chars: int = 2000) -> str:
        """Return the learning context string for cold-call prompt injection."""
        return self.engine.get_learning_brief(DOMAIN, max_chars=max_chars)

    def get_signal_type_stats(self) -> List[Dict[str, Any]]:
        """Per-signal-type win rates and scores for dashboard / introspection."""
        insights = self.engine.analyze(DOMAIN)
        return [
            {
                "signal_type": i.category,
                "sample_count": i.sample_count,
                "win_rate": round(i.win_rate, 3),
                "avg_score": round(i.avg_score, 3),
                "weighted_avg": round(i.weighted_avg_score, 3),
                "confidence": i.confidence,
                "trend": i.trend,
            }
            for i in insights
        ]

    def get_stats(self) -> Dict[str, Any]:
        """Domain stats for Jarvis introspection tool."""
        return self.engine.get_domain_stats(DOMAIN)
