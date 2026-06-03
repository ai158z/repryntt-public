#!/usr/bin/env python3
"""
QLoRa Trainer for SAIGE - Low-Rank Adaptation Fine-Tuning
Handles fine-tuning Llama models with QLoRa for self-evolution
"""

import os
import json
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig, TrainingArguments
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from trl import SFTTrainer
from datasets import Dataset
import logging
from repryntt.paths import data_dir, models_dir

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class QLoRaTrainer:
    def __init__(self):
        # Configuration - Using Hugging Face Phi-3 model (as used in SAIGE)
        self.base_model_name = "microsoft/Phi-3-mini-4k-instruct"  # Original base model
        self.training_data_path = str(data_dir() / "training_data.json")  # JSON with prompt-response pairs
        self.cache_dir = os.path.expanduser("~/.cache/huggingface/hub/models--microsoft--Phi-3-mini-4k-instruct")

        # QLoRa config
        self.lora_config = LoraConfig(
            r=16,  # Rank
            lora_alpha=32,
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
            lora_dropout=0.05,
            bias="none",
            task_type="CAUSAL_LM"
        )

        # BitsAndBytes config for quantization
        self.bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            llm_int8_enable_fp32_cpu_offload=True
        )

        # Create timestamped output directory for this session
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir = str(models_dir() / f"qlora_fine_tuned_{timestamp}")

        # Get best available model (latest fine-tuned or base)
        self.base_model_path = self._get_best_available_model()

    def _check_incremental_update_possible(self):
        """Check if incremental weight updates are possible with current setup"""
        # With GGUF + Llama.cpp, incremental updates aren't possible
        # because GGUF is read-only and Llama.cpp doesn't support hot-swapping weights
        return False

    def _get_best_available_model(self):
        """Get the best available model for training (latest fine-tuned or base)"""
        import os
        from pathlib import Path

        # Check for previously fine-tuned models in the models directory
        models_base = models_dir()
        qlora_dirs = sorted(
            [d for d in models_base.glob("qlora_fine_tuned_*") if d.is_dir()],
            key=lambda d: d.stat().st_mtime,
            reverse=True
        )

        if qlora_dirs:
            # Use the most recently modified fine-tuned model
            latest_model_dir = qlora_dirs[0]
            logger.info(f"🎯 Found previous fine-tuned model: {latest_model_dir}")
            logger.info("🔄 Training will build upon existing adaptations")

            return str(latest_model_dir)

        # Fall back to base model
        logger.info("🆕 No previous fine-tuned model found - starting from base Phi-3")
        return self.base_model_name

    def _create_latest_model_symlink(self):
        """Create symlink to latest trained model for easy access"""
        try:
            import os
            from pathlib import Path

            # Path to the GGUF model
            gguf_path = Path(self.output_dir) / "qlora_fine_tuned.gguf"
            latest_gguf_path = models_dir() / "qlora_fine_tuned.gguf"

            # Remove existing symlink if it exists
            if latest_gguf_path.exists() or latest_gguf_path.is_symlink():
                latest_gguf_path.unlink(missing_ok=True)

            # Create symlink to latest model
            if gguf_path.exists():
                latest_gguf_path.symlink_to(gguf_path)
                logger.info(f"🔗 Created symlink: {latest_gguf_path} -> {gguf_path}")
            else:
                logger.warning(f"⚠️ GGUF model not found at {gguf_path} - symlink not created")

        except Exception as e:
            logger.error(f"❌ Failed to create model symlink: {e}")

    def check_model_available(self):
        """Check if Phi-2 model is already downloaded and available"""
        try:
            # Check if cache directory exists and has model files
            if os.path.exists(self.cache_dir):
                # Look for model files in snapshots subdirectory
                snapshots_dir = os.path.join(self.cache_dir, "snapshots")
                if os.path.exists(snapshots_dir):
                    # Get the latest snapshot
                    snapshots = [d for d in os.listdir(snapshots_dir) if os.path.isdir(os.path.join(snapshots_dir, d))]
                    if snapshots:
                        latest_snapshot = os.path.join(snapshots_dir, snapshots[0])

                        # Check for essential model files
                        model_files = ["config.json", "model.safetensors", "pytorch_model.bin"]
                        for model_file in model_files:
                            if os.path.exists(os.path.join(latest_snapshot, model_file)):
                                logger.info(f"✅ Found model file: {model_file}")
                                return True

            logger.warning(f"❌ Phi-3 model not found in cache: {self.cache_dir}")
            logger.warning("Run 'python scripts/download_phi3_model.py' to download the model first")
            return False

        except Exception as e:
            logger.error(f"Error checking model availability: {e}")
            return False

    def load_training_data(self):
        """Load training data from JSON file"""
        if not os.path.exists(self.training_data_path):
            logger.warning(f"Training data not found: {self.training_data_path}")
            return None

        with open(self.training_data_path, 'r') as f:
            data = json.load(f)

        # Convert to Hugging Face Dataset format
        formatted_data = []
        for item in data:
            formatted_data.append({
                "text": f"Prompt: {item['prompt']}\nResponse: {item['response']}"
            })

        return Dataset.from_list(formatted_data)

    def run_training_session(self):
        """Run a QLoRa fine-tuning session"""
        try:
            logger.info("🔥 Starting QLoRa fine-tuning session")

            # Check if model is available before proceeding
            if not self.check_model_available():
                logger.error("❌ Phi-3 model not available - download it first with:")
                logger.error("   python scripts/download_phi3_model.py")
                logger.error("Skipping QLoRa training session")
                return

            # Load training data
            dataset = self.load_training_data()
            if dataset is None:
                logger.error("No training data available - skipping training")
                return

            # Load model and tokenizer (should be fast now that it's cached)
            logger.info(f"Loading model from {self.base_model_path}")
            logger.info("Using CPU offloading for memory management...")

            # Custom device map that allows CPU offloading for large models
            device_map = {
                "transformer.embed_tokens": "cpu",  # Embeddings to CPU
                "transformer.h": "auto",  # Hidden layers to GPU if possible
                "transformer.ln_f": "cpu",  # Layer norm to CPU
                "lm_head": "cpu"  # Language modeling head to CPU
            }

            try:
                # Handle both HuggingFace model names and local paths
                if os.path.isdir(self.base_model_path):
                    # Loading from local fine-tuned model
                    logger.info(f"Loading from local fine-tuned model: {self.base_model_path}")
                else:
                    # Loading from HuggingFace
                    logger.info(f"Loading from HuggingFace model: {self.base_model_path}")

                model = AutoModelForCausalLM.from_pretrained(
                    self.base_model_path,
                    quantization_config=self.bnb_config,
                    device_map=device_map,
                    torch_dtype=torch.bfloat16,
                    trust_remote_code=True
                )
            except Exception as e:
                logger.warning(f"Custom device map failed: {e}")
                logger.info("Falling back to auto device mapping...")
                model = AutoModelForCausalLM.from_pretrained(
                    self.base_model_path,
                    quantization_config=self.bnb_config,
                    device_map="auto",
                    torch_dtype=torch.bfloat16,
                    trust_remote_code=True
                )
            except Exception as e:
                logger.warning(f"Custom device map failed: {e}")
                logger.info("Falling back to auto device mapping...")
                model = AutoModelForCausalLM.from_pretrained(
                    self.base_model_path,
                    quantization_config=self.bnb_config,
                    device_map="auto",
                    torch_dtype=torch.bfloat16,
                    trust_remote_code=True
                )
            tokenizer = AutoTokenizer.from_pretrained(self.base_model_path)
            tokenizer.pad_token = tokenizer.eos_token

            # Prepare model for QLoRa
            model = prepare_model_for_kbit_training(model)
            model = get_peft_model(model, self.lora_config)

            # Training arguments (conservative for memory management)
            training_args = TrainingArguments(
                output_dir=self.output_dir,
                num_train_epochs=1,  # Reduced for testing
                per_device_train_batch_size=1,  # Smaller batch size to save memory
                gradient_accumulation_steps=4,  # Accumulate gradients to simulate larger batch
                optim="paged_adamw_32bit",  # Memory efficient optimizer
                save_steps=100,
                logging_steps=10,
                learning_rate=2e-4,
                max_grad_norm=0.3,
                warmup_ratio=0.03,
                lr_scheduler_type="constant",
                report_to="none"  # Disable WandB etc.
            )

            # Trainer
            trainer = SFTTrainer(
                model=model,
                train_dataset=dataset,
                peft_config=self.lora_config,
                tokenizer=tokenizer,
                args=training_args
            )

            # Train
            trainer.train()

            # Save the model
            trainer.save_model(self.output_dir)
            logger.info(f"✅ QLoRa training complete - model saved to {self.output_dir}")

            # Automatically convert to GGUF format for Llama.cpp
            logger.info("🔄 Converting to GGUF format for Llama.cpp compatibility")
            try:
                from qlora_peft_to_gguf import convert_peft_to_gguf
                convert_peft_to_gguf(self.output_dir)

                # Create symlink to latest model for easy access
                self._create_latest_model_symlink()

                logger.info("🎉 Full pipeline complete: QLoRa → GGUF conversion finished")
            except Exception as conv_e:
                logger.error(f"❌ GGUF conversion failed: {conv_e}")
                logger.info("💡 You can manually convert later with:")
                logger.info(f"   python scripts/qlora_peft_to_gguf.py {self.output_dir}")

        except Exception as e:
            logger.error(f"❌ QLoRa training failed: {e}")
            raise

if __name__ == "__main__":
    trainer = QLoRaTrainer()
    trainer.run_training_session()
