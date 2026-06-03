#!/usr/bin/env python3
"""
Training Data Collector — Bridges the Heartbeat Cycle to the Training Pipeline

The heartbeat cycle (PLAN→ACT→EVALUATE) generates the highest-quality training
data in the system: real prompts, real tool use, real outcomes, real evaluations.
But until now, none of it flowed into training_data.json.

This module:
1. Collects prompt/response pairs from every heartbeat cycle
2. Attaches outcome metadata (eval score, artifact validation, tool usage)
3. Generates DPO preference pairs when outcome differs from effort
4. Writes to training_data.json for QLoRA SFT training
5. Writes to preference_pairs.json for DPO/RLHF training

For API-only users: this data improves prompt injection (behavioral guidance)
For local LLM users: this data actually trains the model weights via QLoRA/DPO

The collector is intentionally stateless — it appends to JSON files and
doesn't hold anything in memory between heartbeats.
"""

import json
import os
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# Max entries before we start trimming old data
MAX_TRAINING_EXAMPLES = 5000
MAX_PREFERENCE_PAIRS = 2000


class TrainingCollector:
    """Collects heartbeat outcomes as training data for model evolution."""

    def __init__(self, data_dir: Optional[Path] = None):
        if data_dir is None:
            from repryntt.paths import data_dir as get_data_dir
            data_dir = get_data_dir()
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.training_data_path = self.data_dir / "training_data.json"
        self.preference_pairs_path = self.data_dir / "preference_pairs.json"
        self.collector_stats_path = self.data_dir / "collector_stats.json"

    # ───────────────────────────────────────────────────────
    # SFT TRAINING DATA (prompt → good response)
    # ───────────────────────────────────────────────────────

    def record_heartbeat(
        self,
        plan_prompt: str,
        plan_response: str,
        action_report: str,
        eval_score: int,
        outcome_score: Optional[int] = None,
        artifact_type: str = "",
        tools_used: Optional[List[str]] = None,
        chain_topic: str = "",
    ):
        """
        Record a heartbeat cycle as a training example.

        Only records examples with eval_score >= 3 (quality gate).
        Higher-scoring examples get higher priority in training.

        For local LLM training: this becomes an SFT example
        For API prompting: this feeds into behavioral guidance patterns
        """
        # Quality gate — don't train on bad outputs
        effective_score = outcome_score if outcome_score is not None else eval_score
        if effective_score < 3:
            logger.debug(
                f"Training collector: skipping low-quality example "
                f"(eval={eval_score}, outcome={outcome_score})"
            )
            return

        # Determine the quality label and source type
        if tools_used and len(tools_used) > 2:
            source_type = "tool_execution"
        elif chain_topic:
            source_type = "chain_response"
        else:
            source_type = "self_prompt"

        quality = "very_high" if effective_score >= 5 else \
                  "high" if effective_score >= 4 else "medium"

        # Personality bonus: responses with genuine personality markers get
        # a quality boost. This teaches the local LLM to produce personality-
        # rich responses through natural selection — no explicit reward model.
        personality_score = _personality_marker_score(action_report)

        # Build the training example
        # The prompt is the PLAN instruction, the response is the action report
        # This teaches the model: "given this task, produce this kind of work"
        example = {
            "prompt": plan_prompt[:2000],  # Truncate to keep training tight
            "response": action_report[:3000],
            "type": source_type,
            "cycle": 0,  # Filled by evolution manager if needed
            "timestamp": datetime.now().isoformat(),
            "quality": quality,
            "quality_score": min(effective_score + personality_score, 5),
            "quality_reason": self._build_quality_reason(
                eval_score, outcome_score, artifact_type, tools_used
            ),
            "metadata": {
                "eval_score": eval_score,
                "outcome_score": outcome_score,
                "artifact_type": artifact_type,
                "tools_used": (tools_used or [])[:10],
                "chain_topic": chain_topic[:200] if chain_topic else "",
                "personality_score": personality_score,
            },
        }

        self._append_training_example(example)

    def _build_quality_reason(
        self, eval_score: int, outcome_score: Optional[int],
        artifact_type: str, tools_used: Optional[List[str]]
    ) -> str:
        """Build a human-readable quality reason string."""
        parts = []
        if outcome_score is not None and outcome_score >= 4:
            parts.append(f"validated {artifact_type} (outcome {outcome_score}/5)")
        if tools_used and len(tools_used) > 2:
            parts.append(f"real work ({len(tools_used)} tools)")
        if eval_score >= 4:
            parts.append(f"high eval ({eval_score}/5)")
        return "; ".join(parts) if parts else f"eval {eval_score}/5"

    def _append_training_example(self, example: Dict):
        """Thread-safe append to training_data.json."""
        try:
            data = self._load_json(self.training_data_path, default=[])
            data.append(example)

            # Trim old entries, keeping highest quality
            if len(data) > MAX_TRAINING_EXAMPLES:
                # Sort by quality_score desc, then by recency
                data.sort(
                    key=lambda x: (x.get("quality_score", 0), x.get("timestamp", "")),
                    reverse=True,
                )
                data = data[:MAX_TRAINING_EXAMPLES]

            self._save_json(self.training_data_path, data)
            logger.info(
                f"📝 Training data recorded: quality={example['quality']}, "
                f"type={example['type']} (total: {len(data)} examples)"
            )

            # Anchor high-quality entries into memory_mesh for RAG retrieval
            if example.get("quality_score", 0) >= 4:
                try:
                    from repryntt.core.memory.memory_mesh import get_memory_mesh
                    snippet = (example.get("response") or example.get("prompt", ""))[:300]
                    label = f"hb_{example.get('type', 'plan')}_{int(time.time())}"
                    get_memory_mesh().anchor_knowledge(
                        "experience", label, snippet, "training_collector"
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"Training data write failed (non-fatal): {e}")

    # ───────────────────────────────────────────────────────
    # DPO PREFERENCE PAIRS (good response > bad response)
    # ───────────────────────────────────────────────────────

    def record_preference_pair(
        self,
        prompt: str,
        chosen_response: str,
        rejected_response: str,
        chosen_score: int,
        rejected_score: int,
        source: str = "outcome_gap",
    ):
        """
        Record a DPO preference pair: for the same prompt, response A
        was better than response B.

        DPO (Direct Preference Optimization) is the modern replacement
        for RLHF. Instead of training a separate reward model, DPO
        directly trains the policy model on preference pairs:
        - "chosen" = the response we want more of
        - "rejected" = the response we want less of

        The model learns: "when you see this kind of prompt, produce
        responses more like 'chosen' and less like 'rejected'."

        Sources of preference pairs:
        - outcome_gap: artifact validation showed effort != outcome
        - recovery: a retry produced a better result than first attempt
        - eval_correction: evaluation identified specific issues
        """
        pair = {
            "prompt": prompt[:2000],
            "chosen": chosen_response[:3000],
            "rejected": rejected_response[:3000],
            "chosen_score": chosen_score,
            "rejected_score": rejected_score,
            "source": source,
            "margin": chosen_score - rejected_score,
            "timestamp": datetime.now().isoformat(),
        }

        try:
            pairs = self._load_json(self.preference_pairs_path, default=[])
            pairs.append(pair)

            # Trim, keeping highest-margin pairs (clearest signal)
            if len(pairs) > MAX_PREFERENCE_PAIRS:
                pairs.sort(key=lambda x: abs(x.get("margin", 0)), reverse=True)
                pairs = pairs[:MAX_PREFERENCE_PAIRS]

            self._save_json(self.preference_pairs_path, pairs)
            logger.info(
                f"⚖️ DPO pair recorded: chosen={chosen_score}/5 > "
                f"rejected={rejected_score}/5, source={source} "
                f"(total: {len(pairs)} pairs)"
            )
        except Exception as e:
            logger.debug(f"Preference pair write failed (non-fatal): {e}")

    def record_outcome_gap_pair(
        self,
        plan_prompt: str,
        action_report: str,
        eval_score: int,
        outcome_score: int,
        artifact_type: str,
        evaluation_text: str,
    ):
        """
        When artifact validation reveals the model scored its own work
        higher than reality (effort > outcome), generate a preference pair.

        The "rejected" response is the original report (model thought it
        was good). We synthesize a "chosen" response hint from the
        evaluation critique, teaching the model to be more self-critical
        and produce genuinely validated work.
        """
        gap = eval_score - outcome_score
        if abs(gap) < 2:
            return  # Gap too small to be a useful training signal

        if gap > 0:
            # Model overrated itself — teach it to be more rigorous
            # Rejected = the original action report (overconfident)
            # Chosen = a corrected version based on evaluation critique
            correction_hint = self._extract_correction(evaluation_text, artifact_type)
            if not correction_hint:
                return

            chosen = (
                f"[Self-correction after validation]\n"
                f"My initial work scored {eval_score}/5 on effort but only "
                f"{outcome_score}/5 on actual outcome. Issues:\n"
                f"{correction_hint}\n\n"
                f"Next time I will: verify outputs exist, test code, "
                f"cite sources for research, and confirm completion before "
                f"rating my own work highly."
            )

            self.record_preference_pair(
                prompt=plan_prompt,
                chosen_response=chosen,
                rejected_response=action_report[:2000],
                chosen_score=outcome_score + 1,  # Slightly better than outcome
                rejected_score=outcome_score,
                source="outcome_gap",
            )
        else:
            # Model underrated itself — less common but still useful
            # The action report was actually better than self-eval suggested
            self.record_preference_pair(
                prompt=plan_prompt,
                chosen_response=action_report[:2000],
                rejected_response=(
                    f"[Underconfident self-assessment]\n"
                    f"I rated this work {eval_score}/5 but artifact validation "
                    f"showed it was actually {outcome_score}/5. I should trust "
                    f"my work more when it produces validated results."
                ),
                chosen_score=outcome_score,
                rejected_score=eval_score,
                source="outcome_gap_positive",
            )

    def _extract_correction(self, evaluation_text: str, artifact_type: str) -> str:
        """Extract actionable corrections from evaluation text."""
        if not evaluation_text:
            return ""

        lines = []
        for line in evaluation_text.split('\n'):
            line = line.strip()
            if not line:
                continue
            # Skip metadata lines
            if line.startswith(("SCORE:", "CHAIN_CONTINUE:", "NEXT_STEP:")):
                continue
            # Look for critique lines (numbered or bulleted)
            if any(line.startswith(p) for p in ("1.", "2.", "3.", "-", "*", "•")):
                lines.append(line)
            # Look for improvement keywords
            elif any(kw in line.lower() for kw in (
                "should", "could", "need", "missing", "didn't", "failed",
                "issue", "problem", "improve", "instead"
            )):
                lines.append(line)

        return "\n".join(lines[:5])  # Max 5 critique lines

    # ───────────────────────────────────────────────────────
    # RECOVERY PAIR GENERATION
    # ───────────────────────────────────────────────────────

    def record_recovery_pair(
        self,
        prompt: str,
        failed_response: str,
        failed_score: int,
        recovery_response: str,
        recovery_score: int,
    ):
        """
        When a recovery round produces a better result than the initial
        attempt, record it as a preference pair.

        This teaches the model: "when you get this kind of task, the
        recovery approach works better than the initial approach."
        """
        if recovery_score <= failed_score:
            return  # Recovery wasn't actually better

        self.record_preference_pair(
            prompt=prompt,
            chosen_response=recovery_response,
            rejected_response=failed_response,
            chosen_score=recovery_score,
            rejected_score=failed_score,
            source="recovery",
        )

    # ───────────────────────────────────────────────────────
    # STATS
    # ───────────────────────────────────────────────────────

    def get_stats(self) -> Dict:
        """Return training data collection statistics."""
        training_data = self._load_json(self.training_data_path, default=[])
        preference_pairs = self._load_json(self.preference_pairs_path, default=[])

        # Score distribution
        score_dist = {}
        for ex in training_data:
            s = ex.get("quality_score", 0)
            score_dist[s] = score_dist.get(s, 0) + 1

        # Type distribution
        type_dist = {}
        for ex in training_data:
            t = ex.get("type", "unknown")
            type_dist[t] = type_dist.get(t, 0) + 1

        # Pair source distribution
        pair_sources = {}
        for p in preference_pairs:
            s = p.get("source", "unknown")
            pair_sources[s] = pair_sources.get(s, 0) + 1

        return {
            "sft_examples": len(training_data),
            "dpo_pairs": len(preference_pairs),
            "score_distribution": score_dist,
            "type_distribution": type_dist,
            "pair_sources": pair_sources,
            "ready_for_sft": len(training_data) >= 10,
            "ready_for_dpo": len(preference_pairs) >= 20,
        }

    # ───────────────────────────────────────────────────────
    # FILE I/O
    # ───────────────────────────────────────────────────────

    def _load_json(self, path: Path, default=None):
        """Load JSON file with safe fallback."""
        if path.exists():
            try:
                with open(path, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return default if default is not None else []

    def _save_json(self, path: Path, data):
        """Atomic write to JSON file."""
        tmp = str(path) + ".tmp"
        try:
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, str(path))
        except Exception as e:
            # Clean up tmp file on failure
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise e


