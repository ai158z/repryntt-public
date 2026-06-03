"""
repryntt.cortex.training.data_router — Routes training data to region-specific datasets.

Extends the existing ``core.evolution.training_collector`` to tag each training
example with a target region, then writes to per-region dataset files.

Data flow:
  heartbeat → TrainingCollector → DataRouter → per-region JSON datasets
  self-reflection → ConsciousRegion.generate_training_data() → DataRouter
  ROS2 action logs → ExecutorRegion.generate_training_data() → DataRouter
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Max examples per region dataset before trimming
MAX_EXAMPLES_PER_REGION = 3000
# Quality threshold — only store examples scoring >= this
MIN_QUALITY_SCORE = 4
# Dedup: recent prompt hashes to check against
DEDUP_WINDOW = 50


class DataRouter:
    """Routes training examples to per-region dataset files.

    Usage::

        router = DataRouter()
        router.route({
            "region": "conscious",
            "type": "self_reflection",
            "prompt": "Reflect on your recent experience.",
            "response": "I notice I've been more methodical today...",
        })
    """

    def __init__(self, base_dir: Optional[Path] = None):
        self._lock = threading.Lock()
        if base_dir is None:
            from repryntt.paths import data_dir
            base_dir = data_dir()
        self.base_dir = base_dir / "cortex_training"
        self.base_dir.mkdir(parents=True, exist_ok=True)

        # Stats
        self._counts: Dict[str, int] = {}
        # Dedup: recent prompt hashes per region
        self._recent_hashes: Dict[str, List[str]] = {}

    # ── Routing ──────────────────────────────────────────────────────

    def route(self, example: Dict[str, Any]) -> bool:
        """Route a single training example to the appropriate region dataset.

        Each example must have at minimum:
          - "region": target region name
          - "prompt": the input/context
          - "response": the desired output

        Optional:
          - "type": source type (self_reflection, conversation, tool_use, etc.)
          - "quality": score 1-5 (examples below 3 are dropped)
          - "timestamp": ISO timestamp (auto-added if missing)
        """
        region = example.get("region", "")
        if not region:
            region = self._classify_region(example)
            example["region"] = region

        if not region:
            logger.debug("DataRouter: could not classify example — skipping")
            return False

        # Quality gate — only keep high-quality examples
        quality = example.get("quality", 3)
        if quality < MIN_QUALITY_SCORE:
            return False

        # Dedup: skip if prompt hash matches recent examples
        prompt_hash = hashlib.md5(example.get("prompt", "").encode()).hexdigest()
        with self._lock:
            region_hashes = self._recent_hashes.get(region, [])
            if prompt_hash in region_hashes:
                logger.debug("DataRouter: duplicate prompt for %s — skipping", region)
                return False

            # Track hash
            region_hashes.append(prompt_hash)
            if len(region_hashes) > DEDUP_WINDOW:
                region_hashes = region_hashes[-DEDUP_WINDOW:]
            self._recent_hashes[region] = region_hashes

        # Add metadata
        example.setdefault("timestamp", datetime.now().isoformat())
        example.setdefault("type", "unknown")

        return self._append_to_region(region, example)

    def route_batch(self, examples: List[Dict[str, Any]]) -> int:
        """Route multiple examples.  Returns count successfully routed."""
        return sum(1 for ex in examples if self.route(ex))

    def route_preference_pair(self, pair: Dict[str, Any]) -> bool:
        """Route a DPO preference pair (chosen vs rejected response).

        Each pair must have:
          - "region": target region name
          - "prompt": the shared input context
          - "chosen": the preferred response
          - "rejected": the dispreferred response

        Optional:
          - "type": source type
          - "timestamp": ISO timestamp (auto-added)
        """
        region = pair.get("region", "conscious")
        prompt = pair.get("prompt", "")
        chosen = pair.get("chosen", "")
        rejected = pair.get("rejected", "")

        if not prompt or not chosen or not rejected:
            return False
        if chosen == rejected:
            return False

        pair.setdefault("timestamp", datetime.now().isoformat())
        pair.setdefault("type", "preference")

        path = self.base_dir / f"{region}_dpo_pairs.json"

        with self._lock:
            try:
                existing = self._load_dataset(path)
                existing.append(pair)
                if len(existing) > MAX_EXAMPLES_PER_REGION:
                    existing = existing[-MAX_EXAMPLES_PER_REGION:]
                self._save_dataset(path, existing)
                return True
            except Exception as e:
                logger.error("Failed to append DPO pair for '%s': %s", region, e)
                return False

    def get_dpo_pairs(self, region: str) -> List[Dict[str, Any]]:
        """Load all DPO preference pairs for a region."""
        path = self.base_dir / f"{region}_dpo_pairs.json"
        return self._load_dataset(path)

    # ── Classification ───────────────────────────────────────────────

    @staticmethod
    def _classify_region(example: Dict[str, Any]) -> str:
        """Classify which region an example belongs to based on content."""
        etype = example.get("type", "").lower()
        prompt = example.get("prompt", "").lower()
        response = example.get("response", "").lower()

        # Direct mapping from type
        TYPE_TO_REGION = {
            "self_reflection": "conscious",
            "personality": "conscious",
            "identity": "conscious",
            "memory_consolidation": "conscious",
            "voice_response": "conscious",
            "conversation": "conscious",
            "motor_command": "executor",
            "ros2_action": "executor",
            "navigation": "executor",
            "camera_classification": "perception",
            "audio_event": "perception",
            "sensor_fusion": "perception",
            "safety_check": "guardian",
        }

        if etype in TYPE_TO_REGION:
            return TYPE_TO_REGION[etype]

        # Content-based heuristics
        conscious_keywords = {"reflect", "feel", "identity", "who am i", "personality", "voice", "memory"}
        executor_keywords = {"move", "navigate", "velocity", "motor", "action", "ros2", "twist"}
        perception_keywords = {"camera", "image", "audio", "detect", "classify", "sensor"}

        text = prompt + " " + response
        for kw in executor_keywords:
            if kw in text:
                return "executor"
        for kw in perception_keywords:
            if kw in text:
                return "perception"
        for kw in conscious_keywords:
            if kw in text:
                return "conscious"

        # Default: anything personality/conversation → conscious
        return "conscious"

    # ── Dataset management ───────────────────────────────────────────

    def _append_to_region(self, region: str, example: Dict[str, Any]) -> bool:
        """Append an example to a region's dataset file."""
        path = self.base_dir / f"{region}_training.json"

        with self._lock:
            try:
                existing = self._load_dataset(path)
                existing.append(example)

                # Trim if over limit
                if len(existing) > MAX_EXAMPLES_PER_REGION:
                    existing = existing[-MAX_EXAMPLES_PER_REGION:]

                self._save_dataset(path, existing)
                self._counts[region] = self._counts.get(region, 0) + 1
                return True
            except Exception as e:
                logger.error("Failed to append training data for '%s': %s", region, e)
                return False

    @staticmethod
    def _load_dataset(path: Path) -> List[Dict[str, Any]]:
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, IOError):
            return []

    @staticmethod
    def _save_dataset(path: Path, data: List[Dict[str, Any]]) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(path)

    def get_dataset(self, region: str) -> List[Dict[str, Any]]:
        """Load and return all training examples for a region."""
        path = self.base_dir / f"{region}_training.json"
        return self._load_dataset(path)

    def dataset_stats(self) -> Dict[str, Any]:
        """Return per-region dataset statistics."""
        stats = {}
        for path in self.base_dir.glob("*_training.json"):
            region = path.stem.replace("_training", "")
            data = self._load_dataset(path)
            stats[region] = {
                "examples": len(data),
                "file_size_kb": round(path.stat().st_size / 1024, 1) if path.exists() else 0,
                "routed_this_session": self._counts.get(region, 0),
            }
        # Include DPO pair counts
        for path in self.base_dir.glob("*_dpo_pairs.json"):
            region = path.stem.replace("_dpo_pairs", "")
            data = self._load_dataset(path)
            if region in stats:
                stats[region]["dpo_pairs"] = len(data)
            else:
                stats[region] = {"dpo_pairs": len(data)}
        return stats


