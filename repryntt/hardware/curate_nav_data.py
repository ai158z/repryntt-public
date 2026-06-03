"""repryntt.hardware.curate_nav_data — Curate nav_experience JSONL for retraining.

Takes raw JSONL logs (production + quarantined + teleop demos), drops the
poisoned rows, balances the action distribution, splits into train/val,
and writes a structured report.

Phase 3 of the May-2026 bringup plan. Built so the Plan agent's "salvage
report" question is answered empirically: how many rows from the
quarantined batch survive the filters? (Probably zero — _fallback_perception
fired on every entry — but the curator says so explicitly.)

Usage:
    python -m repryntt.hardware.curate_nav_data \\
        --in ~/.repryntt/data/nav_experience \\
        --in ~/.repryntt/data/nav_experience.quarantined_2026-05-08 \\
        --in ~/.repryntt/data/teleop_demos \\
        --out ~/.repryntt/data/nav_experience_curated

Default --in is the production dir + the May-2026 quarantine + teleop demos.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ACTIONS = ("forward", "backward", "turn_left", "turn_right", "stop")
DEFAULT_OUT = Path.home() / ".repryntt" / "data" / "nav_experience_curated"
DEFAULT_INPUTS = [
    Path.home() / ".repryntt" / "data" / "nav_experience",
    Path.home() / ".repryntt" / "data" / "nav_experience.quarantined_2026-05-08",
    Path.home() / ".repryntt" / "data" / "teleop_demos",
]


@dataclass
class CurationReport:
    inputs: List[str]
    output: str
    raw_rows: int = 0
    kept_rows: int = 0
    train_rows: int = 0
    val_rows: int = 0
    drops: Dict[str, int] = field(default_factory=lambda: {
        "no_decision_or_unknown_action": 0,
        "perception_failed_flag": 0,
        "executed_false": 0,
        "legacy_failed_scene": 0,
        "low_confidence": 0,
        "json_decode_error": 0,
    })
    per_input_raw: Dict[str, int] = field(default_factory=dict)
    per_input_kept: Dict[str, int] = field(default_factory=dict)
    class_counts_pre_balance: Dict[str, int] = field(default_factory=dict)
    class_counts_post_balance: Dict[str, int] = field(default_factory=dict)
    val_split: float = 0.15
    seed: int = 42


def _classify_drop(entry: Dict[str, Any], min_confidence: float) -> Optional[str]:
    """Return the drop-reason key, or None if the entry should be kept."""
    decision = entry.get("decision", "")
    if decision not in ACTIONS:
        return "no_decision_or_unknown_action"
    if entry.get("perception_failed"):
        return "perception_failed_flag"
    if entry.get("executed") is False:
        return "executed_false"
    scene = entry.get("scene", "") or ""
    if scene.startswith("perception failed"):
        return "legacy_failed_scene"
    if entry.get("confidence", 0) < min_confidence:
        return "low_confidence"
    return None


def _load_jsonl(path: Path) -> Tuple[List[Dict[str, Any]], int]:
    """Load JSONL rows, returning (rows, decode_errors)."""
    rows: List[Dict[str, Any]] = []
    errors = 0
    if not path.exists():
        return rows, errors
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                errors += 1
    return rows, errors


def _balance_classes(
    kept_by_class: Dict[str, List[Dict[str, Any]]],
    rng: random.Random,
    max_ratio: float = 2.0,
) -> List[Dict[str, Any]]:
    """Cap each class at max_ratio × the smallest non-empty class."""
    nonzero = {k: v for k, v in kept_by_class.items() if v}
    if not nonzero:
        return []
    floor = min(len(v) for v in nonzero.values())
    cap = max(1, int(floor * max_ratio))
    out: List[Dict[str, Any]] = []
    for action, rows in kept_by_class.items():
        if not rows:
            continue
        if len(rows) > cap:
            out.extend(rng.sample(rows, cap))
        else:
            out.extend(rows)
    rng.shuffle(out)
    return out


def curate(
    in_dirs: List[Path],
    out_dir: Path = DEFAULT_OUT,
    min_confidence: float = 0.3,
    val_split: float = 0.15,
    max_class_ratio: float = 2.0,
    seed: int = 42,
) -> CurationReport:
    rng = random.Random(seed)
    report = CurationReport(
        inputs=[str(p) for p in in_dirs],
        output=str(out_dir),
        val_split=val_split,
        seed=seed,
    )

    kept_by_class: Dict[str, List[Dict[str, Any]]] = {a: [] for a in ACTIONS}

    for in_dir in in_dirs:
        in_kept = 0
        in_raw = 0
        if not in_dir.exists():
            logger.warning(f"input does not exist (skipping): {in_dir}")
            report.per_input_raw[str(in_dir)] = 0
            report.per_input_kept[str(in_dir)] = 0
            continue
        for jsonl_file in sorted(in_dir.glob("*.jsonl")):
            if jsonl_file.name.startswith("pipeline"):
                continue
            rows, decode_errors = _load_jsonl(jsonl_file)
            in_raw += len(rows)
            report.drops["json_decode_error"] += decode_errors
            for entry in rows:
                drop_reason = _classify_drop(entry, min_confidence)
                if drop_reason:
                    report.drops[drop_reason] += 1
                    continue
                kept_by_class[entry["decision"]].append(entry)
                in_kept += 1
        report.per_input_raw[str(in_dir)] = in_raw
        report.per_input_kept[str(in_dir)] = in_kept
        report.raw_rows += in_raw

    report.class_counts_pre_balance = {a: len(rows) for a, rows in kept_by_class.items()}
    balanced = _balance_classes(kept_by_class, rng, max_ratio=max_class_ratio)
    report.kept_rows = len(balanced)
    report.class_counts_post_balance = dict(Counter(r["decision"] for r in balanced))

    # Train/val split
    val_n = int(len(balanced) * val_split)
    val_rows = balanced[:val_n]
    train_rows = balanced[val_n:]
    report.train_rows = len(train_rows)
    report.val_rows = len(val_rows)

    out_dir.mkdir(parents=True, exist_ok=True)
    train_path = out_dir / "train.jsonl"
    val_path = out_dir / "val.jsonl"
    report_path = out_dir / "report.json"

    with train_path.open("w") as f:
        for r in train_rows:
            f.write(json.dumps(r) + "\n")
    with val_path.open("w") as f:
        for r in val_rows:
            f.write(json.dumps(r) + "\n")
    with report_path.open("w") as f:
        json.dump({**asdict(report), "ts": time.time()}, f, indent=2)

    return report


def _print_report(report: CurationReport) -> None:
    print("\n=== Curation report ===")
    for in_dir in report.inputs:
        raw = report.per_input_raw.get(in_dir, 0)
        kept = report.per_input_kept.get(in_dir, 0)
        marker = "(MISSING)" if raw == 0 and in_dir not in report.per_input_raw else ""
        print(f"  in: {in_dir}  raw={raw}  kept={kept} {marker}")
    print(f"\n  raw rows total:       {report.raw_rows}")
    print(f"  kept after balance:   {report.kept_rows}")
    print(f"  train / val:          {report.train_rows} / {report.val_rows}")
    print("\n  drops by reason:")
    for k, v in sorted(report.drops.items(), key=lambda kv: -kv[1]):
        print(f"    {k:30s} {v}")
    print("\n  class counts (pre-balance):")
    for a in ACTIONS:
        print(f"    {a:12s} {report.class_counts_pre_balance.get(a, 0)}")
    print("\n  class counts (post-balance):")
    for a in ACTIONS:
        print(f"    {a:12s} {report.class_counts_post_balance.get(a, 0)}")
    print(f"\n  output: {report.output}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Curate nav_experience JSONL.")
    ap.add_argument("--in", dest="inputs", action="append", type=Path,
                    help="Input directory (repeatable). Default: production + quarantine + teleop_demos.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"Output directory (default: {DEFAULT_OUT})")
    ap.add_argument("--min-confidence", type=float, default=0.3)
    ap.add_argument("--val-split", type=float, default=0.15)
    ap.add_argument("--max-class-ratio", type=float, default=2.0,
                    help="Cap each class at this multiple of the smallest non-empty class")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    in_dirs = args.inputs or DEFAULT_INPUTS
    report = curate(
        in_dirs=in_dirs,
        out_dir=args.out,
        min_confidence=args.min_confidence,
        val_split=args.val_split,
        max_class_ratio=args.max_class_ratio,
        seed=args.seed,
    )
    _print_report(report)
    return 0 if report.kept_rows > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
