"""
repryntt.learning.tools — Jarvis-callable learning tools
==========================================================
Standalone functions registered in ToolRegistry so Jarvis can
introspect, query, and interact with the recursive learning engine.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent / "data"


def _get_engine():
    from repryntt.learning.engine import LearningEngine
    return LearningEngine(data_dir=_DATA_DIR)


def _get_trading_learner():
    from repryntt.learning.trading import TradingLearner
    return TradingLearner(_get_engine())


def _get_identity_learner():
    from repryntt.learning.identity import IdentityLearner
    return IdentityLearner(_get_engine())


# ── Trading Learning Tools ────────────────────────────────────────────

def learning_trading_stats(**kwargs) -> str:
    """Get trading learning statistics — events tracked, patterns discovered, win rates.

    Shows how many trading events have been logged, how many have outcomes,
    and summary statistics about your trading performance patterns.
    """
    try:
        tl = _get_trading_learner()
        stats = tl.get_stats()
        signal_stats = tl.get_signal_type_stats()
        return json.dumps({
            "domain_stats": stats,
            "signal_type_analysis": signal_stats,
        }, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def learning_trading_brief(**kwargs) -> str:
    """Get the current trading learning brief — a summary of what patterns work and don't.

    This is the same intelligence that gets injected into your trading cold-call prompts.
    Shows strong patterns to lean into, weak patterns to avoid, and recent trends.
    """
    try:
        tl = _get_trading_learner()
        brief = tl.get_trading_brief(max_chars=3000)
        if not brief:
            return json.dumps({"message": "No trading data yet. Make trades and log outcomes to start learning."})
        return json.dumps({"brief": brief})
    except Exception as e:
        return json.dumps({"error": str(e)})


def learning_signal_weights(**kwargs) -> str:
    """Get the current adaptive signal weights vs. the original base weights.

    Shows how the learning engine has adjusted each signal type's importance
    based on actual trade outcomes. Weights shift gradually via EMA.
    """
    try:
        from repryntt.learning.trading import BASE_SIGNAL_WEIGHTS
        tl = _get_trading_learner()
        adapted = tl.get_adapted_signal_weights()
        changes = {}
        for k in BASE_SIGNAL_WEIGHTS:
            base = BASE_SIGNAL_WEIGHTS[k]
            new = adapted.get(k, base)
            if abs(new - base) > 0.01:
                changes[k] = {
                    "base": base,
                    "adapted": round(new, 4),
                    "change_pct": round(((new / base) - 1) * 100, 1) if base else 0,
                }
        return json.dumps({
            "base_weights": BASE_SIGNAL_WEIGHTS,
            "adapted_weights": adapted,
            "significant_changes": changes,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


def learning_backfill_journal(**kwargs) -> str:
    """Import existing trade_journal.json entries into the learning engine.

    One-shot import of historical trades. Safe to run multiple times — duplicates are skipped.
    This bootstraps the learning engine with past performance data.
    """
    try:
        tl = _get_trading_learner()
        result = tl.backfill_journal()
        if result.get("imported", 0) > 0:
            stats = tl.get_signal_type_stats()
            result["signal_analysis_after_import"] = stats
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Identity Learning Tools ──────────────────────────────────────────

def learning_identity_stats(**kwargs) -> str:
    """Get identity/self-evolution learning statistics.

    Shows how many behavioral events have been tracked, which emotional states
    and drive levels correlate with productive outcomes, and growth trends.
    """
    try:
        il = _get_identity_learner()
        report = il.get_growth_report()
        return json.dumps(report, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def learning_identity_brief(**kwargs) -> str:
    """Get the current identity learning brief — self-awareness intelligence.

    Shows which emotional states and behaviors correlate with your best work,
    helping you self-regulate for optimal performance.
    """
    try:
        il = _get_identity_learner()
        brief = il.get_identity_brief(max_chars=2000)
        if not brief:
            return json.dumps({"message": "Not enough identity data yet. Keep working and the patterns will emerge."})
        return json.dumps({"brief": brief})
    except Exception as e:
        return json.dumps({"error": str(e)})


def learning_optimal_conditions(**kwargs) -> str:
    """Discover which emotional states, moods, and drive levels produce your best outcomes.

    Analyzes your historical performance to find the conditions where you thrive.
    """
    try:
        il = _get_identity_learner()
        conditions = il.get_optimal_conditions()
        if not conditions:
            return json.dumps({"message": "Not enough data yet to identify optimal conditions."})
        return json.dumps(conditions, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Cross-Domain Tools ───────────────────────────────────────────────

def learning_all_domains(**kwargs) -> str:
    """List all learning domains and their event counts.

    Shows which areas of learning have data (trading, identity, etc.)
    and how much data each has accumulated.
    """
    try:
        engine = _get_engine()
        domains = engine.get_all_domains()
        result = {}
        for d in domains:
            result[d] = engine.get_domain_stats(d)
        return json.dumps(result, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def learning_weight_history(**kwargs) -> str:
    """View the history of all weight adjustments across all domains.

    Shows the audit trail of how signal weights and other parameters have
    been adapted by the learning engine, with reasons for each change.
    """
    try:
        engine = _get_engine()
        adjustments = engine._adjustments[-50:]  # Last 50
        return json.dumps([
            {
                "domain": a.domain,
                "category": a.category,
                "old": round(a.old_weight, 4),
                "new": round(a.new_weight, 4),
                "reason": a.reason,
                "timestamp": a.timestamp,
            }
            for a in adjustments
        ], indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})
