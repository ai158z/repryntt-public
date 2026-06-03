"""
repryntt.learning.llm_learner — Recursive Learning for Local LLM Orchestration
================================================================================
Teaches the orchestration layer to USE the local LLM smarter, instead of
trying to make the LLM itself smarter (which doesn't work at 3B Q4).

Three subsystems:
  1. Context Budget Optimizer  — learn which context items improve outputs
  2. Escalation Learner        — learn which tasks the local model fails at
  3. Output Quality Gate       — score outputs and reject bad ones early

Scales with model capability:
  - 3B Q4 (tiny):  Learns to route around the model's weaknesses
  - 7B+ (capable): Learns to inject briefs/weights into prompts the model follows
  - 70B (strong):  Full context injection, minimal routing needed

Design matches the existing LearningEngine pattern (EMA, decay, persistence).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------
DECAY_HALF_LIFE_DAYS = 10.0        # Faster decay than trading (LLM behavior shifts)
DECAY_LAMBDA = math.log(2) / (DECAY_HALF_LIFE_DAYS * 86400)
MIN_SAMPLES = 5                    # Fewer samples needed (LLM calls are frequent)
EMA_ALPHA = 0.2                    # Slightly faster adaptation
MAX_EVENTS = 3000                  # Cap per subsystem
QUALITY_SCORE_THRESHOLD = 0.3      # Below this → output rejected
ESCALATION_FAILURE_THRESHOLD = 0.4 # Below this → task type escalated to cloud

# Context item types and their default priority weights
DEFAULT_CONTEXT_PRIORITIES = {
    "system_identity": 1.0,        # Always include — who am I
    "active_task": 0.95,           # Current task description
    "bootstrap_context": 0.85,     # Identity/priorities/memory brief
    "recent_messages": 0.80,       # Last few conversation messages
    "tool_descriptions": 0.70,     # Available tool schemas
    "hormone_state": 0.60,         # Current emotional context
    "skill_context": 0.55,         # Matched skill files
    "memory_brief": 0.50,          # Compressed memory summary
    "drive_priorities": 0.45,      # Active consciousness drives
    "learning_brief": 0.40,        # Learning engine summary
    "recent_reflections": 0.35,    # Past self-reflections
    "chain_history": 0.30,         # Previous chain steps
    "feeder_stimulus": 0.20,       # Sensor/news/curiosity feeds
}

# Task type taxonomy for escalation tracking
TASK_TYPES = [
    "chain_step", "background_thinking", "task_execution", "tool_synthesis",
    "self_reflection", "morning_startup", "trading_analysis", "research",
    "code_generation", "creative_writing", "summarization", "classification",
    "planning", "conversation", "general",
]


# ---------------------------------------------------------------------------
#  Data classes
# ---------------------------------------------------------------------------

@dataclass
class LLMCall:
    """A single LLM invocation with metadata and outcome."""
    call_id: str
    timestamp: float = 0.0
    task_type: str = "general"          # From TASK_TYPES
    provider: str = "local"             # "local", "openai", "anthropic", etc.
    model: str = "default"
    prompt_tokens: int = 0              # Estimated input tokens
    response_tokens: int = 0            # Estimated output tokens
    context_items: Dict[str, int] = field(default_factory=dict)  # item_type → token_count
    latency_ms: float = 0.0
    quality_score: Optional[float] = None  # 0.0 to 1.0 (set by quality gate)
    was_useful: Optional[bool] = None      # Did it advance the task?
    was_escalated: bool = False            # Was this escalated from local?
    error: Optional[str] = None            # Error string if failed
    quality_signals: Dict[str, float] = field(default_factory=dict)  # Individual quality metrics

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp

    @property
    def decay_weight(self) -> float:
        return math.exp(-DECAY_LAMBDA * self.age_seconds)


@dataclass
class ContextEffectiveness:
    """Tracks how a context item type correlates with output quality."""
    item_type: str
    times_included: int = 0
    times_excluded: int = 0
    avg_quality_when_included: float = 0.5
    avg_quality_when_excluded: float = 0.5
    ema_effectiveness: float = 0.5        # EMA of (quality_included - quality_excluded)
    priority_weight: float = 0.5          # Current optimized priority
    sample_count: int = 0


@dataclass
class EscalationRule:
    """Learned rule about when to escalate to a higher-tier model."""
    task_type: str
    local_success_rate: float = 0.5       # Success rate on local model
    local_avg_quality: float = 0.5
    cloud_success_rate: float = 0.5
    cloud_avg_quality: float = 0.5
    local_samples: int = 0
    cloud_samples: int = 0
    should_escalate: bool = False         # Learned recommendation
    confidence: str = "low"               # low, medium, high, very_high

    # ── Multi-tier cortex escalation (Phase 7) ───────────────────────
    # Tracks success rates across 4 tiers:
    #   0 = cortex_small  (conscious layer: 135M-360M, <500ms)
    #   1 = cortex_medium (local LLM: 3B-7B via llama-server, 2-5s)
    #   2 = cloud         (API: Nemotron 49B etc., 5-30s)
    #   3 = consensus     (multi-model agreement, critical only)
    tier_stats: Dict[str, Any] = field(default_factory=lambda: {
        "cortex_small": {"samples": 0, "avg_quality": 0.5, "success_rate": 0.5},
        "cortex_medium": {"samples": 0, "avg_quality": 0.5, "success_rate": 0.5},
        "cloud": {"samples": 0, "avg_quality": 0.5, "success_rate": 0.5},
        "consensus": {"samples": 0, "avg_quality": 0.5, "success_rate": 0.5},
    })
    recommended_tier: str = "cortex_medium"  # Current learned recommendation


# ── Escalation tier definitions ──────────────────────────────────────────

ESCALATION_TIERS = ["cortex_small", "cortex_medium", "cloud", "consensus"]

# Map provider strings to tiers
PROVIDER_TO_TIER = {
    "cortex_small": "cortex_small",
    "cortex_conscious": "cortex_small",
    "local": "cortex_medium",
    "cortex_medium": "cortex_medium",
    "openai": "cloud",
    "anthropic": "cloud",
    "nvidia": "cloud",
    "openrouter": "cloud",
    "cloud": "cloud",
    "consensus": "consensus",
}


# ---------------------------------------------------------------------------
#  LLM Learner — the orchestration-level learning engine
# ---------------------------------------------------------------------------

class LLMLearner:
    """
    Recursive learning system for local LLM orchestration.

    Learns three things:
      1. Which context items matter most for the current model's output quality
      2. Which task types the local model consistently fails at → escalate
      3. Patterns in output quality → reject bad outputs before they propagate

    Usage:
        learner = LLMLearner(data_dir=Path("brain/llm_learning"))

        # Before each LLM call:
        budget = learner.get_context_budget(task_type="chain_step", max_tokens=3000)
        should_escalate = learner.should_escalate(task_type="code_generation")

        # After each LLM call:
        call_id = learner.log_call(task_type, provider, prompt_tokens, ...)
        quality = learner.score_output(response_text, task_type)
        learner.record_outcome(call_id, quality_score=quality, was_useful=True)
    """

    def __init__(self, data_dir: Path = None):
        self._data_dir = Path(data_dir or "brain/llm_learning")
        self._data_dir.mkdir(parents=True, exist_ok=True)

        self._calls: List[LLMCall] = []
        self._context_effectiveness: Dict[str, ContextEffectiveness] = {}
        self._escalation_rules: Dict[str, EscalationRule] = {}
        self._model_profile: Dict[str, Any] = {}  # Detected model capabilities

        self._load()

    # ── Persistence ──────────────────────────────────────────────────

    def _calls_file(self) -> Path:
        return self._data_dir / "llm_calls.json"

    def _context_file(self) -> Path:
        return self._data_dir / "context_effectiveness.json"

    def _escalation_file(self) -> Path:
        return self._data_dir / "escalation_rules.json"

    def _profile_file(self) -> Path:
        return self._data_dir / "model_profile.json"

    def _load(self):
        """Load persisted learning data."""
        # Calls
        cf = self._calls_file()
        if cf.exists():
            try:
                raw = json.loads(cf.read_text())
                self._calls = [LLMCall(**c) for c in raw]
            except Exception as e:
                logger.warning(f"Failed to load LLM calls: {e}")
                self._calls = []

        # Context effectiveness
        ef = self._context_file()
        if ef.exists():
            try:
                raw = json.loads(ef.read_text())
                self._context_effectiveness = {
                    k: ContextEffectiveness(**v) for k, v in raw.items()
                }
            except Exception as e:
                logger.warning(f"Failed to load context effectiveness: {e}")

        # Escalation rules
        esc = self._escalation_file()
        if esc.exists():
            try:
                raw = json.loads(esc.read_text())
                self._escalation_rules = {
                    k: EscalationRule(**v) for k, v in raw.items()
                }
            except Exception as e:
                logger.warning(f"Failed to load escalation rules: {e}")

        # Model profile
        pf = self._profile_file()
        if pf.exists():
            try:
                self._model_profile = json.loads(pf.read_text())
            except Exception:
                self._model_profile = {}

    def _save(self):
        """Persist all learning data."""
        # Prune old calls
        if len(self._calls) > MAX_EVENTS:
            self._calls.sort(key=lambda c: c.timestamp)
            self._calls = self._calls[-MAX_EVENTS:]

        self._calls_file().write_text(
            json.dumps([asdict(c) for c in self._calls], indent=1, default=str)
        )
        self._context_file().write_text(
            json.dumps(
                {k: asdict(v) for k, v in self._context_effectiveness.items()},
                indent=2, default=str,
            )
        )
        self._escalation_file().write_text(
            json.dumps(
                {k: asdict(v) for k, v in self._escalation_rules.items()},
                indent=2, default=str,
            )
        )
        self._profile_file().write_text(
            json.dumps(self._model_profile, indent=2, default=str)
        )

    # ══════════════════════════════════════════════════════════════════
    #  1. CONTEXT BUDGET OPTIMIZER
    # ══════════════════════════════════════════════════════════════════

    def get_context_budget(
        self,
        task_type: str = "general",
        max_tokens: int = 3000,
        available_items: Dict[str, int] = None,
    ) -> Dict[str, int]:
        """
        Return optimized token allocation for each context item type.

        Args:
            task_type: Type of task being performed
            max_tokens: Total token budget for context
            available_items: {item_type: estimated_tokens} of what's available

        Returns:
            {item_type: allocated_tokens} — items not in result should be excluded
        """
        if not available_items:
            return {}

        # Get learned priorities (or defaults for new/unknown items)
        priorities = {}
        for item_type in available_items:
            if item_type in self._context_effectiveness:
                eff = self._context_effectiveness[item_type]
                # Blend learned priority with default (safety net)
                default = DEFAULT_CONTEXT_PRIORITIES.get(item_type, 0.3)
                if eff.sample_count >= MIN_SAMPLES:
                    # Learned priority dominates after enough samples
                    priorities[item_type] = 0.7 * eff.priority_weight + 0.3 * default
                else:
                    # Not enough data — lean on defaults
                    priorities[item_type] = 0.3 * eff.priority_weight + 0.7 * default
            else:
                priorities[item_type] = DEFAULT_CONTEXT_PRIORITIES.get(item_type, 0.3)

        # Task-specific adjustments
        task_boosts = self._get_task_context_boosts(task_type)
        for item_type, boost in task_boosts.items():
            if item_type in priorities:
                priorities[item_type] = min(1.0, priorities[item_type] + boost)

        # Sort by priority, allocate greedily
        sorted_items = sorted(
            priorities.items(), key=lambda x: x[1], reverse=True
        )

        allocated = {}
        remaining = max_tokens
        for item_type, priority in sorted_items:
            if remaining <= 0:
                break
            requested = available_items.get(item_type, 0)
            if requested <= 0:
                continue
            # High-priority items get full allocation, low-priority may be trimmed
            if priority >= 0.8:
                alloc = min(requested, remaining)
            elif priority >= 0.5:
                alloc = min(requested, remaining, int(max_tokens * 0.25))
            else:
                alloc = min(requested, remaining, int(max_tokens * 0.10))
            if alloc > 0:
                allocated[item_type] = alloc
                remaining -= alloc

        return allocated

    def _get_task_context_boosts(self, task_type: str) -> Dict[str, float]:
        """Task-specific priority boosts for context items."""
        boosts: Dict[str, float] = {}
        if task_type in ("code_generation", "tool_synthesis"):
            boosts["tool_descriptions"] = 0.2
            boosts["skill_context"] = 0.15
        elif task_type in ("trading_analysis",):
            boosts["learning_brief"] = 0.2
            boosts["feeder_stimulus"] = 0.15
        elif task_type in ("self_reflection", "background_thinking"):
            boosts["recent_reflections"] = 0.2
            boosts["hormone_state"] = 0.15
            boosts["drive_priorities"] = 0.15
        elif task_type in ("conversation",):
            boosts["recent_messages"] = 0.2
            boosts["memory_brief"] = 0.15
        elif task_type in ("planning", "morning_startup"):
            boosts["bootstrap_context"] = 0.15
            boosts["drive_priorities"] = 0.15
        return boosts

    def update_context_effectiveness(self, call: LLMCall):
        """
        After scoring an output, update which context items helped.

        The insight: if quality is high when item X is included and low
        when it's excluded, item X is effective for this model.
        """
        if call.quality_score is None:
            return

        for item_type in DEFAULT_CONTEXT_PRIORITIES:
            eff = self._context_effectiveness.setdefault(
                item_type,
                ContextEffectiveness(
                    item_type=item_type,
                    priority_weight=DEFAULT_CONTEXT_PRIORITIES.get(item_type, 0.3),
                ),
            )

            included = item_type in call.context_items and call.context_items[item_type] > 0

            if included:
                eff.times_included += 1
                # EMA update for quality-when-included
                eff.avg_quality_when_included = (
                    EMA_ALPHA * call.quality_score
                    + (1 - EMA_ALPHA) * eff.avg_quality_when_included
                )
            else:
                eff.times_excluded += 1
                eff.avg_quality_when_excluded = (
                    EMA_ALPHA * call.quality_score
                    + (1 - EMA_ALPHA) * eff.avg_quality_when_excluded
                )

            eff.sample_count += 1

            # Update effectiveness EMA
            delta = eff.avg_quality_when_included - eff.avg_quality_when_excluded
            eff.ema_effectiveness = (
                EMA_ALPHA * delta + (1 - EMA_ALPHA) * eff.ema_effectiveness
            )

            # Adjust priority weight based on effectiveness
            if eff.sample_count >= MIN_SAMPLES:
                # Map effectiveness [-1, 1] to priority [0.1, 1.0]
                default = DEFAULT_CONTEXT_PRIORITIES.get(item_type, 0.3)
                adjustment = eff.ema_effectiveness * 0.3  # Max ±30% shift
                eff.priority_weight = max(0.1, min(1.0, default + adjustment))

    # ══════════════════════════════════════════════════════════════════
    #  2. ESCALATION LEARNER
    # ══════════════════════════════════════════════════════════════════

    def should_escalate(self, task_type: str) -> bool:
        """
        Should this task type be escalated to a cloud model?

        Returns True if the local model consistently fails at this task type.
        """
        rule = self._escalation_rules.get(task_type)
        if not rule:
            return False  # No data → default to local (free)

        if rule.local_samples < MIN_SAMPLES:
            return False  # Not enough data to decide

        return rule.should_escalate

    def get_escalation_recommendation(self, task_type: str) -> Dict[str, Any]:
        """Get detailed escalation info for a task type."""
        rule = self._escalation_rules.get(task_type)
        if not rule:
            return {
                "task_type": task_type,
                "recommendation": "local",
                "reason": "No data — defaulting to local (free)",
                "confidence": "none",
            }
        return {
            "task_type": task_type,
            "recommendation": "escalate" if rule.should_escalate else "local",
            "local_success_rate": round(rule.local_success_rate, 3),
            "local_avg_quality": round(rule.local_avg_quality, 3),
            "cloud_success_rate": round(rule.cloud_success_rate, 3),
            "cloud_avg_quality": round(rule.cloud_avg_quality, 3),
            "local_samples": rule.local_samples,
            "cloud_samples": rule.cloud_samples,
            "confidence": rule.confidence,
            "reason": self._escalation_reason(rule),
        }

    def _escalation_reason(self, rule: EscalationRule) -> str:
        if rule.local_samples < MIN_SAMPLES:
            return f"Insufficient data ({rule.local_samples}/{MIN_SAMPLES} samples)"
        if rule.should_escalate:
            return (
                f"Local model quality {rule.local_avg_quality:.2f} is below "
                f"threshold {ESCALATION_FAILURE_THRESHOLD:.2f} for {rule.task_type}"
            )
        return f"Local model adequate (quality {rule.local_avg_quality:.2f})"

    # ══════════════════════════════════════════════════════════════════
    #  2b. MULTI-TIER ESCALATION (Cortex Integration)
    # ══════════════════════════════════════════════════════════════════

    def recommend_tier(self, task_type: str) -> str:
        """Recommend the best tier for a task type based on learned performance.

        Returns one of: "cortex_small", "cortex_medium", "cloud", "consensus"

        Strategy: use the cheapest/fastest tier whose quality exceeds the
        threshold.  Falls through to the next tier when quality is insufficient.

        Tier ladder:
          cortex_small  → 135-360M local model, <500ms, free
          cortex_medium → 3-7B local model, 2-5s, free
          cloud         → 49B+ API model, 5-30s, rate-limited
          consensus     → multi-model, 30-60s, expensive (critical only)
        """
        rule = self._escalation_rules.get(task_type)
        if not rule or not rule.tier_stats:
            return "cortex_medium"  # Default: local llama-server

        # Walk the tier ladder from cheapest to most expensive
        for tier_name in ESCALATION_TIERS:
            stats = rule.tier_stats.get(tier_name, {})
            samples = stats.get("samples", 0)
            avg_quality = stats.get("avg_quality", 0.5)

            # Not enough data for this tier → skip to next
            if samples < MIN_SAMPLES:
                continue

            # This tier is good enough → use it
            if avg_quality >= ESCALATION_FAILURE_THRESHOLD:
                return tier_name

        # Nothing has enough data or everything is below threshold
        # Default to cloud (safest for quality)
        return rule.recommended_tier

    def update_tier_stats(self, call: LLMCall) -> None:
        """Update multi-tier escalation stats from a completed call."""
        if call.quality_score is None:
            return

        task_type = call.task_type
        rule = self._escalation_rules.setdefault(
            task_type, EscalationRule(task_type=task_type),
        )

        # Map provider to tier
        tier = PROVIDER_TO_TIER.get(call.provider, "cloud")
        stats = rule.tier_stats.setdefault(
            tier, {"samples": 0, "avg_quality": 0.5, "success_rate": 0.5},
        )

        is_success = call.quality_score >= QUALITY_SCORE_THRESHOLD
        stats["samples"] += 1
        stats["avg_quality"] = (
            EMA_ALPHA * call.quality_score
            + (1 - EMA_ALPHA) * stats["avg_quality"]
        )
        stats["success_rate"] = (
            EMA_ALPHA * (1.0 if is_success else 0.0)
            + (1 - EMA_ALPHA) * stats["success_rate"]
        )

        # Recompute recommended tier
        rule.recommended_tier = self._compute_recommended_tier(rule)

    def _compute_recommended_tier(self, rule: EscalationRule) -> str:
        """Find the cheapest tier that meets quality threshold."""
        for tier_name in ESCALATION_TIERS:
            stats = rule.tier_stats.get(tier_name, {})
            if stats.get("samples", 0) >= MIN_SAMPLES:
                if stats.get("avg_quality", 0) >= ESCALATION_FAILURE_THRESHOLD:
                    return tier_name
        return "cortex_medium"  # Sensible default

    def get_tier_report(self) -> Dict[str, Any]:
        """Full multi-tier escalation report for all task types."""
        report = {}
        for task_type, rule in self._escalation_rules.items():
            tier_info = {}
            for tier_name in ESCALATION_TIERS:
                stats = rule.tier_stats.get(tier_name, {})
                tier_info[tier_name] = {
                    "samples": stats.get("samples", 0),
                    "avg_quality": round(stats.get("avg_quality", 0.5), 3),
                    "success_rate": round(stats.get("success_rate", 0.5), 3),
                }
            report[task_type] = {
                "recommended_tier": rule.recommended_tier,
                "tiers": tier_info,
            }
        return report

    def update_escalation_rules(self, call: LLMCall):
        """Update escalation rules based on a completed call."""
        if call.quality_score is None:
            return

        task_type = call.task_type
        rule = self._escalation_rules.setdefault(
            task_type, EscalationRule(task_type=task_type)
        )

        is_local = call.provider == "local"
        is_success = call.quality_score >= QUALITY_SCORE_THRESHOLD

        if is_local:
            rule.local_samples += 1
            rule.local_success_rate = (
                EMA_ALPHA * (1.0 if is_success else 0.0)
                + (1 - EMA_ALPHA) * rule.local_success_rate
            )
            rule.local_avg_quality = (
                EMA_ALPHA * call.quality_score
                + (1 - EMA_ALPHA) * rule.local_avg_quality
            )
        else:
            rule.cloud_samples += 1
            rule.cloud_success_rate = (
                EMA_ALPHA * (1.0 if is_success else 0.0)
                + (1 - EMA_ALPHA) * rule.cloud_success_rate
            )
            rule.cloud_avg_quality = (
                EMA_ALPHA * call.quality_score
                + (1 - EMA_ALPHA) * rule.cloud_avg_quality
            )

        # Update escalation decision
        total = rule.local_samples + rule.cloud_samples
        if rule.local_samples >= MIN_SAMPLES:
            rule.should_escalate = rule.local_avg_quality < ESCALATION_FAILURE_THRESHOLD
            if total >= 50:
                rule.confidence = "very_high"
            elif total >= 25:
                rule.confidence = "high"
            elif total >= 15:
                rule.confidence = "medium"
            else:
                rule.confidence = "low"
        else:
            rule.should_escalate = False
            rule.confidence = "low"

    # ══════════════════════════════════════════════════════════════════
    #  3. OUTPUT QUALITY GATE
    # ══════════════════════════════════════════════════════════════════

    def score_output(self, response: str, task_type: str = "general") -> float:
        """
        Score an LLM output's quality using cheap heuristics (no LLM call).

        Returns 0.0 to 1.0 where:
          - 0.0 = garbage (empty, error, repetition)
          - 0.5 = mediocre (generic, off-topic)
          - 1.0 = excellent (structured, relevant, actionable)

        Signals are weighted by learned effectiveness for this model.
        """
        if not response or not response.strip():
            return 0.0

        signals = {}

        # Signal 1: Length adequacy (too short = bad, too long for context = bad)
        length = len(response.strip())
        if length < 20:
            signals["length"] = 0.1
        elif length < 50:
            signals["length"] = 0.3
        elif length < 200:
            signals["length"] = 0.6
        elif length < 2000:
            signals["length"] = 0.9
        else:
            signals["length"] = 0.7  # Slightly penalize very long responses

        # Signal 2: Repetition detection (both sentence-level and character-level)
        sentences = re.split(r'[.!?\n]', response)
        sentences = [s.strip().lower() for s in sentences if len(s.strip()) > 10]
        if len(sentences) >= 2:
            unique_ratio = len(set(sentences)) / len(sentences)
            signals["repetition"] = unique_ratio
        else:
            # Too few sentences — check character-level repetition
            chars = [c for c in response.strip() if c.isalnum()]
            if chars:
                unique_char_ratio = len(set(chars)) / len(chars)
                # Single char repeated → ratio near 0 → score near 0
                signals["repetition"] = min(unique_char_ratio * 5, 1.0)
            else:
                signals["repetition"] = 0.2

        # Signal 3: Coherence — does it look like natural text?
        # Check for garbage: random chars, broken encoding, repeated chars
        garbage_patterns = [
            r'(.)\1{10,}',           # Same char repeated 10+ times
            r'[^\x00-\x7F]{20,}',   # Long non-ASCII stretch
            r'(?:undefined|null|NaN|error){3,}',  # Repeated error tokens
        ]
        coherence = 1.0
        for pattern in garbage_patterns:
            if re.search(pattern, response):
                coherence -= 0.3
        signals["coherence"] = max(0.0, coherence)

        # Signal 4: Structure — does it have paragraphs, lists, or clear structure?
        has_structure = any([
            '\n\n' in response,
            re.search(r'^\s*[-*•]\s', response, re.MULTILINE),
            re.search(r'^\s*\d+[.)]\s', response, re.MULTILINE),
            re.search(r'^#{1,3}\s', response, re.MULTILINE),
        ])
        signals["structure"] = 0.8 if has_structure else 0.4

        # Signal 5: Error indicators
        error_patterns = [
            r'AI_SERVICE_ERROR',
            r'I cannot|I can\'t|I\'m unable',
            r'as an AI|as a language model',
            r'I don\'t have access',
            r'error occurred|exception|traceback',
        ]
        error_hits = sum(1 for p in error_patterns if re.search(p, response, re.IGNORECASE))
        signals["no_errors"] = max(0.0, 1.0 - error_hits * 0.25)

        # Signal 6: Task-specific quality
        signals["task_fit"] = self._task_specific_quality(response, task_type)

        # Signal 7: Tool call formatting (if applicable)
        has_tool_attempt = any(marker in response for marker in [
            "TOOL_CALL:", "tool_name", '"name":', "```json"
        ])
        if has_tool_attempt:
            # Check if it's well-formed
            try:
                # Try to find valid JSON in the response
                json_match = re.search(r'\{[^{}]*"(?:name|tool_name)"[^{}]*\}', response)
                signals["tool_format"] = 0.8 if json_match else 0.3
            except Exception:
                signals["tool_format"] = 0.3
        else:
            signals["tool_format"] = 0.6  # Neutral — not every response needs tools

        # Weighted combination
        weights = self._get_quality_signal_weights()
        total_weight = sum(weights.get(k, 0.5) for k in signals)
        if total_weight == 0:
            return 0.5

        score = sum(signals[k] * weights.get(k, 0.5) for k in signals) / total_weight
        return round(max(0.0, min(1.0, score)), 4)

    def _task_specific_quality(self, response: str, task_type: str) -> float:
        """Task-type-specific quality signal."""
        response_lower = response.lower()

        if task_type == "code_generation":
            has_code = '```' in response or 'def ' in response or 'class ' in response
            return 0.8 if has_code else 0.3

        if task_type == "trading_analysis":
            has_numbers = bool(re.search(r'\$?\d+\.?\d*%?', response))
            has_action = any(w in response_lower for w in ['buy', 'sell', 'hold', 'skip', 'wait'])
            return 0.5 + 0.25 * has_numbers + 0.25 * has_action

        if task_type == "summarization":
            # Good summaries are concise
            return 0.8 if 50 < len(response) < 1000 else 0.5

        if task_type == "classification":
            # Good classifications are short and decisive
            return 0.8 if len(response.strip()) < 200 else 0.4

        if task_type in ("planning", "morning_startup"):
            has_list = bool(re.search(r'^\s*[-*\d]', response, re.MULTILINE))
            return 0.8 if has_list else 0.4

        return 0.6  # Neutral for unknown task types

    def _get_quality_signal_weights(self) -> Dict[str, float]:
        """Get learned weights for quality signals (or defaults)."""
        # Check if we have learned weights from model profile
        learned = self._model_profile.get("quality_signal_weights", {})
        defaults = {
            "length": 0.10,
            "repetition": 0.20,
            "coherence": 0.25,
            "structure": 0.10,
            "no_errors": 0.15,
            "task_fit": 0.10,
            "tool_format": 0.10,
        }
        # Blend learned and defaults
        result = {}
        for k, default in defaults.items():
            if k in learned:
                result[k] = 0.6 * learned[k] + 0.4 * default
            else:
                result[k] = default
        return result

    def should_reject_output(self, quality_score: float) -> bool:
        """Should this output be rejected based on quality score?"""
        threshold = self._model_profile.get(
            "rejection_threshold", QUALITY_SCORE_THRESHOLD
        )
        return quality_score < threshold

    # ══════════════════════════════════════════════════════════════════
    #  Call Logging & Outcome Recording
    # ══════════════════════════════════════════════════════════════════

    def log_call(
        self,
        task_type: str,
        provider: str = "local",
        model: str = "default",
        prompt_tokens: int = 0,
        response_tokens: int = 0,
        context_items: Dict[str, int] = None,
        latency_ms: float = 0.0,
    ) -> str:
        """Log an LLM call. Returns call_id for later outcome recording."""
        call_id = f"llm_{int(time.time()*1000)}_{os.urandom(3).hex()}"
        call = LLMCall(
            call_id=call_id,
            task_type=task_type,
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            response_tokens=response_tokens,
            context_items=context_items or {},
            latency_ms=latency_ms,
        )
        self._calls.append(call)
        return call_id

    def record_outcome(
        self,
        call_id: str,
        quality_score: float,
        was_useful: bool = None,
        error: str = None,
        quality_signals: Dict[str, float] = None,
    ):
        """Record the outcome of an LLM call and update all learning subsystems."""
        call = self._find_call(call_id)
        if not call:
            logger.warning(f"LLM call {call_id} not found for outcome recording")
            return

        call.quality_score = quality_score
        call.was_useful = was_useful
        call.error = error
        call.quality_signals = quality_signals or {}

        # Update all subsystems
        self.update_context_effectiveness(call)
        self.update_escalation_rules(call)
        self.update_tier_stats(call)
        self._update_model_profile(call)

        self._save()

    def _find_call(self, call_id: str) -> Optional[LLMCall]:
        for call in reversed(self._calls):
            if call.call_id == call_id:
                return call
        return None

    # ══════════════════════════════════════════════════════════════════
    #  Model Profiling
    # ══════════════════════════════════════════════════════════════════

    def _update_model_profile(self, call: LLMCall):
        """Build/update a profile of the current model's capabilities."""
        if call.quality_score is None:
            return

        profile = self._model_profile
        profile.setdefault("model_name", call.model)
        profile.setdefault("provider", call.provider)
        profile.setdefault("total_calls", 0)
        profile.setdefault("task_quality", {})
        profile.setdefault("avg_latency_ms", 0.0)
        profile.setdefault("avg_quality", 0.5)

        profile["total_calls"] += 1
        profile["avg_quality"] = (
            EMA_ALPHA * call.quality_score
            + (1 - EMA_ALPHA) * profile["avg_quality"]
        )
        if call.latency_ms > 0:
            profile["avg_latency_ms"] = (
                EMA_ALPHA * call.latency_ms
                + (1 - EMA_ALPHA) * profile["avg_latency_ms"]
            )

        # Per-task-type quality tracking
        tq = profile["task_quality"].setdefault(call.task_type, {
            "avg_quality": 0.5, "samples": 0
        })
        tq["samples"] += 1
        tq["avg_quality"] = (
            EMA_ALPHA * call.quality_score + (1 - EMA_ALPHA) * tq["avg_quality"]
        )

        # Adaptive rejection threshold
        # If model is generally good (>0.6), lower the threshold (less rejection)
        # If model is weak (<0.4), raise the threshold (more aggressive gating)
        if profile["total_calls"] >= MIN_SAMPLES * 2:
            avg_q = profile["avg_quality"]
            if avg_q > 0.6:
                profile["rejection_threshold"] = max(0.15, QUALITY_SCORE_THRESHOLD - 0.1)
            elif avg_q < 0.4:
                profile["rejection_threshold"] = min(0.5, QUALITY_SCORE_THRESHOLD + 0.1)
            else:
                profile["rejection_threshold"] = QUALITY_SCORE_THRESHOLD

    def detect_model_capabilities(self, context_window: int = 4096, model_name: str = ""):
        """
        Set model capability tier. Called once at startup or when model changes.
        Influences how aggressively the system should inject learning data into prompts.
        """
        self._model_profile["context_window"] = context_window
        if model_name:
            self._model_profile["model_name"] = model_name

        # Classify capability tier
        if context_window >= 32768:
            tier = "high"
        elif context_window >= 8192:
            tier = "mid"
        else:
            tier = "low"

        self._model_profile["capability_tier"] = tier

        # Adjust strategies based on tier
        if tier == "high":
            # Big context: inject learning briefs, weight tables, detailed guidance
            self._model_profile["inject_learning_brief"] = True
            self._model_profile["inject_weight_table"] = True
            self._model_profile["max_brief_tokens"] = 500
        elif tier == "mid":
            # Medium: inject brief summary only
            self._model_profile["inject_learning_brief"] = True
            self._model_profile["inject_weight_table"] = False
            self._model_profile["max_brief_tokens"] = 200
        else:
            # Tiny: don't waste tokens on learning briefs the model can't use
            self._model_profile["inject_learning_brief"] = False
            self._model_profile["inject_weight_table"] = False
            self._model_profile["max_brief_tokens"] = 0

        self._save()
        logger.info(
            f"Model profile: {model_name or 'unknown'}, "
            f"context={context_window}, tier={tier}"
        )

    # ══════════════════════════════════════════════════════════════════
    #  Reporting
    # ══════════════════════════════════════════════════════════════════

    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive learning statistics."""
        total_calls = len(self._calls)
        scored = [c for c in self._calls if c.quality_score is not None]
        recent = [c for c in scored if c.age_seconds < 86400]  # Last 24h

        # Per-task-type breakdown
        task_stats = {}
        for call in scored:
            ts = task_stats.setdefault(call.task_type, {
                "count": 0, "avg_quality": 0.0, "escalated": 0
            })
            ts["count"] += 1
            ts["avg_quality"] = (
                EMA_ALPHA * call.quality_score
                + (1 - EMA_ALPHA) * ts["avg_quality"]
            )
            if call.was_escalated:
                ts["escalated"] += 1

        # Context item priorities
        context_priorities = {
            k: round(v.priority_weight, 3)
            for k, v in sorted(
                self._context_effectiveness.items(),
                key=lambda x: x[1].priority_weight,
                reverse=True,
            )
        }

        # Escalation summary
        escalation_summary = {
            k: {
                "escalate": v.should_escalate,
                "local_quality": round(v.local_avg_quality, 3),
                "confidence": v.confidence,
            }
            for k, v in self._escalation_rules.items()
        }

        return {
            "total_calls": total_calls,
            "scored_calls": len(scored),
            "calls_24h": len(recent),
            "avg_quality": round(
                sum(c.quality_score for c in scored) / max(1, len(scored)), 3
            ),
            "avg_quality_24h": round(
                sum(c.quality_score for c in recent) / max(1, len(recent)), 3
            ) if recent else None,
            "model_profile": {
                "name": self._model_profile.get("model_name", "unknown"),
                "tier": self._model_profile.get("capability_tier", "unknown"),
                "context_window": self._model_profile.get("context_window", 0),
                "avg_latency_ms": round(
                    self._model_profile.get("avg_latency_ms", 0), 1
                ),
            },
            "task_stats": task_stats,
            "context_priorities": context_priorities,
            "escalation_rules": escalation_summary,
        }

    def get_brief(self, max_tokens: int = 200) -> str:
        """
        Get a compact text brief for prompt injection (for capable models).

        Only injected if model capability tier is "mid" or "high".
        """
        if not self._model_profile.get("inject_learning_brief", False):
            return ""

        stats = self.get_stats()
        if stats["scored_calls"] < MIN_SAMPLES:
            return ""

        lines = [f"[LLM Learning: {stats['scored_calls']} calls, avg quality {stats['avg_quality']:.2f}]"]

        # Escalation warnings
        for task, info in stats.get("escalation_rules", {}).items():
            if info.get("escalate"):
                lines.append(f"  ⚠ {task}: quality low ({info['local_quality']:.2f}), prefer cloud")

        # Top 3 context priorities
        top = list(stats.get("context_priorities", {}).items())[:3]
        if top:
            names = ", ".join(f"{k}({v})" for k, v in top)
            lines.append(f"  Most effective context: {names}")

        brief = "\n".join(lines)
        # Trim to max tokens (~4 chars per token)
        max_chars = max_tokens * 4
        if len(brief) > max_chars:
            brief = brief[:max_chars - 3] + "..."
        return brief

    def get_escalation_report(self) -> Dict[str, Any]:
        """Get full escalation report for all task types."""
        return {
            task_type: self.get_escalation_recommendation(task_type)
            for task_type in self._escalation_rules
        }

    def get_context_report(self) -> Dict[str, Any]:
        """Get full context effectiveness report."""
        return {
            k: {
                "priority": round(v.priority_weight, 3),
                "effectiveness": round(v.ema_effectiveness, 3),
                "included": v.times_included,
                "excluded": v.times_excluded,
                "avg_quality_in": round(v.avg_quality_when_included, 3),
                "avg_quality_out": round(v.avg_quality_when_excluded, 3),
                "samples": v.sample_count,
            }
            for k, v in sorted(
                self._context_effectiveness.items(),
                key=lambda x: x[1].priority_weight,
                reverse=True,
            )
        }

    def get_model_profile(self) -> Dict[str, Any]:
        """Get the current model capability profile."""
        return dict(self._model_profile)


# ---------------------------------------------------------------------------
#  Singleton access (matches existing learning engine pattern)
# ---------------------------------------------------------------------------
_llm_learner: Optional[LLMLearner] = None


def get_llm_learner(data_dir: Path = None) -> LLMLearner:
    """Get or create the global LLM Learner instance."""
    global _llm_learner
    if _llm_learner is None:
        _llm_learner = LLMLearner(data_dir=data_dir or Path("brain/llm_learning"))
    return _llm_learner
