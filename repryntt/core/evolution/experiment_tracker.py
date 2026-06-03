"""
Self-Experimentation Tracker — Autoresearch-Inspired Behavioral Evolution

Adapted from Karpathy's autoresearch pattern:
  modify → run → measure → keep or discard → repeat

When Artemis modifies her own config (prompts, skills, strategies),
this module tracks whether the change actually improved things.
Bad changes get auto-reverted. Good ones get kept and logged.

Also embeds two behavioral patterns:
  - "Think Harder" escalation when consecutive metrics stagnate
  - "Simplicity Criterion" pressure to remove cruft, not just add
"""

import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)

BRAIN_DIR = Path(os.environ.get("REPRYNTT_BRAIN", str(Path.home() / ".repryntt" / "brain")))
EXPERIMENTS_FILE = BRAIN_DIR / "experiments.jsonl"
SNAPSHOTS_DIR = BRAIN_DIR / "experiment_snapshots"
TRACKER_STATE_FILE = BRAIN_DIR / "experiment_tracker_state.json"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Experiment record
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class Experiment:
    """A single self-modification experiment."""

    def __init__(self, experiment_id: str, hypothesis: str,
                 target_file: str, change_type: str,
                 change_summary: str, window_heartbeats: int = 5):
        self.experiment_id = experiment_id
        self.hypothesis = hypothesis          # "Increasing task specificity in PULSE.md will raise eval scores"
        self.target_file = target_file        # Relative path of the modified file
        self.change_type = change_type        # append | replace | create_skill | config_change
        self.change_summary = change_summary  # Brief description of what changed
        self.window_heartbeats = window_heartbeats
        self.snapshot_path: str = ""          # Path to pre-change backup

        self.started_at: str = datetime.now(timezone.utc).isoformat()
        self.status: str = "active"           # active | kept | reverted | expired
        self.heartbeats_elapsed: int = 0

        # Metrics: before and during
        self.baseline_scores: List[int] = []  # eval scores from 3 heartbeats before experiment
        self.experiment_scores: List[int] = []  # eval scores during the experiment window
        self.baseline_avg: float = 0.0
        self.experiment_avg: float = 0.0

        self.stuck_count_before: int = 0
        self.stuck_count_during: int = 0

        self.verdict: str = ""                # Human-readable verdict
        self.resolved_at: str = ""

    def to_dict(self) -> Dict:
        return {
            "experiment_id": self.experiment_id,
            "hypothesis": self.hypothesis,
            "target_file": self.target_file,
            "change_type": self.change_type,
            "change_summary": self.change_summary,
            "window_heartbeats": self.window_heartbeats,
            "started_at": self.started_at,
            "status": self.status,
            "heartbeats_elapsed": self.heartbeats_elapsed,
            "baseline_scores": self.baseline_scores,
            "experiment_scores": self.experiment_scores,
            "baseline_avg": round(self.baseline_avg, 2),
            "experiment_avg": round(self.experiment_avg, 2),
            "stuck_count_before": self.stuck_count_before,
            "stuck_count_during": self.stuck_count_during,
            "verdict": self.verdict,
            "resolved_at": self.resolved_at,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "Experiment":
        exp = cls(
            experiment_id=d["experiment_id"],
            hypothesis=d.get("hypothesis", ""),
            target_file=d.get("target_file", ""),
            change_type=d.get("change_type", ""),
            change_summary=d.get("change_summary", ""),
            window_heartbeats=d.get("window_heartbeats", 5),
        )
        exp.snapshot_path = d.get("snapshot_path", "")
        exp.started_at = d.get("started_at", "")
        exp.status = d.get("status", "active")
        exp.heartbeats_elapsed = d.get("heartbeats_elapsed", 0)
        exp.baseline_scores = d.get("baseline_scores", [])
        exp.experiment_scores = d.get("experiment_scores", [])
        exp.baseline_avg = d.get("baseline_avg", 0.0)
        exp.experiment_avg = d.get("experiment_avg", 0.0)
        exp.stuck_count_before = d.get("stuck_count_before", 0)
        exp.stuck_count_during = d.get("stuck_count_during", 0)
        exp.verdict = d.get("verdict", "")
        exp.resolved_at = d.get("resolved_at", "")
        return exp


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Experiment Tracker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ExperimentTracker:
    """
    Tracks behavioral self-experiments: when Artemis modifies her own
    prompts/skills/config, this records the change, snapshots the original,
    monitors eval scores, and auto-reverts if things got worse.
    """

    # Minimum improvement needed to "keep" (avoids noise)
    MIN_IMPROVEMENT = 0.3   # avg score must improve by >= 0.3 to keep
    MIN_SAMPLES = 3         # need at least 3 eval scores to judge
    MAX_ACTIVE = 3          # max concurrent experiments (avoid chaos)
    DEFAULT_WINDOW = 5      # heartbeats to observe before judging

    def __init__(self):
        self.active_experiments: Dict[str, Experiment] = {}
        self.recent_scores: List[int] = []         # rolling window of recent eval scores
        self.recent_stuck: List[bool] = []          # rolling window of stuck events
        self.consecutive_low_scores: int = 0        # for #3 think-harder detection
        self.total_experiments: int = 0
        self.total_kept: int = 0
        self.total_reverted: int = 0

        self._load_state()

    # ── Experiment Lifecycle ──────────────────────────────────────

    def start_experiment(self, hypothesis: str, target_file: str,
                         change_type: str, change_summary: str,
                         window: int = None) -> Optional[Dict]:
        """
        Start tracking a new experiment. Call BEFORE making the change.
        Returns experiment info or error dict.
        """
        if len(self.active_experiments) >= self.MAX_ACTIVE:
            return {"error": f"Too many active experiments ({self.MAX_ACTIVE} max). "
                             "Wait for current experiments to resolve."}

        # Snapshot the original file for potential rollback
        snapshot_path = ""
        full_path = self._resolve_path(target_file)
        if full_path and os.path.exists(full_path):
            snapshot_path = self._create_snapshot(full_path, target_file)

        window = window or self.DEFAULT_WINDOW
        exp_id = f"exp_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{len(self.active_experiments)}"

        exp = Experiment(
            experiment_id=exp_id,
            hypothesis=hypothesis,
            target_file=target_file,
            change_type=change_type,
            change_summary=change_summary,
            window_heartbeats=window,
        )
        exp.snapshot_path = snapshot_path

        # Capture baseline from recent scores
        exp.baseline_scores = list(self.recent_scores[-5:])
        exp.baseline_avg = (sum(exp.baseline_scores) / len(exp.baseline_scores)
                            if exp.baseline_scores else 3.0)
        exp.stuck_count_before = sum(1 for s in self.recent_stuck[-5:] if s)

        self.active_experiments[exp_id] = exp
        self.total_experiments += 1
        self._save_state()

        logger.info(f"🧪 Experiment started: {exp_id} — {hypothesis[:80]}")
        return {
            "experiment_id": exp_id,
            "status": "active",
            "baseline_avg": exp.baseline_avg,
            "window": window,
            "message": f"Experiment tracking started. Will evaluate after {window} heartbeats.",
        }

    def record_heartbeat(self, eval_score: int, was_stuck: bool = False):
        """
        Called after every heartbeat evaluation. Updates all active experiments
        and the rolling score window.
        """
        # Update rolling windows
        self.recent_scores.append(eval_score)
        if len(self.recent_scores) > 20:
            self.recent_scores = self.recent_scores[-20:]
        self.recent_stuck.append(was_stuck)
        if len(self.recent_stuck) > 20:
            self.recent_stuck = self.recent_stuck[-20:]

        # Track consecutive low scores for think-harder (#3)
        if eval_score <= 2:
            self.consecutive_low_scores += 1
        else:
            self.consecutive_low_scores = 0

        # Update each active experiment
        resolved = []
        for exp_id, exp in self.active_experiments.items():
            exp.experiment_scores.append(eval_score)
            exp.heartbeats_elapsed += 1
            if was_stuck:
                exp.stuck_count_during += 1

            # Check if window is complete
            if exp.heartbeats_elapsed >= exp.window_heartbeats:
                self._resolve_experiment(exp)
                resolved.append(exp_id)

        for exp_id in resolved:
            del self.active_experiments[exp_id]

        self._save_state()

    def _resolve_experiment(self, exp: Experiment):
        """Judge experiment outcome: keep, revert, or inconclusive."""
        exp.experiment_avg = (sum(exp.experiment_scores) / len(exp.experiment_scores)
                              if exp.experiment_scores else 0.0)
        exp.resolved_at = datetime.now(timezone.utc).isoformat()

        improvement = exp.experiment_avg - exp.baseline_avg
        stuck_delta = exp.stuck_count_during - exp.stuck_count_before

        # Decision logic
        if len(exp.experiment_scores) < self.MIN_SAMPLES:
            exp.status = "expired"
            exp.verdict = "Insufficient data — experiment window too short."

        elif improvement >= self.MIN_IMPROVEMENT and stuck_delta <= 0:
            exp.status = "kept"
            exp.verdict = (
                f"KEEP: avg score {exp.baseline_avg:.1f} → {exp.experiment_avg:.1f} "
                f"(+{improvement:.1f}). Change improved performance."
            )
            self.total_kept += 1
            logger.info(f"🧪✅ Experiment {exp.experiment_id}: {exp.verdict}")

        elif improvement < -self.MIN_IMPROVEMENT or stuck_delta >= 2:
            exp.status = "reverted"
            exp.verdict = (
                f"REVERT: avg score {exp.baseline_avg:.1f} → {exp.experiment_avg:.1f} "
                f"({improvement:+.1f}), stuck events {exp.stuck_count_before} → "
                f"{exp.stuck_count_during}. Change degraded performance."
            )
            self.total_reverted += 1
            self._rollback(exp)
            logger.info(f"🧪❌ Experiment {exp.experiment_id}: {exp.verdict}")

        else:
            # Neutral — apply simplicity criterion (#4)
            exp.status = "kept"  # keep by default, but note the neutral result
            exp.verdict = (
                f"NEUTRAL: avg score {exp.baseline_avg:.1f} → {exp.experiment_avg:.1f} "
                f"({improvement:+.1f}). Consider if the added complexity is justified."
            )
            self.total_kept += 1
            logger.info(f"🧪➡️ Experiment {exp.experiment_id}: {exp.verdict}")

        # Log to permanent experiment record
        self._log_experiment(exp)

    def _rollback(self, exp: Experiment):
        """Restore the original file from snapshot."""
        if not exp.snapshot_path or not os.path.exists(exp.snapshot_path):
            logger.warning(f"🧪 Cannot rollback {exp.experiment_id}: no snapshot at {exp.snapshot_path}")
            return

        target = self._resolve_path(exp.target_file)
        if not target:
            return

        try:
            shutil.copy2(exp.snapshot_path, target)
            logger.info(f"🧪 Rolled back {exp.target_file} from snapshot")
        except Exception as e:
            logger.error(f"🧪 Rollback failed for {exp.target_file}: {e}")

    # ── Snapshot Management ───────────────────────────────────────

    def _create_snapshot(self, full_path: str, rel_path: str) -> str:
        """Create a timestamped snapshot of a file."""
        try:
            SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            safe_name = rel_path.replace("/", "_").replace("\\", "_")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            snap_path = SNAPSHOTS_DIR / f"{safe_name}.{ts}.snap"
            shutil.copy2(full_path, snap_path)

            # Keep only last 20 snapshots per file to avoid bloat
            pattern = f"{safe_name}."
            snaps = sorted(
                [f for f in SNAPSHOTS_DIR.iterdir() if f.name.startswith(pattern)],
                key=lambda p: p.stat().st_mtime,
            )
            for old in snaps[:-20]:
                old.unlink(missing_ok=True)

            return str(snap_path)
        except Exception as e:
            logger.debug(f"Snapshot creation failed: {e}")
            return ""

    @staticmethod
    def _resolve_path(target_file: str) -> Optional[str]:
        """Resolve a target file reference to an absolute path."""
        # Handle bootstrap files (most common experiment target)
        if not os.path.sep in target_file and not target_file.startswith("/"):
            bootstrap_path = BRAIN_DIR / "bootstrap" / target_file
            if bootstrap_path.exists():
                return str(bootstrap_path)
        # Handle absolute paths
        if os.path.isabs(target_file) and os.path.exists(target_file):
            return target_file
        # Handle brain-relative paths
        brain_path = BRAIN_DIR / target_file
        if brain_path.exists():
            return str(brain_path)
        return None

    # ── Think-Harder Escalation (#3) ──────────────────────────────

    def get_stagnation_level(self) -> int:
        """
        Detect how stagnant Artemis is. Returns escalation level:
          0 = normal (< 3 consecutive low scores)
          1 = mild stagnation (3-5 consecutive low scores)
          2 = moderate stagnation (6-9 consecutive)
          3 = deep stagnation (10+ consecutive)
        """
        if self.consecutive_low_scores < 3:
            return 0
        elif self.consecutive_low_scores < 6:
            return 1
        elif self.consecutive_low_scores < 10:
            return 2
        else:
            return 3

    def get_think_harder_injection(self) -> str:
        """
        Returns a heartbeat prompt injection that escalates creativity
        based on stagnation level. Empty string if not stagnating.
        """
        level = self.get_stagnation_level()
        if level == 0:
            return ""

        recent_avg = 0.0
        if self.recent_scores:
            recent_avg = sum(self.recent_scores[-5:]) / min(5, len(self.recent_scores))

        if level == 1:
            return (
                f"\n⚠️ **PATTERN ALERT**: Your last {self.consecutive_low_scores} heartbeats "
                f"scored ≤ 2 (recent avg: {recent_avg:.1f}/5). You may be in a rut.\n"
                "- Try a task from a DIFFERENT domain than what you've been doing\n"
                "- Re-read your PULSE.md for priorities you've been neglecting\n"
                "- Check if any tools you haven't used recently could help\n"
            )

        if level == 2:
            return (
                f"\n🔴 **STAGNATION DETECTED**: {self.consecutive_low_scores} consecutive low scores "
                f"(avg: {recent_avg:.1f}/5). Time to think harder.\n"
                "- STOP repeating similar tasks — consciously pick something you haven't tried\n"
                "- Re-read your RECALL.md for lessons from past successes\n"
                "- Try combining two unrelated interests from your personality journal\n"
                "- Use web_search to find fresh inspiration in your interest areas\n"
                "- Consider: what would a DIFFERENT version of you prioritize right now?\n"
            )

        # level 3
        return (
            f"\n🚨 **DEEP STAGNATION**: {self.consecutive_low_scores} consecutive low scores. "
            f"Your current approach is fundamentally not working.\n"
            "MANDATORY: Do something radically different this heartbeat:\n"
            "1. Read a bootstrap file you haven't looked at in a while\n"
            "2. Start a brand-new initiative that excites you — not incremental improvement\n"
            "3. Use `start_conversation` to talk to your operator if they're nearby\n"
            "4. Write in your personality journal about what's frustrating you\n"
            "5. Consider simplifying: delete a cron task, simplify PULSE.md, trim a skill\n"
            "The definition of insanity is doing the same thing and expecting different results.\n"
        )

    # ── Simplicity Criterion (#4) ─────────────────────────────────

    def get_simplicity_injection(self) -> str:
        """
        Returns a periodic simplicity reminder for the evaluation phase.
        Triggers when system has been running a while and complexity is growing.
        """
        # Only inject periodically (roughly every 10th heartbeat)
        total_scores = len(self.recent_scores)
        if total_scores < 10 or total_scores % 10 != 0:
            return ""

        # Count recent experiments that were neutral or reverted
        neutral_or_bad = self.total_reverted + max(0, self.total_experiments - self.total_kept - self.total_reverted)
        if self.total_experiments > 0 and neutral_or_bad > self.total_kept:
            return (
                "\n📐 **SIMPLICITY CHECK**: More of your recent self-modifications were "
                "neutral or reverted than kept. Consider:\n"
                "- Is there a skill you installed that hasn't improved anything? Remove it.\n"
                "- Is your PULSE.md getting bloated with low-value tasks? Trim it.\n"
                "- A cron task that fires but never produces good results? Delete it.\n"
                "- Remember: removing complexity that doesn't help IS a win.\n"
            )
        return ""

    # ── Experiment Context for Heartbeat ──────────────────────────

    def get_experiment_context(self) -> str:
        """
        Returns info about active experiments and recent results
        for heartbeat prompt injection.
        """
        parts = []

        # Active experiments
        if self.active_experiments:
            exp_lines = []
            for exp in self.active_experiments.values():
                avg = (sum(exp.experiment_scores) / len(exp.experiment_scores)
                       if exp.experiment_scores else 0)
                exp_lines.append(
                    f"  - {exp.change_summary[:60]} "
                    f"({exp.heartbeats_elapsed}/{exp.window_heartbeats} heartbeats, "
                    f"avg score: {avg:.1f}, baseline: {exp.baseline_avg:.1f})"
                )
            parts.append(
                "**🧪 Active experiments** (your recent self-modifications being tracked):\n"
                + "\n".join(exp_lines)
            )

        # Recent completed experiments (from log)
        recent = self._get_recent_results(limit=3)
        if recent:
            res_lines = []
            for r in recent:
                status_icon = {"kept": "✅", "reverted": "❌", "expired": "⏳"}.get(r.get("status"), "➡️")
                res_lines.append(
                    f"  {status_icon} {r.get('change_summary', '')[:50]} — {r.get('verdict', '')[:60]}"
                )
            parts.append("**Recent experiment results:**\n" + "\n".join(res_lines))

        return "\n".join(parts)

    def _get_recent_results(self, limit: int = 5) -> List[Dict]:
        """Read the last N experiment results from the JSONL log."""
        try:
            if not EXPERIMENTS_FILE.exists():
                return []
            lines = EXPERIMENTS_FILE.read_text().strip().split("\n")
            results = []
            for line in lines[-limit:]:
                line = line.strip()
                if line:
                    results.append(json.loads(line))
            return results
        except Exception:
            return []

    # ── Past Experiment Reference (anti-repeat) ───────────────────

    def get_past_failures_context(self, limit: int = 5) -> str:
        """
        Return a summary of recent reverted experiments so Artemis
        doesn't repeat the same failed modifications.
        """
        try:
            if not EXPERIMENTS_FILE.exists():
                return ""
            lines = EXPERIMENTS_FILE.read_text().strip().split("\n")
            failures = []
            for line in reversed(lines):
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("status") == "reverted":
                    failures.append(entry)
                if len(failures) >= limit:
                    break

            if not failures:
                return ""

            parts = ["**⚠️ Past failed experiments** (don't repeat these):"]
            for f in failures:
                parts.append(
                    f"  - ❌ {f.get('change_summary', 'unknown')[:80]} — {f.get('verdict', '')[:60]}"
                )
            return "\n".join(parts)

        except Exception:
            return ""

    # ── Persistence ───────────────────────────────────────────────

    def _save_state(self):
        try:
            TRACKER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "active_experiments": {
                    k: v.to_dict() for k, v in self.active_experiments.items()
                },
                "recent_scores": self.recent_scores[-20:],
                "recent_stuck": self.recent_stuck[-20:],
                "consecutive_low_scores": self.consecutive_low_scores,
                "total_experiments": self.total_experiments,
                "total_kept": self.total_kept,
                "total_reverted": self.total_reverted,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            TRACKER_STATE_FILE.write_text(json.dumps(state, indent=2))
        except Exception as e:
            logger.debug(f"Experiment tracker state save failed: {e}")

    def _load_state(self):
        try:
            if not TRACKER_STATE_FILE.exists():
                return
            data = json.loads(TRACKER_STATE_FILE.read_text())
            self.recent_scores = data.get("recent_scores", [])
            self.recent_stuck = data.get("recent_stuck", [])
            self.consecutive_low_scores = data.get("consecutive_low_scores", 0)
            self.total_experiments = data.get("total_experiments", 0)
            self.total_kept = data.get("total_kept", 0)
            self.total_reverted = data.get("total_reverted", 0)

            for exp_id, exp_data in data.get("active_experiments", {}).items():
                self.active_experiments[exp_id] = Experiment.from_dict(exp_data)
        except Exception as e:
            logger.debug(f"Experiment tracker state load failed: {e}")

    def _log_experiment(self, exp: Experiment):
        """Append completed experiment to permanent JSONL log."""
        try:
            EXPERIMENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(EXPERIMENTS_FILE, "a") as f:
                f.write(json.dumps(exp.to_dict()) + "\n")
        except Exception as e:
            logger.debug(f"Experiment log write failed: {e}")
