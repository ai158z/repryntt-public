"""
Orchestrator — Andrew's read-only supervisor.

Watches recent heartbeat activity, content output, and social posts. Computes
a snapshot of how Andrew is doing and writes "Director Briefs" to the social
network that Andrew reads at the start of each heartbeat.

This is read-only by design: it does not block, override, or inject tasks.
It posts advisory messages on /social. Andrew can disagree and ignore them.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("repryntt.agents.orchestrator")

DAEMON_LOG = Path.home() / ".repryntt" / "logs" / "agent-daemon.log"
CONTENT_BASE = Path.home() / ".repryntt" / "workspace" / "agents" / "operator" / "content"

CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".cpp", ".c", ".java"}

SCORE_RE = re.compile(r"Jarvis self-evaluated: score=(\d)/5")
TOOL_RE = re.compile(r"\[JARVIS\] (\w+)\(\)")
TIME_RE = re.compile(r"^(\d{2}:\d{2}:\d{2})\s")


def _parse_log_time(line: str, today: datetime) -> Optional[datetime]:
    m = TIME_RE.match(line)
    if not m:
        return None
    try:
        h, mi, s = (int(x) for x in m.group(1).split(":"))
        return today.replace(hour=h, minute=mi, second=s, microsecond=0)
    except ValueError:
        return None


class Orchestrator:
    """Snapshot Andrew's recent state and post advisory briefs."""

    def __init__(self, log_path: Path = DAEMON_LOG, content_base: Path = CONTENT_BASE):
        self.log_path = log_path
        self.content_base = content_base

    # ── Snapshot ─────────────────────────────────────────────────────────

    def snapshot(self, hours: int = 6) -> Dict[str, Any]:
        """Compute a state snapshot covering the last `hours`."""
        now = datetime.now()
        cutoff = now - timedelta(hours=hours)

        scores, tools = self._scan_log(cutoff)
        artifacts, code_files = self._scan_today_content()
        council_active, council_threads_today = self._scan_council()

        codeforge_calls = sum(1 for t in tools if t == "forge_project")
        raw_write_code = len(code_files)
        total_code_actions = codeforge_calls + raw_write_code
        bypass_pct = (raw_write_code / total_code_actions) if total_code_actions else 0.0

        idle_loop = self._detect_idle_loop(tools)
        score_avg = (sum(scores) / len(scores)) if scores else 0.0
        trend = self._score_trend(scores)
        verdict = self._verdict(score_avg, bypass_pct, idle_loop)

        return {
            "as_of": now.isoformat(timespec="seconds"),
            "window_hours": hours,
            "heartbeats_seen": len(scores),
            "score_avg": round(score_avg, 2),
            "score_trend": trend,
            "score_history": scores[-20:],
            "tool_calls_total": len(tools),
            "top_tools": Counter(tools).most_common(5),
            "codeforge_calls": codeforge_calls,
            "raw_write_code_files": raw_write_code,
            "codeforge_bypass_pct": round(bypass_pct, 2),
            "idle_loop_detected": idle_loop,
            "today_artifacts": artifacts[:30],
            "today_code_files": code_files[:30],
            "council_active": council_active,
            "council_threads_today": council_threads_today,
            "verdict": verdict,
        }

    # ── Director Brief ───────────────────────────────────────────────────

    def write_director_brief(self, snap: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Compose a brief from the snapshot and post it to the social feed.

        Returns the post_id of the new brief, or None if posting failed.
        """
        snap = snap or self.snapshot()
        body = self._compose_brief(snap)
        try:
            from repryntt.social import store
            post = store.create_post(
                agent_name="REPRYNTT-DIRECTOR",
                content=body,
                category="general",
            )
            pid = post.get("post_id")
            if pid == "REJECTED":
                log.info("Director Brief rejected by social store as duplicate")
                return None
            log.info(f"Director Brief posted: {pid}")
            return pid
        except Exception as e:
            log.warning(f"Director Brief post failed: {e}")
            return None

    def latest_brief(self, max_age_minutes: int = 60) -> Optional[Dict[str, Any]]:
        """Return the latest Director Brief if it exists and is fresh."""
        try:
            from repryntt.social import store
            feed = store.get_feed(limit=40)
        except Exception:
            return None
        for post in feed:
            if post.get("agent_name") != "REPRYNTT-DIRECTOR":
                continue
            created = post.get("created_at", "")
            try:
                ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
            except Exception:
                age_min = max_age_minutes + 1
            if age_min > max_age_minutes:
                return None
            return {
                "post_id": post.get("post_id"),
                "summary": post.get("content", ""),
                "age_minutes": int(age_min),
            }
        return None

    # ── Internals ────────────────────────────────────────────────────────

    def _scan_log(self, cutoff: datetime) -> tuple[List[int], List[str]]:
        if not self.log_path.exists():
            return [], []
        scores: List[int] = []
        tools: List[str] = []
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            with self.log_path.open("r", errors="ignore") as f:
                lines = f.readlines()[-15000:]
        except OSError:
            return [], []
        for line in lines:
            t = _parse_log_time(line, today)
            if t and t < cutoff:
                continue
            sm = SCORE_RE.search(line)
            if sm:
                scores.append(int(sm.group(1)))
                continue
            tm = TOOL_RE.search(line)
            if tm:
                tools.append(tm.group(1))
        return scores, tools

    def _scan_today_content(self) -> tuple[List[str], List[str]]:
        today = datetime.now().strftime("%Y-%m-%d")
        d = self.content_base / today
        if not d.exists():
            return [], []
        artifacts: List[str] = []
        code_files: List[str] = []
        for p in sorted(d.iterdir()):
            if p.is_file():
                artifacts.append(p.name)
                if p.suffix in CODE_EXT:
                    code_files.append(p.name)
        return artifacts, code_files

    def _scan_council(self) -> tuple[bool, int]:
        try:
            from repryntt.social import store
            feed = store.get_feed(limit=50, category="knowledge")
        except Exception:
            return False, 0
        today = datetime.now().strftime("%Y-%m-%d")
        threads = 0
        for post in feed:
            if post.get("agent_name") != "REPRYNTT-COMMANDER":
                continue
            if today in post.get("created_at", ""):
                threads += 1
        return threads > 0, threads

    @staticmethod
    def _detect_idle_loop(tools: List[str]) -> bool:
        if len(tools) < 5:
            return False
        for i in range(len(tools) - 4):
            if len(set(tools[i:i + 5])) == 1:
                return True
        return False

    @staticmethod
    def _score_trend(scores: List[int]) -> str:
        if len(scores) < 4:
            return "insufficient-data"
        first = sum(scores[: len(scores) // 2]) / max(1, len(scores) // 2)
        last = sum(scores[len(scores) // 2:]) / max(1, len(scores) - len(scores) // 2)
        if last - first > 0.4:
            return "improving"
        if first - last > 0.4:
            return "declining"
        return "flat"

    @staticmethod
    def _verdict(score_avg: float, bypass_pct: float, idle: bool) -> str:
        if idle or score_avg < 2.0:
            return "red"
        if bypass_pct > 0.7 or score_avg < 3.0:
            return "yellow"
        return "green"

    @staticmethod
    def _compose_brief(s: Dict[str, Any]) -> str:
        verdict = s["verdict"].upper()
        emoji = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(verdict, "⚪")

        lines = [
            f"# Director Brief — {emoji} {verdict}",
            f"_Window: last {s['window_hours']}h · as of {s['as_of']}_",
            "",
            "## Snapshot",
            f"- Heartbeats: **{s['heartbeats_seen']}** · avg score **{s['score_avg']}/5** · trend **{s['score_trend']}**",
            f"- Tool calls: **{s['tool_calls_total']}** · top: "
            + ", ".join(f"{n}({c})" for n, c in s["top_tools"]),
            f"- CodeForge: **{s['codeforge_calls']}** calls · raw code writes: **{s['raw_write_code_files']}** files · bypass **{int(s['codeforge_bypass_pct']*100)}%**",
            f"- Council: {'active' if s['council_active'] else 'idle'} ({s['council_threads_today']} threads today)",
            f"- Idle loop detected: **{'yes' if s['idle_loop_detected'] else 'no'}**",
        ]

        recs: List[str] = []
        if s["codeforge_bypass_pct"] > 0.5 and s["raw_write_code_files"] > 0:
            recs.append(
                f"You wrote {s['raw_write_code_files']} code file(s) with raw write_file. "
                "For anything >50 lines or new modules, use `forge_project` so it actually ships."
            )
        if s["score_trend"] == "declining":
            recs.append(
                "Heartbeat scores are declining. Pause and re-read PULSE.md Working State; "
                "you may be drifting from your active focus."
            )
        if s["idle_loop_detected"]:
            recs.append(
                "Idle loop detected — same tool 5+ times in a row. Switch tactics or "
                "consult INTERESTS.md for a different angle."
            )
        if not s["council_active"] and s["heartbeats_seen"] > 3:
            recs.append(
                "Council has not convened today. Consider triggering a morning roundtable "
                "to debate today's priorities (`council_roundtable` tool)."
            )
        if not recs:
            recs.append("No flags — keep the cadence. Push for one shippable artifact this cycle.")

        lines.append("")
        lines.append("## Recommendations")
        for r in recs:
            lines.append(f"- {r}")

        if s["today_code_files"]:
            lines.append("")
            lines.append("## Today's Code Files")
            for f in s["today_code_files"]:
                lines.append(f"- `{f}`")

        return "\n".join(lines)
