"""
repryntt.cortex.model_registry — Central catalog of all local models.

Tracks which models are available on disk, their capabilities, and which
brain region each serves.  The ResourceManager uses this to decide what
to load/unload based on hardware budget.

Thread-safe singleton — one registry per process.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional

from repryntt.cortex.model_config import (
    CortexConfig,
    ModelEntry,
    ModelFormat,
    load_config,
)

logger = logging.getLogger(__name__)


class ModelRegistry:
    """Central catalog of all cortex models.

    Responsibilities:
    - Track registered models and their on-disk status
    - Auto-discover GGUF/ONNX files in ``~/.repryntt/models/cortex/``
    - Select the best model for a region given a VRAM budget
    """

    def __init__(self, config: Optional[CortexConfig] = None):
        self._lock = threading.Lock()
        self.config = config or load_config()
        self._models: Dict[str, ModelEntry] = {}
        self._rebuild_index()

    # ── Index management ─────────────────────────────────────────────

    def _rebuild_index(self) -> None:
        """Build name→ModelEntry mapping from config."""
        with self._lock:
            self._models = {m.name: m for m in self.config.models}

    def register(self, entry: ModelEntry) -> None:
        """Add or update a model entry."""
        with self._lock:
            self._models[entry.name] = entry
            # Sync back to config
            self.config.models = list(self._models.values())
        logger.info("Model registered: %s (%s, %d MB VRAM)",
                     entry.name, entry.format, entry.vram_mb)

    def unregister(self, name: str) -> None:
        with self._lock:
            self._models.pop(name, None)
            self.config.models = list(self._models.values())

    def get(self, name: str) -> Optional[ModelEntry]:
        return self._models.get(name)

    def all_models(self) -> List[ModelEntry]:
        return list(self._models.values())

    # ── Discovery ────────────────────────────────────────────────────

    def discover_models(self, search_dir: Optional[Path] = None) -> int:
        """Scan a directory for model files and register any new ones.

        Returns the number of newly discovered models.
        """
        if search_dir is None:
            from repryntt.paths import models_dir
            search_dir = models_dir() / "cortex"

        if not search_dir.exists():
            return 0

        found = 0
        # Check both filename and stem against existing model names + paths
        known_paths = {
            Path(m.path).expanduser().resolve()
            for m in self._models.values()
            if m.path
        }
        for p in search_dir.iterdir():
            if p.name in self._models or p.stem in self._models:
                continue
            if p.resolve() in known_paths:
                continue

            entry = self._identify_model_file(p)
            if entry:
                self.register(entry)
                found += 1

        if found:
            logger.info("Auto-discovered %d model(s) in %s", found, search_dir)
        return found

    @staticmethod
    def _identify_model_file(path: Path) -> Optional[ModelEntry]:
        """Try to create a ModelEntry from a file's name and extension."""
        suffix = path.suffix.lower()
        if suffix == ".gguf":
            fmt = ModelFormat.GGUF
        elif suffix == ".onnx":
            fmt = ModelFormat.ONNX
        elif suffix == ".bin" or suffix == ".pt":
            fmt = ModelFormat.PYTORCH
        elif suffix == ".engine" or suffix == ".trt":
            fmt = ModelFormat.TENSORRT
        elif suffix == ".safetensors":
            fmt = ModelFormat.SAFETENSORS
        else:
            return None

        # Estimate VRAM from file size (rough, but useful as a default)
        try:
            size_mb = path.stat().st_size // (1024 * 1024)
        except OSError:
            size_mb = 0

        return ModelEntry(
            name=path.stem,
            role="unknown",
            format=fmt,
            path=str(path),
            vram_mb=size_mb,
            ram_mb=size_mb,
            description=f"Auto-discovered {fmt} model",
        )

    # ── Selection ────────────────────────────────────────────────────

    def select_for_region(
        self,
        role: str,
        budget_mb: int,
        *,
        prefer_gpu: bool = True,
    ) -> Optional[ModelEntry]:
        """Pick the best model for a region that fits within budget_mb.

        Strategy: largest model that fits within budget.  If nothing fits,
        return None (region will use rule-based fallback).
        """
        candidates = self.config.models_for_role(role)  # sorted smallest→largest
        best: Optional[ModelEntry] = None
        for m in candidates:
            cost = m.vram_mb if prefer_gpu else m.ram_mb
            if cost <= budget_mb:
                best = m  # keep going — we want the largest that fits
        return best

    def select_smallest_for_region(self, role: str) -> Optional[ModelEntry]:
        """Return the smallest model for a role (for ultra-constrained devices)."""
        candidates = self.config.models_for_role(role)
        return candidates[0] if candidates else None

    # ── Availability ─────────────────────────────────────────────────

    def is_available_on_disk(self, name: str) -> bool:
        """Check if a model's file actually exists on disk."""
        entry = self.get(name)
        if not entry:
            return False
        return entry.resolved_path().exists()

    def missing_models(self) -> List[ModelEntry]:
        """Return models that are registered but not yet downloaded."""
        return [m for m in self._models.values()
                if m.active and not m.resolved_path().exists()]

    def download_model(
        self,
        name: str,
        *,
        progress_callback=None,
    ) -> bool:
        """Download a registered model from HuggingFace.

        Returns True if download succeeded, False otherwise.
        The model must have hf_repo set in its config.
        """
        entry = self.get(name)
        if not entry:
            logger.error("Cannot download unknown model: %s", name)
            return False

        if entry.resolved_path().exists():
            logger.info("Model %s already exists on disk — skipping download", name)
            return True

        if not entry.hf_repo:
            logger.error("Model %s has no hf_repo configured — cannot download", name)
            return False

        # Determine the expected filename from the path
        target_path = entry.resolved_path()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        filename = target_path.name

        logger.info("Downloading model %s from %s ...", name, entry.hf_repo)

        try:
            from huggingface_hub import hf_hub_download
            downloaded_path = hf_hub_download(
                repo_id=entry.hf_repo,
                filename=filename,
                local_dir=str(target_path.parent),
                local_dir_use_symlinks=False,
            )
            if Path(downloaded_path).exists():
                logger.info("Model %s downloaded successfully → %s", name, downloaded_path)
                return True
        except ImportError:
            logger.warning("huggingface_hub not installed — trying direct GGUF URL download")
        except Exception as e:
            logger.warning("HuggingFace Hub download failed for %s: %s — trying direct URL", name, e)

        # Fallback: try direct URL download for GGUF files
        if entry.format == "gguf" and entry.hf_repo:
            try:
                import urllib.request
                url = f"https://huggingface.co/{entry.hf_repo}/resolve/main/{filename}"
                logger.info("Direct download: %s", url)

                def _progress(block_num, block_size, total_size):
                    if progress_callback and total_size > 0:
                        progress_callback(min(block_num * block_size / total_size, 1.0))

                urllib.request.urlretrieve(url, str(target_path), reporthook=_progress)
                if target_path.exists() and target_path.stat().st_size > 1_000_000:
                    logger.info("Model %s downloaded via direct URL → %s", name, target_path)
                    return True
                else:
                    # Clean up partial/failed download
                    target_path.unlink(missing_ok=True)
                    logger.info(
                        "Model %s not directly downloadable (404 or auth-gated). "
                        "Cortex will run in fallback mode without this model.", name
                    )
            except Exception as e:
                logger.info(
                    "Model %s not directly downloadable (%s). "
                    "Cortex will run in fallback mode without this model.", name, e
                )
                target_path.unlink(missing_ok=True)

        return False

    def download_missing(self, *, progress_callback=None) -> int:
        """Download all missing models. Returns count of successful downloads."""
        missing = self.missing_models()
        if not missing:
            return 0
        downloaded = 0
        for model in missing:
            if self.download_model(model.name, progress_callback=progress_callback):
                downloaded += 1
        return downloaded

    # ── Persistence ──────────────────────────────────────────────────

    def save(self) -> None:
        """Persist the current registry to cortex_config.json."""
        from repryntt.cortex.model_config import get_config_path
        self.config.save(get_config_path())


# ── Singleton ────────────────────────────────────────────────────────────

_instance: Optional[ModelRegistry] = None
_init_lock = threading.Lock()


def get_registry(*, force_refresh: bool = False) -> ModelRegistry:
    """Return the singleton ModelRegistry."""
    global _instance
    if _instance is not None and not force_refresh:
        return _instance
    with _init_lock:
        if _instance is None or force_refresh:
            _instance = ModelRegistry()
    return _instance
