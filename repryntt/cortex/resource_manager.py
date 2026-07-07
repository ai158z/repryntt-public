"""
repryntt.cortex.resource_manager — Dynamic VRAM/RAM budget and model lifecycle.

Manages model loading, unloading, and eviction based on available hardware
resources.  Adapts automatically to any machine — from a 4GB Raspberry Pi
to a 48GB workstation.

Architecture:
  - Each loaded model occupies a *slot* in the resource budget.
  - When the budget is exceeded, the lowest-priority non-resident model
    is evicted (LRU among equal priorities).
  - Models are loaded lazily — only when a region requests inference.
  - Thread-safe for concurrent region access.

Supported backends:
  - llama-cpp-python  → GGUF language models (conscious layer, etc.)
  - ONNX Runtime      → classifiers, policy nets (executor, perception)
  - PyTorch           → small MLPs, training-only models
"""

from __future__ import annotations

import logging
import threading
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from repryntt.cortex.model_config import (
    CortexConfig,
    ModelEntry,
    ModelFormat,
    RegionConfig,
    load_config,
)
from repryntt.cortex.model_registry import ModelRegistry, get_registry

logger = logging.getLogger(__name__)


# ── Loaded model wrapper ─────────────────────────────────────────────────

@dataclass
class LoadedModel:
    """A model currently resident in memory."""

    entry: ModelEntry
    backend: str                       # "llama_cpp", "onnx", "pytorch"
    handle: Any = None                 # Backend-specific model handle
    vram_used_mb: int = 0
    load_time: float = 0.0            # time.monotonic() when loaded
    last_used: float = 0.0            # time.monotonic() of last inference
    inference_count: int = 0


# ── Resource Manager ─────────────────────────────────────────────────────

