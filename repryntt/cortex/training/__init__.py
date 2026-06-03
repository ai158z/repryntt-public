"""Per-region training pipeline — routes data and orchestrates LoRA training."""

import json
import logging
from pathlib import Path
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

_LEGACY_MIGRATED = False


def migrate_legacy_training_data() -> int:
    """One-time migration of legacy training_data.json into cortex DataRouter.

    Reads ~/.repryntt/data/training_data.json (786+ legacy examples from
    earlier evolution system), filters for quality, and routes through
    DataRouter into per-region datasets.

    Returns number of examples migrated. Safe to call multiple times
    (writes a marker file after first migration).
    """
    global _LEGACY_MIGRATED
    if _LEGACY_MIGRATED:
        return 0

    from repryntt.paths import data_dir
    legacy_path = data_dir() / "training_data.json"
    marker_path = data_dir() / "cortex_training" / ".legacy_migrated"

    if marker_path.exists():
        _LEGACY_MIGRATED = True
        return 0

    if not legacy_path.exists():
        _LEGACY_MIGRATED = True
        return 0

    try:
        legacy_data: List[Dict[str, Any]] = json.loads(legacy_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not read legacy training data: %s", e)
        _LEGACY_MIGRATED = True
        return 0

    from repryntt.cortex.training.data_router import get_data_router
    router = get_data_router()

    count = 0
    for ex in legacy_data:
        prompt = ex.get("prompt", "")
        response = ex.get("response", "")
        if not prompt or not response or len(response) < 20:
            continue

        routed = router.route({
            "region": "conscious",
            "type": ex.get("type", "legacy_migration"),
            "prompt": prompt,
            "response": response,
            "quality": 4,  # Legacy data is curated enough
            "timestamp": ex.get("timestamp", ""),
        })
        if routed:
            count += 1

    # Write marker so we don't re-migrate
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(f"Migrated {count} examples from training_data.json")

    _LEGACY_MIGRATED = True
    logger.info("Migrated %d legacy training examples into cortex DataRouter", count)
    return count
