"""
repryntt.cortex.model_config — Configuration schema for the Neural Cortex.

Defines how models are catalogued, how regions are configured, and how the
overall cortex adapts to the host machine's hardware budget.

Config lives at ``~/.repryntt/models/cortex_config.json`` and is auto-generated
on first run from hardware detection.  Users can override any value.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Model formats the cortex can load ─────────────────────────────────────

class ModelFormat:
    GGUF = "gguf"              # llama.cpp / llama-cpp-python
    ONNX = "onnx"              # ONNX Runtime (classifiers, policy nets)
    PYTORCH = "pytorch"        # Raw PyTorch (training only, or small MLPs)
    TENSORRT = "tensorrt"      # NVIDIA TensorRT (max perf on Jetson/NVIDIA)
    SAFETENSORS = "safetensors"  # HuggingFace safetensors


# ── Model entry — one registered model ───────────────────────────────────

@dataclass
class ModelEntry:
    """A single model registered in the cortex."""

    name: str                          # Human-readable: "smollm2-360m-q4"
    role: str                          # Brain region: "conscious", "executor", etc.
    format: str                        # ModelFormat value
    path: str                          # Absolute or ~/-relative path to model file
    param_count: int = 0               # Approximate parameter count
    quantization: str = ""             # "q4_k_m", "q8_0", "fp16", ""
    vram_mb: int = 0                   # Estimated VRAM usage when loaded
    ram_mb: int = 0                    # Estimated system RAM when loaded (CPU fallback)
    max_latency_ms: int = 0            # Required latency ceiling (0 = no constraint)
    context_length: int = 2048         # For language models: max context
    hf_repo: str = ""                  # HuggingFace repo for download: "HuggingFaceTB/SmolLM2-360M-Instruct"
    description: str = ""
    active: bool = True                # Whether this model is eligible for loading
    lora_adapter: str = ""             # Path to LoRA adapter (if any)

    def resolved_path(self) -> Path:
        """Resolve ~ and return an absolute Path."""
        return Path(self.path).expanduser().resolve()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ModelEntry":
        # Filter unknown keys for forward compat
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in valid})


# ── Region configuration ─────────────────────────────────────────────────

@dataclass
class RegionConfig:
    """Configuration for one brain region."""

    name: str                          # "guardian", "conscious", "executor", "perception"
    priority: int = 2                  # 0 = critical (never evict), 1 = high, 2 = normal
    resident: bool = False             # If True, always kept loaded
    max_latency_ms: int = 500          # Maximum acceptable inference latency
    enabled: bool = True               # Can be disabled entirely
    model_name: str = ""               # Which ModelEntry to use (empty = auto-select)
    fallback_to_rules: bool = True     # If model fails/unavailable, use rule-based fallback

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RegionConfig":
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in valid})


# ── Top-level cortex configuration ───────────────────────────────────────

# Default region configs — tuned for minimal viable setup
_DEFAULT_REGIONS = {
    "guardian": RegionConfig(
        name="guardian",
        priority=0,
        resident=True,
        max_latency_ms=1,
        fallback_to_rules=True,
    ),
    "conscious": RegionConfig(
        name="conscious",
        priority=2,
        resident=False,
        max_latency_ms=500,
        model_name="",  # auto-select based on VRAM
    ),
    "executor": RegionConfig(
        name="executor",
        priority=1,
        resident=False,
        max_latency_ms=10,
        enabled=False,  # Activates when ROS2 hardware present
    ),
    "perception": RegionConfig(
        name="perception",
        priority=1,
        resident=False,
        max_latency_ms=50,
        enabled=False,  # Activates when sensor data available
    ),
}

# Default model catalog — known models that can be auto-downloaded
_DEFAULT_MODELS: List[Dict[str, Any]] = [
    {
        "name": "smollm2-360m-instruct-q8",
        "role": "conscious",
        "format": "gguf",
        "path": "~/.repryntt/models/cortex/smollm2-360m-instruct-q8_0.gguf",
        "param_count": 360_000_000,
        "quantization": "q8_0",
        "vram_mb": 400,
        "ram_mb": 400,
        "max_latency_ms": 300,
        "context_length": 2048,
        # The canonical GGUF lives in the -GGUF sibling repo
        # (HuggingFaceTB/SmolLM2-360M-Instruct serves PyTorch weights only).
        "hf_repo": "HuggingFaceTB/SmolLM2-360M-Instruct-GGUF",
        "description": "Conscious layer — good coherence at 360M params; ~400 MB RAM",
    },
    # The 135M variant's GGUF repo currently 401s for unauthenticated
    # downloads, so it's been dropped from the default install. The 360M
    # is small enough (~400 MB) that it's the canonical conscious layer
    # for OSS installs. If you want a 135M model, point at one you have
    # local access to via cortex_config.json.
]


@dataclass
class CortexConfig:
    """Top-level cortex configuration — persisted to disk."""

    # What fraction of available VRAM/RAM to allocate to cortex models.
    # The rest is reserved for the main LLM server, API pipeline, OS, etc.
    memory_budget_percent: int = 15

    # Override: explicit memory budget in MB (0 = use percent-based)
    memory_budget_mb: int = 0

    # Evolution training window (cron-style, default: 2-4 AM local)
    training_window_start_hour: int = 2
    training_window_end_hour: int = 4

    # How many heartbeats between conscious-layer evolution cycles
    evolution_interval_heartbeats: int = 1440  # ~24h at 60s intervals

    # Region configs
    regions: Dict[str, RegionConfig] = field(default_factory=lambda: dict(_DEFAULT_REGIONS))

    # Registered models
    models: List[ModelEntry] = field(default_factory=list)

    # ── Persistence ──────────────────────────────────────────────────

    def save(self, path: Path) -> None:
        """Write config to JSON."""
        d = {
            "memory_budget_percent": self.memory_budget_percent,
            "memory_budget_mb": self.memory_budget_mb,
            "training_window_start_hour": self.training_window_start_hour,
            "training_window_end_hour": self.training_window_end_hour,
            "evolution_interval_heartbeats": self.evolution_interval_heartbeats,
            "regions": {k: v.to_dict() for k, v in self.regions.items()},
            "models": [m.to_dict() for m in self.models],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2))
        tmp.replace(path)
        logger.info("Cortex config saved → %s", path)

    @classmethod
    def load(cls, path: Path) -> "CortexConfig":
        """Load config from JSON, falling back to defaults for missing fields."""
        if not path.exists():
            logger.info("No cortex config at %s — using defaults", path)
            return cls()
        try:
            raw = json.loads(path.read_text())
        except Exception as e:
            logger.warning("Failed to parse cortex config: %s — using defaults", e)
            return cls()

        regions = {}
        for k, v in raw.get("regions", {}).items():
            if isinstance(v, dict):
                v.setdefault("name", k)
                regions[k] = RegionConfig.from_dict(v)
        # Merge in defaults for missing regions
        for k, v in _DEFAULT_REGIONS.items():
            if k not in regions:
                regions[k] = v

        models = []
        for m in raw.get("models", []):
            if isinstance(m, dict):
                models.append(ModelEntry.from_dict(m))

        return cls(
            memory_budget_percent=raw.get("memory_budget_percent", 15),
            memory_budget_mb=raw.get("memory_budget_mb", 0),
            training_window_start_hour=raw.get("training_window_start_hour", 2),
            training_window_end_hour=raw.get("training_window_end_hour", 4),
            evolution_interval_heartbeats=raw.get("evolution_interval_heartbeats", 1440),
            regions=regions,
            models=models,
        )

    # ── Helpers ──────────────────────────────────────────────────────

    def get_region(self, name: str) -> Optional[RegionConfig]:
        return self.regions.get(name)

    def get_model(self, name: str) -> Optional[ModelEntry]:
        for m in self.models:
            if m.name == name:
                return m
        return None

    def models_for_role(self, role: str) -> List[ModelEntry]:
        """Return all models registered for a given role, smallest first."""
        return sorted(
            [m for m in self.models if m.role == role and m.active],
            key=lambda m: m.vram_mb,
        )

    def ensure_default_models(self) -> None:
        """Add default model entries if none are registered for a role."""
        existing_names = {m.name for m in self.models}
        for md in _DEFAULT_MODELS:
            if md["name"] not in existing_names:
                self.models.append(ModelEntry.from_dict(md))


def get_config_path() -> Path:
    """Return the standard cortex config path."""
    from repryntt.paths import models_dir
    return models_dir() / "cortex_config.json"


def load_config() -> CortexConfig:
    """Load (or create default) cortex config."""
    cfg = CortexConfig.load(get_config_path())
    cfg.ensure_default_models()
    return cfg