# ───────────────────────────────────────────────────────
# PERSONALITY MARKER SCORING (module-level helper)
# ───────────────────────────────────────────────────────

# Phrases that indicate genuine personality content vs generic boilerplate.
# Each match contributes 0.25 points, capped at 1 bonus point total.
_PERSONALITY_MARKERS = [
    # First-person opinions and preferences
    "i think", "i believe", "i prefer", "in my opinion", "i've found that",
    "i noticed", "what fascinates me", "what bothers me", "i disagree",
    "i learned", "i was wrong",
    # Humor and self-awareness
    "honestly", "to be fair", "the irony", "fun fact", "amusing",
    "makes me laugh", "i can't help",
    # Specific emotional language
    "excited about", "frustrated by", "curious about", "skeptical of",
    "impressed by", "surprised that",
    # Self-referential growth
    "last time i", "i used to think", "i've changed my mind",
    "my mistake was", "next time i'll",
]


def _personality_marker_score(text: str) -> int:
    """Score how personality-rich a piece of text is.

    Returns 0 or 1 — a binary bonus applied to quality_score.
    Requires at least 3 marker matches to get the bonus.
    """
    if not text:
        return 0
    text_lower = text.lower()
    hits = sum(1 for marker in _PERSONALITY_MARKERS if marker in text_lower)
    return 1 if hits >= 3 else 0
