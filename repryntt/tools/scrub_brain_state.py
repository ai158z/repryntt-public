"""
repryntt.tools.scrub_brain_state — One-shot scrubber for brain-state files.

The deep dive found that ava_brain.json carries a Pattern7-themed active
chain from before the cleanup window. Every heartbeat that reads brain state
inherits that goal-vector, partially anchoring the goal-coherence axis at 5/10.

This tool walks the brain-state files (ava_brain.json,
jarvis_consciousness*, cot_queue.json, ai_chain_queue.json, and the
per-chain files under brain/chains/) and removes or de-activates any entry
whose topic/goal saturates the operator-configured blocklist
(`~/.repryntt/brain/intake_blocklist.json`).

It is idempotent: ships as a no-op when the operator's blocklist is empty
(the default for open-source users).

Run:
    python -m repryntt.tools.scrub_brain_state            # report only
    python -m repryntt.tools.scrub_brain_state --apply    # actually write
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


BRAIN_DIR = Path(os.environ.get("REPRYNTT_BRAIN_DIR",
                                Path.home() / ".repryntt" / "brain"))

# Heuristic: an entry is saturated when it hits >=THRESHOLD distinct blocked
# terms in its topic+goal. Matches append_daily_memory / update_bootstrap_file
# so the scrubber and the write guards agree.
THRESHOLD = 2


def _vocab_hits(text: str) -> List[str]:
    try:
        from repryntt.agents.intake_gate import blocklist_hits
        return blocklist_hits(text or "")
    except Exception:
        return []


def _is_saturated(*texts: str) -> Tuple[bool, List[str]]:
    blob = "\n".join(t or "" for t in texts)
    hits = _vocab_hits(blob)
    distinct = sorted(set(hits))
    return (len(distinct) >= THRESHOLD), distinct


def _backup(path: Path) -> Path:
    if not path.exists():
        return path
    stamp = int(time.time())
    backup = path.with_suffix(path.suffix + f".bak_{stamp}")
    shutil.copy2(path, backup)
    return backup


# ─── Per-file scrubbers ───────────────────────────────────────────────


def scrub_ava_brain(path: Path, apply: bool, report: List[Dict]) -> int:
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        report.append({"file": str(path), "error": f"unreadable: {e}"})
        return 0
    chains = data.get("active_chains_of_thought", []) or []
    if not chains:
        return 0
    kept: List[Dict] = []
    removed = 0
    for c in chains:
        sat, hits = _is_saturated(c.get("topic", ""), c.get("goal", ""))
        if sat:
            removed += 1
            report.append({
                "file": str(path),
                "removed_chain_id": c.get("chain_id"),
                "topic": (c.get("topic", "") or "")[:120],
                "hits": hits,
            })
        else:
            kept.append(c)
    if removed and apply:
        _backup(path)
        data["active_chains_of_thought"] = kept
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return removed


def scrub_chains_dir(chains_dir: Path, apply: bool, report: List[Dict]) -> int:
    if not chains_dir.is_dir():
        return 0
    removed = 0
    for p in chains_dir.glob("chain_*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            report.append({"file": str(p), "error": f"unreadable: {e}"})
            continue
        sat, hits = _is_saturated(
            data.get("topic", ""),
            data.get("goal", ""),
            data.get("description", ""),
        )
        if sat:
            removed += 1
            report.append({
                "file": str(p),
                "action": "marked-closed" if apply else "would-mark-closed",
                "topic": (data.get("topic", "") or "")[:120],
                "hits": hits,
            })
            if apply:
                _backup(p)
                data["status"] = "closed"
                data["closed_reason"] = "scrub_brain_state: vocabulary saturation"
                data["closed_at"] = time.time()
                p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return removed


def scrub_chain_queues(brain_dir: Path, apply: bool, report: List[Dict]) -> int:
    """ai_chain_queue.json / cot_queue.json — pending chain requests."""
    removed = 0
    for name in ("ai_chain_queue.json", "cot_queue.json"):
        path = brain_dir / name
        if not path.exists():
            continue
        try:
            raw = path.read_text(encoding="utf-8").strip()
            if not raw or raw == "{}" or raw == "[]":
                continue
            data = json.loads(raw)
        except Exception as e:
            report.append({"file": str(path), "error": f"unreadable: {e}"})
            continue
        if not isinstance(data, list):
            # Wrap-shaped JSON — try common keys
            for key in ("queue", "items", "chains"):
                if isinstance(data, dict) and isinstance(data.get(key), list):
                    items = data[key]
                    kept: List[Dict] = []
                    for it in items:
                        sat, hits = _is_saturated(
                            it.get("topic", ""), it.get("goal", ""),
                            it.get("description", ""),
                        )
                        if sat:
                            removed += 1
                            report.append({
                                "file": str(path), "key": key,
                                "topic": (it.get("topic", "") or "")[:120],
                                "hits": hits,
                            })
                        else:
                            kept.append(it)
                    data[key] = kept
                    break
            else:
                continue
        else:
            kept = []
            for it in data:
                if not isinstance(it, dict):
                    kept.append(it)
                    continue
                sat, hits = _is_saturated(
                    it.get("topic", ""), it.get("goal", ""),
                    it.get("description", ""),
                )
                if sat:
                    removed += 1
                    report.append({
                        "file": str(path),
                        "topic": (it.get("topic", "") or "")[:120],
                        "hits": hits,
                    })
                else:
                    kept.append(it)
            data = kept
        if removed and apply:
            _backup(path)
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return removed


def scrub_consciousness(brain_dir: Path, apply: bool, report: List[Dict]) -> int:
    """jarvis_consciousness.json / consciousness_state.json — recent experiences."""
    removed = 0
    for name in ("jarvis_consciousness.json", "consciousness_state.json"):
        path = brain_dir / name
        if not path.exists():
            continue
        try:
            raw = path.read_text(encoding="utf-8").strip()
            if not raw or raw == "{}" or raw == "[]":
                continue
            data = json.loads(raw)
        except Exception as e:
            report.append({"file": str(path), "error": f"unreadable: {e}"})
            continue
        if not isinstance(data, dict):
            continue
        changed = False
        for key in ("recent_experiences", "experiences", "active_goals"):
            arr = data.get(key)
            if not isinstance(arr, list):
                continue
            kept = []
            for it in arr:
                if not isinstance(it, dict):
                    kept.append(it)
                    continue
                sat, hits = _is_saturated(
                    it.get("topic", ""), it.get("goal", ""),
                    it.get("summary", ""), it.get("description", ""),
                )
                if sat:
                    removed += 1
                    changed = True
                    report.append({
                        "file": str(path), "key": key,
                        "topic": (it.get("topic") or it.get("goal") or "")[:120],
                        "hits": hits,
                    })
                else:
                    kept.append(it)
            data[key] = kept
        if changed and apply:
            _backup(path)
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return removed


# ─── Driver ────────────────────────────────────────────────────────────


def scrub_all(brain_dir: Path = BRAIN_DIR, apply: bool = False) -> Dict[str, Any]:
    """Returns {removed: int, report: [...], applied: bool}."""
    report: List[Dict] = []
    total = 0
    total += scrub_ava_brain(brain_dir / "ava_brain.json", apply, report)
    total += scrub_chains_dir(brain_dir / "chains", apply, report)
    total += scrub_chain_queues(brain_dir, apply, report)
    total += scrub_consciousness(brain_dir, apply, report)
    return {"removed": total, "report": report, "applied": apply,
            "brain_dir": str(brain_dir)}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually write changes (default: dry-run report only)")
    ap.add_argument("--brain-dir", default=str(BRAIN_DIR),
                    help="Brain directory (defaults to REPRYNTT_BRAIN_DIR or ~/.repryntt/brain)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = scrub_all(Path(args.brain_dir), apply=args.apply)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
