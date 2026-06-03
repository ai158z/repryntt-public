#!/usr/bin/env python3
"""
DPO LoRA Trainer — Direct Preference Optimization for Self-Evolution

This is the RL component that makes local LLMs actually smarter over time.

SFT (existing qlora_trainer.py): "Here's a good response, learn from it"
DPO (this file):                  "Response A is better than B — learn WHY"

DPO directly optimizes the policy model on preference pairs without needing
a separate reward model. It's simpler, more stable, and uses less memory
than PPO/RLHF — critical for Jetson Orin's 7.4GB unified RAM.

Data flow:
    Heartbeat → Artifact Validation → Outcome Gap Detected
        → training_collector.py generates preference pair
        → preference_pairs.json accumulates pairs
        → This trainer loads pairs, trains LoRA adapter
        → self_evolution_manager.py converts to GGUF + hot-swaps

Requirements (same as existing QLoRA):
    - transformers, peft, trl, datasets, bitsandbytes, torch
    - These are already in the environment for qlora_trainer.py
"""

import json
import os
import time
import logging
import psutil
import torch
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)


class DPOLoRATrainer:
    """
    Direct Preference Optimization trainer for local LLM self-evolution.

    Trains a LoRA adapter that pushes the model toward responses that
    scored well on artifact validation and away from responses that
    looked good on paper but didn't validate in reality.
    """

    def __init__(self):
        from repryntt.paths import data_dir as get_data_dir, models_dir
        self.data_dir = get_data_dir()
        self.models_dir = models_dir()

        # Data paths
        self.preference_pairs_path = self.data_dir / "preference_pairs.json"
        self.training_log_path = self.data_dir / "dpo_training_log.json"

        # LoRA output
        self.lora_dir = self.models_dir / "lora_adapters"
        self.peft_output_dir = self.lora_dir / "peft_dpo_latest"
        self.lora_dir.mkdir(parents=True, exist_ok=True)
        self.peft_output_dir.mkdir(parents=True, exist_ok=True)

        # Auto-detect model architecture
        qwen_model = self.data_dir / "models/qwen2.5_3b_instruct/qwen2.5-3b-instruct-q4_k_m.gguf"
        if qwen_model.exists() or os.environ.get("REPRYNTT_MODEL", "").lower().startswith("qwen"):
            self.hf_model_name = "Qwen/Qwen2.5-3B-Instruct"
            self.target_modules = [
                "q_proj", "k_proj", "v_proj", "o_proj",
                "gate_proj", "up_proj", "down_proj",
            ]
        else:
            self.hf_model_name = "microsoft/Phi-3-mini-4k-instruct"
            self.target_modules = [
                "qkv_proj", "o_proj", "gate_up_proj", "down_proj",
            ]

        # Training hyperparams — conservative for Jetson
        self.lora_rank = 16
        self.lora_alpha = 32
        self.learning_rate = 5e-5  # Lower than SFT — DPO is more sensitive
        self.beta = 0.1            # DPO temperature — controls preference strength
        self.max_steps = 30        # Fewer steps than SFT — preference learning converges faster
        self.max_length = 512      # Max token length per example
        self.micro_batch_size = 1
        self.gradient_accumulation = 2

        # Minimum viable dataset
        self.min_pairs = 20  # Need at least 20 preference pairs

    def has_sufficient_data(self) -> bool:
        """Check if enough preference pairs exist for training."""
        if not self.preference_pairs_path.exists():
            return False
        try:
            with open(self.preference_pairs_path) as f:
                pairs = json.load(f)
            return len(pairs) >= self.min_pairs
        except Exception:
            return False

    def load_preference_data(self) -> Optional[List[Dict]]:
        """Load and validate preference pairs."""
        try:
            with open(self.preference_pairs_path) as f:
                raw_pairs = json.load(f)
        except Exception as e:
            logger.error(f"Could not load preference pairs: {e}")
            return None

        # Filter valid pairs
        valid = []
        for pair in raw_pairs:
            prompt = pair.get("prompt", "").strip()
            chosen = pair.get("chosen", "").strip()
            rejected = pair.get("rejected", "").strip()

            if not prompt or not chosen or not rejected:
                continue
            if len(chosen) < 30 or len(rejected) < 30:
                continue
            # Ensure chosen != rejected (degenerate pair)
            if chosen == rejected:
                continue

            valid.append({
                "prompt": prompt,
                "chosen": chosen,
                "rejected": rejected,
            })

        if len(valid) < self.min_pairs:
            logger.warning(
                f"Only {len(valid)} valid pairs (need {self.min_pairs})"
            )
            return None

        logger.info(f"📊 Loaded {len(valid)} valid preference pairs for DPO training")
        return valid

    def run_dpo_training(self) -> bool:
        """
        Run DPO training on accumulated preference pairs.

        This is the method that actually modifies model weights.
        Should only be called when:
        - Local LLM is in use (not API mode)
        - llama-server has been stopped (to free RAM)
        - Sufficient preference pairs exist (≥20)

        Returns True if training succeeded, False otherwise.
        """
        try:
            start_time = time.time()
            logger.info("🧬 Starting DPO LoRA training...")

            # Memory check
            mem = psutil.virtual_memory()
            available_gb = mem.available / (1024**3)
            if available_gb < 2.0:
                logger.error(
                    f"Insufficient RAM ({available_gb:.1f}GB). "
                    f"Stop llama-server first."
                )
                return False

            # Load data
            pairs = self.load_preference_data()
            if not pairs:
                return False

            # Import training libraries (lazy — heavy imports)
            from transformers import (
                AutoTokenizer,
                AutoModelForCausalLM,
                BitsAndBytesConfig,
            )
            from peft import LoraConfig, TaskType
            from trl import DPOConfig, DPOTrainer
            from datasets import Dataset

            # Compatibility: patch DynamicCache if needed
            try:
                from transformers.cache_utils import DynamicCache
                if not hasattr(DynamicCache, 'get_usable_length'):
                    def _compat(self, new_seq_length: int, layer_idx: int = 0) -> int:
                        return self.get_seq_length(layer_idx)
                    DynamicCache.get_usable_length = _compat
            except Exception:
                pass

            # 4-bit quantization (same as existing SFT trainer)
            bnb_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_use_double_quant=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.float16,
            )

            logger.info(f"Loading tokenizer for {self.hf_model_name}...")
            tokenizer = AutoTokenizer.from_pretrained(
                self.hf_model_name,
                use_fast=True,
                trust_remote_code=True,
            )
            if tokenizer.pad_token is None:
                tokenizer.pad_token = tokenizer.eos_token

            # GPU memory limit (same logic as SFT trainer)
            if torch.cuda.is_available():
                props = torch.cuda.get_device_properties(0)
                total_bytes = getattr(props, 'total_memory', None) or getattr(props, 'total_mem', 0)
                max_gpu_mb = int((total_bytes / (1024**2)) * 0.60)
                max_memory = {0: f"{max_gpu_mb}MB", "cpu": "4GB"}
            else:
                max_memory = {"cpu": "4GB"}

            logger.info(f"Loading {self.hf_model_name} in 4-bit...")
            model = AutoModelForCausalLM.from_pretrained(
                self.hf_model_name,
                quantization_config=bnb_config,
                device_map="auto",
                max_memory=max_memory,
                torch_dtype=torch.float16,
                trust_remote_code=True,
                low_cpu_mem_usage=True,
                attn_implementation="eager",
            )

            # LoRA config
            lora_config = LoraConfig(
                r=self.lora_rank,
                lora_alpha=self.lora_alpha,
                target_modules=self.target_modules,
                lora_dropout=0.05,
                bias="none",
                task_type=TaskType.CAUSAL_LM,
            )

            # Build DPO dataset
            dataset = Dataset.from_list(pairs)

            # DPO training config
            training_args = DPOConfig(
                output_dir=str(self.peft_output_dir),
                max_steps=self.max_steps,
                per_device_train_batch_size=self.micro_batch_size,
                gradient_accumulation_steps=self.gradient_accumulation,
                learning_rate=self.learning_rate,
                beta=self.beta,
                max_length=self.max_length,
                max_prompt_length=self.max_length // 2,
                logging_steps=5,
                save_steps=self.max_steps,
                save_total_limit=1,
                report_to="none",
                fp16=torch.cuda.is_available(),
                optim="paged_adamw_8bit",
                gradient_checkpointing=True,
                max_grad_norm=1.0,
                remove_unused_columns=False,
                dataloader_pin_memory=False,
            )

            logger.info(
                f"🔥 DPO training: {len(pairs)} pairs, "
                f"{self.max_steps} steps, beta={self.beta}"
            )

            trainer = DPOTrainer(
                model=model,
                args=training_args,
                train_dataset=dataset,
                processing_class=tokenizer,
                peft_config=lora_config,
            )

            # Train
            trainer.train()

            # Save adapter
            logger.info(f"Saving DPO LoRA adapter to {self.peft_output_dir}")
            trainer.save_model(str(self.peft_output_dir))
            tokenizer.save_pretrained(str(self.peft_output_dir))

            duration = time.time() - start_time

            # Log the training event
            self._log_training_event(len(pairs), duration)

            logger.info(f"✅ DPO training complete in {duration:.1f}s")
            return True

        except ImportError as e:
            logger.error(
                f"DPO training requires trl>=0.7.0 with DPO support: {e}. "
                f"Install with: pip install trl>=0.7.0"
            )
            return False
        except Exception as e:
            logger.error(f"DPO training failed: {e}", exc_info=True)
            return False
        finally:
            # Force cleanup to reclaim GPU memory
            try:
                import gc
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass

    def _log_training_event(self, num_pairs: int, duration: float):
        """Log training event for tracking evolution history."""
        event = {
            "timestamp": datetime.now().isoformat(),
            "type": "dpo",
            "num_pairs": num_pairs,
            "duration_seconds": round(duration, 1),
            "model": self.hf_model_name,
            "lora_rank": self.lora_rank,
            "beta": self.beta,
            "max_steps": self.max_steps,
        }
        try:
            log = []
            if self.training_log_path.exists():
                with open(self.training_log_path) as f:
                    log = json.load(f)
            log.append(event)
            # Keep last 100 events
            log = log[-100:]
            with open(self.training_log_path, 'w') as f:
                json.dump(log, f, indent=2)
        except Exception:
            pass
