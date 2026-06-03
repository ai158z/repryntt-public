import json
from pathlib import Path

import pytest

from scripts.seed_autonomy_long_run_test import seed_long_run_test


def test_long_run_seed_dry_run_does_not_write(tmp_path: Path):
    result = seed_long_run_test(
        tmp_path,
        apply=False,
        with_chain=True,
        episodes=4,
        target_chars=12000,
    )

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert "AUTONOMY LONG-RUN STRESS TEST" in result["task"]["title"]
    assert result["chain"]["success_criteria"].startswith("A final mini-series")
    assert not (tmp_path / "task_queue.json").exists()
    assert not (tmp_path / "reasoning_chain.json").exists()


def test_long_run_seed_writes_operator_task_and_chain(tmp_path: Path):
    result = seed_long_run_test(
        tmp_path,
        apply=True,
        with_chain=True,
        episodes=6,
        target_chars=30000,
        target_steps=18,
    )

    assert result["ok"] is True
    assert result["dry_run"] is False
    assert result["task"]["priority"] == 0
    assert result["task"]["source"] == "operator"

    queue_data = json.loads((tmp_path / "task_queue.json").read_text())
    assert queue_data["tasks"][0]["priority"] == 0
    assert queue_data["tasks"][0]["status"] == "queued"
    assert "30000 characters" in queue_data["tasks"][0]["description"]

    chain_data = json.loads((tmp_path / "reasoning_chain.json").read_text())
    assert chain_data["goal_type"] == "locked"
    assert chain_data["source"] == "operator_long_run_test"
    assert chain_data["target_steps"] == 18
    assert "30000 characters" in chain_data["success_criteria"]
    assert "17-18" in chain_data["phase_guide"]


def test_long_run_seed_refuses_to_replace_active_chain(tmp_path: Path):
    (tmp_path / "reasoning_chain.json").write_text(json.dumps({"status": "active"}))

    with pytest.raises(FileExistsError):
        seed_long_run_test(tmp_path, apply=True, with_chain=True)

