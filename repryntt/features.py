"""Feature flag helpers for optional Repryntt subsystems.

New installs should run the local AI stack without assuming the blockchain is
enabled. Operators can opt in through setup, config, or environment variables.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


DEFAULT_FEATURES: dict[str, Any] = {
    "blockchain_enabled": False,
}


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _falsey(value: str) -> bool:
    return value.strip().lower() in {"0", "false", "no", "n", "off"}


def feature_config_path(data_dir: Path | None = None) -> Path:
    if data_dir is None:
        from repryntt.paths import get_data_dir

        data_dir = get_data_dir()
    return data_dir / "config" / "features.json"


def load_features(data_dir: Path | None = None) -> dict[str, Any]:
    path = feature_config_path(data_dir)
    features = dict(DEFAULT_FEATURES)
    try:
        if path.exists():
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                features.update(data)
    except Exception:
        pass
    return features


def save_features(updates: dict[str, Any], data_dir: Path | None = None) -> dict[str, Any]:
    features = load_features(data_dir)
    features.update(updates)
    path = feature_config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(features, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)
    return features


def blockchain_enabled(data_dir: Path | None = None, default: bool = False) -> bool:
    """Return whether the Rust blockchain should participate in normal startup.

    Precedence:
      1. REPRYNTT_BLOCKCHAIN_ENABLED / REPRYNTT_ENABLE_BLOCKCHAIN
      2. config/features.json
      3. provided default, false for new installs
    """
    for key in ("REPRYNTT_BLOCKCHAIN_ENABLED", "REPRYNTT_ENABLE_BLOCKCHAIN"):
        raw = os.environ.get(key, "").strip()
        if raw:
            if _truthy(raw):
                return True
            if _falsey(raw):
                return False
    features = load_features(data_dir)
    return bool(features.get("blockchain_enabled", default))
