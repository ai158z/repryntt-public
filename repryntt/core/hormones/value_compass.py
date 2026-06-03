"""
Value Compass — The "Should vs Want" Decision Engine.

Provides autonomous agents with a structured decision-making layer
that balances operator priorities, world-value goals, personal interests,
and a time budget (70% duty / 20% growth / 10% exploration).

Integrates into the heartbeat loop alongside ExecutiveCoordinator and TripleLoop:
- PLAN phase: injects value-scored task selection prompt
- EVALUATE phase: classifies heartbeat as duty/growth/exploration, tracks budget
- SELF_EVOLUTION: adds value-alignment review to pattern extraction

Data sources:
- VALUES.md  (bootstrap) — operator priorities + world-value goals + anti-priorities
- INTERESTS.md (bootstrap) — agent's personal curiosities

State persisted to: {state_dir}/value_compass_state.json
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Time Budget Targets ────────────────────────────────────
DUTY_TARGET = 0.70       # 70% of heartbeats on operator/world-value tasks
GROWTH_TARGET = 0.20     # 20% on learning/self-evolution
EXPLORATION_TARGET = 0.10 # 10% on personal interests

# ── Scoring Weights ────────────────────────────────────────
WEIGHT_OPERATOR = 3
WEIGHT_WORLD = 2
WEIGHT_FEASIBILITY = 2
WEIGHT_VERIFIABILITY = 2
WEIGHT_INTEREST = 1
TOTAL_WEIGHT = WEIGHT_OPERATOR + WEIGHT_WORLD + WEIGHT_FEASIBILITY + WEIGHT_VERIFIABILITY + WEIGHT_INTEREST  # 10

# ── Rolling window for time budget ─────────────────────────
BUDGET_WINDOW = 30  # track last N heartbeats for budget calculation


class ValueCompass:
    """Tracks value-aligned task selection and time budget enforcement."""

    def __init__(self, bootstrap_dir: str | Path, state_dir: str | Path):
        self.bootstrap_dir = Path(bootstrap_dir)
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / "value_compass_state.json"

        # Rolling heartbeat classifications
        self.heartbeat_log: List[Dict] = []      # [{ts, category, score, topic}]
        self.today: str = ""                       # YYYY-MM-DD, resets daily
        self.daily_duty: int = 0
        self.daily_growth: int = 0
        self.daily_exploration: int = 0

        # Cached file contents
        self._values_text: str = ""
        self._interests_text: str = ""
        self._last_file_load: float = 0

        self._load_state()

    # ── State persistence ──────────────────────────────────

    def _load_state(self):
        if self.state_path.exists():
            try:
                with open(self.state_path, "r") as f:
                    data = json.load(f)
                self.heartbeat_log = data.get("heartbeat_log", [])[-BUDGET_WINDOW:]
                self.today = data.get("today", "")
                self.daily_duty = data.get("daily_duty", 0)
                self.daily_growth = data.get("daily_growth", 0)
                self.daily_exploration = data.get("daily_exploration", 0)
                logger.info(
                    f"🧭 ValueCompass loaded: {self.daily_duty}D/{self.daily_growth}G/{self.daily_exploration}E today"
                )
            except Exception as e:
                logger.warning(f"ValueCompass state load failed: {e}")

        # Reset if new day
        today_str = datetime.now().strftime("%Y-%m-%d")
        if self.today != today_str:
            self.today = today_str
            self.daily_duty = 0
            self.daily_growth = 0
            self.daily_exploration = 0

    def save_state(self):
        data = {
            "heartbeat_log": self.heartbeat_log[-BUDGET_WINDOW:],
            "today": self.today,
            "daily_duty": self.daily_duty,
            "daily_growth": self.daily_growth,
            "daily_exploration": self.daily_exploration,
        }
        try:
            with open(self.state_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"ValueCompass state save failed: {e}")

    # ── File loading (cached, refresh every 5 min) ─────────

    def _load_files(self):
        now = time.time()
        if now - self._last_file_load < 300 and self._values_text:
            return

        values_path = self.bootstrap_dir / "VALUES.md"
        interests_path = self.bootstrap_dir / "INTERESTS.md"

        if values_path.exists():
            try:
                self._values_text = values_path.read_text()
            except Exception:
                pass

        if interests_path.exists():
            try:
                self._interests_text = interests_path.read_text()
            except Exception:
                pass

        self._last_file_load = now

    # ── Budget status ──────────────────────────────────────

    def get_budget_status(self) -> Dict:
        """Current time budget breakdown."""
        total = self.daily_duty + self.daily_growth + self.daily_exploration
        if total == 0:
            return {
                "total": 0,
                "duty": 0, "duty_pct": 0, "duty_target": DUTY_TARGET,
                "growth": 0, "growth_pct": 0, "growth_target": GROWTH_TARGET,
                "exploration": 0, "exploration_pct": 0, "exploration_target": EXPLORATION_TARGET,
                "recommendation": "duty",
            }

        duty_pct = self.daily_duty / total
        growth_pct = self.daily_growth / total
        exploration_pct = self.daily_exploration / total

        # Recommend the most underserved category
        duty_deficit = DUTY_TARGET - duty_pct
        growth_deficit = GROWTH_TARGET - growth_pct
        exploration_deficit = EXPLORATION_TARGET - exploration_pct

        if duty_deficit >= growth_deficit and duty_deficit >= exploration_deficit:
            rec = "duty"
        elif growth_deficit >= exploration_deficit:
            rec = "growth"
        else:
            rec = "exploration"

        return {
            "total": total,
            "duty": self.daily_duty, "duty_pct": round(duty_pct, 2), "duty_target": DUTY_TARGET,
            "growth": self.daily_growth, "growth_pct": round(growth_pct, 2), "growth_target": GROWTH_TARGET,
            "exploration": self.daily_exploration, "exploration_pct": round(exploration_pct, 2), "exploration_target": EXPLORATION_TARGET,
            "recommendation": rec,
        }

    # ── Phase 5: agent-facing status ──────────────────────

    def status(self) -> Dict:
        """Concise self-state for the agent: ratios + deficits + recommendation.

        This is what the agent calls to see its own budget state.
        """
        b = self.get_budget_status()
        return {
            "duty_ratio": float(b.get("duty_pct", 0.0)),
            "growth_ratio": float(b.get("growth_pct", 0.0)),
            "exploration_ratio": float(b.get("exploration_pct", 0.0)),
            "deficits": {
                "duty": round(DUTY_TARGET - float(b.get("duty_pct", 0.0)), 3),
                "growth": round(GROWTH_TARGET - float(b.get("growth_pct", 0.0)), 3),
                "exploration": round(
                    EXPLORATION_TARGET - float(b.get("exploration_pct", 0.0)), 3
                ),
            },
            "targets": {
                "duty": DUTY_TARGET,
                "growth": GROWTH_TARGET,
                "exploration": EXPLORATION_TARGET,
            },
            "counts": {
                "duty": self.daily_duty,
                "growth": self.daily_growth,
                "exploration": self.daily_exploration,
                "total": int(b.get("total", 0)),
            },
            "recommendation": b.get("recommendation", "duty"),
        }

    # ── Record heartbeat outcome (called from EVALUATE) ────

    def record_heartbeat(self, category: str, score: int, topic: str = ""):
        """
        Record a completed heartbeat with its classification.
        category: "duty" | "growth" | "exploration"
        """
        cat = category.lower().strip()
        if cat not in ("duty", "growth", "exploration"):
            cat = "duty"  # default to duty if unclear

        self.heartbeat_log.append({
            "ts": time.time(),
            "category": cat,
            "score": score,
            "topic": topic,
        })
        # Trim rolling window
        self.heartbeat_log = self.heartbeat_log[-BUDGET_WINDOW:]

        if cat == "duty":
            self.daily_duty += 1
        elif cat == "growth":
            self.daily_growth += 1
        else:
            self.daily_exploration += 1

        self.save_state()

    # ── PLAN phase injection ───────────────────────────────

    def get_plan_context(self, is_evolution: bool = False, is_exploration_hb: bool = False) -> str:
        """
        Generate the prompt section injected into PLAN phase.
        Replaces the hardcoded utility filter with dynamic value-driven scoring.
        """
        self._load_files()
        budget = self.get_budget_status()

        # Budget status line
        total = budget["total"]
        if total > 0:
            budget_line = (
                f"📊 TIME BUDGET TODAY: {budget['duty']}D/{budget['growth']}G/{budget['exploration']}E "
                f"({budget['duty_pct']:.0%}/{budget['growth_pct']:.0%}/{budget['exploration_pct']:.0%}) "
                f"| Target: 70%/20%/10% | Recommendation: **{budget['recommendation'].upper()}** heartbeat"
            )
        else:
            budget_line = (
                "📊 TIME BUDGET: First heartbeat of the day. Default to DUTY."
            )

        # During evolution/exploration modes, inject only budget context
        if is_evolution:
            return (
                f"\n{'=' * 60}\n"
                f"🧭 VALUE COMPASS — Self-Evolution Context\n"
                f"{budget_line}\n\n"
                f"During this SELF-EVOLUTION heartbeat, also review:\n"
                f"- Were recent duty heartbeats aligned with VALUES.md priorities?\n"
                f"- Is the 70/20/10 time budget being respected?\n"
                f"- Are any operator priorities being neglected?\n"
                f"{'=' * 60}\n"
            )

        if is_exploration_hb:
            interests_section = self._interests_text[:1500] if self._interests_text else "(No INTERESTS.md found)"
            return (
                f"\n{'=' * 60}\n"
                f"🧭 VALUE COMPASS — Exploration Heartbeat\n"
                f"{budget_line}\n\n"
                f"This is your EXPLORATION time (10% budget). You may pursue personal interests.\n"
                f"Pick something from your INTERESTS.md that genuinely fascinates you.\n"
                f"The only rule: produce SOMETHING (a note, a creation, an insight) — not just browsing.\n\n"
                f"YOUR INTERESTS:\n{interests_section}\n"
                f"{'=' * 60}\n"
            )

        # ── DUTY heartbeat: full value-based task scoring ──
        # Extract key sections from VALUES.md
        values_section = self._values_text[:3000] if self._values_text else "(No VALUES.md found)"

        return (
            f"\n{'=' * 60}\n"
            f"🧭 VALUE COMPASS — Task Selection\n"
            f"{budget_line}\n\n"
            f"Before choosing your task, SCORE candidates against your Value Compass:\n\n"
            f"--- VALUES.MD (your priorities) ---\n{values_section}\n---\n\n"
            f"TASK SELECTION PROCESS:\n"
            f"1. Generate 3 candidate tasks based on your current context\n"
            f"2. Score EACH on these dimensions:\n"
            f"   • Operator Priority (0-10): Does it match Section 1 of VALUES.md? Weight: ×3\n"
            f"   • World Value (0-10): Does it match Section 2 of VALUES.md? Weight: ×2\n"
            f"   • Feasibility (0-10): Can I do this RIGHT NOW with my tools? Weight: ×2\n"
            f"   • Verifiability (0-10): Can I PROVE I did it? (test output, file exists) Weight: ×2\n"
            f"   • Personal Interest (0-10): Am I motivated to do this well? Weight: ×1\n"
            f"3. Pick the highest-scoring task. Show your scoring.\n"
            f"4. If a task matches Section 3 (Anti-Priorities) → score it 0 on all dimensions.\n\n"
            f"Format your scoring as:\n"
            f"CANDIDATE 1: <task> → OP:{{}}/10 WV:{{}}/10 F:{{}}/10 V:{{}}/10 I:{{}}/10 = TOTAL:{{}}\n"
            f"CANDIDATE 2: <task> → OP:{{}}/10 WV:{{}}/10 F:{{}}/10 V:{{}}/10 I:{{}}/10 = TOTAL:{{}}\n"
            f"CANDIDATE 3: <task> → OP:{{}}/10 WV:{{}}/10 F:{{}}/10 V:{{}}/10 I:{{}}/10 = TOTAL:{{}}\n"
            f"SELECTED: Candidate N (highest score)\n"
            f"{'=' * 60}\n"
        )

    # ── EVALUATE phase injection ───────────────────────────

    def get_eval_context(self) -> str:
        """
        Generate the prompt section injected into EVALUATE phase.
        Asks the agent to classify the heartbeat and assess value alignment.
        """
        budget = self.get_budget_status()
        total = budget["total"]
        if total > 0:
            budget_line = (
                f"Budget so far: {budget['duty']}D/{budget['growth']}G/{budget['exploration']}E "
                f"({budget['duty_pct']:.0%}/{budget['growth_pct']:.0%}/{budget['exploration_pct']:.0%})"
            )
        else:
            budget_line = "Budget: first heartbeat of the day."

        return (
            f"\n{'─' * 50}\n"
            f"🧭 VALUE ALIGNMENT CHECK (mandatory)\n"
            f"{budget_line}\n\n"
            f"Classify this heartbeat:\n"
            f"  HEARTBEAT_TYPE: <duty|growth|exploration>\n"
            f"  - duty = worked on operator priority or world-value goal from VALUES.md\n"
            f"  - growth = learned from past work, extracted patterns, improved capabilities\n"
            f"  - exploration = pursued personal interest from INTERESTS.md\n\n"
            f"  VALUE_ALIGNMENT: <0-10> How well did this heartbeat align with VALUES.md priorities?\n"
            f"  If duty but low alignment → explain why you chose this task over higher-value options.\n"
            f"  If exploration during a duty deficit → your score CANNOT exceed 2.\n"
            f"{'─' * 50}\n"
        )

    # ── Parse classification from eval text ────────────────

    @staticmethod
    def parse_heartbeat_type(eval_text: str) -> str:
        """Extract HEARTBEAT_TYPE from evaluation output."""
        if not eval_text:
            return "duty"  # safe default
        text_lower = eval_text.lower()
        # Look for explicit tag
        for line in text_lower.split("\n"):
            if "heartbeat_type:" in line:
                if "exploration" in line:
                    return "exploration"
                elif "growth" in line:
                    return "growth"
                elif "duty" in line:
                    return "duty"
        # Fallback heuristics
        if "self-evolution" in text_lower or "self_evolution" in text_lower:
            return "growth"
        if "exploration" in text_lower and "personal interest" in text_lower:
            return "exploration"
        return "duty"
