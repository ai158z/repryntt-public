"""
repryntt.cortex.training.peft_trainer — Real PEFT LoRA fine-tuning for SmolLM2 on Jetson Orin Nano.

This is the bridge between the training data pipeline (DataRouter) and the
inference engine (llama-cpp via ResourceManager).  It:

  1. Loads SmolLM2-360M from HuggingFace (cached after first download)
  2. Applies LoRA configuration (rank 8, alpha 16, ~2MB trainable params)
  3. Fine-tunes on collected training data (SFT format)
  4. Saves PEFT adapter to disk
  5. Converts PEFT adapter → GGUF LoRA format for llama-cpp
  6. Returns path to GGUF adapter for ResourceManager to load

Memory budget: ~1.2GB peak (SmolLM2-360M fp16 + LoRA + optimizer + gradients)
Training time: ~60-120s for 50 steps on Orin Nano (GPU-assisted)

Dependencies (all installed):
  - peft >= 0.10
  - transformers >= 4.40
  - datasets >= 2.14
  - torch (CUDA)
"""

from __future__ import annotations

import gc
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Default HuggingFace model for conscious region
def recommended_student() -> str:
    """Hardware-adaptive student model: the biggest brain this box can actually
    train. Jetson dev kits (~5-7GB usable) get 360M; 9-15GB boxes 1.7B; bigger
    hardware 3B. Override with REPRYNTT_CORTEX_STUDENT=<hf repo>."""
    import os as _os
    env = (_os.environ.get("REPRYNTT_CORTEX_STUDENT") or "").strip()
    if env:
        return env
    try:
        import psutil as _ps
        total_gb = _ps.virtual_memory().total / (1024 ** 3)
    except Exception:
        total_gb = 8.0
    if total_gb < 9:
        return "HuggingFaceTB/SmolLM2-360M-Instruct"
    if total_gb < 15:
        return "HuggingFaceTB/SmolLM2-1.7B-Instruct"
    return "Qwen/Qwen2.5-3B-Instruct"


DEFAULT_HF_MODEL = recommended_student()

# GGUF conversion script from llama.cpp
_LLAMA_CPP_DIR = Path(os.environ.get("LLAMA_CPP_DIR", str(Path.home() / "llama.cpp")))
CONVERT_LORA_SCRIPT = Path(os.environ.get("CONVERT_LORA_SCRIPT", str(_LLAMA_CPP_DIR / "convert_lora_to_gguf.py")))

# LoRA hyperparameters tuned for SmolLM2 on 8GB Orin Nano
DEFAULT_LORA_RANK = 8
DEFAULT_LORA_ALPHA = 16
DEFAULT_TARGET_MODULES = ["q_proj", "v_proj"]  # SmolLM2 uses standard Llama-style attention
DEFAULT_MAX_STEPS = 50
DEFAULT_LEARNING_RATE = 5e-4
DEFAULT_MAX_SEQ_LEN = 512
DEFAULT_BATCH_SIZE = 1
DEFAULT_GRAD_ACCUM = 4  # Effective batch = 4

# Memory safety
MIN_AVAILABLE_MB = 800  # Don't start if less than 800MB free


