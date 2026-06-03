"""
repryntt.tools.scrub_daily_memory — Section-level vocabulary scrubber for
daily-memory / PULSE / RECALL files.

The intake_gate write-side blocklist stops NEW writes containing the
operator's blocked vocabulary, but historical files (daily memory from
prior days, current PULSE.md, cortex_reflections.jsonl) still carry the
vocabulary. Andrew's bootstrap loader pulls these into his heartbeat
context every cycle — so even with the write-side guard active, he sees
the vocabulary in his prompt and re-emits it.

This tool walks the historical files and drops any section that saturates
the blocklist (≥2 distinct hits in title + body), preserving the
operationally clean sections intact. Same threshold as
append_daily_memory / update_bootstrap_file.

Daily memory format (one file per day under
~/.repryntt/workspace/agents/operator/memory/2026-MM-DD.md): sections
delimited by leading-`##` headings. A section is the heading line + every
line up to the next `##`-headed line.

Run:
    python -m repryntt.tools.scrub_daily_memory                 # dry-run
    python -m repryntt.tools.scrub_daily_memory --apply         # write
    python -m repryntt.tools.scrub_daily_memory --apply --days 14
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Threshold for retrospective scrubbing. Stricter than the write-time guard
# (which uses 2) because here we're cleaning *context-feed* files — any
# blocked vocabulary in daily memory / reflections gets loaded into Andrew's
# prompt every cycle and biases his output. The write-time guard tolerates
# a single passing mention; the retrospective scrub does not.
# Override at the CLI: --threshold 2 for the lenient mode.
THRESHOLD = 1

# Marker we drop in place of removed sections so the operator can see this
# file was scrubbed (and timestamps stay roughly aligned).
SCRUB_MARKER = (
    "## [scrubbed]\n"
    "_Section removed by scrub_daily_memory: vocabulary blocklist saturation_\n"
)


def _workspace() -> Path:
    return Path.home() / ".repryntt" / "workspace"


def _brain_dir() -> Path:
    return Path.home() / ".repryntt" / "brain"


# ── Hit counting (uses operator blocklist) ────────────────────────────


def _vocab_hits(text: str) -> List[str]:
    try:
        from repryntt.agents.intake_gate import blocklist_hits
        return blocklist_hits(text or "")
    except Exception:
        return []


def _is_saturated(text: str, threshold: int = THRESHOLD) -> Tuple[bool, List[str]]:
    hits = _vocab_hits(text)
    distinct = sorted(set(hits))
    return (len(distinct) >= threshold), distinct


# ── Section splitter ─────────────────────────────────────────────────


@dataclass
class Section:
    heading: str            # the "## ..." line (or "" for preamble before any heading)
    body_lines: List[str] = field(default_factory=list)

    def text(self) -> str:
        return self.heading + ("\n" if self.heading else "") + "\n".join(self.body_lines)

    def joined(self) -> str:
        # Reassemble for output. Trailing newline preserved by caller.
        if self.heading:
            return self.heading + "\n" + "\n".join(self.body_lines)
        return "\n".join(self.body_lines)


def _split_sections(content: str) -> List[Section]:
    sections: List[Section] = []
    cur = Section(heading="")
    for line in content.splitlines():
        if line.startswith("## "):
            if cur.heading or cur.body_lines:
                sections.append(cur)
            cur = Section(heading=line)
        else:
            cur.body_lines.append(line)
    if cur.heading or cur.body_lines:
        sections.append(cur)
    return sections


def _scrub_markdown_file(path: Path, apply: bool, report: List[Dict]) -> int:
    """Scrub one .md file. Returns count of sections removed."""
    if not path.exists():
        return 0
    try:
        original = path.read_text(encoding="utf-8")
    except Exception as e:
        report.append({"file": str(path), "error": f"read failed: {e}"})
        return 0
    sections = _split_sections(original)
    kept: List[str] = []
    removed = 0
    for sec in sections:
        text = sec.joined()
        sat, hits = _is_saturated(text)
        if sat:
            removed += 1
            report.append({
                "file": str(path),
                "heading": sec.heading[:100],
                "hits": hits,
                "section_chars": len(text),
            })
            kept.append(SCRUB_MARKER)
        else:
            kept.append(sec.joined())
    if removed and apply:
        stamp = int(time.time())
        shutil.copy2(path, path.with_suffix(path.suffix + f".bak_{stamp}"))
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return removed


def _scrub_jsonl_file(path: Path, apply: bool, report: List[Dict],
                     text_fields: Tuple[str, ...] = ("content", "text", "reflection", "summary", "thought")) -> int:
    """Drop saturated JSONL records. Returns count removed."""
    if not path.exists():
        return 0
    removed = 0
    kept_lines: List[str] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except Exception as e:
        report.append({"file": str(path), "error": f"read failed: {e}"})
        return 0
    for line in lines:
        line_strip = line.strip()
        if not line_strip:
            kept_lines.append(line)
            continue
        try:
            rec = json.loads(line_strip)
        except Exception:
            # Not parseable — leave it
            kept_lines.append(line)
            continue
        if isinstance(rec, dict):
            blob = " ".join(str(rec.get(f, "")) for f in text_fields)
            sat, hits = _is_saturated(blob)
            if sat:
                removed += 1
                report.append({
                    "file": str(path),
                    "ts": rec.get("ts") or rec.get("timestamp") or "?",
                    "hits": hits,
                })
                continue
        kept_lines.append(line)
    if removed and apply:
        stamp = int(time.time())
        shutil.copy2(path, path.with_suffix(path.suffix + f".bak_{stamp}"))
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(kept_lines)
    return removed


# ── Driver ───────────────────────────────────────────────────────────


def scrub_all(days: int = 14, apply: bool = False) -> Dict[str, Any]:
    """Scrub the last `days` daily-memory files + PULSE + cortex_reflections."""
    report: List[Dict] = []
    total = 0
    today = date.today()

    # 1. Daily memory
    mem_dir = _workspace() / "agents" / "operator" / "memory"
    for n in range(days + 1):
        d = today - timedelta(days=n)
        f = mem_dir / f"{d.isoformat()}.md"
        if f.exists():
            r = _scrub_markdown_file(f, apply, report)
            if r:
                logger.info(f"  {f.name}: removed {r} section(s)")
            total += r

    # 2. PULSE.md (the small lingering hits)
    bootstrap_dir = _brain_dir() / "bootstrap"
    for name in ("PULSE.md", "HEARTBEAT.md", "RECALL.md"):
        f = bootstrap_dir / name
        if f.exists():
            r = _scrub_markdown_file(f, apply, report)
            if r:
                logger.info(f"  bootstrap/{name}: removed {r} section(s)")
            total += r

    # 3. cortex_reflections.jsonl — recent contamination Andrew sees as
    # "recent reflections" context.
    cr = _brain_dir() / "cortex_reflections.jsonl"
    if cr.exists():
        r = _scrub_jsonl_file(cr, apply, report)
        if r:
            logger.info(f"  cortex_reflections.jsonl: removed {r} record(s)")
        total += r

    return {"removed_total": total, "applied": apply,
            "files_scrubbed": len({r['file'] for r in report if 'file' in r}),
            "report": report}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually write changes (default: dry-run report only)")
    ap.add_argument("--days", type=int, default=14,
                    help="How many days of daily-memory to scrub (default 14)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = scrub_all(days=args.days, apply=args.apply)
    print(json.dumps({
        "applied": result["applied"],
        "removed_total": result["removed_total"],
        "files_scrubbed": result["files_scrubbed"],
        "sample_report": result["report"][:8],
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
