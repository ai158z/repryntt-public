"""
Curiosity Budget — Intrinsic Motivation for Autonomous Agent Discovery.

Without curiosity, the agent only exploits known-good tasks and never
discovers new capabilities. This implements bounded curiosity:

  - 1 in 10 utility heartbeats is a "curiosity heartbeat"
  - Curiosity heartbeats must produce a TESTABLE HYPOTHESIS, not vague research
  - Results feed back into the skill library (successful explorations become skills)
  - Failed explorations are tracked to avoid repeating them

Based on:
  - Pathak et al. 2017 "Curiosity-driven Exploration"
  - Burda et al. 2018 "Random Network Distillation"
  - Adapted for LLM agents instead of RL policies

Storage: ~/.repryntt/brain/curiosity/
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from repryntt.paths import brain_dir as _brain_dir

logger = logging.getLogger(__name__)

CURIOSITY_DIR = _brain_dir() / "curiosity"
STATE_FILE = CURIOSITY_DIR / "curiosity_state.json"

CURIOSITY_FREQUENCY = 10  # 1 in every N utility heartbeats
MAX_EXPLORATIONS = 100    # Rolling window
MAX_FAILED_TOPICS = 50    # Remember failed explorations to avoid repeats
NOVELTY_BONUS = 0.5       # Score bonus for genuinely novel topics


class CuriosityBudget:
    """Manages the agent's exploration budget and hypothesis tracking."""

    def __init__(self):
        CURIOSITY_DIR.mkdir(parents=True, exist_ok=True)

        self.utility_count: int = 0        # utility heartbeats since last curiosity
        self.total_explorations: int = 0
        self.successful_explorations: int = 0
        self.explorations: List[Dict] = []  # history
        self.failed_topics: List[str] = []  # topics to avoid
        self.discovery_log: List[Dict] = []  # things we discovered

        self._load()

    def _load(self):
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, 'r') as f:
                    data = json.load(f)
                self.utility_count = data.get("utility_count", 0)
                self.total_explorations = data.get("total_explorations", 0)
                self.successful_explorations = data.get("successful_explorations", 0)
                self.explorations = data.get("explorations", [])[-MAX_EXPLORATIONS:]
                self.failed_topics = data.get("failed_topics", [])[-MAX_FAILED_TOPICS:]
                self.discovery_log = data.get("discovery_log", [])[-50:]
            except Exception as e:
                logger.debug(f"Curiosity state load failed: {e}")

    def save(self):
        try:
            data = {
                "utility_count": self.utility_count,
                "total_explorations": self.total_explorations,
                "successful_explorations": self.successful_explorations,
                "explorations": self.explorations[-MAX_EXPLORATIONS:],
                "failed_topics": self.failed_topics[-MAX_FAILED_TOPICS:],
                "discovery_log": self.discovery_log[-50:],
                "last_saved": time.time(),
            }
            tmp = str(STATE_FILE) + ".tmp"
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, str(STATE_FILE))
        except Exception as e:
            logger.debug(f"Curiosity state save failed: {e}")

    # ── Budget check ────────────────────────────────────────

    def should_explore(self) -> bool:
        """Check if this utility heartbeat should be a curiosity heartbeat.

        Called BEFORE mode selection in the heartbeat loop.
        Returns True ~1 in CURIOSITY_FREQUENCY utility heartbeats.
        """
        self.utility_count += 1

        if self.utility_count >= CURIOSITY_FREQUENCY:
            self.utility_count = 0
            logger.info(f"🔍 Curiosity triggered (every {CURIOSITY_FREQUENCY} utility heartbeats)")
            return True
        return False

    # ── Hypothesis generation ───────────────────────────────

    def generate_hypothesis_prompt(self, known_tools: List[str],
                                   recent_topics: List[str]) -> str:
        """Build a prompt that guides the agent toward productive exploration.

        The key constraint: exploration must produce a TESTABLE HYPOTHESIS,
        not vague research. "Can I use tool X to do Y?" → try it → record result.
        """
        # Filter out topics we've already failed at
        avoid_topics = set(self.failed_topics)
        recently_explored = set()
        for exp in self.explorations[-10:]:
            recently_explored.add(exp.get("topic", ""))

        lines = [
            "\n## 🔍 CURIOSITY HEARTBEAT — Exploration Mode",
            "",
            "This heartbeat is dedicated to DISCOVERING something new.",
            "You have a bounded curiosity budget. Use it wisely.",
            "",
            "**Rules for exploration:**",
            "1. Form a TESTABLE HYPOTHESIS: 'I predict that [tool/approach] can [outcome]'",
            "2. TEST IT with actual tool calls — don't just research",
            "3. Record the result: success (new capability) or failure (avoid in future)",
            "4. If successful, this becomes a new skill in your library",
            "",
            "**Good explorations:**",
            "- 'Can I use capture_camera to read text from physical objects?'",
            "- 'Can I chain web_search + write_file to auto-generate documentation?'",
            "- 'Can I use run_code to benchmark different approaches to a problem?'",
            "",
            "**Bad explorations:**",
            "- 'What is consciousness?' (not testable)",
            "- 'Research geopolitics trends' (not a hypothesis)",
            "- 'Explore quantum computing theory' (no tool-based test)",
            "",
        ]

        # Show tools the agent has but may not have explored
        if known_tools:
            tool_sample = sorted(known_tools)[:20]
            lines.append(f"**Available tools you could explore**: {', '.join(tool_sample)}")
            lines.append("")

        # Show topics to avoid (already failed)
        if avoid_topics:
            avoid_list = ", ".join(sorted(avoid_topics)[:10])
            lines.append(f"**Topics that FAILED before (avoid)**: {avoid_list}")
            lines.append("")

        # Show recent explorations to avoid repeats
        if recently_explored:
            recent_list = ", ".join(sorted(recently_explored - {""})[:5])
            lines.append(f"**Recently explored (don't repeat)**: {recent_list}")
            lines.append("")

        # Show successful discoveries for inspiration
        if self.discovery_log:
            lines.append("**Previous discoveries (these WORKED):**")
            for d in self.discovery_log[-3:]:
                lines.append(f"  ✅ {d.get('hypothesis', '?')} → {d.get('result', '?')[:80]}")
            lines.append("")

        lines.append("**Format your output as:**")
        lines.append("```")
        lines.append("HYPOTHESIS: [what you predict]")
        lines.append("TEST: [what tools/steps you'll use to test it]")
        lines.append("RESULT: [what happened — success/failure + specific outcome]")
        lines.append("LEARNED: [what capability this unlocks, or why it failed]")
        lines.append("```")

        return "\n".join(lines)

    # ── Recording exploration results ───────────────────────

    def record_exploration(self, topic: str, hypothesis: str,
                           result: str, score: int,
                           was_successful: bool):
        """Record the outcome of a curiosity heartbeat."""
        self.total_explorations += 1

        exploration = {
            "topic": topic,
            "hypothesis": hypothesis[:200],
            "result": result[:300],
            "score": score,
            "successful": was_successful,
            "timestamp": time.time(),
        }
        self.explorations.append(exploration)

        if was_successful:
            self.successful_explorations += 1
            self.discovery_log.append({
                "hypothesis": hypothesis[:200],
                "result": result[:200],
                "timestamp": time.time(),
            })
            logger.info(f"🔍 Exploration SUCCESS: {hypothesis[:60]}")
        else:
            # Add to failed topics to avoid
            if topic and topic not in self.failed_topics:
                self.failed_topics.append(topic)
            logger.info(f"🔍 Exploration FAILED: {hypothesis[:60]}")

        self.save()

    def parse_exploration_output(self, text: str) -> Optional[Dict]:
        """Parse the structured output from a curiosity heartbeat."""
        if not text:
            return None

        result = {
            "hypothesis": "",
            "test": "",
            "result": "",
            "learned": "",
        }

        for line in text.split("\n"):
            line = line.strip()
            for key in ("HYPOTHESIS:", "TEST:", "RESULT:", "LEARNED:"):
                if line.upper().startswith(key):
                    result[key[:-1].lower()] = line[len(key):].strip()

        if not result["hypothesis"]:
            return None

        # Determine success from result text
        result_lower = (result.get("result", "") + " " + result.get("learned", "")).lower()
        success_words = {"success", "works", "worked", "confirmed", "yes", "can",
                         "discovered", "new capability", "functional"}
        failure_words = {"fail", "cannot", "doesn't", "error", "impossible",
                         "not possible", "no way"}

        success_count = sum(1 for w in success_words if w in result_lower)
        failure_count = sum(1 for w in failure_words if w in result_lower)
        result["was_successful"] = success_count > failure_count

        return result

    # ── Stats ───────────────────────────────────────────────

    def get_stats(self) -> Dict:
        """Return curiosity budget statistics."""
        return {
            "total_explorations": self.total_explorations,
            "successful_explorations": self.successful_explorations,
            "success_rate": round(
                self.successful_explorations / max(self.total_explorations, 1), 2
            ),
            "utility_until_next": CURIOSITY_FREQUENCY - self.utility_count,
            "failed_topics_count": len(self.failed_topics),
            "discoveries_count": len(self.discovery_log),
            "recent_discoveries": self.discovery_log[-5:],
        }


