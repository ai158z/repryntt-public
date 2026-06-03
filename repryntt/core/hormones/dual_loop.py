"""
Triple-Loop Cognitive Architecture for Autonomous Agent Operation.

Three interconnected circles that form a continuous improvement cycle:

    ┌──────────────────┐         ┌──────────────────┐
    │  SELF-EVOLUTION   │ lessons │     UTILITY       │
    │  (tactical)       ├────────►│  (operational)    │
    │                   │         │                   │
    │ Review work       │◄────────┤ Build things      │
    │ Extract patterns  │ results │ Complete tasks    │
    │ Update rules      │         │ Produce artifacts │
    └────────┬──────────┘         └──────────────────┘
             │ ▲
    insights │ │ meta-data
             ▼ │
    ┌──────────────────┐
    │ SELF-EXPLORATION  │
    │  (strategic)      │
    │                   │
    │ Is the loop       │
    │ actually working? │
    │ Score trends?     │
    │ Stale patterns?   │
    └──────────────────┘

Frequencies:
  - UTILITY:          ~80% of heartbeats (the default)
  - SELF_EVOLUTION:   ~1 every 5 utility heartbeats
  - SELF_EXPLORATION: ~1 every 20 heartbeats (or once per day)
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Loop modes ──────────────────────────────────────────────

class LoopMode(str, Enum):
    UTILITY = "utility"
    SELF_EVOLUTION = "self_evolution"
    SELF_EXPLORATION = "self_exploration"


# ── Data containers ─────────────────────────────────────────

@dataclass
class WorkOutput:
    """Result of a UTILITY heartbeat."""
    heartbeat: int
    timestamp: float
    score: int  # 1-5
    topic: str
    tool_count: int
    summary: str  # first 300 chars of report
    was_successful: bool

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CapabilityUpdate:
    """Lesson extracted by a SELF_EVOLUTION heartbeat."""
    heartbeat: int
    timestamp: float
    source_heartbeats: List[int]  # which work outputs were reviewed
    lesson: str  # concrete rule: "Always test code before marking done"
    category: str  # "skill_gap" | "pattern" | "anti_pattern" | "process"
    applied_count: int = 0  # how many times injected into utility context
    helped: Optional[bool] = None  # did utility scores improve after?

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MetaInsight:
    """Strategic insight from SELF_EXPLORATION."""
    heartbeat: int
    timestamp: float
    score_trend: str  # "improving" | "declining" | "flat"
    avg_score_before: float
    avg_score_after: float
    insight: str  # e.g. "capability updates about error handling aren't helping"
    action: str  # "drop_pattern:error_handling" | "keep_pattern:testing" | "rotate_focus"

    def to_dict(self) -> dict:
        return asdict(self)


# ── Configuration ───────────────────────────────────────────

EVOLUTION_FREQUENCY = 5     # trigger self-evolution every N utility heartbeats
EXPLORATION_FREQUENCY = 20  # trigger self-exploration every N heartbeats total
MAX_WORK_OUTPUTS = 100      # rolling window
MAX_CAPABILITY_UPDATES = 50
MAX_META_INSIGHTS = 20
CAPABILITY_ACTIVE_WINDOW = 10  # only inject last N capability updates into prompt


class TripleLoopEngine:
    """Manages the three-circle cognitive loop for an autonomous agent."""

    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / "triple_loop_state.json"

        # Counters
        self.total_heartbeats: int = 0
        self.utility_since_evolution: int = 0
        self.heartbeats_since_exploration: int = 0

        # Data stores
        self.work_outputs: List[Dict] = []
        self.capability_updates: List[Dict] = []
        self.meta_insights: List[Dict] = []

        # Current active capability updates (injected into utility prompts)
        self.active_capabilities: List[str] = []

        self._load_state()

    # ── State persistence ───────────────────────────────────

    def _load_state(self):
        if self.state_path.exists():
            try:
                with open(self.state_path, 'r') as f:
                    data = json.load(f)
                self.total_heartbeats = data.get("total_heartbeats", 0)
                self.utility_since_evolution = data.get("utility_since_evolution", 0)
                self.heartbeats_since_exploration = data.get("heartbeats_since_exploration", 0)
                self.work_outputs = data.get("work_outputs", [])[-MAX_WORK_OUTPUTS:]
                self.capability_updates = data.get("capability_updates", [])[-MAX_CAPABILITY_UPDATES:]
                self.meta_insights = data.get("meta_insights", [])[-MAX_META_INSIGHTS:]
                self.active_capabilities = data.get("active_capabilities", [])
                logger.info(f"🔄 TripleLoop loaded: {self.total_heartbeats} total heartbeats, "
                            f"{len(self.capability_updates)} capabilities, {len(self.meta_insights)} insights")
            except Exception as e:
                logger.warning(f"Failed to load triple loop state: {e}")

    def save_state(self):
        try:
            data = {
                "total_heartbeats": self.total_heartbeats,
                "utility_since_evolution": self.utility_since_evolution,
                "heartbeats_since_exploration": self.heartbeats_since_exploration,
                "work_outputs": self.work_outputs[-MAX_WORK_OUTPUTS:],
                "capability_updates": self.capability_updates[-MAX_CAPABILITY_UPDATES:],
                "meta_insights": self.meta_insights[-MAX_META_INSIGHTS:],
                "active_capabilities": self.active_capabilities,
                "last_saved": time.time(),
            }
            tmp = str(self.state_path) + ".tmp"
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, str(self.state_path))
        except Exception as e:
            logger.warning(f"Failed to save triple loop state: {e}")

    # ── Mode selection ──────────────────────────────────────

    def get_current_mode(self) -> LoopMode:
        """Determine which circle should fire this heartbeat.

        Priority:
          1. Self-exploration (rarest, once every ~20 heartbeats)
          2. Self-evolution (every ~5 utility heartbeats)
          3. Utility (default)
        """
        self.total_heartbeats += 1
        self.heartbeats_since_exploration += 1

        # Self-exploration: every EXPLORATION_FREQUENCY heartbeats
        # AND we have enough data to analyze (at least 1 capability update)
        if (self.heartbeats_since_exploration >= EXPLORATION_FREQUENCY
                and len(self.capability_updates) >= 1
                and len(self.work_outputs) >= 5):
            self.heartbeats_since_exploration = 0
            logger.info(f"🔬 TripleLoop: SELF_EXPLORATION mode (heartbeat {self.total_heartbeats})")
            return LoopMode.SELF_EXPLORATION

        # Self-evolution: every EVOLUTION_FREQUENCY utility heartbeats
        # AND we have work outputs to review
        if (self.utility_since_evolution >= EVOLUTION_FREQUENCY
                and len(self.work_outputs) >= 3):
            self.utility_since_evolution = 0
            logger.info(f"🧬 TripleLoop: SELF_EVOLUTION mode (heartbeat {self.total_heartbeats})")
            return LoopMode.SELF_EVOLUTION

        # Default: utility
        self.utility_since_evolution += 1
        logger.info(f"⚙️ TripleLoop: UTILITY mode (heartbeat {self.total_heartbeats}, "
                     f"next evolution in {EVOLUTION_FREQUENCY - self.utility_since_evolution})")
        return LoopMode.UTILITY

    # ── Recording results ───────────────────────────────────

    def record_work_output(self, score: int, topic: str, tool_count: int,
                           summary: str, was_successful: bool):
        """Called after a UTILITY heartbeat completes."""
        output = WorkOutput(
            heartbeat=self.total_heartbeats,
            timestamp=time.time(),
            score=score,
            topic=topic or "unknown",
            tool_count=tool_count,
            summary=(summary or "")[:300],
            was_successful=was_successful,
        )
        self.work_outputs.append(output.to_dict())
        self.work_outputs = self.work_outputs[-MAX_WORK_OUTPUTS:]

        # Track whether active capabilities helped
        self._evaluate_capability_effectiveness(score)

        self.save_state()

    def record_capability_update(self, lesson: str, category: str,
                                 source_heartbeats: Optional[List[int]] = None):
        """Called after a SELF_EVOLUTION heartbeat produces a lesson."""
        update = CapabilityUpdate(
            heartbeat=self.total_heartbeats,
            timestamp=time.time(),
            source_heartbeats=source_heartbeats or [],
            lesson=lesson,
            category=category,
        )
        self.capability_updates.append(update.to_dict())
        self.capability_updates = self.capability_updates[-MAX_CAPABILITY_UPDATES:]

        # Add to active capabilities (most recent N)
        self.active_capabilities.append(lesson)
        self.active_capabilities = self.active_capabilities[-CAPABILITY_ACTIVE_WINDOW:]

        self.save_state()

    def record_meta_insight(self, insight: str, action: str):
        """Called after a SELF_EXPLORATION heartbeat."""
        # Compute score trend
        recent = self.work_outputs[-10:]
        older = self.work_outputs[-20:-10] if len(self.work_outputs) > 10 else []

        avg_recent = sum(w["score"] for w in recent) / len(recent) if recent else 0
        avg_older = sum(w["score"] for w in older) / len(older) if older else avg_recent

        if avg_recent > avg_older + 0.3:
            trend = "improving"
        elif avg_recent < avg_older - 0.3:
            trend = "declining"
        else:
            trend = "flat"

        mi = MetaInsight(
            heartbeat=self.total_heartbeats,
            timestamp=time.time(),
            score_trend=trend,
            avg_score_before=round(avg_older, 2),
            avg_score_after=round(avg_recent, 2),
            insight=insight,
            action=action,
        )
        self.meta_insights.append(mi.to_dict())
        self.meta_insights = self.meta_insights[-MAX_META_INSIGHTS:]

        # Execute the action
        self._execute_meta_action(action)

        self.save_state()

    # ── Prompt context builders ─────────────────────────────

    def get_utility_context(self) -> str:
        """Inject active capabilities into a UTILITY heartbeat's PLAN prompt."""
        if not self.active_capabilities:
            return ""

        lines = [
            "\n## 🔄 Capability Updates (lessons from self-reflection — APPLY THESE)",
        ]
        for i, cap in enumerate(self.active_capabilities[-5:], 1):
            lines.append(f"  {i}. {cap}")
        lines.append(
            "  → These lessons came from reviewing your recent work. Apply them NOW.\n"
        )
        return "\n".join(lines)

    def get_evolution_context(self) -> str:
        """Build the prompt for a SELF_EVOLUTION heartbeat.

        Feeds the last N work outputs into the prompt so the agent
        reviews REAL work, not abstract philosophy.
        """
        recent_work = self.work_outputs[-EVOLUTION_FREQUENCY:]
        if not recent_work:
            return ""

        lines = [
            "## 🧬 SELF-EVOLUTION HEARTBEAT",
            "",
            "This heartbeat is dedicated to REFLECTION on your recent work.",
            "You are NOT doing utility work this heartbeat. Instead:",
            "",
            "1. Review the work outputs below",
            "2. Identify ONE concrete pattern (what worked or what failed)",
            "3. Produce a **capability update**: a specific rule or lesson",
            "",
            "**Format your output as:**",
            "```",
            "CAPABILITY_UPDATE",
            "category: skill_gap | pattern | anti_pattern | process",
            "lesson: [specific, actionable rule — e.g. 'Always run code before marking task done']",
            "source_heartbeats: [list of heartbeat numbers reviewed]",
            "```",
            "",
            "### Recent Work Outputs to Review:",
            "",
        ]

        for w in recent_work:
            emoji = "✅" if w.get("was_successful") else "❌"
            lines.append(
                f"- {emoji} **HB #{w['heartbeat']}** | score: {w['score']}/5 | "
                f"topic: {w.get('topic', '?')} | tools: {w.get('tool_count', 0)}"
            )
            if w.get("summary"):
                lines.append(f"  Summary: {w['summary'][:200]}")
            lines.append("")

        # Inject current capabilities for reference
        if self.active_capabilities:
            lines.append("### Current Active Rules (still in effect):")
            for cap in self.active_capabilities:
                lines.append(f"  - {cap}")
            lines.append("")
            lines.append("→ Don't repeat rules that are already active. Find NEW patterns.")

        # Score statistics
        scores = [w["score"] for w in recent_work]
        avg = sum(scores) / len(scores) if scores else 0
        lines.append(f"\n**Average score in this window: {avg:.1f}/5**")
        if avg < 3:
            lines.append("⚠️ Scores are LOW. Focus on what's going WRONG.")
        elif avg >= 4:
            lines.append("✅ Scores look good. Identify what's WORKING and reinforce it.")

        lines.append("")
        lines.append("**RULES**: Your output MUST be grounded in the data above.")
        lines.append("Do NOT produce abstract philosophy. Extract a SPECIFIC lesson from SPECIFIC heartbeats.")
        lines.append("If all scores are good and no clear pattern exists, output: `NO_UPDATE_NEEDED`")

        return "\n".join(lines)

    def get_exploration_context(self) -> str:
        """Build the prompt for a SELF_EXPLORATION heartbeat.

        Analyzes whether the evolution→utility feedback loop is actually
        working. This is the meta-cognitive layer.
        """
        lines = [
            "## 🔬 SELF-EXPLORATION HEARTBEAT",
            "",
            "This heartbeat is for META-ANALYSIS of your learning process.",
            "You are analyzing WHETHER your self-evolution is actually helping.",
            "",
            "**Question to answer**: Is the triple-loop working?",
            "- Are capability updates being applied?",
            "- Are scores improving over time?",
            "- Are there stale rules that should be dropped?",
            "- Is the evolution process itself stuck in a pattern?",
            "",
            "**Format your output as:**",
            "```",
            "META_INSIGHT",
            "insight: [what you discovered about the loop itself]",
            "action: keep_all | drop_pattern:<name> | rotate_focus | reset_capabilities",
            "```",
            "",
        ]

        # Score trend analysis
        all_scores = [w["score"] for w in self.work_outputs]
        if len(all_scores) >= 10:
            first_half = all_scores[:len(all_scores) // 2]
            second_half = all_scores[len(all_scores) // 2:]
            avg_first = sum(first_half) / len(first_half)
            avg_second = sum(second_half) / len(second_half)
            lines.append("### Score Trend:")
            lines.append(f"  - First half avg: {avg_first:.2f}/5 ({len(first_half)} heartbeats)")
            lines.append(f"  - Second half avg: {avg_second:.2f}/5 ({len(second_half)} heartbeats)")
            delta = avg_second - avg_first
            if delta > 0.3:
                lines.append(f"  - Trend: 📈 IMPROVING (+{delta:.2f})")
            elif delta < -0.3:
                lines.append(f"  - Trend: 📉 DECLINING ({delta:.2f})")
            else:
                lines.append(f"  - Trend: ➡️ FLAT ({delta:+.2f})")
        elif all_scores:
            avg = sum(all_scores) / len(all_scores)
            lines.append(f"### Score Summary: avg {avg:.2f}/5 ({len(all_scores)} heartbeats)")
        lines.append("")

        # Capability update effectiveness
        if self.capability_updates:
            lines.append("### Capability Updates History:")
            for cu in self.capability_updates[-8:]:
                helped = cu.get("helped")
                status = "✅" if helped else ("❌" if helped is False else "❓")
                lines.append(
                    f"  - {status} [{cu.get('category', '?')}] {cu.get('lesson', '?')[:100]}"
                    f" (applied {cu.get('applied_count', 0)}x)"
                )
            lines.append("")

            # Count by effectiveness
            helped_count = sum(1 for cu in self.capability_updates if cu.get("helped") is True)
            hurt_count = sum(1 for cu in self.capability_updates if cu.get("helped") is False)
            unknown_count = sum(1 for cu in self.capability_updates if cu.get("helped") is None)
            lines.append(f"  Helped: {helped_count} | Hurt: {hurt_count} | Unknown: {unknown_count}")
        lines.append("")

        # Previous meta-insights
        if self.meta_insights:
            lines.append("### Previous Meta-Insights:")
            for mi in self.meta_insights[-3:]:
                lines.append(
                    f"  - [{mi.get('score_trend', '?')}] {mi.get('insight', '?')[:150]}"
                    f" → action: {mi.get('action', '?')}"
                )
            lines.append("")

        lines.append("**RULES**: Analyze the DATA above. This is not abstract self-reflection.")
        lines.append("You are a performance analyst examining a feedback system.")
        lines.append("If the loop is working (scores improving, good capabilities), output: `keep_all`")
        lines.append("If specific rules aren't helping, name them: `drop_pattern:<lesson_keyword>`")
        lines.append("If the system is stuck, try: `rotate_focus` (diversify next evolution cycle)")
        lines.append("If capabilities are stale/contradictory: `reset_capabilities` (clear and restart)")

        return "\n".join(lines)

    # ── Internal helpers ────────────────────────────────────

    def _evaluate_capability_effectiveness(self, score: int):
        """After a utility heartbeat, check if active capabilities helped."""
        if not self.capability_updates:
            return

        # Find the most recent capability update
        latest = self.capability_updates[-1]
        latest["applied_count"] = latest.get("applied_count", 0) + 1

        # After 3+ applications, determine if it helped
        if latest["applied_count"] >= 3 and latest.get("helped") is None:
            # Get scores since this capability was added
            cap_heartbeat = latest.get("heartbeat", 0)
            scores_after = [
                w["score"] for w in self.work_outputs
                if w.get("heartbeat", 0) > cap_heartbeat
            ]
            scores_before = [
                w["score"] for w in self.work_outputs
                if w.get("heartbeat", 0) <= cap_heartbeat
            ][-5:]  # last 5 before

            if scores_after and scores_before:
                avg_after = sum(scores_after) / len(scores_after)
                avg_before = sum(scores_before) / len(scores_before)
                if avg_after > avg_before + 0.2:
                    latest["helped"] = True
                elif avg_after < avg_before - 0.3:
                    latest["helped"] = False
                # else: not enough signal, leave as None

    def _execute_meta_action(self, action: str):
        """Execute a strategic action from self-exploration."""
        if action == "keep_all":
            logger.info("🔬 Meta-action: keep_all — loop is working")
            return

        if action == "reset_capabilities":
            logger.info("🔬 Meta-action: reset_capabilities — clearing stale rules")
            self.active_capabilities = []
            return

        if action == "rotate_focus":
            logger.info("🔬 Meta-action: rotate_focus — diversifying next evolution")
            # Drop the oldest half of active capabilities to force fresh patterns
            half = len(self.active_capabilities) // 2
            self.active_capabilities = self.active_capabilities[half:]
            return

        if action.startswith("drop_pattern:"):
            pattern_keyword = action.split(":", 1)[1].strip().lower()
            before = len(self.active_capabilities)
            self.active_capabilities = [
                cap for cap in self.active_capabilities
                if pattern_keyword not in cap.lower()
            ]
            dropped = before - len(self.active_capabilities)
            logger.info(f"🔬 Meta-action: drop_pattern '{pattern_keyword}' — dropped {dropped} capabilities")
            return

        logger.info(f"🔬 Meta-action: unrecognized '{action}' — no action taken")

    # ── Parsing LLM outputs ─────────────────────────────────

    def parse_evolution_output(self, text: str) -> Optional[Dict]:
        """Extract CAPABILITY_UPDATE from self-evolution report."""
        if not text or "NO_UPDATE_NEEDED" in text:
            return None

        result = {"lesson": "", "category": "pattern", "source_heartbeats": []}

        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("lesson:"):
                result["lesson"] = line[7:].strip()
            elif line.startswith("category:"):
                cat = line[9:].strip().split("|")[0].strip()
                if cat in ("skill_gap", "pattern", "anti_pattern", "process"):
                    result["category"] = cat
            elif line.startswith("source_heartbeats:"):
                try:
                    nums = line[18:].strip().strip("[]")
                    result["source_heartbeats"] = [int(n.strip()) for n in nums.split(",") if n.strip()]
                except Exception:
                    pass

        # Fallback: if no structured output, use the first substantial line as lesson
        if not result["lesson"]:
            for line in text.split("\n"):
                line = line.strip()
                if len(line) > 20 and not line.startswith("#") and not line.startswith("```"):
                    result["lesson"] = line[:200]
                    break

        return result if result["lesson"] else None

    def parse_exploration_output(self, text: str) -> Optional[Dict]:
        """Extract META_INSIGHT from self-exploration report."""
        if not text:
            return None

        result = {"insight": "", "action": "keep_all"}

        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("insight:"):
                result["insight"] = line[8:].strip()
            elif line.startswith("action:"):
                result["action"] = line[7:].strip().split()[0]  # first word only

        # Fallback
        if not result["insight"]:
            for line in text.split("\n"):
                line = line.strip()
                if len(line) > 20 and not line.startswith("#") and not line.startswith("```"):
                    result["insight"] = line[:200]
                    break

        return result if result["insight"] else None

    # ── Stats for dashboard / debugging ─────────────────────

    def get_stats(self) -> Dict:
        """Return loop statistics for monitoring."""
        recent_scores = [w["score"] for w in self.work_outputs[-20:]]
        return {
            "total_heartbeats": self.total_heartbeats,
            "utility_since_evolution": self.utility_since_evolution,
            "heartbeats_since_exploration": self.heartbeats_since_exploration,
            "work_outputs_count": len(self.work_outputs),
            "capability_updates_count": len(self.capability_updates),
            "meta_insights_count": len(self.meta_insights),
            "active_capabilities": len(self.active_capabilities),
            "avg_recent_score": round(sum(recent_scores) / len(recent_scores), 2) if recent_scores else 0,
            "next_evolution_in": max(0, EVOLUTION_FREQUENCY - self.utility_since_evolution),
            "next_exploration_in": max(0, EXPLORATION_FREQUENCY - self.heartbeats_since_exploration),
        }
