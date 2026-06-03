"""
repryntt.paths — runtime path resolution.

Runtime paths should go through this module. The base directory can be set via:

1. Environment variable:  REPRYNTT_DATA_DIR=/path/to/data
2. Explicit call:         repryntt.paths.set_data_dir("/path/to/data")
3. Default:               ~/.repryntt/

Legacy installs can point this at their previous runtime directory to stay compatible.
"""

from __future__ import annotations

import os
from pathlib import Path

_data_dir: Path | None = None


def get_data_dir() -> Path:
    """Return the base data directory, creating it if needed."""
    global _data_dir
    if _data_dir is not None:
        return _data_dir

    env = os.environ.get("REPRYNTT_DATA_DIR")
    if env:
        _data_dir = Path(env)
    else:
        _data_dir = Path.home() / ".repryntt"

    _data_dir.mkdir(parents=True, exist_ok=True)
    return _data_dir


def set_data_dir(path: str | Path) -> None:
    """Override the data directory (call early, before other imports)."""
    global _data_dir
    _data_dir = Path(path)
    _data_dir.mkdir(parents=True, exist_ok=True)


# ── Derived paths ────────────────────────────────────────────────────────

def brain_dir() -> Path:
    """brain/ subdirectory — personality files, chain data, etc."""
    d = get_data_dir() / "brain"
    d.mkdir(parents=True, exist_ok=True)
    return d


def models_dir() -> Path:
    """models/ subdirectory — GGUF files, LoRA adapters."""
    d = get_data_dir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_dir() -> Path:
    """data/ subdirectory — training data, evolution locks."""
    d = get_data_dir() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir() -> Path:
    """logs/ subdirectory."""
    d = get_data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def workspace_dir() -> Path:
    """workspace/ subdirectory — all agent work products."""
    d = get_data_dir() / "workspace"
    d.mkdir(parents=True, exist_ok=True)
    return d


def operator_dir() -> Path:
    """workspace/agents/operator/ — Jarvis/Artemis private workspace."""
    d = workspace_dir() / "agents" / "operator"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Service endpoints ────────────────────────────────────────────────────

def local_llm_endpoint() -> str:
    """Return the local LLM server endpoint (e.g. llama.cpp).

    Override via REPRYNTT_LLM_ENDPOINT env var.
    Default: http://localhost:8080/v1/chat/completions
    """
    return os.environ.get(
        "REPRYNTT_LLM_ENDPOINT",
        "http://localhost:8080/v1/chat/completions",
    )


def local_llm_base() -> str:
    """Return the local LLM server base URL (without /v1/... path).

    Override via REPRYNTT_LLM_BASE env var.
    Default: http://localhost:8080
    """
    return os.environ.get("REPRYNTT_LLM_BASE", "http://localhost:8080")


def nexus_url() -> str:
    """Return the Nexus web server URL.

    Override via REPRYNTT_NEXUS_URL env var.
    Default: http://localhost:8089
    """
    return os.environ.get("REPRYNTT_NEXUS_URL", "http://localhost:8089")
