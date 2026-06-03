"""Pin the May-2026 quarantine bug: driver_trainer.load_experience must drop
poisoned rows so the MLP doesn't collapse to "always say forward" again.
"""

from __future__ import annotations

import json
from pathlib import Path

from repryntt.hardware.driver_trainer import load_experience


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def test_load_experience_drops_poisoned_rows(tmp_path: Path) -> None:
    clean = {
        "ts": 1.0,
        "decision": "turn_right",
        "confidence": 0.9,
        "executed": True,
        "perception_failed": False,
        "scene": "open hallway",
        "stereo_left": 0.7,
        "stereo_center": 0.6,
        "stereo_right": 0.5,
    }
    perception_failed_row = {**clean, "ts": 2.0, "perception_failed": True}
    not_executed_row = {**clean, "ts": 3.0, "executed": False}
    legacy_failed_scene = {
        **clean,
        "ts": 4.0,
        "scene": "perception failed — acting conservatively",
    }
    low_conf = {**clean, "ts": 5.0, "confidence": 0.1}
    bad_action = {**clean, "ts": 6.0, "decision": "moonwalk"}

    _write_jsonl(
        tmp_path / "2026-05-10.jsonl",
        [
            clean,
            perception_failed_row,
            not_executed_row,
            legacy_failed_scene,
            low_conf,
            bad_action,
        ],
    )

    entries = load_experience(data_dir=str(tmp_path))

    # Only the clean row survives.
    assert len(entries) == 1
    assert entries[0]["ts"] == 1.0
    assert entries[0]["decision"] == "turn_right"


def test_load_experience_keeps_legacy_rows_without_executed_field(
    tmp_path: Path,
) -> None:
    """Legacy rows from before the executed-bug fix simply lack the field.
    They should be kept (we don't have evidence they were broken)."""
    legacy = {
        "ts": 10.0,
        "decision": "forward",
        "confidence": 0.7,
        "scene": "clear path",
        "stereo_left": 0.8,
        "stereo_center": 0.8,
        "stereo_right": 0.8,
    }
    _write_jsonl(tmp_path / "2026-04-30.jsonl", [legacy])

    entries = load_experience(data_dir=str(tmp_path))

    assert len(entries) == 1
    assert "executed" not in entries[0]


def test_load_experience_returns_empty_on_missing_dir(tmp_path: Path) -> None:
    entries = load_experience(data_dir=str(tmp_path / "nope"))
    assert entries == []
