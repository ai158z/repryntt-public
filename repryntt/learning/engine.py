"""
repryntt.learning.engine — Core Recursive Learning Engine
===========================================================
Domain-agnostic event logging, outcome tracking, pattern analysis,
and adaptive weight management.

Every agent category (trading, research, content, coding, etc.) plugs
into this same engine with its own domain adapter.

Design principles:
  • Temporal decay — recent events carry more weight (14-day half-life)
  • Statistical significance — won't adjust weights until min_samples met
  • Gradual adaptation — exponential moving average, no flip-flopping
  • Full audit trail — every weight change logged with reasoning
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------
DECAY_HALF_LIFE_DAYS = 14.0        # 50% weight after 14 days
DECAY_LAMBDA = math.log(2) / (DECAY_HALF_LIFE_DAYS * 86400)
MIN_SAMPLES = 8                    # Minimum events before adjusting weights
EMA_ALPHA = 0.15                   # Smoothing factor (0-1), higher = faster adapt
MAX_EVENTS = 5000                  # Per-domain event cap (old pruned first)
CONFIDENCE_THRESHOLDS = {
    "very_high": 50,
    "high": 25,
    "medium": 15,
    "low": 8,
}

# ---------------------------------------------------------------------------
#  Data classes
# ---------------------------------------------------------------------------

@dataclass
class LearningEvent:
    """A single recorded action with its outcome."""
    event_id: str                    # Unique ID
    domain: str                      # "trading", "research", "content", etc.
    category: str                    # Signal type, task type, strategy, etc.
    action: str                      # What was done: "buy", "sell", "skip", etc.
    context: Dict[str, Any]          # Freeform context at decision time
    timestamp: float = 0.0           # Unix epoch
    outcome: Optional[Dict[str, Any]] = None  # Filled in when outcome known
    outcome_score: Optional[float] = None     # Normalized -1.0 to +1.0
    outcome_timestamp: Optional[float] = None
    tags: List[str] = field(default_factory=list)

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp

    @property
    def decay_weight(self) -> float:
        """Exponential decay weight based on age."""
        return math.exp(-DECAY_LAMBDA * self.age_seconds)

    @property
    def has_outcome(self) -> bool:
        return self.outcome_score is not None


@dataclass
class PatternInsight:
    """A discovered pattern from historical events."""
    category: str                    # e.g. "TP2 Buy"
    domain: str
    sample_count: int
    win_rate: float                  # 0.0 to 1.0
    avg_score: float                 # Average outcome_score (-1 to +1)
    weighted_avg_score: float        # Decay-weighted average
    confidence: str                  # "very_high", "high", "medium", "low"
    best_context: Dict[str, Any]     # Context features of best outcomes
    worst_context: Dict[str, Any]    # Context features of worst outcomes
    trend: str                       # "improving", "stable", "declining"
    updated_at: float = 0.0

    def __post_init__(self):
        if self.updated_at == 0.0:
            self.updated_at = time.time()


@dataclass
class WeightAdjustment:
    """A logged weight change with reasoning."""
    category: str
    domain: str
    old_weight: float
    new_weight: float
    reason: str
    insight: PatternInsight
    timestamp: float = 0.0

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()


# ---------------------------------------------------------------------------
#  LearningEngine — the core
# ---------------------------------------------------------------------------

class LearningEngine:
    """
    Domain-agnostic recursive learning engine.

    Usage:
        engine = LearningEngine(data_dir=Path("..."))

        # 1. Log an event when an action is taken
        eid = engine.log_event("trading", "TP2 Buy", "buy",
                               context={"score": 8.5, "mcap": 120000})

        # 2. Later, record the outcome
        engine.record_outcome(eid, score=0.7,
                              details={"pnl_pct": 18.4, "hold_hours": 3.2})

        # 3. Analyze patterns
        insights = engine.analyze("trading")

        # 4. Get adaptive weights
        weights = engine.get_adaptive_weights("trading", base_weights)

        # 5. Get context brief for prompt injection
        brief = engine.get_learning_brief("trading")
    """

    def __init__(self, data_dir: Path):
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._events: Dict[str, List[LearningEvent]] = {}   # domain → events
        self._weights: Dict[str, Dict[str, float]] = {}     # domain → {cat: weight}
        self._insights: Dict[str, List[PatternInsight]] = {}
        self._adjustments: List[WeightAdjustment] = []
        self._load()

    # ── Persistence ──────────────────────────────────────────────────

    def _domain_file(self, domain: str) -> Path:
        return self._data_dir / f"events_{domain}.json"

    def _weights_file(self, domain: str) -> Path:
        return self._data_dir / f"weights_{domain}.json"

    def _insights_file(self, domain: str) -> Path:
        return self._data_dir / f"insights_{domain}.json"

    def _adjustments_file(self) -> Path:
        return self._data_dir / "weight_adjustments.json"

    def _load(self):
        """Load all existing learning data from disk."""
        for f in self._data_dir.glob("events_*.json"):
            domain = f.stem.replace("events_", "")
            try:
                raw = json.loads(f.read_text())
                self._events[domain] = [LearningEvent(**e) for e in raw]
            except Exception as e:
                logger.warning(f"Failed to load {f}: {e}")
                self._events[domain] = []

        for f in self._data_dir.glob("weights_*.json"):
            domain = f.stem.replace("weights_", "")
            try:
                self._weights[domain] = json.loads(f.read_text())
            except Exception:
                self._weights[domain] = {}

        for f in self._data_dir.glob("insights_*.json"):
            domain = f.stem.replace("insights_", "")
            try:
                raw = json.loads(f.read_text())
                self._insights[domain] = [PatternInsight(**i) for i in raw]
            except Exception:
                self._insights[domain] = []

        af = self._adjustments_file()
        if af.exists():
            try:
                raw = json.loads(af.read_text())
                self._adjustments = [WeightAdjustment(**a) for a in raw
                                     if isinstance(a, dict)]
            except Exception:
                self._adjustments = []

    def _save_domain(self, domain: str):
        """Persist a single domain's events and weights."""
        events = self._events.get(domain, [])
        # Prune old events
        if len(events) > MAX_EVENTS:
            events.sort(key=lambda e: e.timestamp)
            events = events[-MAX_EVENTS:]
            self._events[domain] = events

        self._domain_file(domain).write_text(
            json.dumps([asdict(e) for e in events], indent=1, default=str)
        )
        if domain in self._weights:
            self._weights_file(domain).write_text(
                json.dumps(self._weights[domain], indent=2)
            )
        if domain in self._insights:
            self._insights_file(domain).write_text(
                json.dumps([asdict(i) for i in self._insights[domain]],
                           indent=2, default=str)
            )

    def _save_adjustments(self):
        self._adjustments_file().write_text(
            json.dumps([asdict(a) for a in self._adjustments[-500:]],
                       indent=1, default=str)
        )

    # ── 1. Event Logging ─────────────────────────────────────────────

    def log_event(self, domain: str, category: str, action: str,
                  context: Dict[str, Any] = None,
                  tags: List[str] = None) -> str:
        """Log an action event. Returns event_id for later outcome recording."""
        event_id = f"{domain}_{int(time.time()*1000)}_{os.urandom(3).hex()}"
        event = LearningEvent(
            event_id=event_id,
            domain=domain,
            category=category,
            action=action,
            context=context or {},
            tags=tags or [],
        )
        self._events.setdefault(domain, []).append(event)
        self._save_domain(domain)
        return event_id

    # ── 2. Outcome Recording ─────────────────────────────────────────

    def record_outcome(self, event_id: str, score: float,
                       details: Dict[str, Any] = None) -> bool:
        """
        Record the outcome of a previously logged event.

        Args:
            event_id: ID returned from log_event()
            score: Normalized outcome score from -1.0 (worst) to +1.0 (best)
            details: Freeform outcome details (PnL, duration, etc.)
        """
        for domain, events in self._events.items():
            for event in events:
                if event.event_id == event_id:
                    event.outcome_score = max(-1.0, min(1.0, score))
                    event.outcome = details or {}
                    event.outcome_timestamp = time.time()
                    self._save_domain(domain)
                    logger.info(f"[LEARN] Outcome recorded: {event_id} → "
                                f"score={score:.2f} ({domain}/{event.category})")
                    return True
        logger.warning(f"[LEARN] Event not found: {event_id}")
        return False

    def record_outcome_by_context(self, domain: str, match_fn, score: float,
                                  details: Dict[str, Any] = None) -> int:
        """
        Record outcomes on events matching a custom function.
        Useful when event_id wasn't stored (e.g., trade journal backfill).

        Args:
            match_fn: callable(LearningEvent) → bool
            Returns count of events matched.
        """
        count = 0
        for event in self._events.get(domain, []):
            if not event.has_outcome and match_fn(event):
                event.outcome_score = max(-1.0, min(1.0, score))
                event.outcome = details or {}
                event.outcome_timestamp = time.time()
                count += 1
        if count:
            self._save_domain(domain)
        return count

    # ── 3. Pattern Analysis ──────────────────────────────────────────

    def analyze(self, domain: str) -> List[PatternInsight]:
        """
        Crunch all events with outcomes in a domain to find patterns.
        Returns a list of PatternInsight objects.
        """
        events = [e for e in self._events.get(domain, []) if e.has_outcome]
        if not events:
            return []

        # Group by category
        by_cat: Dict[str, List[LearningEvent]] = defaultdict(list)
        for e in events:
            by_cat[e.category].append(e)

        insights = []
        for cat, cat_events in by_cat.items():
            n = len(cat_events)
            if n < 2:
                continue

            # Basic stats
            wins = sum(1 for e in cat_events if e.outcome_score > 0)
            win_rate = wins / n

            # Simple average
            avg_score = sum(e.outcome_score for e in cat_events) / n

            # Decay-weighted average
            total_w = sum(e.decay_weight for e in cat_events)
            if total_w > 0:
                weighted_avg = sum(e.outcome_score * e.decay_weight
                                  for e in cat_events) / total_w
            else:
                weighted_avg = avg_score

            # Confidence level
            confidence = "low"
            for level, threshold in sorted(CONFIDENCE_THRESHOLDS.items(),
                                           key=lambda x: x[1], reverse=True):
                if n >= threshold:
                    confidence = level
                    break

            # Trend detection (compare recent half vs older half)
            sorted_events = sorted(cat_events, key=lambda e: e.timestamp)
            mid = len(sorted_events) // 2
            if mid > 0:
                old_avg = sum(e.outcome_score for e in sorted_events[:mid]) / mid
                new_avg = sum(e.outcome_score for e in sorted_events[mid:]) / max(1, len(sorted_events) - mid)
                diff = new_avg - old_avg
                if diff > 0.1:
                    trend = "improving"
                elif diff < -0.1:
                    trend = "declining"
                else:
                    trend = "stable"
            else:
                trend = "stable"

            # Best/worst context feature extraction
            best_events = sorted(cat_events, key=lambda e: e.outcome_score, reverse=True)[:3]
            worst_events = sorted(cat_events, key=lambda e: e.outcome_score)[:3]
            best_ctx = self._extract_common_context(best_events)
            worst_ctx = self._extract_common_context(worst_events)

            insight = PatternInsight(
                category=cat,
                domain=domain,
                sample_count=n,
                win_rate=win_rate,
                avg_score=avg_score,
                weighted_avg_score=weighted_avg,
                confidence=confidence,
                best_context=best_ctx,
                worst_context=worst_ctx,
                trend=trend,
            )
            insights.append(insight)

        # Sort by confidence then by sample count
        insights.sort(key=lambda i: (-CONFIDENCE_THRESHOLDS.get(i.confidence, 0),
                                     -i.sample_count))
        self._insights[domain] = insights
        self._save_domain(domain)
        return insights

    @staticmethod
    def _extract_common_context(events: List[LearningEvent]) -> Dict[str, Any]:
        """Extract common numeric ranges and string values from contexts."""
        if not events:
            return {}
        common = {}
        all_keys = set()
        for e in events:
            all_keys.update(e.context.keys())
        for key in all_keys:
            vals = [e.context[key] for e in events if key in e.context]
            if not vals:
                continue
            if all(isinstance(v, (int, float)) for v in vals):
                common[key] = {
                    "avg": sum(vals) / len(vals),
                    "min": min(vals),
                    "max": max(vals),
                }
            elif all(isinstance(v, str) for v in vals):
                # Most common string value
                from collections import Counter
                mc = Counter(vals).most_common(1)
                if mc:
                    common[key] = mc[0][0]
        return common

    # ── 4. Adaptive Weight Calculation ────────────────────────────────

    def get_adaptive_weights(self, domain: str,
                             base_weights: Dict[str, float]) -> Dict[str, float]:
        """
        Compute adapted weights by blending base_weights with learned performance.

        Categories with enough data get their weights shifted toward their
        actual effectiveness. Categories with insufficient data keep base weights.

        Uses EMA blending: new_weight = (1-α)*base + α*performance_weight
        """
        insights = self._insights.get(domain) or self.analyze(domain)
        insight_map = {i.category: i for i in insights}

        adapted = {}
        for cat, base_w in base_weights.items():
            insight = insight_map.get(cat)
            if not insight or insight.sample_count < MIN_SAMPLES:
                adapted[cat] = base_w
                continue

            # Performance weight: scale base by win rate and trend
            perf_factor = insight.weighted_avg_score  # -1 to +1
            # Map to a multiplier: score of +1 → 2x base, score of -1 → 0.1x base
            multiplier = max(0.1, 1.0 + perf_factor)
            perf_weight = base_w * multiplier

            # EMA blend
            old_weight = self._weights.get(domain, {}).get(cat, base_w)
            new_weight = (1 - EMA_ALPHA) * old_weight + EMA_ALPHA * perf_weight

            # Clamp to reasonable range (0.1x to 5x base)
            new_weight = max(base_w * 0.1, min(base_w * 5.0, new_weight))

            # Log significant changes
            if abs(new_weight - old_weight) > 0.01:
                adj = WeightAdjustment(
                    category=cat, domain=domain,
                    old_weight=old_weight, new_weight=new_weight,
                    reason=(f"win_rate={insight.win_rate:.0%} "
                            f"(n={insight.sample_count}, {insight.confidence}), "
                            f"trend={insight.trend}, "
                            f"weighted_avg={insight.weighted_avg_score:.2f}"),
                    insight=insight,
                )
                self._adjustments.append(adj)
                logger.info(f"[LEARN] Weight adjusted: {domain}/{cat} "
                            f"{old_weight:.2f} → {new_weight:.2f} "
                            f"({adj.reason})")

            adapted[cat] = round(new_weight, 4)

        # Save updated weights
        self._weights[domain] = adapted
        self._save_domain(domain)
        self._save_adjustments()
        return adapted

    # ── 5. Context Injection (Learning Brief) ─────────────────────────

    def get_learning_brief(self, domain: str, max_chars: int = 2000) -> str:
        """
        Generate a human-readable learning brief for prompt injection.
        This is what gets added to Jarvis's context each cycle.
        """
        insights = self._insights.get(domain) or self.analyze(domain)
        if not insights:
            return ""

        events = self._events.get(domain, [])
        with_outcome = [e for e in events if e.has_outcome]
        total_events = len(with_outcome)
        if total_events == 0:
            return ""

        overall_wins = sum(1 for e in with_outcome if e.outcome_score > 0)
        overall_wr = overall_wins / total_events if total_events else 0

        lines = [
            f"📊 LEARNING INTELLIGENCE ({domain.upper()}) — {total_events} events analyzed",
            f"Overall: {overall_wr:.0%} success rate",
            "",
        ]

        # Top performers
        good = [i for i in insights if i.win_rate >= 0.5 and i.sample_count >= MIN_SAMPLES]
        if good:
            good.sort(key=lambda i: i.weighted_avg_score, reverse=True)
            lines.append("✅ STRONG PATTERNS (lean into these):")
            for i in good[:5]:
                trend_icon = {"improving": "📈", "stable": "➡️", "declining": "📉"}.get(i.trend, "")
                lines.append(
                    f"  • {i.category}: {i.win_rate:.0%} win rate "
                    f"(n={i.sample_count}, {i.confidence}) {trend_icon}"
                )
            lines.append("")

        # Underperformers
        bad = [i for i in insights if i.win_rate < 0.4 and i.sample_count >= MIN_SAMPLES]
        if bad:
            bad.sort(key=lambda i: i.weighted_avg_score)
            lines.append("⚠️ WEAK PATTERNS (reduce exposure):")
            for i in bad[:5]:
                trend_icon = {"improving": "📈", "stable": "➡️", "declining": "📉"}.get(i.trend, "")
                lines.append(
                    f"  • {i.category}: {i.win_rate:.0%} win rate "
                    f"(n={i.sample_count}, {i.confidence}) {trend_icon}"
                )
            lines.append("")

        # Insufficient data
        low_data = [i for i in insights if i.sample_count < MIN_SAMPLES]
        if low_data:
            cats = ", ".join(i.category for i in low_data[:8])
            lines.append(f"📋 Need more data: {cats}")
            lines.append("")

        # Recent trend
        recent = sorted(with_outcome, key=lambda e: e.timestamp, reverse=True)[:10]
        if len(recent) >= 3:
            recent_wins = sum(1 for e in recent if e.outcome_score > 0)
            recent_wr = recent_wins / len(recent)
            streak_icon = "🔥" if recent_wr > 0.7 else "❄️" if recent_wr < 0.3 else ""
            lines.append(f"Recent ({len(recent)} events): {recent_wr:.0%} success {streak_icon}")

        brief = "\n".join(lines)
        return brief[:max_chars]

    # ── 6. Query Interface ────────────────────────────────────────────

    def get_domain_stats(self, domain: str) -> Dict[str, Any]:
        """Get summary stats for a domain."""
        events = self._events.get(domain, [])
        with_outcome = [e for e in events if e.has_outcome]
        pending = [e for e in events if not e.has_outcome]
        insights = self._insights.get(domain, [])
        weights = self._weights.get(domain, {})

        return {
            "domain": domain,
            "total_events": len(events),
            "events_with_outcome": len(with_outcome),
            "pending_outcomes": len(pending),
            "categories_tracked": len(set(e.category for e in events)),
            "insights_count": len(insights),
            "adapted_weights": len(weights),
            "oldest_event": min((e.timestamp for e in events), default=0),
            "newest_event": max((e.timestamp for e in events), default=0),
        }

    def get_category_detail(self, domain: str, category: str) -> Dict[str, Any]:
        """Get detailed analysis for a specific category."""
        events = [e for e in self._events.get(domain, [])
                  if e.category == category and e.has_outcome]
        if not events:
            return {"category": category, "event_count": 0}

        insight = None
        for i in self._insights.get(domain, []):
            if i.category == category:
                insight = i
                break

        recent = sorted(events, key=lambda e: e.timestamp, reverse=True)[:10]
        return {
            "category": category,
            "event_count": len(events),
            "insight": asdict(insight) if insight else None,
            "recent_outcomes": [
                {
                    "score": e.outcome_score,
                    "action": e.action,
                    "outcome": e.outcome,
                    "age_hours": e.age_seconds / 3600,
                }
                for e in recent
            ],
            "current_weight": self._weights.get(domain, {}).get(category),
        }

    def get_all_domains(self) -> List[str]:
        """Return all domains that have events."""
        return list(self._events.keys())
