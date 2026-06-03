"""
Hardware detection for Repryntt — runs on ANY device (desktop, laptop, tablet, Jetson, server).

Provides a single ``get_profile()`` call that returns a frozen dataclass describing
what the current machine can do.  Every other module should import this instead of
doing its own GPU probing.

No heavy dependencies — only stdlib + optional torch / psutil.
"""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class HardwareProfile:
    """Immutable snapshot of the machine's compute capabilities."""

    # ── Identity ──────────────────────────────────────────────
    platform: str          # "Linux", "Darwin", "Windows"
    arch: str              # "aarch64", "x86_64", "arm64", etc.
    hostname: str

    # ── Accelerator ───────────────────────────────────────────
    has_gpu: bool          # True if ANY usable GPU detected
    gpu_backend: str       # "cuda", "mps", "xpu", "rocm", "cpu"
    gpu_name: str          # e.g. "NVIDIA Orin (8GB)", "Apple M2", "CPU"
    gpu_vram_mb: int       # 0 when cpu-only

    # ── Memory / disk ────────────────────────────────────────
    ram_mb: int
    disk_free_mb: int

    # ── Capabilities ─────────────────────────────────────────
    can_run_local_llm: bool   # enough RAM/VRAM for >=3B model
    can_train: bool           # GPU + enough VRAM for QLoRA
    can_mine: bool            # always True (CPU mining is valid)
    llm_gpu_layers: int       # recommended -ngl value (0 = pure CPU)

    @property
    def is_cpu_only(self) -> bool:
        return self.gpu_backend == "cpu"


# ── Detection helpers ────────────────────────────────────────────────────

def _detect_gpu() -> tuple[str, str, int]:
    """Return (backend, name, vram_mb).  Pure-stdlib fallback if torch absent."""
    try:
        import torch

        # CUDA (covers NVIDIA + AMD ROCm)
        if torch.cuda.is_available():
            idx = torch.cuda.current_device()
            name = torch.cuda.get_device_name(idx)
            props = torch.cuda.get_device_properties(idx)
            # Attribute name varies across torch versions
            total_bytes = getattr(props, "total_memory", 0) or getattr(props, "total_mem", 0)
            vram = total_bytes // (1024 * 1024)
            backend = "rocm" if ("AMD" in name.upper() or "RADEON" in name.upper()) else "cuda"
            return backend, name, vram

        # Apple Metal
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps", f"Apple {platform.processor() or 'Metal'}", 0

        # Intel XPU
        if hasattr(torch, "xpu") and torch.xpu.is_available():
            name = torch.xpu.get_device_name(0)
            return "xpu", name, 0

    except Exception:
        pass

    # No torch or no GPU — fall back to platform-specific heuristic
    from repryntt.platform_utils import has_nvidia_device, has_amd_gpu
    if has_nvidia_device():
        return "cuda", "NVIDIA (unknown — torch not installed)", 0
    if has_amd_gpu():
        return "rocm", "AMD/Intel (unknown — torch not installed)", 0
    return "cpu", "CPU", 0


def _get_ram_mb() -> int:
    try:
        import psutil
        return psutil.virtual_memory().total // (1024 * 1024)
    except ImportError:
        pass
    from repryntt.platform_utils import get_ram_mb
    return get_ram_mb()


def _get_disk_free_mb() -> int:
    try:
        return shutil.disk_usage(Path.home()).free // (1024 * 1024)
    except Exception:
        return 0


def _recommended_ngl(gpu_backend: str, vram_mb: int) -> int:
    """Recommend -ngl (number of GPU layers) based on available VRAM."""
    if gpu_backend == "cpu":
        return 0
    if vram_mb == 0:
        # VRAM unknown but GPU exists — conservative
        return 10
    if vram_mb >= 24_000:
        return 99  # offload everything
    if vram_mb >= 8_000:
        return 33
    if vram_mb >= 4_000:
        return 20
    return 10


# ── Singleton ────────────────────────────────────────────────────────────

_cached: Optional[HardwareProfile] = None


def get_profile(*, force_refresh: bool = False) -> HardwareProfile:
    """Return the hardware profile for this machine (cached after first call)."""
    global _cached
    if _cached is not None and not force_refresh:
        return _cached

    gpu_backend, gpu_name, gpu_vram = _detect_gpu()
    ram = _get_ram_mb()
    disk = _get_disk_free_mb()
    ngl = _recommended_ngl(gpu_backend, gpu_vram)

    # A 3B-parameter Q4 model needs ~2 GB RAM/VRAM
    can_local_llm = (gpu_vram >= 2048) or (ram >= 4096)
    # QLoRA needs a real GPU with at least ~4 GB
    can_train = gpu_backend != "cpu" and gpu_vram >= 4096

    _cached = HardwareProfile(
        platform=platform.system(),
        arch=platform.machine(),
        hostname=platform.node(),
        has_gpu=(gpu_backend != "cpu"),
        gpu_backend=gpu_backend,
        gpu_name=gpu_name,
        gpu_vram_mb=gpu_vram,
        ram_mb=ram,
        disk_free_mb=disk,
        can_run_local_llm=can_local_llm,
        can_train=can_train,
        can_mine=True,  # CPU mining always supported
        llm_gpu_layers=ngl,
    )
    return _cached