class PredictiveTaskScorer:
    """Predicts task scores BEFORE execution using historical work output data.

    Model-based RL approach: instead of learning only from post-execution scores,
    build a lightweight world model that predicts "if I do task X, my score will
    likely be Y" based on past topic→score correlations.

    Based on:
      - Hafner et al. 2020 "Dream to Control" (Dreamer)
      - Simplified for LLM agents: topic+tool→score lookup, not neural world model

    Storage: ~/.repryntt/brain/predictions/
    """

    PREDICTIONS_DIR = _brain_dir() / "predictions"
    STATE_FILE = PREDICTIONS_DIR / "predictor_state.json"

    def __init__(self):
        self.PREDICTIONS_DIR.mkdir(parents=True, exist_ok=True)

        # Topic → score history
        self.topic_scores: Dict[str, List[float]] = {}
        # Tool combination → score history
        self.tool_scores: Dict[str, List[float]] = {}
        # Time-of-day → score history (hour buckets)
        self.hour_scores: Dict[int, List[float]] = {}
        # Combined predictions cache
        self._prediction_cache: Dict[str, float] = {}

        self._load()

    def _load(self):
        if self.STATE_FILE.exists():
            try:
                with open(self.STATE_FILE, 'r') as f:
                    data = json.load(f)
                self.topic_scores = data.get("topic_scores", {})
                self.tool_scores = data.get("tool_scores", {})
                self.hour_scores = {
                    int(k): v for k, v in data.get("hour_scores", {}).items()
                }
            except Exception as e:
                logger.debug(f"Predictor state load failed: {e}")

    def save(self):
        try:
            data = {
                "topic_scores": self._trim_scores(self.topic_scores),
                "tool_scores": self._trim_scores(self.tool_scores),
                "hour_scores": {str(k): v[-50:] for k, v in self.hour_scores.items()},
                "last_saved": time.time(),
            }
            tmp = str(self.STATE_FILE) + ".tmp"
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, str(self.STATE_FILE))
        except Exception as e:
            logger.debug(f"Predictor save failed: {e}")

    @staticmethod
    def _trim_scores(scores: Dict[str, List[float]],
                     max_per_key: int = 50,
                     max_keys: int = 200) -> Dict[str, List[float]]:
        """Keep the dictionary bounded."""
        trimmed = {}
        # Keep most-used keys
        sorted_keys = sorted(scores.keys(),
                             key=lambda k: len(scores[k]), reverse=True)
        for key in sorted_keys[:max_keys]:
            trimmed[key] = scores[key][-max_per_key:]
        return trimmed

    # ── Recording outcomes ──────────────────────────────────

    def record_outcome(self, topic: str, score: int,
                       tool_names: List[str]):
        """Record a heartbeat outcome for future predictions."""
        import datetime as _dt

        # Topic score
        topic_key = (topic or "unknown").lower().strip()
        if topic_key not in self.topic_scores:
            self.topic_scores[topic_key] = []
        self.topic_scores[topic_key].append(float(score))

        # Tool combination score (sorted for consistency)
        if tool_names:
            tool_key = ",".join(sorted(set(t.lower() for t in tool_names[:5])))
            if tool_key not in self.tool_scores:
                self.tool_scores[tool_key] = []
            self.tool_scores[tool_key].append(float(score))

        # Time-of-day score
        hour = _dt.datetime.now().hour
        if hour not in self.hour_scores:
            self.hour_scores[hour] = []
        self.hour_scores[hour].append(float(score))

        self.save()

    # ── Predictions ─────────────────────────────────────────

    def predict_score(self, topic: str,
                      planned_tools: Optional[List[str]] = None) -> float:
        """Predict the expected score for a task before execution.

        Returns predicted score (1.0-5.0). Uses weighted average across
        topic history, tool history, and time-of-day patterns.
        """
        import datetime as _dt

        predictions = []
        weights = []

        # Topic-based prediction (highest weight)
        topic_key = (topic or "").lower().strip()
        if topic_key in self.topic_scores:
            scores = self.topic_scores[topic_key]
            # Recent scores matter more (exponential weighting)
            if len(scores) >= 2:
                recent = scores[-5:]
                pred = sum(recent) / len(recent)
                predictions.append(pred)
                weights.append(3.0)  # high weight

        # Fuzzy topic matching (partial word overlap)
        if not predictions and topic_key:
            topic_words = set(topic_key.split())
            best_overlap = 0
            best_pred = None
            for key, scores in self.topic_scores.items():
                key_words = set(key.split())
                overlap = len(topic_words & key_words)
                if overlap > best_overlap and len(scores) >= 2:
                    best_overlap = overlap
                    best_pred = sum(scores[-5:]) / len(scores[-5:])
            if best_pred is not None and best_overlap >= 1:
                predictions.append(best_pred)
                weights.append(1.5)

        # Tool-based prediction
        if planned_tools:
            tool_key = ",".join(sorted(set(t.lower() for t in planned_tools[:5])))
            if tool_key in self.tool_scores:
                scores = self.tool_scores[tool_key]
                if len(scores) >= 2:
                    pred = sum(scores[-5:]) / len(scores[-5:])
                    predictions.append(pred)
                    weights.append(2.0)

        # Time-of-day prediction
        hour = _dt.datetime.now().hour
        if hour in self.hour_scores:
            scores = self.hour_scores[hour]
            if len(scores) >= 3:
                pred = sum(scores[-10:]) / len(scores[-10:])
                predictions.append(pred)
                weights.append(1.0)

        # Weighted average
        if not predictions:
            return 3.0  # no data → neutral prediction

        total_weight = sum(weights)
        predicted = sum(p * w for p, w in zip(predictions, weights)) / total_weight
        return round(max(1.0, min(5.0, predicted)), 2)

    def get_task_rankings(self, candidate_topics: List[str]) -> List[Dict]:
        """Rank multiple candidate tasks by predicted score.

        Used to help the agent pick the highest-value task.
        """
        rankings = []
        for topic in candidate_topics:
            pred = self.predict_score(topic)
            data_points = len(self.topic_scores.get(topic.lower().strip(), []))
            rankings.append({
                "topic": topic,
                "predicted_score": pred,
                "confidence": min(1.0, data_points / 10),
                "data_points": data_points,
            })

        rankings.sort(key=lambda x: x["predicted_score"], reverse=True)
        return rankings

    def get_prediction_context(self, task_description: str) -> str:
        """Get prompt injection with task prediction for PLAN phase."""
        topic = task_description.lower().strip()
        pred = self.predict_score(topic)

        # Only inject if we have meaningful data
        data_points = len(self.topic_scores.get(topic, []))
        if data_points < 2:
            return ""

        # Find highest and lowest scoring topics for comparison
        top_topics = sorted(
            [(k, sum(v[-3:]) / len(v[-3:])) for k, v in self.topic_scores.items()
             if len(v) >= 3],
            key=lambda x: -x[1]
        )[:3]

        bottom_topics = sorted(
            [(k, sum(v[-3:]) / len(v[-3:])) for k, v in self.topic_scores.items()
             if len(v) >= 3],
            key=lambda x: x[1]
        )[:3]

        lines = [
            f"\n## 📊 Task Score Prediction",
            f"Predicted score for current approach: **{pred:.1f}/5** "
            f"(based on {data_points} past examples)",
        ]

        if pred < 3.0:
            lines.append(f"⚠️ This topic historically scores LOW. Consider switching to a different task.")

        if top_topics:
            top_list = ", ".join(f"{t[0]} ({t[1]:.1f})" for t in top_topics)
            lines.append(f"**Highest-scoring topics**: {top_list}")

        if bottom_topics and pred < 3.0:
            bottom_list = ", ".join(f"{t[0]} ({t[1]:.1f})" for t in bottom_topics)
            lines.append(f"**Lowest-scoring topics (avoid)**: {bottom_list}")

        lines.append("")
        return "\n".join(lines)

    # ── Stats ───────────────────────────────────────────────

    def get_stats(self) -> Dict:
        """Return predictor statistics."""
        all_topics = sorted(
            [(k, len(v), sum(v[-5:]) / len(v[-5:]))
             for k, v in self.topic_scores.items() if v],
            key=lambda x: -x[2]
        )

        return {
            "total_topics_tracked": len(self.topic_scores),
            "total_tool_combos_tracked": len(self.tool_scores),
            "top_scoring_topics": [
                {"topic": t[0], "count": t[1], "avg": round(t[2], 2)}
                for t in all_topics[:5]
            ],
            "bottom_scoring_topics": [
                {"topic": t[0], "count": t[1], "avg": round(t[2], 2)}
                for t in all_topics[-5:]
            ] if len(all_topics) > 5 else [],
            "hours_with_data": len(self.hour_scores),
        }