class ResourceManager:
    """Manages the memory budget and model lifecycle for all cortex regions.

    Usage::

        mgr = ResourceManager()
        model = mgr.ensure_loaded("smollm2-360m-instruct-q8")
        result = mgr.infer("smollm2-360m-instruct-q8", prompt="Hello")
        mgr.unload("smollm2-360m-instruct-q8")
    """

    def __init__(
        self,
        config: Optional[CortexConfig] = None,
        registry: Optional[ModelRegistry] = None,
    ):
        self._lock = threading.RLock()
        self.config = config or load_config()
        self.registry = registry or get_registry()
        self._loaded: Dict[str, LoadedModel] = {}  # model_name → LoadedModel
        self._budget_mb = self._compute_budget()
        self._latency_history: Dict[str, List[float]] = {}  # model → [ms, ms, ...]
        self._max_latency_history = 200

        logger.info(
            "Cortex ResourceManager: budget=%d MB (%.0f%% of available)",
            self._budget_mb,
            self.config.memory_budget_percent,
        )

    # ── Budget computation ───────────────────────────────────────────

    def _compute_budget(self) -> int:
        """Compute the cortex memory budget from hardware profile + config."""
        if self.config.memory_budget_mb > 0:
            return self.config.memory_budget_mb

        from repryntt.hardware_profile import get_profile
        hw = get_profile()

        # Use GPU VRAM if available, else system RAM
        if hw.has_gpu and hw.gpu_vram_mb > 0:
            total = hw.gpu_vram_mb
        else:
            total = hw.ram_mb

        budget = int(total * self.config.memory_budget_percent / 100)

        # Minimum floor: 128 MB (enough for 135M model)
        return max(budget, 128)

    @property
    def budget_mb(self) -> int:
        return self._budget_mb

    @property
    def used_mb(self) -> int:
        with self._lock:
            return sum(lm.vram_used_mb for lm in self._loaded.values())

    @property
    def available_mb(self) -> int:
        return max(0, self._budget_mb - self.used_mb)

    # ── Model loading ────────────────────────────────────────────────

    def ensure_loaded(self, model_name: str) -> Optional[LoadedModel]:
        """Ensure a model is loaded, evicting others if needed.

        Returns the LoadedModel, or None if the model can't be loaded
        (not on disk, doesn't fit even after eviction, etc.).
        """
        with self._lock:
            # Already loaded?
            if model_name in self._loaded:
                self._loaded[model_name].last_used = time.monotonic()
                return self._loaded[model_name]

            entry = self.registry.get(model_name)
            if not entry:
                logger.warning("Model '%s' not in registry", model_name)
                return None

            if not entry.resolved_path().exists():
                logger.warning("Model file not found: %s", entry.resolved_path())
                return None

            cost = entry.vram_mb
            # Evict until we have room
            while self.used_mb + cost > self._budget_mb:
                evicted = self._evict_one()
                if not evicted:
                    logger.warning(
                        "Cannot fit %s (%d MB) — budget=%d MB, used=%d MB, "
                        "nothing left to evict",
                        model_name, cost, self._budget_mb, self.used_mb,
                    )
                    return None

            return self._load_model(entry)

    def _load_model(self, entry: ModelEntry) -> Optional[LoadedModel]:
        """Actually load a model into memory.  Must hold self._lock."""
        t0 = time.monotonic()
        backend = self._select_backend(entry)

        try:
            handle = self._load_backend(entry, backend)
        except Exception as e:
            logger.error("Failed to load %s via %s: %s", entry.name, backend, e)
            return None

        elapsed = time.monotonic() - t0
        lm = LoadedModel(
            entry=entry,
            backend=backend,
            handle=handle,
            vram_used_mb=entry.vram_mb,
            load_time=time.monotonic(),
            last_used=time.monotonic(),
        )
        self._loaded[entry.name] = lm
        logger.info(
            "Loaded %s (%s, %d MB) in %.1fs — budget: %d/%d MB used",
            entry.name, backend, entry.vram_mb, elapsed,
            self.used_mb, self._budget_mb,
        )
        # Emit telemetry event
        try:
            from repryntt.telemetry import get_ops_logger
            _ops = get_ops_logger()
            if _ops:
                _ops.log("cortex", "cortex_model_load", "ACT",
                         metadata={"model": entry.name, "backend": backend,
                                   "vram_mb": entry.vram_mb, "load_time_s": round(elapsed, 1)})
        except Exception:
            pass
        return lm

    @staticmethod
    def _select_backend(entry: ModelEntry) -> str:
        """Choose the inference backend for a model format."""
        fmt = entry.format
        if fmt == ModelFormat.GGUF:
            return "llama_cpp"
        if fmt == ModelFormat.ONNX:
            return "onnx"
        if fmt == ModelFormat.TENSORRT:
            return "tensorrt"
        return "pytorch"

    def _load_backend(self, entry: ModelEntry, backend: str) -> Any:
        """Load a model with the appropriate backend.  Returns the handle."""
        path = str(entry.resolved_path())

        if backend == "llama_cpp":
            return self._load_llama_cpp(entry, path)
        elif backend == "onnx":
            return self._load_onnx(path)
        elif backend == "pytorch":
            return self._load_pytorch(path)
        elif backend == "tensorrt":
            return self._load_tensorrt(path)
        else:
            raise ValueError(f"Unknown backend: {backend}")

    def _load_llama_cpp(self, entry: ModelEntry, path: str) -> Any:
        """Load a GGUF model via llama-cpp-python — UNLESS the external llama-server
        is already serving this exact model file, in which case we proxy to it
        instead of loading a SECOND copy into unified memory. Kills both the ~700MB
        duplication and the in-process GGML/CUDA crash class (llama-context.cpp
        asserts abort the whole daemon; a server crash just restarts the server).
        Opt out with REPRYNTT_CORTEX_VIA_SERVER=0."""
        if os.environ.get("REPRYNTT_CORTEX_VIA_SERVER", "1") != "0":
            shim = _try_server_handle(path)
            if shim is not None:
                logger.info("🔌 Cortex model '%s' → external llama-server "
                            "(no duplicate in-process load)", entry.name)
                return shim
        try:
            from llama_cpp import Llama
        except ImportError:
            raise ImportError(
                "llama-cpp-python not installed. "
                "Install with: pip install llama-cpp-python"
            )

        from repryntt.hardware_profile import get_profile
        hw = get_profile()

        # GPU layers: offload as many as fit
        n_gpu_layers = 0
        if hw.has_gpu and hw.gpu_backend == "cuda":
            # For small models, offload everything
            if entry.param_count <= 500_000_000:
                n_gpu_layers = 99
            elif entry.param_count <= 2_000_000_000:
                n_gpu_layers = 33
            else:
                n_gpu_layers = hw.llm_gpu_layers

        try:
            model = Llama(
                model_path=path,
                n_ctx=entry.context_length,
                n_gpu_layers=n_gpu_layers,
                n_threads=2,          # Conservative — leave CPU for main workload
                verbose=False,
                use_mlock=False,      # Don't pin memory — let OS manage
                logits_all=True,      # Required for logprob-based classification
            )
        except (MemoryError, RuntimeError, OSError) as e:
            # GPU OOM or CUDA error — retry with CPU-only
            logger.warning(
                "GPU load failed for %s (%s) — falling back to CPU-only: %s",
                entry.name, e.__class__.__name__, e,
            )
            try:
                model = Llama(
                    model_path=path,
                    n_ctx=entry.context_length,
                    n_gpu_layers=0,       # CPU-only fallback
                    n_threads=2,
                    verbose=False,
                    use_mlock=False,
                    logits_all=True,
                )
            except Exception as e2:
                raise RuntimeError(
                    f"Failed to load {entry.name} even on CPU: {e2}"
                ) from e2

        # Apply LoRA adapter if configured
        if entry.lora_adapter:
            lora_path = Path(entry.lora_adapter).expanduser().resolve()
            if lora_path.exists():
                try:
                    model.load_lora(str(lora_path))
                    logger.info("Applied LoRA adapter: %s", lora_path.name)
                except Exception as e:
                    logger.warning("Failed to load LoRA adapter %s: %s", lora_path, e)

        return model

    @staticmethod
    def _load_onnx(path: str) -> Any:
        """Load an ONNX model via ONNX Runtime."""
        try:
            import onnxruntime as ort
        except ImportError:
            raise ImportError(
                "onnxruntime not installed. "
                "Install with: pip install onnxruntime-gpu  (or onnxruntime for CPU)"
            )

        providers = []
        try:
            import torch
            if torch.cuda.is_available():
                providers.append("CUDAExecutionProvider")
        except ImportError:
            pass
        providers.append("CPUExecutionProvider")

        return ort.InferenceSession(path, providers=providers)

    @staticmethod
    def _load_pytorch(path: str) -> Any:
        """Load a PyTorch model (.pt or .bin)."""
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = torch.load(path, map_location=device, weights_only=False)
        if hasattr(model, "eval"):
            model.eval()
        return model

    @staticmethod
    def _load_tensorrt(path: str) -> Any:
        """Load a TensorRT engine."""
        try:
            import tensorrt as trt
            trt_logger = trt.Logger(trt.Logger.WARNING)
            runtime = trt.Runtime(trt_logger)
            with open(path, "rb") as f:
                engine = runtime.deserialize_cuda_engine(f.read())
            return engine
        except ImportError:
            raise ImportError("TensorRT not available on this system")

    # ── Inference ────────────────────────────────────────────────────

    def infer_llm(
        self,
        model_name: str,
        prompt: str,
        *,
        max_tokens: int = 200,
        temperature: float = 0.7,
        stop: Optional[List[str]] = None,
        system_prompt: str = "",
        timeout_s: float = 15.0,
    ) -> Optional[str]:
        """Run inference on a loaded GGUF language model.

        Returns the generated text, or None if model not available or timed out.
        """
        lm = self.ensure_loaded(model_name)
        if not lm or lm.backend != "llama_cpp":
            return None

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        t0 = time.monotonic()
        try:
            # Run inference in a thread with timeout to prevent hangs
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    lm.handle.create_chat_completion,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stop=stop or [],
                )
                result = future.result(timeout=timeout_s)

            elapsed_ms = (time.monotonic() - t0) * 1000
            lm.inference_count += 1
            lm.last_used = time.monotonic()

            # Track latency
            self._record_latency(model_name, elapsed_ms)

            return result["choices"][0]["message"]["content"]
        except concurrent.futures.TimeoutError:
            elapsed_ms = (time.monotonic() - t0) * 1000
            logger.warning("Inference TIMED OUT on %s after %.0fms (limit=%.0fs)",
                           model_name, elapsed_ms, timeout_s)
            self._record_latency(model_name, elapsed_ms)
            return None
        except Exception as e:
            logger.error("Inference failed on %s: %s", model_name, e)
            return None

    def classify_yes_no(
        self,
        model_name: str,
        prompt: str,
        *,
        system_prompt: str = "Answer YES or NO only.",
    ) -> Optional[float]:
        """Binary classification using logprobs — returns P(yes) in [0, 1].

        More reliable than text parsing for small models (135M-360M).
        Returns None if model not available.
        """
        import math

        lm = self.ensure_loaded(model_name)
        if not lm or lm.backend != "llama_cpp":
            return None

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        try:
            result = lm.handle.create_chat_completion(
                messages=messages,
                max_tokens=1,
                temperature=0.0,
                logprobs=True,
                top_logprobs=10,
            )
            lm.inference_count += 1
            lm.last_used = time.monotonic()

            # Extract logprobs for yes/no tokens
            lps = result["choices"][0].get("logprobs", {}).get("content", [])
            if not lps:
                # Fallback: just check the token text
                token = result["choices"][0]["message"]["content"].strip().lower()
                return 0.7 if token.startswith("y") else 0.3

            top = lps[0].get("top_logprobs", [])
            yes_lp = -10.0
            no_lp = -10.0
            for entry in top:
                tok = entry["token"].strip().lower()
                lp = entry["logprob"]
                if tok in ("yes", "y"):
                    yes_lp = max(yes_lp, lp)
                elif tok in ("no", "n"):
                    no_lp = max(no_lp, lp)

            # Convert to probabilities via softmax
            yes_p = math.exp(yes_lp)
            no_p = math.exp(no_lp)
            total = yes_p + no_p
            if total < 1e-10:
                return 0.5
            return yes_p / total

        except Exception as e:
            logger.error("classify_yes_no failed on %s: %s", model_name, e)
            return None

    def infer_classifier(
        self,
        model_name: str,
        inputs: Dict[str, Any],
    ) -> Optional[Any]:
        """Run inference on a loaded ONNX/PyTorch classifier.

        Returns raw output (numpy array or tensor), or None.
        """
        lm = self.ensure_loaded(model_name)
        if not lm:
            return None

        try:
            if lm.backend == "onnx":
                import numpy as np
                result = lm.handle.run(None, inputs)
                lm.inference_count += 1
                lm.last_used = time.monotonic()
                return result
            elif lm.backend == "pytorch":
                import torch
                with torch.no_grad():
                    result = lm.handle(**{k: torch.tensor(v) for k, v in inputs.items()})
                lm.inference_count += 1
                lm.last_used = time.monotonic()
                return result
        except Exception as e:
            logger.error("Classifier inference failed on %s: %s", model_name, e)
        return None

    # ── Latency tracking ───────────────────────────────────────────

    def _record_latency(self, model_name: str, ms: float) -> None:
        """Record an inference latency measurement."""
        hist = self._latency_history.setdefault(model_name, [])
        hist.append(ms)
        if len(hist) > self._max_latency_history:
            self._latency_history[model_name] = hist[-self._max_latency_history:]

    def latency_stats(self, model_name: str) -> Dict[str, float]:
        """Return p50, p95, p99 latency for a model."""
        hist = self._latency_history.get(model_name, [])
        if not hist:
            return {"p50": 0, "p95": 0, "p99": 0, "count": 0}
        s = sorted(hist)
        n = len(s)
        return {
            "p50": round(s[n // 2], 1),
            "p95": round(s[int(n * 0.95)], 1),
            "p99": round(s[int(n * 0.99)], 1),
            "count": n,
        }

    # ── Eviction ─────────────────────────────────────────────────────

    def _evict_one(self) -> bool:
        """Evict the lowest-priority, least-recently-used model.

        Returns True if something was evicted, False if nothing evictable.
        Must hold self._lock.
        """
        candidates = [
            (name, lm) for name, lm in self._loaded.items()
            if not self._is_resident(name)
        ]
        if not candidates:
            return False

        # Sort by priority (highest number = lowest priority), then by LRU
        candidates.sort(key=lambda x: (-self._get_priority(x[0]), x[1].last_used))
        name, lm = candidates[0]
        self._unload_model(name, lm)
        return True

    def _is_resident(self, model_name: str) -> bool:
        """Check if a model must stay resident (never evicted)."""
        entry = self.registry.get(model_name)
        if not entry:
            return False
        region_cfg = self.config.get_region(entry.role)
        return region_cfg.resident if region_cfg else False

    def _get_priority(self, model_name: str) -> int:
        """Get region priority for a model (0 = critical, higher = less important)."""
        entry = self.registry.get(model_name)
        if not entry:
            return 99
        region_cfg = self.config.get_region(entry.role)
        return region_cfg.priority if region_cfg else 99

    def _unload_model(self, name: str, lm: LoadedModel) -> None:
        """Unload a model and free its resources.  Must hold self._lock."""
        logger.info("Evicting %s (%d MB) — priority=%d, idle=%.0fs",
                     name, lm.vram_used_mb, self._get_priority(name),
                     time.monotonic() - lm.last_used)
        try:
            if lm.backend == "llama_cpp" and lm.handle is not None:
                del lm.handle
            elif lm.backend == "onnx" and lm.handle is not None:
                del lm.handle
            elif lm.backend == "pytorch" and lm.handle is not None:
                del lm.handle
            elif lm.backend == "tensorrt" and lm.handle is not None:
                del lm.handle
        except Exception as e:
            logger.warning("Error unloading %s: %s", name, e)

        self._loaded.pop(name, None)

        # Hint the GPU to reclaim memory
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

    def unload(self, model_name: str) -> None:
        """Explicitly unload a model."""
        with self._lock:
            lm = self._loaded.get(model_name)
            if lm:
                self._unload_model(model_name, lm)

    def unload_all(self) -> None:
        """Unload all models (for shutdown or training)."""
        with self._lock:
            for name in list(self._loaded.keys()):
                lm = self._loaded[name]
                self._unload_model(name, lm)

    # ── Status ───────────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        """Return a status summary for monitoring/CLI."""
        with self._lock:
            loaded = []
            for name, lm in self._loaded.items():
                loaded.append({
                    "name": name,
                    "backend": lm.backend,
                    "vram_mb": lm.vram_used_mb,
                    "inference_count": lm.inference_count,
                    "idle_seconds": round(time.monotonic() - lm.last_used, 1),
                    "role": lm.entry.role,
                })

            return {
                "budget_mb": self._budget_mb,
                "used_mb": self.used_mb,
                "available_mb": self.available_mb,
                "loaded_models": loaded,
                "registered_models": len(self.registry.all_models()),
                "missing_models": [m.name for m in self.registry.missing_models()],
                "latency": {
                    name: self.latency_stats(name)
                    for name in self._latency_history
                },
            }


# ── Singleton ────────────────────────────────────────────────────────────

_instance: Optional[ResourceManager] = None
_init_lock = threading.Lock()


def get_resource_manager(*, force_refresh: bool = False) -> ResourceManager:
    """Return the singleton ResourceManager."""
    global _instance
    if _instance is not None and not force_refresh:
        return _instance
    with _init_lock:
        if _instance is None or force_refresh:
            _instance = ResourceManager()
    return _instance

class _LlamaServerHandle:
    """Drop-in for llama_cpp.Llama, limited to create_chat_completion — proxies to
    the external llama-server (OpenAI-compatible, same response shape incl. logprobs)."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint

    def create_chat_completion(self, messages=None, max_tokens: int = 256,
                               temperature: float = 0.7, stop=None,
                               logprobs=None, top_logprobs=None, **kw):
        import requests
        body = {"messages": messages or [], "max_tokens": int(max_tokens),
                "temperature": float(temperature)}
        if stop:
            body["stop"] = stop
        if logprobs:
            body["logprobs"] = True
        if top_logprobs:
            body["top_logprobs"] = int(top_logprobs)
        for extra in ("grammar", "json_schema"):
            if extra in kw and kw[extra]:
                body[extra] = kw[extra]
        r = requests.post(self.endpoint, json=body, timeout=180)
        r.raise_for_status()
        return r.json()


def _try_server_handle(model_path: str):
    """The proxy handle IF llama-server is up AND serving this same model file."""
    try:
        import requests
        from repryntt.paths import local_llm_endpoint
        ep = local_llm_endpoint()
        base = ep.split("/v1/")[0]
        r = requests.get(base + "/v1/models", timeout=3)
        if r.status_code != 200:
            return None
        served = ""
        data = (r.json() or {}).get("data") or []
        if data:
            served = str(data[0].get("id", ""))
        import os as _os
        if _os.path.basename(served) and                 _os.path.basename(served) == _os.path.basename(model_path):
            return _LlamaServerHandle(ep)
        return None
    except Exception:
        return None