class PeftTrainer:
    """Real PEFT LoRA fine-tuning for SmolLM2 on constrained hardware.

    Usage::

        trainer = PeftTrainer(
            hf_model="HuggingFaceTB/SmolLM2-360M-Instruct",
            output_dir=Path("~/.repryntt/models/lora_adapters/conscious/peft_latest"),
        )
        result = trainer.train(training_examples)
        if result["success"]:
            gguf_path = result["gguf_adapter_path"]  # Ready for llama-cpp
    """

    def __init__(
        self,
        hf_model: str = DEFAULT_HF_MODEL,
        output_dir: Optional[Path] = None,
        lora_rank: int = DEFAULT_LORA_RANK,
        lora_alpha: int = DEFAULT_LORA_ALPHA,
        target_modules: Optional[List[str]] = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        learning_rate: float = DEFAULT_LEARNING_RATE,
        max_seq_len: int = DEFAULT_MAX_SEQ_LEN,
    ):
        self.hf_model = hf_model
        self.lora_rank = lora_rank
        self.lora_alpha = lora_alpha
        self.target_modules = target_modules or DEFAULT_TARGET_MODULES
        self.max_steps = max_steps
        self.learning_rate = learning_rate
        self.max_seq_len = max_seq_len

        if output_dir is None:
            from repryntt.paths import models_dir
            output_dir = models_dir() / "lora_adapters" / "conscious" / "peft_latest"
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.gguf_output_dir = self.output_dir.parent / "gguf_versions"
        self.gguf_output_dir.mkdir(parents=True, exist_ok=True)

    # ── Memory check ─────────────────────────────────────────────────

    @staticmethod
    def check_memory() -> Dict[str, Any]:
        """Check if we have enough memory to train."""
        try:
            import psutil
            mem = psutil.virtual_memory()
            available_mb = mem.available / (1024 * 1024)
        except ImportError:
            available_mb = 2000  # Assume OK if psutil unavailable

        gpu_free_mb = 0
        try:
            import torch
            if torch.cuda.is_available():
                gpu_free_mb = torch.cuda.mem_get_info()[0] / (1024 * 1024)
        except Exception:
            pass

        return {
            "available_ram_mb": round(available_mb),
            "gpu_free_mb": round(gpu_free_mb),
            "can_train": available_mb >= MIN_AVAILABLE_MB,
        }

    # ── Main training entry point ────────────────────────────────────

    def train(self, examples: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Run PEFT LoRA fine-tuning on the given examples.

        Each example must have "prompt" and "response" keys.

        Returns::

            {
                "success": True,
                "peft_adapter_path": str,
                "gguf_adapter_path": str,    # Ready for llama-cpp load_lora()
                "metrics": {"loss": float, "steps": int, "elapsed_s": float},
            }
        """
        t0 = time.time()

        # Filter to valid examples
        valid = [ex for ex in examples if ex.get("prompt") and ex.get("response")]
        if len(valid) < 5:
            return {"success": False, "error": f"Need at least 5 valid examples, got {len(valid)}"}

        # Memory check
        mem = self.check_memory()
        if not mem["can_train"]:
            return {
                "success": False,
                "error": f"Insufficient memory: {mem['available_ram_mb']}MB available, need {MIN_AVAILABLE_MB}MB",
            }

        logger.info(
            "Starting PEFT LoRA training: %d examples, %d steps, rank=%d, model=%s",
            len(valid), self.max_steps, self.lora_rank, self.hf_model,
        )

        try:
            # Exclude the promotion gate's deterministic holdout — the gate's
            # held-out loss is only honest if these were never trained on.
            try:
                from repryntt.cortex.training.promotion_gate import is_holdout
                _n0 = len(valid)
                valid = [e for e in valid if not is_holdout(e)]
                logger.info("Holdout excluded from training: %d → %d examples",
                            _n0, len(valid))
            except Exception:
                pass
            metrics = self._run_peft_training(valid)
        except Exception as e:
            logger.error("PEFT training failed: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}
        finally:
            # Always clean up GPU memory after training
            self._cleanup_gpu()

        # Convert PEFT adapter to GGUF format for llama-cpp
        gguf_path = self._convert_to_gguf()
        if not gguf_path:
            logger.warning("GGUF conversion failed — PEFT adapter saved but not usable by llama-cpp")
            return {
                "success": True,
                "peft_adapter_path": str(self.output_dir),
                "gguf_adapter_path": "",
                "metrics": metrics,
                "warning": "GGUF conversion failed — adapter not loadable by llama-cpp",
            }

        elapsed = time.time() - t0
        metrics["elapsed_s"] = round(elapsed, 1)

        logger.info(
            "Training complete in %.1fs — loss=%.4f, adapter=%s",
            elapsed, metrics.get("loss", -1), gguf_path,
        )

        return {
            "success": True,
            "peft_adapter_path": str(self.output_dir),
            "gguf_adapter_path": str(gguf_path),
            "metrics": metrics,
        }

    # ── PEFT training implementation ─────────────────────────────────

    def _run_peft_training(self, examples: List[Dict[str, Any]]) -> Dict[str, Any]:
        """The actual PEFT/LoRA training loop."""
        import torch
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            TrainingArguments,
            Trainer,
            DataCollatorForLanguageModeling,
        )
        from peft import LoraConfig, get_peft_model, TaskType
        from datasets import Dataset

        # ── 1. Load tokenizer ────────────────────────────────────────
        logger.info("Loading tokenizer: %s", self.hf_model)
        tokenizer = AutoTokenizer.from_pretrained(
            self.hf_model,
            use_fast=True,
            trust_remote_code=True,
        )
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token

        # ── 2. Load base model (fp16 on GPU for speed) ───────────────
        logger.info("Loading base model (fp16, GPU-assisted)")
        device_map = "auto"  # Let accelerate place layers optimally

        # On Orin Nano with shared memory, "auto" works well for 360M
        model = AutoModelForCausalLM.from_pretrained(
            self.hf_model,
            torch_dtype=torch.float16,
            device_map=device_map,
            low_cpu_mem_usage=True,
            trust_remote_code=True,
        )

        # ── 3. Apply LoRA configuration ──────────────────────────────
        logger.info("Applying LoRA: rank=%d, alpha=%d, targets=%s",
                     self.lora_rank, self.lora_alpha, self.target_modules)

        lora_config = LoraConfig(
            r=self.lora_rank,
            lora_alpha=self.lora_alpha,
            target_modules=self.target_modules,
            lora_dropout=0.05,
            bias="none",
            task_type=TaskType.CAUSAL_LM,
        )

        model = get_peft_model(model, lora_config)
        # REQUIRED with gradient_checkpointing=True: checkpointing recomputes
        # segments whose inputs come from FROZEN embeddings — without forcing the
        # inputs to require grad there is no grad path to the LoRA weights, and
        # every backward dies with "element 0 of tensors does not require grad"
        # (the bug that killed all five overnight training runs, Jul 8-11).
        model.enable_input_require_grads()
        model.config.use_cache = False   # incompatible with checkpointing
        trainable, total = model.get_nb_trainable_parameters()
        logger.info("Trainable params: %s / %s (%.2f%%)",
                     f"{trainable:,}", f"{total:,}", 100 * trainable / total)

        # ── 4. Format training data ──────────────────────────────────
        def tokenize_example(ex):
            text = f"<|im_start|>user\n{ex['prompt']}<|im_end|>\n<|im_start|>assistant\n{ex['response']}<|im_end|>"
            tokens = tokenizer(
                text,
                truncation=True,
                max_length=self.max_seq_len,
                padding="max_length",
                return_tensors=None,
            )
            tokens["labels"] = tokens["input_ids"].copy()
            return tokens

        logger.info("Tokenizing %d examples", len(examples))
        tokenized = [tokenize_example(ex) for ex in examples]
        dataset = Dataset.from_list(tokenized)

        # ── 5. Training arguments (Orin-optimized) ───────────────────
        training_args = TrainingArguments(
            output_dir=str(self.output_dir / "checkpoints"),
            max_steps=self.max_steps,
            per_device_train_batch_size=DEFAULT_BATCH_SIZE,
            gradient_accumulation_steps=DEFAULT_GRAD_ACCUM,
            learning_rate=self.learning_rate,
            weight_decay=0.01,
            warmup_steps=min(5, self.max_steps // 10),
            logging_steps=10,
            save_strategy="no",  # We save manually at the end
            report_to="none",
            fp16=torch.cuda.is_available(),
            dataloader_pin_memory=False,  # Shared memory on Orin
            gradient_checkpointing=True,  # Saves ~30% memory
            optim="adamw_torch",
        )

        data_collator = DataCollatorForLanguageModeling(
            tokenizer=tokenizer,
            mlm=False,
        )

        # ── 6. Train ─────────────────────────────────────────────────
        trainer = Trainer(
            model=model,
            args=training_args,
            train_dataset=dataset,
            data_collator=data_collator,
        )

        logger.info("Starting training for %d steps...", self.max_steps)
        train_result = trainer.train()

        # Extract metrics
        metrics = {
            "loss": round(train_result.metrics.get("train_loss", -1), 4),
            "steps": self.max_steps,
            "examples": len(examples),
            "trainable_params": trainable,
        }

        # ── 7. Save PEFT adapter ─────────────────────────────────────
        logger.info("Saving PEFT adapter to %s", self.output_dir)
        model.save_pretrained(str(self.output_dir))
        tokenizer.save_pretrained(str(self.output_dir))

        # Clean up model from memory before GGUF conversion
        del trainer, model, dataset, tokenized
        self._cleanup_gpu()

        return metrics

    # ── PEFT → GGUF conversion ───────────────────────────────────────

    def _convert_to_gguf(self) -> Optional[Path]:
        """Convert PEFT adapter to GGUF LoRA format for llama-cpp.

        Uses llama.cpp's convert_lora_to_gguf.py script.
        Returns path to the generated .gguf file, or None on failure.
        """
        # Check that PEFT adapter exists
        adapter_files = list(self.output_dir.glob("adapter_model*"))
        if not adapter_files:
            logger.error("No PEFT adapter files found in %s", self.output_dir)
            return None

        # Output path
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        gguf_filename = f"lora_{self.lora_rank}_{timestamp}.gguf"
        gguf_path = self.gguf_output_dir / gguf_filename

        # Method 1: Use llama.cpp convert_lora_to_gguf.py
        if CONVERT_LORA_SCRIPT.exists():
            try:
                cmd = [
                    "python3", str(CONVERT_LORA_SCRIPT),
                    "--outfile", str(gguf_path),
                    str(self.output_dir),
                ]
                logger.info("Converting PEFT → GGUF: %s", " ".join(cmd))
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=str(CONVERT_LORA_SCRIPT.parent),
                )
                if result.returncode == 0 and gguf_path.exists():
                    size_mb = gguf_path.stat().st_size / (1024 * 1024)
                    logger.info("GGUF adapter created: %s (%.1f MB)", gguf_path, size_mb)
                    return gguf_path
                else:
                    logger.error("GGUF conversion failed (exit %d): %s",
                                 result.returncode, result.stderr[-500:] if result.stderr else "no output")
            except subprocess.TimeoutExpired:
                logger.error("GGUF conversion timed out after 120s")
            except Exception as e:
                logger.error("GGUF conversion error: %s", e)

        # Method 2: Merge adapter into base model and re-quantize (heavier but reliable)
        logger.info("Attempting merge-and-export fallback...")
        return self._merge_and_export_gguf(gguf_path)

    def _merge_and_export_gguf(self, gguf_path: Path) -> Optional[Path]:
        """Fallback: merge PEFT adapter into base, export merged model as GGUF."""
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
            from peft import PeftModel

            logger.info("Loading base model for merge...")
            base_model = AutoModelForCausalLM.from_pretrained(
                self.hf_model,
                torch_dtype=torch.float16,
                device_map="cpu",
                low_cpu_mem_usage=True,
                trust_remote_code=True,
            )

            logger.info("Loading and merging PEFT adapter...")
            model = PeftModel.from_pretrained(base_model, str(self.output_dir))
            model = model.merge_and_unload()

            # Save merged model to temp dir
            merged_dir = self.output_dir.parent / "merged_tmp"
            merged_dir.mkdir(exist_ok=True)
            model.save_pretrained(str(merged_dir))

            tokenizer = AutoTokenizer.from_pretrained(self.hf_model, trust_remote_code=True)
            tokenizer.save_pretrained(str(merged_dir))

            del model, base_model
            self._cleanup_gpu()

            # Convert merged HF model to GGUF
            convert_hf_script = Path(os.environ.get("CONVERT_HF_SCRIPT", str(_LLAMA_CPP_DIR / "convert_hf_to_gguf.py")))
            if convert_hf_script.exists():
                cmd = [
                    "python3", str(convert_hf_script),
                    str(merged_dir),
                    "--outfile", str(gguf_path),
                    "--outtype", "q8_0",
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if result.returncode == 0 and gguf_path.exists():
                    size_mb = gguf_path.stat().st_size / (1024 * 1024)
                    logger.info("Merged GGUF model created: %s (%.1f MB)", gguf_path, size_mb)
                    # Clean up temp dir
                    import shutil
                    shutil.rmtree(merged_dir, ignore_errors=True)
                    return gguf_path
                else:
                    logger.error("HF→GGUF conversion failed: %s", result.stderr[-500:] if result.stderr else "")

            # Clean up
            import shutil
            shutil.rmtree(merged_dir, ignore_errors=True)
            return None

        except Exception as e:
            logger.error("Merge-and-export failed: %s", e, exc_info=True)
            return None

    # ── Cleanup ──────────────────────────────────────────────────────

    @staticmethod
    def _cleanup_gpu():
        """Free GPU memory after training."""
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.synchronize()
        except Exception:
            pass
