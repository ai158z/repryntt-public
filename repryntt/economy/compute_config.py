"""
Shared compute contribution configuration for the chain and Nexus UI.

The Rust node consumes measured TFLOPS plus an operator-selected share.  Nexus
writes the same durable JSON file, and the CLI mirrors those values into the
systemd EnvironmentFile so service restarts keep the chain in sync.
"""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
from typing import Any


def compute_config_path() -> Path:
    return Path.home() / ".repryntt" / "data" / "compute_config.json"


def normalize_compute_share(value: Any, default: float = 1.0) -> float:
    try:
        share = float(value)
    except (TypeError, ValueError):
        share = default
    return max(0.0, min(1.0, share))


def load_compute_config() -> dict:
    path = compute_config_path()
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                data["compute_share"] = normalize_compute_share(
                    data.get("compute_share", 1.0)
                )
                return data
        except Exception:
            pass
    return {"compute_share": 1.0}


def save_compute_config(cfg: dict) -> None:
    data = dict(cfg)
    data["compute_share"] = round(
        normalize_compute_share(data.get("compute_share", 1.0)), 4
    )
    path = compute_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def estimate_tflops(profile) -> dict:
    """Estimate FP16/FP32 TFLOPS from the shared hardware profile."""
    vram = getattr(profile, "gpu_vram_mb", 0)
    backend = getattr(profile, "gpu_backend", "cpu")
    name = getattr(profile, "gpu_name", "CPU").upper()

    if backend == "cpu":
        cores = multiprocessing.cpu_count() or 4
        ram_gb = getattr(profile, "ram_mb", 4096) / 1024
        fp32 = round(min(cores * 0.08, 4.0) * min(ram_gb / 16, 1.5), 2)
        fp32 = max(fp32, 0.05)
        return {"fp32": fp32, "fp16": round(fp32 * 2, 2)}

    if "4090" in name:
        return {"fp32": 82.6, "fp16": 165.2}
    if "4080" in name:
        return {"fp32": 48.7, "fp16": 97.5}
    if "4070" in name:
        if "SUPER" in name:
            return {"fp32": 35.5, "fp16": 71.0}
        if "TI" in name:
            return {"fp32": 40.1, "fp16": 80.2}
        return {"fp32": 29.1, "fp16": 58.3}
    if "3090" in name:
        return {"fp32": 35.6, "fp16": 71.0}
    if "3080" in name:
        return {"fp32": 29.8, "fp16": 59.6}
    if "3070" in name:
        return {"fp32": 20.3, "fp16": 40.6}
    if "3060" in name:
        return {"fp32": 12.7, "fp16": 25.4}
    if "A100" in name:
        return {"fp32": 19.5, "fp16": 312.0}
    if "H100" in name:
        return {"fp32": 51.2, "fp16": 989.4}
    if "ORIN" in name:
        return {"fp32": 5.3, "fp16": 10.6}

    if vram >= 20000:
        return {"fp32": 30.0, "fp16": 60.0}
    if vram >= 8000:
        return {"fp32": 15.0, "fp16": 30.0}
    if vram >= 4000:
        return {"fp32": 8.0, "fp16": 16.0}
    return {"fp32": 3.0, "fp16": 6.0}


def local_compute_runtime() -> dict:
    """Return measured/share/effective compute values for this machine."""
    cfg = load_compute_config()
    share = normalize_compute_share(cfg.get("compute_share", 1.0))
    try:
        from repryntt.hardware_profile import get_profile

        profile = get_profile()
        tflops = estimate_tflops(profile)
        measured = float(tflops["fp16"])
        return {
            "profile": profile,
            "compute_share": share,
            "tflops_measured": measured,
            "tflops_fp16": float(tflops["fp16"]),
            "tflops_fp32": float(tflops["fp32"]),
            "tflops_effective": measured * share,
        }
    except Exception:
        measured = 1.0
        return {
            "profile": None,
            "compute_share": share,
            "tflops_measured": measured,
            "tflops_fp16": measured,
            "tflops_fp32": 0.5,
            "tflops_effective": measured * share,
        }


def read_env_file(path: Path) -> dict:
    values: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def write_env_file(path: Path, values: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".env.tmp")
    tmp.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    tmp.replace(path)

