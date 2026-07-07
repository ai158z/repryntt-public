"""
Autonomy Scorecard — the entity's daily report card, from its own logs.

"What's your evaluation for autonomy?" — this is it. Parses the day's daemon +
evolution logs (including rotated archives) and scores the loop's INTEGRITY, not
vibes: is memory reaching decisions, are directives flowing, is evolution running,
are consequences landing, is the executive mind engaging, did anything crash.

Run:  python -m repryntt.core.evaluation.autonomy_scorecard [--date YYYY-MM-DD]
Output: ~/.repryntt/reports/autonomy_scorecard_<date>.json (+ printed report).
History accumulates so week-over-week curves (local-only vs executive-assisted)
can prove — or disprove — that the entity is getting more capable over time.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

LOG_DIR = Path.home() / ".repryntt" / "logs"
REPORT_DIR = Path.home() / ".repryntt" / "reports"

# (metric key, regex, groups→ints)
_PATTERNS = {
    "brain_context": re.compile(r"Brain Context Acquired: (\d+) memories, (\d+) tool suggestions"),
    "coordination": re.compile(r"Subsystem Coordination: (\d+) directives sent, (\d+) responses received"),
    "goals": re.compile(r"Autonomous Goals: (\d+) active, (\d+) new"),
    "oom_skip": re.compile(r"skipping evolution cycle"),
    "lite_cycle": re.compile(r"Lite cycle:"),
    "starved": re.compile(r"Evolution starved:"),
    "consequence": re.compile(r"🎯 CONSEQUENCE:"),
    "consolidated": re.compile(r"Consolidated (\d+) lived-consequence"),
    "executive": re.compile(r"EXECUTIVE mind engaged \(([^)]+)\)"),
    "burn": re.compile(r"Heartbeat burn: ([\d,]+) tokens"),
    "self_eval": re.compile(r"self-evaluated: score=(\d)/5"),
    "tool_ok": re.compile(r"✅ \[[^\]]+\] ([a-z_]+)\("),
    "queued_approval": re.compile(r"QUEUED for operator approval"),
    "claimed_not_done": re.compile(r"CLAIMED-BUT-NOT-DONE"),
    "crash": re.compile(r"GGML_ASSERT|CUDA error|Traceback \(most recent call last\)"),
    "training_run": re.compile(r"Cortex training (triggered|complete)"),
    "memories_loaded": re.compile(r"Loaded compartmentalized memories: (\d+) episodic, (\d+) semantic"),
    "vector_init": re.compile(r"Vector search initialized — (\d+) vectors"),
}


def _log_files_for(date: str) -> Iterable[Path]:
    """Live logs + any archives whose rotation stamp matches the date."""
    for name in ("agent-daemon.log", "evolution-loop.log", "nexus.log"):
        p = LOG_DIR / name
        if p.exists():
            yield p
    arch = LOG_DIR / "archive"
    if arch.exists():
        stamp = date.replace("-", "-")
        for p in arch.glob("*.log"):
            if date in p.name or stamp in p.name:
                yield p


def collect(date: str) -> Dict[str, Any]:
    m: Dict[str, Any] = {
        "date": date, "cycles": 0, "cycles_with_memories": 0, "memories_total": 0,
        "tool_suggestions_total": 0, "directives_sent": 0, "responses_received": 0,
        "coordination_samples": 0, "goals_new_total": 0, "goals_active_last": 0,
        "oom_skips": 0, "lite_cycles": 0, "starvation_alarms": 0,
        "consequence_ticks": 0, "consolidated_memories": 0,
        "executive_engagements": 0, "executive_purposes": {},
        "heartbeats": 0, "tokens_burned": 0,
        "self_eval_scores": [], "tool_calls_ok": 0, "distinct_tools": set(),
        "approvals_queued": 0, "claimed_not_done": 0, "crashes": 0,
        "training_events": 0, "memories_loaded_semantic": 0, "vectors_indexed": 0,
    }
    for path in _log_files_for(date):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for line in text.splitlines():
            r = _PATTERNS["brain_context"].search(line)
            if r:
                m["cycles"] += 1
                mem = int(r.group(1))
                m["memories_total"] += mem
                m["tool_suggestions_total"] += int(r.group(2))
                if mem > 0:
                    m["cycles_with_memories"] += 1
                continue
            r = _PATTERNS["coordination"].search(line)
            if r:
                m["coordination_samples"] += 1
                m["directives_sent"] += int(r.group(1))
                m["responses_received"] += int(r.group(2))
                continue
            r = _PATTERNS["goals"].search(line)
            if r:
                m["goals_active_last"] = int(r.group(1))
                m["goals_new_total"] += int(r.group(2))
                continue
            r = _PATTERNS["executive"].search(line)
            if r:
                m["executive_engagements"] += 1
                p = r.group(1)
                m["executive_purposes"][p] = m["executive_purposes"].get(p, 0) + 1
                continue
            r = _PATTERNS["burn"].search(line)
            if r:
                m["heartbeats"] += 1
                m["tokens_burned"] += int(r.group(1).replace(",", ""))
                continue
            r = _PATTERNS["self_eval"].search(line)
            if r:
                m["self_eval_scores"].append(int(r.group(1)))
                continue
            r = _PATTERNS["tool_ok"].search(line)
            if r:
                m["tool_calls_ok"] += 1
                m["distinct_tools"].add(r.group(1))
                continue
            r = _PATTERNS["consolidated"].search(line)
            if r:
                m["consolidated_memories"] += int(r.group(1))
                continue
            r = _PATTERNS["memories_loaded"].search(line)
            if r:
                m["memories_loaded_semantic"] = max(m["memories_loaded_semantic"], int(r.group(2)))
                continue
            r = _PATTERNS["vector_init"].search(line)
            if r:
                m["vectors_indexed"] = max(m["vectors_indexed"], int(r.group(1)))
                continue
            for key, name in (("oom_skip", "oom_skips"), ("lite_cycle", "lite_cycles"),
                              ("starved", "starvation_alarms"),
                              ("consequence", "consequence_ticks"),
                              ("queued_approval", "approvals_queued"),
                              ("claimed_not_done", "claimed_not_done"),
                              ("crash", "crashes"), ("training_run", "training_events")):
                if _PATTERNS[key].search(line):
                    m[name] += 1
                    break
    m["distinct_tools"] = sorted(m["distinct_tools"])
    return m


def grade(m: Dict[str, Any]) -> Dict[str, Any]:
    """0-5 per dimension — loop INTEGRITY grades, deliberately hard to please."""
    def clamp(x):
        return round(max(0.0, min(5.0, x)), 1)
    cycles = max(1, m["cycles"])
    g = {
        # memory reaching deliberation (the amnesia axis)
        "memory": clamp(5.0 * m["cycles_with_memories"] / cycles),
        # subsystems actually being driven
        "coordination": clamp(2.5 * (m["directives_sent"] / max(1, m["coordination_samples"]))),
        # evolution actually running (not OOM-starved)
        "growth": clamp(5.0 - 4.0 * (m["oom_skips"] / max(1, m["oom_skips"] + m["lite_cycles"] + cycles))
                        + (1.0 if m["training_events"] else 0.0)),
        # consequences flowing into the organism
        "consequence": clamp(1.5 * m["consequence_ticks"] + 1.0 * m["consolidated_memories"]),
        # judgment quality (self-eval average, harsh blend kept)
        "judgment": clamp(sum(m["self_eval_scores"]) / len(m["self_eval_scores"])
                          if m["self_eval_scores"] else 0.0),
        # honesty (claimed-but-not-done drags hard)
        "honesty": clamp(5.0 - 1.5 * m["claimed_not_done"]),
        # stability
        "stability": clamp(5.0 - 2.5 * m["crashes"]),
    }
    g["overall"] = round(sum(g.values()) / len(g), 2)
    return g


def run(date: str = "") -> Dict[str, Any]:
    date = date or datetime.now().strftime("%Y-%m-%d")
    metrics = collect(date)
    grades = grade(metrics)
    report = {"date": date, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "grades": grades, "metrics": metrics}
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"autonomy_scorecard_{date}.json"
    out.write_text(json.dumps(report, indent=1, default=str))
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")
    args = ap.parse_args()
    r = run(args.date)
    g, m = r["grades"], r["metrics"]
    print(f"\n🧠 AUTONOMY SCORECARD — {r['date']}")
    print("─" * 46)
    for k in ("memory", "coordination", "growth", "consequence",
              "judgment", "honesty", "stability"):
        bar = "█" * int(g[k]) + "░" * (5 - int(g[k]))
        print(f"  {k:<13} {bar}  {g[k]}/5")
    print("─" * 46)
    print(f"  OVERALL       {g['overall']}/5")
    print(f"\n  cycles={m['cycles']}  w/memories={m['cycles_with_memories']}  "
          f"directives={m['directives_sent']}  oom_skips={m['oom_skips']}  "
          f"lite={m['lite_cycles']}")
    print(f"  consequence_ticks={m['consequence_ticks']}  consolidated={m['consolidated_memories']}  "
          f"executive={m['executive_engagements']}  training={m['training_events']}")
    print(f"  tools_ok={m['tool_calls_ok']}  claimed_not_done={m['claimed_not_done']}  "
          f"crashes={m['crashes']}  tokens={m['tokens_burned']:,}")
    print(f"\n  saved → {REPORT_DIR / ('autonomy_scorecard_' + r['date'] + '.json')}\n")


if __name__ == "__main__":
    main()
