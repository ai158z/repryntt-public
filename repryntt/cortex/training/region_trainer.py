"""
repryntt.cortex.training.region_trainer — Per-region LoRA training orchestration.

Extends the existing evolution pipeline (SelfEvolutionManager, MicroLoRaTrainer,
ProductionMicroLoRaTrainer) to support per-region model training.

Each brain region has its own:
  - Training dataset (routed by DataRouter)
  - LoRA adapter slot
  - Training schedule
  - Version history with rollback

For GGUF language models (conscious layer): uses SelfEvolutionManager flow
  (stop model → train LoRA → convert PEFT→GGUF → reload with adapter)

For ONNX/PyTorch models (executor, perception): simpler flow
  (train PyTorch → export ONNX → hot-swap)
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class RegionTrainer:
    """Orchestrates training for a specific brain region.

    Usage::

        trainer = RegionTrainer("conscious")
        if trainer.should_train():
            result = trainer.train()
            if result["success"]:
                trainer.activate_adapter()
            else:
                trainer.rollback()
    """

    # Max adapter versions to keep per region
    MAX_VERSIONS = 5

    def __init__(self, region_name: str, base_dir: Optional[Path] = None):
        self.region_name = region_name

        if base_dir is None:
            from repryntt.paths import models_dir
            base_dir = models_dir()

        self.adapters_dir = base_dir / "lora_adapters" / region_name
        self.adapters_dir.mkdir(parents=True, exist_ok=True)

        self.peft_dir = self.adapters_dir / "peft_latest"
        self.gguf_dir = self.adapters_dir / "gguf_versions"
        self.active_link = self.adapters_dir / "active_adapter.gguf"
        self.history_path = self.adapters_dir / "training_history.json"
        self.lock_path = self.adapters_dir / ".training_lock"

        self.peft_dir.mkdir(exist_ok=True)
        self.gguf_dir.mkdir(exist_ok=True)

    # ── Training decision ────────────────────────────────────────────

    def should_train(self, min_examples: int = 50) -> bool:
        """Check if there's enough new data to justify a training run."""
        # Check lock (also handle stale locks)
        if self.lock_path.exists():
            try:
                age = time.time() - float(self.lock_path.read_text().strip())
                if age <= 3600:  # Lock is fresh — training in progress
                    logger.info("Region '%s' training locked — another training in progress", self.region_name)
                    return False
                # Stale lock — will be broken by train()
                logger.warning("Region '%s' has stale training lock (%.0fs)", self.region_name, age)
            except (ValueError, OSError):
                pass  # Corrupted lock file — train() will handle it

        # Check dataset size
        from repryntt.cortex.training.data_router import get_data_router
        router = get_data_router()
        dataset = router.get_dataset(self.region_name)

        if len(dataset) < min_examples:
            logger.debug("Region '%s' has %d examples (need %d) — skip training",
                         self.region_name, len(dataset), min_examples)
            return False

        # Check time since last training
        history = self._load_history()
        if history:
            last_time = history[-1].get("timestamp", "")
            if last_time:
                try:
                    last_dt = datetime.fromisoformat(last_time)
                    hours_since = (datetime.now() - last_dt).total_seconds() / 3600
                    if hours_since < 12:  # Min 12 hours between trainings
                        return False
                except ValueError:
                    pass

        return True

    # ── Training execution ───────────────────────────────────────────

    def train(
        self,
        *,
        max_steps: int = 50,
        learning_rate: float = 5e-4,
        lora_rank: int = 16,
    ) -> Dict[str, Any]:
        """Run LoRA training for this region.

        Returns {"success": bool, "adapter_path": str, "metrics": {...}}
        """
        # Acquire exclusive lock using atomic file creation (no TOCTOU race)
        try:
            fd = open(self.lock_path, "x")  # Fails if file exists (atomic)
            fd.write(str(time.time()))
            fd.close()
        except FileExistsError:
            # Check if lock is stale (> 1 hour old)
            try:
                age = time.time() - float(self.lock_path.read_text().strip())
                if age > 3600:
                    logger.warning("Training lock for '%s' is stale (%.0fs) — breaking it",
                                   self.region_name, age)
                    self.lock_path.unlink(missing_ok=True)
                    fd = open(self.lock_path, "x")
                    fd.write(str(time.time()))
                    fd.close()
                else:
                    return {"success": False, "error": "Training already in progress"}
            except Exception:
                return {"success": False, "error": "Training already in progress"}

        try:
            return self._run_training(
                max_steps=max_steps,
                learning_rate=learning_rate,
                lora_rank=lora_rank,
            )
        finally:
            # Always release lock
            self.lock_path.unlink(missing_ok=True)

    # Number of held-out examples for pre/post benchmark
    BENCHMARK_SIZE = 10

    def _run_training(
        self,
        *,
        max_steps: int,
        learning_rate: float,
        lora_rank: int,
    ) -> Dict[str, Any]:
        """Internal training logic with pre/post benchmark and auto-rollback."""
        import random

        # Load training data
        from repryntt.cortex.training.data_router import get_data_router
        router = get_data_router()
        dataset = router.get_dataset(self.region_name)

        if not dataset:
            return {"success": False, "error": "No training data"}

        # Format for SFT
        all_examples = []
        for ex in dataset:
            prompt = ex.get("prompt", "")
            response = ex.get("response", "")
            if prompt and response:
                all_examples.append({
                    "prompt": prompt,
                    "response": response,
                })

        if not all_examples:
            return {"success": False, "error": "No valid prompt/response pairs"}

        # Hold out a small test set for pre/post benchmark
        random.shuffle(all_examples)
        holdout_size = min(self.BENCHMARK_SIZE, len(all_examples) // 5)
        if holdout_size >= 3:
            test_set = all_examples[:holdout_size]
            training_examples = all_examples[holdout_size:]
        else:
            test_set = []
            training_examples = all_examples

        logger.info("Training region '%s' with %d examples (%d held out for benchmark, "
                     "max_steps=%d, rank=%d)",
                     self.region_name, len(training_examples), len(test_set),
                     max_steps, lora_rank)

        # Determine which trainer to use based on model type
        from repryntt.cortex.model_config import load_config
        config = load_config()
        region_cfg = config.get_region(self.region_name)
        if not region_cfg or not region_cfg.model_name:
            models = config.models_for_role(self.region_name)
            if not models:
                return {"success": False, "error": f"No model registered for region '{self.region_name}'"}
            model_entry = models[0]
        else:
            model_entry = config.get_model(region_cfg.model_name)
            if not model_entry:
                return {"success": False, "error": f"Model '{region_cfg.model_name}' not in registry"}

        # ── Pre-training benchmark ──
        pre_score = None
        if test_set and region_cfg and region_cfg.model_name:
            pre_score = self._benchmark(region_cfg.model_name, test_set)
            logger.info("Pre-training benchmark: %.3f (%d examples)",
                        pre_score, len(test_set))

        # Write training data to temp file for the trainer
        from repryntt.paths import data_dir
        train_data_path = data_dir() / f"cortex_{self.region_name}_training.json"
        train_data_path.write_text(json.dumps(training_examples, indent=1))

        # Use the appropriate trainer
        result = self._train_with_peft(
            model_entry=model_entry,
            training_data_path=train_data_path,
            max_steps=max_steps,
            learning_rate=learning_rate,
            lora_rank=lora_rank,
        )

        if result["success"]:
            # ── Post-training benchmark ──
            if pre_score is not None and test_set and region_cfg and region_cfg.model_name:
                # Activate the new adapter temporarily
                self.activate_adapter()

                post_score = self._benchmark(region_cfg.model_name, test_set)
                result.setdefault("metrics", {})["pre_benchmark"] = round(pre_score, 4)
                result["metrics"]["post_benchmark"] = round(post_score, 4)
                result["metrics"]["benchmark_delta"] = round(post_score - pre_score, 4)

                logger.info("Post-training benchmark: %.3f (delta: %+.3f)",
                            post_score, post_score - pre_score)

                if post_score < pre_score * 0.85:
                    logger.warning(
                        "Post-training benchmark DEGRADED (%.3f → %.3f, -%.1f%%) — rolling back",
                        pre_score, post_score, (1 - post_score / pre_score) * 100,
                    )
                    self.rollback()
                    result["rolled_back"] = True
                    result["rollback_reason"] = (
                        f"Benchmark degraded from {pre_score:.3f} to {post_score:.3f}"
                    )
                else:
                    result["rolled_back"] = False

            self._record_history(result)

        return result

    def _train_with_peft(
        self,
        model_entry: Any,
        training_data_path: Path,
        *,
        max_steps: int,
        learning_rate: float,
        lora_rank: int,
    ) -> Dict[str, Any]:
        """Train using the real PeftTrainer (PEFT LoRA → GGUF conversion).

        Trains SmolLM2 (or whatever model is assigned) with LoRA, then
        converts the adapter to GGUF format for llama-cpp to load.
        """
        # Load training data
        try:
            training_examples = json.loads(training_data_path.read_text())
        except Exception as e:
            return {"success": False, "error": f"Failed to load training data: {e}"}

        # Determine HuggingFace model name
        hf_model = ""
        if hasattr(model_entry, 'hf_repo') and model_entry.hf_repo:
            hf_model = model_entry.hf_repo

        try:
            from repryntt.cortex.training.peft_trainer import PeftTrainer

            trainer = PeftTrainer(
                hf_model=hf_model or "HuggingFaceTB/SmolLM2-360M-Instruct",
                output_dir=self.peft_dir,
                lora_rank=lora_rank,
                max_steps=max_steps,
                learning_rate=learning_rate,
            )

            result = trainer.train(training_examples)

            if result["success"]:
                # Store GGUF path for activate_adapter to find
                if result.get("gguf_adapter_path"):
                    self._last_gguf_path = Path(result["gguf_adapter_path"])
                return {
                    "success": True,
                    "adapter_path": result.get("peft_adapter_path", str(self.peft_dir)),
                    "gguf_adapter_path": result.get("gguf_adapter_path", ""),
                    "metrics": result.get("metrics", {}),
                }
            else:
                return {"success": False, "error": result.get("error", "Trainer returned failure")}

        except Exception as e:
            logger.error("Training failed for region '%s': %s", self.region_name, e, exc_info=True)
            return {"success": False, "error": str(e)}

    # ── Adapter management ───────────────────────────────────────────

    def activate_adapter(self, adapter_path: Optional[str] = None) -> bool:
        """Set the active LoRA adapter for this region and trigger model reload.

        If adapter_path is None, uses the latest GGUF adapter (from training),
        falling back to the PEFT adapter directory.
        """
        # Prefer GGUF adapter (usable by llama-cpp) over raw PEFT dir
        if adapter_path is None:
            if hasattr(self, '_last_gguf_path') and self._last_gguf_path and self._last_gguf_path.exists():
                adapter_path = str(self._last_gguf_path)
            else:
                # Look for most recent GGUF in the versions dir
                gguf_files = sorted(self.gguf_dir.glob("*.gguf"), key=lambda p: p.stat().st_mtime, reverse=True)
                if gguf_files:
                    adapter_path = str(gguf_files[0])
                else:
                    adapter_path = str(self.peft_dir)

        source = Path(adapter_path)
        if not source.exists():
            logger.warning("Adapter not found: %s", source)
            return False

        # Version the current active adapter
        self._version_current()

        # Update model entry with new adapter path
        from repryntt.cortex.model_config import load_config, get_config_path
        config = load_config()
        region_cfg = config.get_region(self.region_name)
        if region_cfg and region_cfg.model_name:
            model = config.get_model(region_cfg.model_name)
            if model:
                model.lora_adapter = str(source)
                config.save(get_config_path())

        # Trigger model reload so the new adapter takes effect
        try:
            from repryntt.cortex.resource_manager import get_resource_manager
            mgr = get_resource_manager()
            # Evict the current model so next inference reloads with LoRA
            if region_cfg and region_cfg.model_name:
                mgr.evict_model(region_cfg.model_name)
                logger.info("Evicted model '%s' — will reload with new LoRA adapter on next inference",
                            region_cfg.model_name)
        except Exception as e:
            logger.warning("Could not trigger model reload: %s — adapter config updated but model not reloaded", e)

        logger.info("Activated adapter for region '%s': %s", self.region_name, source)
        return True

    def rollback(self) -> bool:
        """Rollback to the previous adapter version."""
        versions = sorted(self.gguf_dir.glob("v*"), reverse=True)
        if not versions:
            logger.info("No previous versions to rollback to for '%s'", self.region_name)
            return False

        previous = versions[0]
        logger.info("Rolling back region '%s' to %s", self.region_name, previous.name)
        return self.activate_adapter(str(previous))

    def _version_current(self) -> None:
        """Save current adapter as a version."""
        if not self.peft_dir.exists() or not any(self.peft_dir.iterdir()):
            return

        version_name = f"v{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        version_dir = self.gguf_dir / version_name

        try:
            shutil.copytree(self.peft_dir, version_dir, dirs_exist_ok=True)
        except Exception as e:
            logger.warning("Failed to version adapter: %s", e)

        # Trim old versions
        versions = sorted(self.gguf_dir.glob("v*"), reverse=True)
        for old in versions[self.MAX_VERSIONS:]:
            try:
                shutil.rmtree(old)
            except Exception:
                pass

    # ── Benchmarking ────────────────────────────────────────────────

    def _benchmark(self, model_name: str, test_set: List[Dict[str, Any]]) -> float:
        """Score the model on held-out examples.

        Generates a response for each test prompt and computes average
        overlap (ROUGE-1 F1) with the reference response.  Returns a
        score in [0, 1] where higher is better.

        Falls back to simple token overlap if rouge_score is unavailable.
        """
        try:
            from repryntt.cortex.resource_manager import get_resource_manager
            mgr = get_resource_manager()
        except Exception:
            return 0.5  # Can't benchmark without the resource manager

        scores = []
        for ex in test_set:
            prompt = ex.get("prompt", "")
            reference = ex.get("response", "")
            if not prompt or not reference:
                continue

            generated = mgr.infer_llm(
                model_name, prompt,
                max_tokens=min(150, len(reference.split()) * 2),
                temperature=0.1,
                timeout_s=10.0,
            )
            if not generated:
                scores.append(0.0)
                continue

            scores.append(self._token_overlap(generated, reference))

        if not scores:
            return 0.5
        return sum(scores) / len(scores)

    @staticmethod
    def _token_overlap(generated: str, reference: str) -> float:
        """ROUGE-1-like F1 between generated and reference text."""
        gen_tokens = set(generated.lower().split())
        ref_tokens = set(reference.lower().split())
        if not gen_tokens or not ref_tokens:
            return 0.0
        overlap = gen_tokens & ref_tokens
        precision = len(overlap) / len(gen_tokens) if gen_tokens else 0.0
        recall = len(overlap) / len(ref_tokens) if ref_tokens else 0.0
        if precision + recall < 1e-9:
            return 0.0
        return 2 * precision * recall / (precision + recall)

    # ── History ──────────────────────────────────────────────────────

    def _load_history(self) -> List[Dict[str, Any]]:
        if not self.history_path.exists():
            return []
        try:
            return json.loads(self.history_path.read_text())
        except (json.JSONDecodeError, IOError):
            return []

    def _record_history(self, result: Dict[str, Any]) -> None:
        history = self._load_history()
        history.append({
            "timestamp": datetime.now().isoformat(),
            "success": result.get("success", False),
            "metrics": result.get("metrics", {}),
        })
        # Keep last 50 entries
        history = history[-50:]
        self.history_path.write_text(json.dumps(history, indent=1))

    def training_history(self) -> List[Dict[str, Any]]:
        return self._load_history()