# ── Singleton ────────────────────────────────────────────────────────────

_instance: Optional[DataRouter] = None


def get_data_router() -> DataRouter:
    global _instance
    if _instance is None:
        _instance = DataRouter()
    return _instance


# ── Cortex Performance Metrics ───────────────────────────────────────────

class CortexMetrics:
    """Tracks pre-filter accuracy and cortex decision quality over time.

    Records each pre-filter decision (score + skip/run) and the actual
    heartbeat evaluation score.  Computes accuracy metrics that feed back
    into training data quality signals.
    """

    MAX_ENTRIES = 500

    def __init__(self, base_dir: Optional[Path] = None):
        self._lock = threading.Lock()
        if base_dir is None:
            from repryntt.paths import data_dir
            base_dir = data_dir()
        self.metrics_path = base_dir / "cortex_training" / "prefilter_metrics.json"
        self.metrics_path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        filter_score: float,
        skipped: bool,
        eval_score: int,
        heartbeat: int,
    ) -> None:
        """Record a pre-filter decision and its actual outcome."""
        entry = {
            "ts": datetime.now().isoformat(),
            "hb": heartbeat,
            "filter": round(filter_score, 3),
            "skipped": skipped,
            "eval": eval_score,
        }

        with self._lock:
            entries = self._load()
            entries.append(entry)
            if len(entries) > self.MAX_ENTRIES:
                entries = entries[-self.MAX_ENTRIES:]
            self._save(entries)

    def accuracy(self, window: int = 50) -> Dict[str, Any]:
        """Compute pre-filter accuracy over the last N decisions.

        A "correct" decision is:
          - skip=True  AND eval would have been <= 2  (correctly skipped junk)
          - skip=False AND eval >= 3                  (correctly ran good work)

        A "wrong" decision is:
          - skip=True  AND eval >= 3  (skipped something worthwhile — false negative)
          - skip=False AND eval <= 1  (wasted an API call — false positive)
        """
        entries = self._load()[-window:]
        if not entries:
            return {"total": 0, "accuracy": 0.0}

        correct = 0
        false_neg = 0
        false_pos = 0
        for e in entries:
            if e.get("skipped"):
                # We can't know the eval for skipped heartbeats, so we
                # trust the filter decision was correct for skips.
                correct += 1
            else:
                ev = e.get("eval", 3)
                if ev >= 3:
                    correct += 1
                elif ev <= 1:
                    false_pos += 1
                else:
                    correct += 1  # Score 2 is borderline, count as OK

        total = len(entries)
        return {
            "total": total,
            "correct": correct,
            "false_positives": false_pos,
            "false_negatives": false_neg,
            "accuracy": round(correct / total, 3) if total else 0.0,
            "filter_score_avg": round(
                sum(e.get("filter", 0) for e in entries) / total, 3
            ) if total else 0.0,
            "eval_score_avg": round(
                sum(e.get("eval", 0) for e in entries if not e.get("skipped")) /
                max(1, sum(1 for e in entries if not e.get("skipped"))), 2
            ),
        }

    def _load(self) -> list:
        if not self.metrics_path.exists():
            return []
        try:
            return json.loads(self.metrics_path.read_text())
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, data: list) -> None:
        tmp = self.metrics_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.metrics_path)


_metrics_instance: Optional[CortexMetrics] = None


def get_cortex_metrics() -> CortexMetrics:
    global _metrics_instance
    if _metrics_instance is None:
        _metrics_instance = CortexMetrics()
    return _metrics_instance
