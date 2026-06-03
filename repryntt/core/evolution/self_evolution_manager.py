#!/usr/bin/env python3
"""
SAIGE Self-Evolution Manager
=============================
Orchestrates autonomous AI self-evolution through QLoRA adapter training.

Lifecycle:
  1. AI decides if it wants to evolve (via micro_lora_trainer decision system)
  2. Stop llama-server to free GPU/RAM (~2.5GB freed)
  3. Train QLoRA adapter on self-generated chain/research data
  4. Convert PEFT adapter to GGUF-LoRA format (llama.cpp native)
  5. Restart llama-server with --lora adapter loaded
  6. Health check — rollback to previous adapter if failed

The AI genuinely evolves: it researches topics, generates training data from
its own chain-of-thought work, then trains LoRA weights that change how its
neurons fire. Each evolution cycle produces a measurably different model.
"""

import os
import sys
import json
import time
import signal
import logging
import subprocess
import shutil
import psutil
import torch
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


class SelfEvolutionManager:
    """
    Manages the complete self-evolution lifecycle for SAIGE.

    The AI trains its own LoRA adapter from self-generated research data,
    converts it to GGUF-LoRA format, and hot-loads it into llama-server.
    Includes versioning and automatic rollback on failure.
    """

    def __init__(self):
        from repryntt.paths import get_data_dir
        self.base_dir = get_data_dir()

        # Server configuration (matches llama-watchdog.cpp)
        self.llama_server_bin = Path.home() / "llama.cpp/build/bin/llama-server"
        if not self.llama_server_bin.exists():
            # Fallback to the external copy
            alt = self.base_dir / "external/llama.cpp/build/bin/llama-server"
            if alt.exists():
                self.llama_server_bin = alt

        # Auto-detect active model: prefer Qwen2.5 if available, fallback to kappa-phi
        qwen_model = self.base_dir / "models/qwen2.5_3b_instruct/qwen2.5-3b-instruct-q4_k_m.gguf"
        kappa_model = self.base_dir / "models/kappa_4k_q4/kappa-3-phi-abliterated.Q4_K_M.gguf"
        self.base_model_gguf = qwen_model if qwen_model.exists() else kappa_model
        self.server_port = 8080
        self.server_host = "0.0.0.0"

        # Server launch args (mirroring watchdog — must match llama-watchdog.cpp)
        self.server_ngl = 20      # GPU layers
        self.server_ctx = 4096    # Context length
        self.server_np = 1        # Parallel slots — MUST be 1 (gives full 4096 ctx)
        self.server_threads = 4   # CPU threads

        # LoRA adapter paths
        self.lora_dir = self.base_dir / "models/lora_adapters"
        self.peft_output_dir = self.lora_dir / "peft_latest"
        self.gguf_lora_dir = self.lora_dir / "gguf_versions"
        self.active_adapter_symlink = self.lora_dir / "active_adapter.gguf"

        # Training configuration — sized for Jetson Orin 7.4GB UNIFIED RAM
        # OOM-safe: model(~2GB) + LoRA(~25MB) + optimizer(~100MB) + grads(~50MB) = ~2.3GB
        # Remaining system processes need ~1GB → total ~3.3GB of the 3.3GB freed
        # Match HF base model to the active GGUF model
        if "qwen" in self.base_model_gguf.name.lower():
            self.hf_model_name = "Qwen/Qwen2.5-3B-Instruct"
        else:
            self.hf_model_name = "microsoft/Phi-3-mini-4k-instruct"
        self.training_data_path = self.base_dir / "data/training_data.json"
        self.lora_rank = 16         # Rank 16 = good personality expression, 4x less memory than r=32
        self.lora_alpha = 32        # 2x rank — controls adapter influence strength
        # LoRA target modules — differ by architecture
        if "qwen" in self.hf_model_name.lower():
            self.lora_target_modules = [
                "q_proj", "k_proj", "v_proj",  # Attention (Qwen uses separate Q/K/V)
                "o_proj",                        # Attention output
                "gate_proj", "up_proj",          # MLP gating (Qwen uses separate gate/up)
                "down_proj",                      # MLP output
            ]
        else:
            self.lora_target_modules = [
                "qkv_proj",       # Attention: what the AI focuses on
                "o_proj",          # Attention: how it combines what it noticed
                "gate_up_proj",    # MLP: personality, reasoning style, behavioral patterns
                "down_proj",       # MLP: output transformation, response character
            ]
        self.max_training_steps = 50      # 50 steps with effective batch 2 = 100 effective samples
        self.max_training_samples = 200   # Pull from larger pool, quality filter reduces this
        self.learning_rate = 2e-5         # Stable personality development
        self.micro_batch_size = 1
        self.gradient_accumulation = 2    # Effective batch of 2 (lower = less memory)

        # Versioning — keep more history for personality continuity
        self.max_adapter_versions = 5
        self.evolution_log_path = self.lora_dir / "evolution_log.json"

        # Conversion tool (from llama.cpp)
        self.convert_lora_script = self.base_dir / "external/llama.cpp/convert_lora_to_gguf.py"

        # Evolution lock file — prevents dual instances from both training
        self.evolution_lock_path = self.base_dir / "data" / "evolution.lock"

        # Create directories
        self.lora_dir.mkdir(parents=True, exist_ok=True)
        self.gguf_lora_dir.mkdir(parents=True, exist_ok=True)
        self.peft_output_dir.mkdir(parents=True, exist_ok=True)
        (self.base_dir / "data").mkdir(parents=True, exist_ok=True)

        # Load evolution log
        self.evolution_log = self._load_evolution_log()

    # ═══════════════════════════════════════════════════════════
    # EVOLUTION LOG
    # ═══════════════════════════════════════════════════════════

    def _load_evolution_log(self) -> dict:
        """Load evolution history"""
        if self.evolution_log_path.exists():
            try:
                with open(self.evolution_log_path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {"evolutions": [], "total_count": 0}

    def _save_evolution_log(self):
        """Save evolution history"""
        try:
            with open(self.evolution_log_path, 'w') as f:
                json.dump(self.evolution_log, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save evolution log: {e}")

    # ═══════════════════════════════════════════════════════════
    # SERVER LIFECYCLE
    # ═══════════════════════════════════════════════════════════

    def stop_llama_server(self) -> bool:
        """Stop the llama-server process to free RAM for training"""
        try:
            logger.info("🔌 Stopping llama-server to free RAM for evolution...")

            # Find llama-server processes
            killed = False
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    cmdline = ' '.join(proc.info.get('cmdline') or [])
                    if 'llama-server' in cmdline or 'llama_server' in cmdline:
                        logger.info(f"   Killing llama-server PID {proc.pid}")
                        proc.terminate()
                        killed = True
                    elif 'llama-watchdog' in cmdline:
                        logger.info(f"   Killing watchdog PID {proc.pid}")
                        proc.terminate()
                        killed = True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            if not killed:
                # Fallback: use platform-agnostic kill
                from repryntt.platform_utils import kill_process_by_name
                kill_process_by_name("llama-server")
                kill_process_by_name("llama-watchdog")

            # Wait for processes to die
            time.sleep(3)

            # Force kill if still running
            for proc in psutil.process_iter(['pid', 'cmdline']):
                try:
                    cmdline = ' '.join(proc.info.get('cmdline') or [])
                    if 'llama-server' in cmdline:
                        logger.warning(f"   Force killing PID {proc.pid}")
                        proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            time.sleep(2)

            # Verify it's dead
            for proc in psutil.process_iter(['cmdline']):
                try:
                    cmdline = ' '.join(proc.info.get('cmdline') or [])
                    if 'llama-server' in cmdline:
                        logger.error("❌ llama-server STILL running after force kill!")
                        return False
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            # Drop filesystem caches to reclaim memory
            try:
                subprocess.run(
                    ["sudo", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"],
                    capture_output=True, timeout=5
                )
            except Exception:
                pass

            time.sleep(1)

            mem = psutil.virtual_memory()
            logger.info(f"✅ llama-server stopped — {mem.available / (1024**2):.0f} MB RAM now available")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to stop llama-server: {e}")
            return False

    def start_llama_server(self, lora_path: Optional[str] = None) -> bool:
        """Start llama-server, optionally with a LoRA adapter.

        Pre-start: sync + drop_caches to give the server a clean memory slate.
        Removed --no-warmup so the KV cache is pre-populated on startup,
        preventing the first-request stall that made the system feel sluggish.
        """
        try:
            if not self.llama_server_bin.exists():
                logger.error(f"❌ llama-server binary not found at {self.llama_server_bin}")
                return False

            if not self.base_model_gguf.exists():
                logger.error(f"❌ Base model not found at {self.base_model_gguf}")
                return False

            # ── Pre-start memory flush ────────────────────────────────
            # Ensure stale page-cache from training is reclaimed BEFORE
            # llama-server tries to mmap/mlock the GGUF model.
            try:
                from repryntt.platform_utils import sync_filesystem
                sync_filesystem()
                if sys.platform.startswith("linux"):
                    subprocess.run(
                        ["sudo", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"],
                        capture_output=True, timeout=5,
                    )
                logger.info("🧹 Pre-start: OS caches dropped for clean server launch")
            except Exception:
                pass

            time.sleep(1)  # brief pause for memory reclaim

            mem = psutil.virtual_memory()
            logger.info(f"💾 Available RAM before server start: {mem.available / (1024**2):.0f} MB")

            # Build server command (matching watchdog config exactly)
            # NOTE: --no-warmup REMOVED — warmup pre-populates the KV cache
            #       so the first inference request is not painfully slow
            cmd = [
                str(self.llama_server_bin),
                "-m", str(self.base_model_gguf),
                "-ngl", str(self.server_ngl),
                "-c", str(self.server_ctx),
                "-np", str(self.server_np),
                "-t", str(self.server_threads),
                "--host", self.server_host,
                "--port", str(self.server_port),
                "--mlock",
                "--log-file", str(Path.home() / ".repryntt" / "logs" / "saige_inference.log"),
                "--path", str(self.base_dir / "saige_web"),
            ]

            # Add LoRA adapter if available
            if lora_path and os.path.exists(lora_path):
                cmd.extend(["--lora", str(lora_path)])
                logger.info(f"🧬 Starting with LoRA adapter: {Path(lora_path).name}")
            else:
                logger.info("🔄 Starting without LoRA adapter (base model only)")

            logger.info(f"🚀 Starting llama-server...")

            # Start as detached background process
            log_file = open(self.base_dir / "logs/llama_server.log", 'a')
            log_file.write(f"\n{'='*60}\n")
            log_file.write(f"Server started at {datetime.now().isoformat()}\n")
            log_file.write(f"LoRA: {lora_path or 'none'}\n")
            log_file.write(f"{'='*60}\n")
            log_file.flush()

            # Set env var BEFORE fork to prevent tokenizers deadlock warning
            env = os.environ.copy()
            env['TOKENIZERS_PARALLELISM'] = 'false'

            _popen_kwargs = dict(
                stdout=log_file,
                stderr=log_file,
                env=env,
            )
            if os.name == 'nt':
                _popen_kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                _popen_kwargs['start_new_session'] = True

            process = subprocess.Popen(
                cmd,
                **_popen_kwargs,
            )

            logger.info(f"🔄 llama-server started (PID: {process.pid}), waiting for health check...")

            # Wait for server to be ready
            if self.health_check(max_retries=40, delay=3):
                logger.info("✅ llama-server is healthy and responding")
                return True
            else:
                logger.error("❌ llama-server failed health check after startup")
                return False

        except Exception as e:
            logger.error(f"❌ Failed to start llama-server: {e}")
            return False

    def health_check(self, max_retries: int = 40, delay: int = 3) -> bool:
        """Check if llama-server is responding on the configured port"""
        import requests

        url = f"http://localhost:{self.server_port}/health"

        for i in range(max_retries):
            try:
                resp = requests.get(url, timeout=5)
                if resp.status_code == 200:
                    return True
            except Exception:
                pass

            if i < max_retries - 1:
                time.sleep(delay)

        return False

    # ═══════════════════════════════════════════════════════════
    # TRAINING DATA
    # ═══════════════════════════════════════════════════════════

    def _load_and_format_training_data(self) -> Optional[List[dict]]:
        """
        Load training data and format it in ChatML (Phi-3 format).

        The base model uses ChatML:
            <|user|>\n{prompt}<|end|>\n<|assistant|>\n{response}<|end|>

        Training on the correct format ensures the LoRA adapter
        reinforces proper response patterns.
        """
        if not self.training_data_path.exists():
            logger.error("❌ No training data file found")
            return None

        try:
            with open(self.training_data_path) as f:
                raw_data = json.load(f)
        except Exception as e:
            logger.error(f"❌ Could not load training data: {e}")
            return None

        if len(raw_data) < 5:
            logger.warning(f"Only {len(raw_data)} training samples — too few for meaningful training")
            return None

        # Priority: trading decisions & chain_response highest (learn from trades first)
        priority_map = {
            'trading_decision': 0,
            'trading_correction': 0,
            'chain_response': 1,
            'grokipedia_knowledge': 2,
            'node2040_reflection': 3,
            'self_prompt': 4,
            'node2040_general': 5,
            'emotional_thought': 6,
        }

        # Sort by priority (best first), then by recency (newest first via reversed index)
        indexed = [(i, item) for i, item in enumerate(raw_data)]
        indexed.sort(key=lambda x: (priority_map.get(x[1].get('type', ''), 9), -x[0]))

        # Take top N samples
        selected = [item for _, item in indexed[:self.max_training_samples]]

        # Format as ChatML (matching Phi-3 expected format)
        formatted = []
        for item in selected:
            prompt = (item.get('prompt') or '').strip()
            response = (item.get('response') or '').strip()

            if not prompt or not response or len(response) < 30:
                continue

            # Skip error messages and garbage data
            skip_patterns = [
                'AI_SERVICE_ERROR', 'HTTPConnectionPool', 'Read timed out',
                'Connection refused', 'error occurred', 'Traceback (most recent',
                'EARLY_CONCLUSION', 'Request failed',
            ]
            combined = prompt + response
            if any(pat.lower() in combined.lower() for pat in skip_patterns):
                continue

            # Phi-3 ChatML template
            text = f"<|user|>\n{prompt}<|end|>\n<|assistant|>\n{response}<|end|>"
            formatted.append({"text": text})

        if len(formatted) < 3:
            logger.warning(f"Only {len(formatted)} valid examples after filtering — too few")
            return None

        # Log data composition
        type_counts = {}
        for _, item in indexed[:self.max_training_samples]:
            t = item.get('type', 'unknown')
            type_counts[t] = type_counts.get(t, 0) + 1

        logger.info(f"📚 Prepared {len(formatted)} training examples in ChatML format")
        logger.info(f"📊 Composition: {type_counts}")
        return formatted

    # ═══════════════════════════════════════════════════════════
    # QLORA TRAINING
    # ═══════════════════════════════════════════════════════════

    def run_qlora_training_subprocess(self) -> bool:
        """Run QLoRA training in an isolated subprocess.

        This is the preferred entry point for the evolution cycle.
        Running training in a subprocess means that when the child
        exits, the OS reclaims ALL memory (PyTorch, CUDA, HuggingFace
        caches, etc.).  On Jetson's unified-memory architecture this
        is critical: in-process cleanup only recovers ~50% of the
        ~2 GB consumed by training, leaving the relaunched llama-server
        starved for RAM and sluggish.
        """
        try:
            logger.info("🧬 Launching QLoRA training in isolated subprocess...")
            result = subprocess.run(
                [
                    sys.executable,
                    str(self.base_dir / "scripts/self_evolution_manager.py"),
                    "--train",
                ],
                capture_output=True,
                text=True,
                timeout=600,  # 10 min max for training
                cwd=str(self.base_dir),
            )

            if result.returncode == 0:
                logger.info("✅ Subprocess training completed successfully")
                if result.stdout:
                    for line in result.stdout.strip().split('\n')[-10:]:
                        logger.info(f"   [train] {line}")
                return True
            else:
                logger.error(f"❌ Subprocess training failed (exit {result.returncode})")
                if result.stderr:
                    for line in result.stderr.strip().split('\n')[-15:]:
                        logger.error(f"   [train] {line}")
                return False

        except subprocess.TimeoutExpired:
            logger.error("❌ Subprocess training timed out (>10 min)")
            return False
        except Exception as e:
            logger.error(f"❌ Subprocess training error: {e}")
            return False

    def run_qlora_training(self) -> bool:
        """
        Execute LoRA/QLoRA training on self-generated data.

        Platform-aware:
        - CUDA: QLoRA with 4-bit NF4 quantization (bitsandbytes)
        - MPS (Apple Silicon): Standard LoRA with float32 (no quantization)
        - CPU: Standard LoRA with float32 (no quantization)

        Uses rank-4 LoRA adapter. On CUDA, 4-bit quantization loads the model
        at ~2GB. Server must be stopped first to have enough RAM.
        """
        try:
            logger.info("🧬 Starting LoRA self-evolution training...")
            start_time = time.time()

            # Verify sufficient memory (server should be stopped)
            mem = psutil.virtual_memory()
            available_gb = mem.available / (1024**3)
            logger.info(f"💾 Available RAM: {available_gb:.1f} GB")

            if available_gb < 2.0:
                logger.error(
                    f"❌ Insufficient RAM ({available_gb:.1f} GB). "
                    f"Need ≥2 GB. Is llama-server still running?"
                )
                return False

            # Load and format training data
            training_data = self._load_and_format_training_data()
            if not training_data:
                return False

            # Import training libraries
            from transformers import (
                AutoTokenizer,
                AutoModelForCausalLM,
                TrainingArguments,
                Trainer,
                DataCollatorForLanguageModeling,
            )
            from peft import LoraConfig, get_peft_model, TaskType

            # ── Compatibility fix: transformers 4.57+ renamed DynamicCache API ──
            # Phi-3's custom modeling code calls get_usable_length() which was
            # renamed to get_seq_length() in newer transformers. Monkey-patch
            # DynamicCache so the old call still works.
            try:
                from transformers.cache_utils import DynamicCache
                if not hasattr(DynamicCache, 'get_usable_length'):
                    def _get_usable_length(self, new_seq_length: int, layer_idx: int = 0) -> int:
                        """Compat shim: maps old get_usable_length → get_seq_length."""
                        return self.get_seq_length(layer_idx)
                    DynamicCache.get_usable_length = _get_usable_length
                    logger.info("🔧 Patched DynamicCache.get_usable_length → get_seq_length (transformers compat)")
            except Exception as e:
                logger.warning(f"⚠️ DynamicCache patch skipped: {e}")
            from datasets import Dataset
            import torch

            # ── Detect compute backend ──
            if torch.cuda.is_available():
                backend = "cuda"
            elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                backend = "mps"
            else:
                backend = "cpu"
            logger.info(f"🖥️ Training backend: {backend}")

            # ── Quantization config (CUDA-only — bitsandbytes requires CUDA) ──
            bnb_config = None
            model_dtype = torch.float32  # safe default for MPS and CPU
            use_quantization = False

            if backend == "cuda":
                try:
                    from transformers import BitsAndBytesConfig
                    bnb_config = BitsAndBytesConfig(
                        load_in_4bit=True,
                        bnb_4bit_use_double_quant=True,
                        bnb_4bit_quant_type="nf4",
                        bnb_4bit_compute_dtype=torch.float16,
                    )
                    model_dtype = torch.float16
                    use_quantization = True
                    logger.info("🔧 Using QLoRA with 4-bit NF4 quantization")
                except ImportError:
                    logger.warning("⚠️ bitsandbytes not available — falling back to standard LoRA on CUDA")
                    model_dtype = torch.float16
            elif backend == "mps":
                # MPS (Apple Silicon) — float32 is most stable
                # float16 has limited op coverage on MPS
                model_dtype = torch.float32
                logger.info("🍎 Using standard LoRA on Apple Silicon (MPS, float32)")
            else:
                model_dtype = torch.float32
                logger.info("🔧 Using standard LoRA on CPU (float32)")

            logger.info("🔧 Loading tokenizer...")
            tokenizer = AutoTokenizer.from_pretrained(
                self.hf_model_name,
                use_fast=True,
                trust_remote_code=True,
            )
            tokenizer.pad_token = tokenizer.eos_token

            quant_label = "4-bit quantization" if use_quantization else f"{model_dtype}"
            logger.info(f"🔧 Loading {self.hf_model_name} ({quant_label})...")

            # ── Memory limits ──
            if backend == "cuda":
                if torch.cuda.is_available():
                    props = torch.cuda.get_device_properties(0)
                    total_gpu_bytes = getattr(props, 'total_memory', None) or getattr(props, 'total_mem', 0)
                    total_gpu_mb = total_gpu_bytes / (1024**2)
                    max_gpu_mb = int(total_gpu_mb * 0.60)
                    max_memory = {0: f"{max_gpu_mb}MB", "cpu": "4GB"}
                    logger.info(f"💾 GPU memory limit: {max_gpu_mb}MB / {total_gpu_mb:.0f}MB total")
                else:
                    max_memory = {"cpu": "4GB"}
                device_map = "auto"
            elif backend == "mps":
                max_memory = None  # MPS doesn't use device_map max_memory
                device_map = None  # We'll move model to MPS after loading
            else:
                max_memory = {"cpu": "4GB"}
                device_map = "cpu"

            load_kwargs = dict(
                trust_remote_code=True,
                low_cpu_mem_usage=True,
                attn_implementation="eager",
            )
            if use_quantization and bnb_config is not None:
                load_kwargs["quantization_config"] = bnb_config
            if device_map is not None:
                load_kwargs["device_map"] = device_map
            if max_memory is not None:
                load_kwargs["max_memory"] = max_memory
            load_kwargs["torch_dtype"] = model_dtype

            model = AutoModelForCausalLM.from_pretrained(
                self.hf_model_name,
                **load_kwargs,
            )

            # On MPS, move model to device after loading
            if backend == "mps":
                model = model.to("mps")
                logger.info("🍎 Model moved to MPS device")

            mem_after = psutil.virtual_memory()
            logger.info(f"💾 RAM after model load: {mem_after.available / (1024**2):.0f} MB available")

            # LoRA configuration (minimal for Jetson)
            lora_config = LoraConfig(
                r=self.lora_rank,
                lora_alpha=self.lora_alpha,
                target_modules=self.lora_target_modules,
                lora_dropout=0.05,
                bias="none",
                task_type=TaskType.CAUSAL_LM,
            )

            logger.info(f"🔧 Applying LoRA (rank={self.lora_rank}, modules={self.lora_target_modules})...")
            model = get_peft_model(model, lora_config)
            model.print_trainable_parameters()

            # Tokenize dataset
            dataset = Dataset.from_list(training_data)

            def tokenize_fn(examples):
                tokens = tokenizer(  # noqa: F821 — captured from enclosing scope
                    examples["text"],
                    truncation=True,
                    max_length=256,       # 256 tokens max — halves memory vs 512
                    padding="max_length",
                )
                tokens["labels"] = tokens["input_ids"].copy()
                return tokens

            logger.info("🔧 Tokenizing dataset...")
            tokenized = dataset.map(tokenize_fn, batched=True, remove_columns=["text"])

            # Enable gradient checkpointing — trades compute for memory
            # Recomputes activations during backward pass instead of storing them
            model.gradient_checkpointing_enable()
            model.enable_input_require_grads()  # Required for gradient checkpointing with LoRA
            logger.info("🔧 Gradient checkpointing enabled (saves ~500MB RAM)")

            # Training arguments — platform-aware
            # CUDA: fp16 + paged_adamw_8bit (memory-efficient)
            # MPS/CPU: fp32 + adamw_torch (compatible)
            use_fp16 = backend == "cuda"
            optim_name = "paged_adamw_8bit" if use_quantization else "adamw_torch"

            training_args = TrainingArguments(
                output_dir=str(self.peft_output_dir),
                max_steps=self.max_training_steps,
                per_device_train_batch_size=self.micro_batch_size,
                gradient_accumulation_steps=self.gradient_accumulation,
                learning_rate=self.learning_rate,
                logging_steps=5,
                save_steps=self.max_training_steps,
                save_total_limit=1,
                report_to="none",
                fp16=use_fp16,
                optim=optim_name,
                remove_unused_columns=False,
                dataloader_pin_memory=False,  # Save memory on constrained devices
                gradient_checkpointing=True,   # Must match model setting
                max_grad_norm=1.0,             # Clip gradients to prevent memory spikes
                # MPS: use_mps_device is auto-detected by Trainer via torch.backends.mps
            )

            data_collator = DataCollatorForLanguageModeling(
                tokenizer=tokenizer,
                mlm=False,
            )

            trainer = Trainer(
                model=model,
                args=training_args,
                train_dataset=tokenized,
                data_collator=data_collator,
            )

            logger.info(f"🔥 Training LoRA adapter ({self.max_training_steps} steps)...")
            trainer.train()

            # Save PEFT adapter
            logger.info(f"💾 Saving PEFT adapter to {self.peft_output_dir}")
            model.save_pretrained(str(self.peft_output_dir))
            tokenizer.save_pretrained(str(self.peft_output_dir))

            duration = time.time() - start_time
            logger.info(f"✅ QLoRA training complete in {duration:.1f}s")

            # ── Aggressive memory cleanup to prevent slow restart ──
            # On Jetson the unified memory pool is shared between CPU/GPU.
            # If we don't reclaim every byte now, the relaunched llama-server
            # will compete with stale Python allocations and run at a crawl.
            logger.info("🧹 Aggressive post-training memory cleanup...")
            del model, trainer, tokenized, dataset
            del tokenizer  # also release tokenizer
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.synchronize()  # wait for all GPU ops to finish

            import gc
            gc.collect()  # reclaim Python-side objects
            gc.collect()  # second pass catches ref-cycles

            if torch.cuda.is_available():
                torch.cuda.empty_cache()  # sweep again after gc

            # Flush filesystem writes and drop OS page/slab caches
            try:
                from repryntt.platform_utils import sync_filesystem
                sync_filesystem()
                if sys.platform.startswith("linux"):
                    subprocess.run(
                        ["sudo", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"],
                        capture_output=True, timeout=5,
                    )
                logger.info("🧹 OS page caches dropped")
            except Exception:
                pass

            mem = psutil.virtual_memory()
            logger.info(f"🧹 Post-cleanup available RAM: {mem.available / (1024**2):.0f} MB")

            return True

        except ImportError as e:
            logger.error(f"❌ Missing training library: {e}")
            logger.error("Install with: pip install peft transformers bitsandbytes datasets")
            return False
        except Exception as e:
            logger.error(f"❌ QLoRA training failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            # Aggressive cleanup on failure too
            try:
                import gc
                gc.collect()
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    torch.cuda.synchronize()
                from repryntt.platform_utils import sync_filesystem
                sync_filesystem()
                if sys.platform.startswith("linux"):
                    subprocess.run(
                        ["sudo", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"],
                        capture_output=True, timeout=5,
                    )
            except Exception:
                pass
            return False

    # ═══════════════════════════════════════════════════════════
    # ADAPTER CONVERSION (PEFT → GGUF-LoRA)
    # ═══════════════════════════════════════════════════════════

    def convert_adapter_to_gguf(self) -> Optional[str]:
        """
        Convert PEFT adapter to GGUF-LoRA format using llama.cpp's converter.

        This produces a small GGUF file (~16MB) containing just the LoRA
        weights, which llama-server loads via --lora at startup.
        """
        try:
            version = self.evolution_log["total_count"] + 1
            output_path = self.gguf_lora_dir / f"saige_evolved_v{version}.gguf"

            # Verify converter exists
            if not self.convert_lora_script.exists():
                logger.error(f"❌ convert_lora_to_gguf.py not found at {self.convert_lora_script}")
                return None

            # Verify PEFT adapter exists
            adapter_config = self.peft_output_dir / "adapter_config.json"
            if not adapter_config.exists():
                logger.error(f"❌ No PEFT adapter_config.json found in {self.peft_output_dir}")
                return None

            # Ensure conversion dependencies are installed
            self._ensure_conversion_dependencies()

            logger.info(f"🔄 Converting PEFT adapter → GGUF-LoRA (v{version})...")

            # Call llama.cpp's convert_lora_to_gguf.py
            # It reads base_model_name_or_path from adapter_config.json
            # and auto-downloads the config from HuggingFace if needed
            result = subprocess.run(
                [
                    sys.executable,
                    str(self.convert_lora_script),
                    "--outfile", str(output_path),
                    "--outtype", "f16",
                    "--base-model-id", self.hf_model_name,
                    str(self.peft_output_dir),
                ],
                capture_output=True,
                text=True,
                timeout=300,
                cwd=str(self.base_dir / "external/llama.cpp"),
            )

            if result.returncode != 0:
                logger.error(f"❌ GGUF-LoRA conversion failed (exit {result.returncode})")
                logger.error(f"   stderr: {result.stderr[:500]}")
                logger.error(f"   stdout: {result.stdout[:500]}")
                return None

            if not output_path.exists():
                logger.error("❌ GGUF-LoRA output file was not created")
                return None

            size_mb = output_path.stat().st_size / (1024 * 1024)
            logger.info(f"✅ Created GGUF-LoRA adapter: {output_path.name} ({size_mb:.1f} MB)")

            # Update active adapter symlink
            if self.active_adapter_symlink.exists() or self.active_adapter_symlink.is_symlink():
                self.active_adapter_symlink.unlink()
            self.active_adapter_symlink.symlink_to(output_path)
            logger.info(f"🔗 Active adapter symlink → {output_path.name}")

            # Clean old versions
            self._cleanup_old_versions()

            return str(output_path)

        except subprocess.TimeoutExpired:
            logger.error("❌ GGUF-LoRA conversion timed out (>5 min)")
            return None
        except Exception as e:
            logger.error(f"❌ Adapter conversion failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    def _ensure_conversion_dependencies(self):
        """Install gguf and mistral-common if not already present"""
        missing = []
        try:
            import gguf  # noqa: F401
        except ImportError:
            missing.append("gguf")
        try:
            import mistral_common  # noqa: F401
        except ImportError:
            missing.append("mistral-common")
        try:
            import sentencepiece  # noqa: F401
        except ImportError:
            missing.append("sentencepiece")

        if missing:
            logger.info(f"📦 Installing conversion dependencies: {missing}")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet"] + missing,
                capture_output=True,
                timeout=120,
            )

    def _cleanup_old_versions(self):
        """Keep only the last N adapter versions"""
        try:
            versions = sorted(
                self.gguf_lora_dir.glob("saige_evolved_v*.gguf"),
                key=lambda x: x.stat().st_mtime,
                reverse=True,
            )

            for old_version in versions[self.max_adapter_versions:]:
                logger.info(f"🗑️  Removing old adapter: {old_version.name}")
                old_version.unlink()

        except Exception as e:
            logger.warning(f"Could not cleanup old adapter versions: {e}")

    # ═══════════════════════════════════════════════════════════
    # ADAPTER VERSION MANAGEMENT
    # ═══════════════════════════════════════════════════════════

    def get_active_adapter_path(self) -> Optional[str]:
        """Get path to the currently active GGUF-LoRA adapter"""
        # Check symlink
        if self.active_adapter_symlink.exists():
            resolved = self.active_adapter_symlink.resolve()
            if resolved.exists():
                return str(resolved)

        # Fallback: find latest version
        versions = sorted(
            self.gguf_lora_dir.glob("saige_evolved_v*.gguf"),
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )

        return str(versions[0]) if versions else None

    def get_rollback_adapter_path(self) -> Optional[str]:
        """Get path to the previous adapter version for rollback"""
        versions = sorted(
            self.gguf_lora_dir.glob("saige_evolved_v*.gguf"),
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )

        # Return the second-newest (current is newest)
        return str(versions[1]) if len(versions) >= 2 else None

    # ═══════════════════════════════════════════════════════════
    # MAIN EVOLUTION CYCLE
    # ═══════════════════════════════════════════════════════════

    def _acquire_evolution_lock(self) -> bool:
        """Acquire a lock file to prevent dual evolution instances.
        Returns True if lock acquired, False if another evolution is running."""
        try:
            if self.evolution_lock_path.exists():
                # Check if the PID in the lock is still alive
                try:
                    lock_data = json.loads(self.evolution_lock_path.read_text())
                    lock_pid = lock_data.get('pid', 0)
                    lock_time = lock_data.get('timestamp', '')
                    if psutil.pid_exists(lock_pid):
                        logger.error(f"❌ Evolution already running (PID {lock_pid}, started {lock_time})")
                        return False
                    else:
                        logger.warning(f"⚠️ Stale evolution lock from dead PID {lock_pid} — removing")
                except (json.JSONDecodeError, KeyError):
                    logger.warning("⚠️ Corrupt evolution lock file — removing")

            # Write our lock
            self.evolution_lock_path.write_text(json.dumps({
                'pid': os.getpid(),
                'timestamp': datetime.now().isoformat(),
            }))
            return True
        except Exception as e:
            logger.error(f"Could not acquire evolution lock: {e}")
            return False

    def _release_evolution_lock(self):
        """Release the evolution lock file."""
        try:
            if self.evolution_lock_path.exists():
                self.evolution_lock_path.unlink()
        except Exception:
            pass

    def execute_evolution_cycle(self) -> bool:
        """
        Execute the full self-evolution cycle.

        Phases:
          1. Stop llama-server (free RAM)
          2. Train QLoRA adapter on self-generated data
          3. Convert PEFT → GGUF-LoRA
          4. Restart server with new adapter
          5. Rollback if health check fails

        Returns True if evolution succeeded, False otherwise.
        The server is ALWAYS restarted (even on failure) to ensure
        SAIGE stays operational.
        """
        # Acquire lock to prevent dual evolution runs
        if not self._acquire_evolution_lock():
            return False

        evolution_start = time.time()

        logger.info("=" * 70)
        logger.info("🧬 SAIGE SELF-EVOLUTION CYCLE STARTING")
        logger.info(f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"📊 Previous evolutions: {self.evolution_log['total_count']}")
        logger.info("=" * 70)

        previous_adapter = self.get_active_adapter_path()

        try:
            return self._execute_evolution_cycle_inner(previous_adapter, evolution_start)
        except Exception as e:
            logger.error(f"❌ FATAL evolution error: {e}")
            import traceback
            logger.error(traceback.format_exc())
            self._ensure_server_running(previous_adapter)
            return False
        finally:
            self._release_evolution_lock()

    def _execute_evolution_cycle_inner(self, previous_adapter, evolution_start) -> bool:
        """Inner evolution cycle — wrapped in try/finally for lock + server safety."""

        # ── Phase 1: Stop server ──────────────────────────────
        logger.info("📍 Phase 1/5: Stopping llama-server to free RAM...")
        if not self.stop_llama_server():
            logger.error("❌ Could not stop llama-server — aborting evolution")
            # Try to ensure server is running
            self._ensure_server_running(previous_adapter)
            return False

        # ── Phase 2: Train (in subprocess for clean memory reclaim) ──
        logger.info("📍 Phase 2/5: Training QLoRA adapter on self-generated data...")
        training_success = self.run_qlora_training_subprocess()

        if not training_success:
            logger.error("❌ Training failed — restarting server with previous adapter")
            self._ensure_server_running(previous_adapter)
            return False

        # ── Phase 2b: DPO training (if preference pairs available) ──
        # SFT teaches "what good responses look like."
        # DPO teaches "why response A is better than response B."
        # Together they produce a model that generates quality AND avoids pitfalls.
        dpo_success = False
        try:
            from repryntt.core.evolution.dpo_trainer import DPOLoRATrainer
            dpo = DPOLoRATrainer()
            if dpo.has_sufficient_data():
                logger.info("📍 Phase 2b: DPO preference training (RL from self-evaluation)...")
                dpo_success = dpo.run_dpo_training()
                if dpo_success:
                    logger.info("✅ DPO training successful — model learned from outcome gaps")
                else:
                    logger.warning("⚠️ DPO training failed — continuing with SFT-only adapter")
            else:
                logger.info("📊 DPO: not enough preference pairs yet — skipping (need ≥20)")
        except ImportError:
            logger.debug("DPO trainer not available (missing trl>0.7.0) — SFT only")
        except Exception as e:
            logger.warning(f"DPO training error (non-fatal, SFT adapter still valid): {e}")

        # ── Post-training: reclaim any residual memory ────────
        # The subprocess already freed its memory on exit, but let's
        # also flush OS page caches and call malloc_trim to return
        # any freed pages from the parent process.
        try:
            from repryntt.platform_utils import sync_filesystem
            sync_filesystem()
            if sys.platform.startswith("linux"):
                subprocess.run(
                    ["sudo", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"],
                    capture_output=True, timeout=5,
                )
            # Force glibc to release freed heap pages back to the OS
            import ctypes
            try:
                if sys.platform.startswith("linux"):
                    libc = ctypes.CDLL("libc.so.6")
                    libc.malloc_trim(0)
                    logger.info("🧹 malloc_trim(0) released heap pages to OS")
            except Exception:
                pass
        except Exception:
            pass

        mem = psutil.virtual_memory()
        logger.info(f"💾 Post-training available RAM: {mem.available / (1024**2):.0f} MB")
        time.sleep(3)  # brief settle time before server launch

        # ── Phase 3: Convert ──────────────────────────────────
        logger.info("📍 Phase 3/5: Converting PEFT adapter → GGUF-LoRA format...")
        new_adapter_path = self.convert_adapter_to_gguf()

        if not new_adapter_path:
            logger.error("❌ Conversion failed — restarting server with previous adapter")
            self._ensure_server_running(previous_adapter)
            return False

        # ── Phase 4: Restart with new adapter ─────────────────
        logger.info("📍 Phase 4/5: Restarting llama-server with evolved LoRA adapter...")
        if self.start_llama_server(lora_path=new_adapter_path):
            logger.info("✅ Server running with NEW LoRA adapter!")
        else:
            # ── Phase 5: Rollback ─────────────────────────────
            logger.warning("⚠️  Phase 5/5: New adapter failed — rolling back...")
            rollback_path = self.get_rollback_adapter_path()

            if rollback_path:
                logger.info(f"🔄 Rolling back to: {Path(rollback_path).name}")
                if not self.start_llama_server(lora_path=rollback_path):
                    logger.error("❌ Rollback failed — starting without any adapter")
                    self.start_llama_server(lora_path=None)
            else:
                logger.info("🔄 No previous adapter available — starting without LoRA")
                self.start_llama_server(lora_path=None)

            return False

        # ── Record evolution ──────────────────────────────────
        duration = time.time() - evolution_start
        self.evolution_log["total_count"] += 1
        self.evolution_log["evolutions"].append({
            "version": self.evolution_log["total_count"],
            "timestamp": datetime.now().isoformat(),
            "duration_seconds": round(duration, 1),
            "adapter_path": new_adapter_path,
            "adapter_size_mb": round(Path(new_adapter_path).stat().st_size / (1024 * 1024), 1),
            "training_samples": self.max_training_samples,
            "lora_rank": self.lora_rank,
            "max_steps": self.max_training_steps,
            "dpo_applied": dpo_success,
            "success": True,
        })

        # Keep log bounded
        if len(self.evolution_log["evolutions"]) > 50:
            self.evolution_log["evolutions"] = self.evolution_log["evolutions"][-50:]

        self._save_evolution_log()

        logger.info("=" * 70)
        logger.info(f"🎉 SELF-EVOLUTION v{self.evolution_log['total_count']} COMPLETE")
        logger.info(f"⏱️  Duration: {duration:.1f}s ({duration/60:.1f} min)")
        logger.info(f"🧬 Adapter: {Path(new_adapter_path).name}")
        logger.info(f"📊 Total evolutions: {self.evolution_log['total_count']}")
        logger.info("=" * 70)

        return True

    def _ensure_server_running(self, adapter_path: Optional[str] = None):
        """Safety net: make sure the server is running no matter what"""
        try:
            import requests
            resp = requests.get(f"http://localhost:{self.server_port}/health", timeout=5)
            if resp.status_code == 200:
                logger.info("✅ Server is already running")
                return
        except Exception:
            pass

        logger.info("🔄 Server is down — restarting...")

        # Try with adapter first
        if adapter_path and os.path.exists(adapter_path):
            if self.start_llama_server(lora_path=adapter_path):
                return

        # Fall back to no adapter
        if self.start_llama_server(lora_path=None):
            return

        logger.error("❌ CRITICAL: Could not restart llama-server!")
        logger.error("   Manual intervention may be needed.")
        logger.error(f"   Try: {self.llama_server_bin} -m {self.base_model_gguf} "
                     f"-ngl {self.server_ngl} -c {self.server_ctx} "
                     f"--host {self.server_host} --port {self.server_port}")

    # ═══════════════════════════════════════════════════════════
    # STATUS / INFO
    # ═══════════════════════════════════════════════════════════

    def get_evolution_status(self) -> dict:
        """Get current evolution status for brain monitor / API"""
        active = self.get_active_adapter_path()
        return {
            "total_evolutions": self.evolution_log["total_count"],
            "active_adapter": Path(active).name if active else None,
            "active_adapter_path": active,
            "last_evolution": (
                self.evolution_log["evolutions"][-1]
                if self.evolution_log["evolutions"]
                else None
            ),
            "adapter_versions": [
                p.name for p in sorted(
                    self.gguf_lora_dir.glob("saige_evolved_v*.gguf"),
                    key=lambda x: x.stat().st_mtime,
                    reverse=True,
                )
            ],
        }


# ═══════════════════════════════════════════════════════════════════
# STANDALONE ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from repryntt.paths import logs_dir
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(str(logs_dir() / 'self_evolution.log')),
            logging.StreamHandler(),
        ],
    )

    manager = SelfEvolutionManager()

    if len(sys.argv) > 1 and sys.argv[1] == "--status":
        status = manager.get_evolution_status()
        print(json.dumps(status, indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "--train":
        # Isolated training mode — run QLoRA training and exit.
        # Called as a subprocess by execute_evolution_cycle() so that
        # ALL training memory (PyTorch, CUDA, HF caches) is reclaimed
        # when this process exits.
        print("🧬 [subprocess] Starting QLoRA training...")
        success = manager.run_qlora_training()
        if success:
            print("✅ [subprocess] Training complete")
            sys.exit(0)
        else:
            print("❌ [subprocess] Training failed")
            sys.exit(1)
    elif len(sys.argv) > 1 and sys.argv[1] == "--force":
        print("🧬 Forcing self-evolution cycle NOW...")
        success = manager.execute_evolution_cycle()
        print(f"\n{'✅ SUCCESS' if success else '❌ FAILED'}")
    else:
        print("Usage:")
        print("  python self_evolution_manager.py --status   Show evolution status")
        print("  python self_evolution_manager.py --train    Run QLoRA training (subprocess mode)")
        print("  python self_evolution_manager.py --force    Force evolution now (caution!)")
