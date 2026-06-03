#!/usr/bin/env python3
"""
Training Data Quality Gate for SAIGE Tier 1 QLoRA Evolution

Scores training examples 1-5 and filters out low-quality data.
Only examples scoring >= 3 are used for weight evolution.

Scoring criteria:
- Source reliability (real task > self-generated busywork)
- Response quality (length, specificity, actionability)
- Diversity (avoid training on repeated patterns)
- Tool usage (examples with real tool results score higher)
"""

import re
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)

# Source quality weights — real work > self-generated fluff
SOURCE_WEIGHTS = {
    "chain_response": 1.0,        # From actual reasoning chains — highest quality
    "user_conversation": 1.0,     # Direct user interactions — highest quality
    "tool_execution": 0.9,        # Tool calls with real results
    "trade_execution": 0.9,       # Actual trading decisions
    "self_prompt": 0.5,           # Self-generated prompts — mid quality
    "node2040_reflection": 0.4,   # Self-reflection — often busywork
    "node2040_general": 0.2,      # General self-thoughts — lowest
    "grokipedia_knowledge": 0.6,  # Curated knowledge — decent
    "emotional_thought": 0.3,     # Emotional baseline — low
}

# Words/phrases that indicate busywork (score penalty)
BUSYWORK_INDICATORS = [
    "write an essay", "explore the topic", "general thoughts",
    "what are your thoughts on", "reflect on the concept",
    "write a comprehensive", "analyze the implications",
    "discuss the ethical", "write about", "summarize your understanding",
    "your perspective on", "philosophical implications",
]

# Words/phrases that indicate real work (score boost)
REAL_WORK_INDICATORS = [
    "execute", "trade", "buy", "sell", "transfer", "search",
    "analyze token", "check holdings", "portfolio", "wallet",
    "price action", "market cap", "volume", "liquidity",
    "deploy", "build", "fix", "debug", "implement",
    "user asked", "operator requested", "responding to",
]

MINIMUM_SCORE = 3  # Don't train on anything below this


def score_training_example(example: Dict) -> Tuple[int, str]:
    """
    Score a training example 1-5.

    Returns:
        (score, reason) — score 1-5 and brief explanation
    """
    score = 3.0  # Start at baseline
    reasons = []

    prompt = example.get("prompt", "")
    response = example.get("response", "")
    source_type = example.get("type", "unknown")
    quality_label = example.get("quality", "medium")

    # --- Factor 1: Source reliability (±1.5) ---
    source_weight = SOURCE_WEIGHTS.get(source_type, 0.3)
    source_adjustment = (source_weight - 0.5) * 3  # -1.5 to +1.5
    score += source_adjustment
    if source_weight >= 0.9:
        reasons.append(f"high-value source ({source_type})")
    elif source_weight <= 0.3:
        reasons.append(f"low-value source ({source_type})")

    # --- Factor 2: Response quality (±1.0) ---
    resp_len = len(response)
    if resp_len < 30:
        score -= 1.0
        reasons.append("response too short")
    elif resp_len > 200:
        score += 0.5
        reasons.append("substantive response")
    if resp_len > 1000:
        score += 0.3  # Extra credit for detailed responses

    # --- Factor 3: Busywork detection (−1.5) ---
    prompt_lower = prompt.lower()
    busywork_count = sum(1 for indicator in BUSYWORK_INDICATORS if indicator in prompt_lower)
    if busywork_count > 0:
        penalty = min(1.5, busywork_count * 0.75)
        score -= penalty
        reasons.append(f"busywork detected ({busywork_count} indicators)")

    # --- Factor 4: Real work detection (+1.0) ---
    real_work_count = sum(1 for indicator in REAL_WORK_INDICATORS if indicator in prompt_lower)
    if real_work_count > 0:
        boost = min(1.0, real_work_count * 0.4)
        score += boost
        reasons.append(f"real work ({real_work_count} indicators)")

    # --- Factor 5: Has tool results (+0.5) ---
    if "tool_result" in response.lower() or "tool_calls" in str(example):
        score += 0.5
        reasons.append("contains tool results")

    # --- Factor 6: Quality label from collector (±0.5) ---
    quality_map = {"very_high": 0.5, "high": 0.25, "medium": 0, "baseline": -0.5, "low": -0.75}
    score += quality_map.get(quality_label, 0)

    # --- Factor 7: Specificity check (±0.5) ---
    # Generic responses that could apply to anything score lower
    generic_phrases = ["interesting topic", "this is a complex", "there are many aspects",
                       "in conclusion", "it's important to note"]
    resp_lower = response.lower()
    generic_count = sum(1 for phrase in generic_phrases if phrase in resp_lower)
    if generic_count >= 2:
        score -= 0.5
        reasons.append("generic response")

    # Clamp to 1-5
    final_score = max(1, min(5, round(score)))
    reason_str = "; ".join(reasons) if reasons else "baseline"

    return final_score, reason_str


def filter_training_data(
    examples: List[Dict],
    min_score: int = MINIMUM_SCORE,
) -> Tuple[List[Dict], Dict]:
    """
    Score and filter training examples.

    Returns:
        (filtered_examples, stats)
    """
    scored = []
    score_distribution = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    rejected_types = {}

    for example in examples:
        score, reason = score_training_example(example)
        example["quality_score"] = score
        example["quality_reason"] = reason
        score_distribution[score] = score_distribution.get(score, 0) + 1

        if score >= min_score:
            scored.append(example)
        else:
            source_type = example.get("type", "unknown")
            rejected_types[source_type] = rejected_types.get(source_type, 0) + 1

    # Sort by score descending — best quality first
    scored.sort(key=lambda x: x.get("quality_score", 0), reverse=True)

    stats = {
        "total_input": len(examples),
        "total_accepted": len(scored),
        "total_rejected": len(examples) - len(scored),
        "score_distribution": score_distribution,
        "rejected_by_type": rejected_types,
        "min_score_threshold": min_score,
        "avg_score": sum(e.get("quality_score", 0) for e in scored) / max(1, len(scored)),
    }

    return scored, stats


def deduplicate_training_data(examples: List[Dict], similarity_threshold: float = 0.85) -> List[Dict]:
    """
    Remove near-duplicate training examples.
    Uses simple prompt similarity (Jaccard on word sets).
    """
    if len(examples) <= 1:
        return examples

    seen_prompts = []
    unique = []

    for example in examples:
        prompt = example.get("prompt", "")
        prompt_words = set(prompt.lower().split())

        is_duplicate = False
        for seen_words in seen_prompts:
            if not prompt_words or not seen_words:
                continue
            intersection = prompt_words & seen_words
            union = prompt_words | seen_words
            jaccard = len(intersection) / len(union) if union else 0
            if jaccard >= similarity_threshold:
                is_duplicate = True
                break

        if not is_duplicate:
            unique.append(example)
            seen_prompts.append(prompt_words)

    removed = len(examples) - len(unique)
    if removed > 0:
        logger.info(f"🔄 Dedup removed {removed} near-duplicate training examples")

    return unique
