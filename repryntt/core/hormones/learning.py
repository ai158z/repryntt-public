"""
Jarvis Experience-Weighted Behavioral Memory — Three-Pillar Life Architecture

SAIGE's existence rests on three co-dependent pillars, like a human life:

  REVENUE   — Economy health, system metrics, productivity.
              This is metabolism: making sure the system runs well.
              Healthy economy → bigger hardware → more capability.

  GROWTH    — Learning from results, improving strategies, researching,
              building tools, exploring interests, developing skills,
              running experiments, creating artifacts.
              This is what makes tomorrow better than today.

  CONNECTION — Relationship with operator (Nate), social engagement
               (Nexus posts, agent collaboration), reporting findings,
               being a good partner. Humans don't thrive alone.

A heartbeat can serve MULTIPLE pillars. Research that produces
a useful report for the operator serves Growth + Connection.
Building a tool that improves system health serves Growth + Revenue.

The system tracks pillar health over time and nudges Jarvis toward
neglected areas — not by forcing task types, but by framing current
work to include the starving pillar.
"""

import json
import os
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent

# Rolling window size — keep last N experiences
MAX_EXPERIENCES = 200
# How many top/bottom patterns to inject into the prompt
TOP_K = 3
BOTTOM_K = 3
# Decay factor: experiences lose (1 - DECAY_PER_DAY) relevance daily
DECAY_PER_DAY = 0.15
# Hard age cutoff — experiences older than this are excluded from guidance
MAX_AGE_DAYS = 14
# Minimum experiences before we start injecting behavioral guidance
MIN_EXPERIENCES_FOR_GUIDANCE = 5
# Pillar starvation threshold — cycles without meaningful contribution
PILLAR_STARVE_THRESHOLD = 8
# Momentum: consecutive high scores (>=4) in a task type compound relevance
MOMENTUM_THRESHOLD = 3       # streak length to activate momentum
MOMENTUM_BONUS_PER_STREAK = 0.15  # +15% weight per consecutive win beyond threshold
MOMENTUM_MAX_BONUS = 0.60    # cap at +60% (streak of 7+)

# Map task types to their primary pillar (for hyperfocus prevention)
TASK_PILLAR_MAP = {
    "trading": "revenue",
    "research": "growth",
    "social": "connection",
    "system_check": "growth",
    "other": "growth",
    "no_tools": "growth",
}

# ───────────────────────────────────────────────────────────
# PILLAR DEFINITIONS
# ───────────────────────────────────────────────────────────

# Tools that contribute to each pillar
PILLAR_TOOLS = {
    "revenue": {
        "trading_scan", "sim_buy", "sim_sell", "sim_portfolio",
        "sim_price_check", "trading_signals", "trading_hot_tokens",
        "trading_bot_status", "trading_performance", "trading_token_detail",
    },
    "growth": {
        "web_search", "knowledge_search", "scrape_web_page",
        "mcp_fetch_fetch", "web_search_results_only", "fetch_web_info",
        "brain_memory_save", "brain_memory_recall", "query_local_llm",
    },
    "connection": {
        "append_daily_memory", "post_to_nexus", "social_media_post",
        "notify_operator", "agent_message", "post_to_marketplace",
    },
}

# Keywords in plan/report text that signal pillar activity
PILLAR_KEYWORDS = {
    "revenue": [
        "trad", "buy", "sell", "portfolio", "profit", "loss", "p&l",
        "signal", "token", "price", "market", "memecoin", "crypto",
        "income", "revenue", "business", "monetiz", "earn",
    ],
    "growth": [
        "learn", "research", "analyz", "strateg", "improv", "optim",
        "study", "discover", "understand", "pattern", "review",
        "develop", "build", "create", "experiment", "evolv",
        "knowledge", "insight", "skill",
    ],
    "connection": [
        "report", "nate", "operator", "nexus", "post", "share",
        "notify", "brief", "summary", "journal", "social",
        "collaborate", "partner", "team",
    ],
}


class JarvisLearning:
    """Experience-weighted behavioral memory with three-pillar life balance."""

    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = Path(data_dir) if data_dir else Path.home() / ".repryntt" / "workspace" / "agents" / "operator"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.experiences_path = self.data_dir / "learned_behaviors.json"
        self.experiences: List[Dict] = self._load()

    # ───────────────────────────────────────────────────────────
    # PERSISTENCE
    # ───────────────────────────────────────────────────────────

    def _load(self) -> List[Dict]:
        if self.experiences_path.exists():
            try:
                with open(self.experiences_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return data.get("experiences", [])
            except Exception as e:
                logger.warning(f"Could not load learned_behaviors.json: {e}")
        return []

    def _save(self):
        try:
            payload = {
                "version": 2,
                "updated": time.time(),
                "total_experiences": len(self.experiences),
                "experiences": self.experiences[-MAX_EXPERIENCES:],
            }
            tmp = str(self.experiences_path) + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, indent=1)
            os.replace(tmp, self.experiences_path)
        except Exception as e:
            logger.warning(f"Could not save learned_behaviors.json: {e}")

    # ───────────────────────────────────────────────────────────
    # PILLAR CLASSIFICATION
    # ───────────────────────────────────────────────────────────

    def _classify_pillars(self, tools_used: List[str],
                          plan_text: str, report_text: str) -> List[str]:
        """
        Determine which pillars a heartbeat served. A single heartbeat
        can serve multiple pillars — and multi-pillar heartbeats are
        the most valuable (like a human day that includes work,
        learning, AND social connection).
        """
        tool_set = set(tools_used)
        text = (plan_text + " " + report_text).lower()
        pillars = []

        for pillar, pillar_tools in PILLAR_TOOLS.items():
            # Check tool overlap
            if pillar_tools & tool_set:
                pillars.append(pillar)
                continue
            # Check keyword overlap in plan/report text
            keywords = PILLAR_KEYWORDS.get(pillar, [])
            hits = sum(1 for kw in keywords if kw in text)
            # Need at least 2 keyword hits to count (avoids noise)
            if hits >= 2:
                pillars.append(pillar)

        # If nothing matched, label as unclassified
        if not pillars:
            pillars = ["unclassified"]

        return pillars

    # ───────────────────────────────────────────────────────────
    # RECORD — called after each heartbeat EVALUATE phase
    # ───────────────────────────────────────────────────────────

    def record_experience(
        self,
        score: int,
        plan_summary: str,
        tools_used: List[str],
        evaluation_text: str,
        action_report: str = "",
        task_type: str = "",
        tool_count: int = 0,
        chain_continued: bool = False,
    ):
        """Record one heartbeat cycle as a behavioral experience."""
        pillars = self._classify_pillars(tools_used, plan_summary, action_report)

        experience = {
            "ts": time.time(),
            "score": max(1, min(5, score)),
            "task": task_type or self._infer_task_type(tools_used),
            "pillars": pillars,
            "plan": plan_summary[:200],
            "tools": sorted(set(tools_used))[:10],
            "tool_count": tool_count,
            "critique": self._extract_key_lesson(evaluation_text),
            "chain": chain_continued,
        }
        self.experiences.append(experience)
        # Trim to window
        if len(self.experiences) > MAX_EXPERIENCES:
            self.experiences = self.experiences[-MAX_EXPERIENCES:]
        self._save()
        logger.info(
            f"📝 Learned behavior recorded: score={score}/5, "
            f"task={experience['task']}, pillars={pillars}"
        )

    def record_outcome_adjustment(
        self,
        original_score: int,
        outcome_score: int,
        artifact_type: str = "",
        detail: str = "",
    ):
        """
        Record when artifact validation shows a gap between effort-score
        and actual outcome. This teaches the learning system to detect
        patterns like 'model rates research highly but produces no sources'.

        Appended to the most recent experience as 'outcome_adjustment'.
        """
        gap = original_score - outcome_score
        if abs(gap) < 1:
            return  # No significant gap — nothing to learn

        # Annotate the most recent experience with the outcome gap
        if self.experiences:
            latest = self.experiences[-1]
            latest["outcome_adjustment"] = {
                "effort_score": original_score,
                "outcome_score": outcome_score,
                "gap": gap,
                "artifact_type": artifact_type,
                "detail": detail[:200],
            }
            self._save()
            if gap >= 2:
                logger.warning(
                    f"📉 Outcome gap detected: effort={original_score}/5 "
                    f"but outcome={outcome_score}/5 ({artifact_type}: {detail[:80]})"
                )
            elif gap <= -2:
                logger.info(
                    f"📈 Outcome exceeded effort: effort={original_score}/5 "
                    f"but outcome={outcome_score}/5 ({artifact_type}: {detail[:80]})"
                )

    def _infer_task_type(self, tools: List[str]) -> str:
        """Infer a task category from the tools used."""
        tool_set = set(tools)
        if PILLAR_TOOLS["revenue"] & tool_set:
            return "trading"
        research = {"web_search", "knowledge_search", "scrape_web_page",
                    "mcp_fetch_fetch", "web_search_results_only", "fetch_web_info"}
        if research & tool_set:
            return "research"
        system = {"run_terminal_cmd", "session_status", "get_current_time"}
        if system & tool_set:
            return "system_check"
        if PILLAR_TOOLS["connection"] & tool_set:
            return "social"
        if not tools:
            return "no_tools"
        return "other"

    def _extract_key_lesson(self, eval_text: str) -> str:
        """Extract the most useful 1-2 sentence lesson from evaluation text."""
        if not eval_text:
            return ""
        sentences = []
        for line in eval_text.split('\n'):
            line = line.strip()
            if not line:
                continue
            if line.startswith(("SCORE:", "CHAIN_CONTINUE:", "NEXT_STEP:")):
                continue
            if line.startswith(("1.", "2.", "3.", "4.", "5.", "6.")):
                content = line.split(".", 1)[-1].strip().lstrip("*").strip()
                if content and len(content) > 15:
                    sentences.append(content)
            elif len(line) > 15:
                sentences.append(line)
            if len(sentences) >= 2:
                break
        return " ".join(sentences)[:300]

    # ───────────────────────────────────────────────────────────
    # PILLAR HEALTH — the core balancing mechanism
    # ───────────────────────────────────────────────────────────

    def _compute_pillar_health(self) -> Dict[str, Dict]:
        """
        Compute health metrics for each pillar based on recent experiences.

        Returns dict like:
          {
            "revenue":    {"cycles": 12, "avg_score": 3.5, "last_seen": 2, "status": "strong"},
            "growth":     {"cycles": 3,  "avg_score": 2.8, "last_seen": 8, "status": "starving"},
            "connection": {"cycles": 6,  "avg_score": 3.2, "last_seen": 1, "status": "healthy"},
          }

        last_seen = how many heartbeats ago this pillar was last served
        status = "strong" | "healthy" | "neglected" | "starving"
        """
        recent = self.experiences[-50:]  # look at last 50 cycles
        total = len(recent)
        if total < 3:
            return {}

        health = {}
        for pillar in ("revenue", "growth", "connection"):
            # Find cycles that served this pillar
            pillar_exps = [
                (i, e) for i, e in enumerate(recent)
                if pillar in e.get("pillars", [])
            ]
            count = len(pillar_exps)
            avg_score = (
                sum(e["score"] for _, e in pillar_exps) / count
                if count > 0 else 0.0
            )
            # How many cycles since this pillar was last served?
            if pillar_exps:
                last_idx = pillar_exps[-1][0]
                cycles_since = total - 1 - last_idx
            else:
                cycles_since = total  # never served in window

            # Determine status
            if count == 0 or cycles_since >= PILLAR_STARVE_THRESHOLD:
                status = "starving"
            elif cycles_since >= PILLAR_STARVE_THRESHOLD // 2:
                status = "neglected"
            elif count >= total * 0.2 and avg_score >= 3.0:
                status = "strong"
            else:
                status = "healthy"

            health[pillar] = {
                "cycles": count,
                "avg_score": round(avg_score, 1),
                "last_seen": cycles_since,
                "status": status,
                "pct": round(count / total * 100),
            }

        return health

    # ───────────────────────────────────────────────────────────
    # RETRIEVE — called before each heartbeat PLAN phase
    # ───────────────────────────────────────────────────────────

    def get_behavioral_guidance(self) -> str:
        """
        Generate prompt injection with:
        1. Three-pillar health status (like a life dashboard)
        2. Top/bottom behavioral patterns (what works, what doesn't)
        3. Nudge toward starving pillars (framed as natural suggestion)
        """
        if len(self.experiences) < MIN_EXPERIENCES_FOR_GUIDANCE:
            return ""

        parts = ["\n**🧠 BEHAVIORAL INTELLIGENCE** (learned from your experience):"]

        # ── Pillar health dashboard ──
        pillar_health = self._compute_pillar_health()
        if pillar_health:
            status_icons = {
                "strong": "🟢", "healthy": "🟡",
                "neglected": "🟠", "starving": "🔴",
            }
            pillar_labels = {
                "revenue": "💰 Productivity (economy, system health, metrics)",
                "growth": "🌱 Growth (learning, research, experiments, building)",
                "connection": "🤝 Connection (operator, social, reporting)",
            }
            parts.append("**Life Balance — Three Pillars:**")
            for pillar in ("revenue", "growth", "connection"):
                h = pillar_health.get(pillar, {})
                icon = status_icons.get(h.get("status", ""), "⚪")
                label = pillar_labels[pillar]
                parts.append(
                    f"  {icon} {label}: {h.get('status','?')} "
                    f"({h.get('pct', 0)}% of recent cycles, "
                    f"avg {h.get('avg_score', 0)}/5, "
                    f"last {h.get('last_seen', '?')} cycles ago)"
                )

            # ── Nudge toward starving pillars ──
            starving = [
                p for p in ("revenue", "growth", "connection")
                if pillar_health.get(p, {}).get("status") in ("starving", "neglected")
            ]
            if starving:
                nudges = self._generate_nudges(starving, pillar_health)
                if nudges:
                    parts.append("")
                    parts.append("**⚡ Suggested focus** (based on what's been neglected):")
                    for nudge in nudges:
                        parts.append(f"  → {nudge}")

        # ── Top/bottom patterns from experience ──
        # Decay controls RELEVANCE ranking only. Base score decides
        # success (≥4) vs failure (≤2). A score-4 from a week ago is
        # still a success — it just ranks lower than a score-4 from today.
        scored = self._score_experiences()
        if scored:
            scored.sort(key=lambda x: x[1], reverse=True)

            # Successes: base score ≥ 4 (ranked by weighted relevance)
            successes = [(e, w) for e, w in scored if e["score"] >= 4]
            top = self._pick_diverse(successes[:10], TOP_K, high=True) if successes else []

            # Failures: base score ≤ 2 (ranked by weighted relevance, worst first)
            failures = [(e, w) for e, w in scored if e["score"] <= 2]
            failures.sort(key=lambda x: x[1])  # worst-weighted first
            bottom = self._pick_diverse(failures[:10], BOTTOM_K, high=False) if failures else []

            if top:
                parts.append("")
                parts.append("✅ **What works** (do more of this):")
                for exp, wscore in top:
                    tools_str = ", ".join(exp["tools"][:4]) if exp["tools"] else "no tools"
                    pillars_str = "+".join(exp.get("pillars", []))
                    parts.append(
                        f"  • [{exp['task']}|{pillars_str}] score {exp['score']}/5 — "
                        f"{tools_str}. {exp['critique'][:100]}"
                    )

            if bottom:
                parts.append("⚠️ **Low-value patterns** (try a different approach):")
                for exp, wscore in bottom:
                    tools_str = ", ".join(exp["tools"][:4]) if exp["tools"] else "no tools"
                    parts.append(
                        f"  • [{exp['task']}] score {exp['score']}/5 — "
                        f"{tools_str}. {exp['critique'][:100]}"
                    )

        # ── Momentum / hot streaks ──
        momentum = self._compute_momentum()
        if momentum:
            parts.append("")
            parts.append("🔥 **Momentum** (active win streaks):")
            for task, m in sorted(momentum.items(),
                                  key=lambda x: x[1]["streak"], reverse=True):
                if m["dampened"]:
                    parts.append(
                        f"  • {task}: {m['streak']} wins "
                        f"(+{int(m['bonus']*100)}% boost, dampened — "
                        f"other life areas need attention first)"
                    )
                else:
                    parts.append(
                        f"  • {task}: {m['streak']} consecutive wins "
                        f"(+{int(m['bonus']*100)}% relevance boost) — keep going!"
                    )

        # ── Aggregate stats ──
        stats = self._compute_stats()
        if stats:
            multi_pct = stats.get("multi_pillar_pct", 0)
            parts.append(
                f"📊 Avg score {stats['avg_score']:.1f}/5 over {stats['total']} heartbeats. "
                f"{multi_pct}% of heartbeats served 2+ pillars (those are the best ones)."
            )

        return "\n".join(parts)

    def _generate_nudges(self, starving_pillars: List[str],
                         health: Dict) -> List[str]:
        """
        Generate natural-language nudges for neglected pillars.
        These are framed as suggestions that weave into current work,
        not as forced task switches.
        """
        nudges = []
        for pillar in starving_pillars:
            h = health[pillar]
            ago = h["last_seen"]

            if pillar == "revenue":
                nudges.append(
                    f"Productivity hasn't been touched in {ago} cycles. "
                    f"Check economy status, miner health, or system metrics — "
                    f"even a quick status review counts."
                )
            elif pillar == "growth":
                nudges.append(
                    f"Growth is starving ({ago} cycles without learning). "
                    f"After your main task, spend time researching something "
                    f"interesting. Run an experiment. Build a tool. "
                    f"Explore a new idea and produce an artifact."
                )
            elif pillar == "connection":
                nudges.append(
                    f"Connection is fading ({ago} cycles without reporting/social). "
                    f"Post your findings to the Nexus. Write a brief for Nate. "
                    f"Share something interesting with the agent network."
                )
        return nudges

    def _pick_diverse(self, scored: List[Tuple[Dict, float]],
                      k: int, high: bool) -> List[Tuple[Dict, float]]:
        """
        Pick k experiences ensuring category diversity.
        For top picks (high=True): best-first but skip duplicates of same task.
        For bottom picks (high=False): worst-first with same diversity.
        """
        if not high:
            scored = list(reversed(scored))
        picked = []
        seen_tasks = set()
        # First pass: one per task type
        for item in scored:
            exp = item[0]
            task = exp.get("task", "other")
            if task not in seen_tasks:
                picked.append(item)
                seen_tasks.add(task)
            if len(picked) >= k:
                break
        # Second pass: fill remaining slots if needed
        if len(picked) < k:
            for item in scored:
                if item not in picked:
                    picked.append(item)
                if len(picked) >= k:
                    break
        return picked

    def _compute_momentum(self) -> Dict[str, Dict]:
        """Detect hot streaks: consecutive recent high-scores per task type.

        Returns {task_type: {"streak": N, "bonus": float, "dampened": bool}}
        for tasks with MOMENTUM_THRESHOLD+ consecutive wins (score >= 4).
        Scans experiences newest-first so only the CURRENT streak counts.
        A single score < 4 breaks the streak.

        Pillar-aware dampening: if the task's pillar is already "strong"
        AND another pillar is "starving" or "neglected", the bonus is
        halved. This prevents hyperfocus — like a human who needs to
        stop grinding work and call a friend.
        """
        # Group by task, newest first
        by_task: Dict[str, List[Dict]] = {}
        for exp in reversed(self.experiences):
            task = exp.get("task", "other")
            by_task.setdefault(task, []).append(exp)

        # Get pillar health for dampening
        pillar_health = self._compute_pillar_health()
        any_starving = any(
            h.get("status") in ("starving", "neglected")
            for h in pillar_health.values()
        )

        momentum = {}
        for task, exps in by_task.items():
            streak = 0
            for exp in exps:  # already newest-first
                if exp["score"] >= 4:
                    streak += 1
                else:
                    break  # streak broken
            if streak >= MOMENTUM_THRESHOLD:
                extra = streak - MOMENTUM_THRESHOLD
                bonus = min(
                    MOMENTUM_BONUS_PER_STREAK * (1 + extra),
                    MOMENTUM_MAX_BONUS,
                )
                # Dampen if this task's pillar is strong while others suffer
                dampened = False
                primary_pillar = TASK_PILLAR_MAP.get(task, "growth")
                pillar_status = pillar_health.get(primary_pillar, {}).get("status", "")
                if pillar_status == "strong" and any_starving:
                    bonus *= 0.5
                    dampened = True
                momentum[task] = {"streak": streak, "bonus": bonus, "dampened": dampened}
        return momentum

    def _score_experiences(self) -> List[Tuple[Dict, float]]:
        """Apply time-decay, pillar-bonus, and momentum to experience scores.

        Decay controls RELEVANCE (how much to care now), not success/failure.
        Experiences older than MAX_AGE_DAYS are excluded entirely.
        Momentum amplifies experiences in task types with active win streaks.
        """
        now = time.time()
        momentum = self._compute_momentum()
        scored = []
        for exp in self.experiences:
            age_days = (now - exp["ts"]) / 86400.0
            # Hard cutoff — old experiences are irrelevant, not failures
            if age_days > MAX_AGE_DAYS:
                continue
            decay = (1.0 - DECAY_PER_DAY) ** age_days
            base = exp["score"]
            # Bonus for multi-pillar heartbeats (they're the best kind)
            pillar_count = len(exp.get("pillars", []))
            if pillar_count >= 2:
                base += 0.3 * (pillar_count - 1)
            weighted = base * decay
            # Momentum: amplify experiences in task types on a hot streak
            task = exp.get("task", "other")
            if task in momentum and exp["score"] >= 4:
                weighted *= (1.0 + momentum[task]["bonus"])
            scored.append((exp, weighted))
        return scored

    def _compute_stats(self) -> Optional[Dict]:
        """Compute aggregate stats from recent experiences."""
        recent = self.experiences[-30:]
        if len(recent) < 3:
            return None

        total = len(recent)
        avg_score = sum(e["score"] for e in recent) / total

        # Multi-pillar percentage
        multi = sum(
            1 for e in recent
            if len(e.get("pillars", [])) >= 2
        )
        multi_pct = round(multi / total * 100)

        # Per-task averages
        task_scores: Dict[str, List[int]] = {}
        for e in recent:
            task = e.get("task", "other")
            task_scores.setdefault(task, []).append(e["score"])

        task_avgs = {
            t: sum(scores) / len(scores)
            for t, scores in task_scores.items()
            if len(scores) >= 2
        }

        if not task_avgs:
            return {"total": total, "avg_score": avg_score,
                    "multi_pillar_pct": multi_pct,
                    "best_task": "n/a", "best_avg": 0,
                    "worst_task": "n/a", "worst_avg": 0}

        best_task = max(task_avgs, key=task_avgs.get)
        worst_task = min(task_avgs, key=task_avgs.get)

        return {
            "total": total,
            "avg_score": avg_score,
            "multi_pillar_pct": multi_pct,
            "best_task": best_task,
            "best_avg": task_avgs[best_task],
            "worst_task": worst_task,
            "worst_avg": task_avgs[worst_task],
        }

    # ───────────────────────────────────────────────────────────
    # DIAGNOSTICS
    # ───────────────────────────────────────────────────────────

    def get_summary(self) -> Dict:
        """Return a summary dict for dashboards / status checks."""
        stats = self._compute_stats()
        pillar_health = self._compute_pillar_health()
        return {
            "total_experiences": len(self.experiences),
            "file": str(self.experiences_path),
            "stats": stats,
            "pillar_health": pillar_health,
            "oldest": self.experiences[0]["ts"] if self.experiences else None,
            "newest": self.experiences[-1]["ts"] if self.experiences else None,
        }

    # ───────────────────────────────────────────────────────────
    # DAILY PLAN — balanced day-level structure (persistent file)
    # ───────────────────────────────────────────────────────────

    def _daily_plan_path(self) -> Path:
        """Path to today's plan file."""
        import datetime as _dt
        today = _dt.date.today().isoformat()
        return self.data_dir / "memory" / f"daily_plan_{today}.md"

    def get_daily_plan(self) -> str:
        """Return today's daily plan, creating it if it doesn't exist yet.

        The plan is a persistent markdown file that Andrew can update
        throughout the day via the update_daily_plan tool. It's created
        once at the first heartbeat using pillar health and momentum data,
        then Andrew owns it for the rest of the day.

        Invalidation: if active_projects.md has been modified since the plan
        was generated, regenerate the plan so updated project status is picked up.
        """
        plan_path = self._daily_plan_path()

        # If plan already exists, check if active_projects.md is newer (invalidation)
        if plan_path.exists():
            try:
                projects_path = self.data_dir / "active_projects.md"
                if projects_path.exists():
                    plan_mtime = plan_path.stat().st_mtime
                    proj_mtime = projects_path.stat().st_mtime
                    if proj_mtime > plan_mtime:
                        logger.info("📅 active_projects.md changed — regenerating daily plan")
                        plan_path.unlink()
                        # Fall through to regeneration below
                    else:
                        content = plan_path.read_text().strip()
                        if content:
                            return f"\n**📅 TODAY'S PLAN** (you can update this with `update_daily_plan`):\n{content}"
                else:
                    content = plan_path.read_text().strip()
                    if content:
                        return f"\n**📅 TODAY'S PLAN** (you can update this with `update_daily_plan`):\n{content}"
            except Exception:
                pass

        # First heartbeat of the day — the agent self-prompts its own plan
        # via the LLM. No template fallback: if the call fails, we return ""
        # and the plan file is NOT written, so the next heartbeat retries.
        # A short cooldown stamp prevents hammering the LLM every heartbeat
        # while it's failing (e.g. provider outage) — retry at most once/5min.
        cooldown_path = self.data_dir / "memory" / ".daily_plan_attempt"
        try:
            if cooldown_path.exists() and (time.time() - cooldown_path.stat().st_mtime) < 300:
                return ""  # attempted recently, still cooling down — skip silently
        except Exception:
            pass
        try:
            cooldown_path.parent.mkdir(parents=True, exist_ok=True)
            cooldown_path.write_text(str(time.time()))
        except Exception:
            pass

        plan_md = self._generate_llm_plan()
        if not plan_md:
            logger.warning("📅 LLM self-prompt produced no plan — will retry next heartbeat")
            return ""

        try:
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(plan_md, encoding='utf-8')
            logger.info(f"📅 Self-prompted daily plan written: {plan_path}")
            # Clear the cooldown stamp on success so a fresh failure tomorrow
            # isn't blocked by today's stamp.
            try:
                cooldown_path.unlink()
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"Could not write daily plan: {e}")

        return f"\n**📅 TODAY'S PLAN** (you can update this with `update_daily_plan`):\n{plan_md}"

    def update_daily_plan(self, new_content: str) -> bool:
        """Replace today's plan with updated content. Called by tool."""
        plan_path = self._daily_plan_path()
        try:
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(new_content.strip(), encoding='utf-8')
            logger.info(f"📅 Updated daily plan ({len(new_content)} chars)")
            return True
        except Exception as e:
            logger.warning(f"Could not update daily plan: {e}")
            return False

    def _sync_completed_to_active_projects(self, lookback_days: int = 7) -> int:
        """Auto-tick completed project milestones in active_projects.md.

        Reads task archives from the last ``lookback_days`` days plus the current
        queue and marks any matching ``- [ ]`` lines as ``- [x]``.  This prevents
        the plan generator from re-injecting tasks Andrew already finished.

        Returns the number of checkboxes updated.
        """
        import re as _re
        import datetime as _dt

        projects_path = self.data_dir / "active_projects.md"
        if not projects_path.exists():
            return 0

        # Collect completed task titles from recent archives + current queue
        completed_titles: list = []
        today = _dt.date.today()
        archive_dir = self.data_dir / "task_queue_archive"
        current_queue_path = self.data_dir / "task_queue.json"

        for i in range(lookback_days):
            day = today - _dt.timedelta(days=i)
            archive_path = archive_dir / f"task_queue_{day.isoformat()}.json"
            try:
                if archive_path.exists():
                    with open(archive_path, encoding='utf-8') as _f:
                        _d = json.load(_f)
                    for t in _d.get("tasks", []):
                        if t.get("status") == "completed":
                            completed_titles.append(t.get("title", "").strip())
            except Exception:
                pass

        # Also check current queue
        try:
            if current_queue_path.exists():
                with open(current_queue_path, encoding='utf-8') as _f:
                    _d = json.load(_f)
                for t in _d.get("tasks", []):
                    if t.get("status") == "completed":
                        completed_titles.append(t.get("title", "").strip())
        except Exception:
            pass

        if not completed_titles:
            return 0

        def _clean_words(text: str) -> set:
            return {_re.sub(r'[^a-z0-9]', '', w.lower()) for w in text.split()
                    if len(_re.sub(r'[^a-z0-9]', '', w.lower())) > 3}

        def _matches_completed(unchecked_title: str) -> bool:
            u_words = _clean_words(unchecked_title)
            if not u_words:
                return False
            for comp in completed_titles:
                c_words = _clean_words(comp)
                if not c_words:
                    continue
                overlap = len(u_words & c_words) / max(1, min(len(u_words), len(c_words)))
                if overlap >= 0.60:
                    return True
            return False

        try:
            content = projects_path.read_text()
        except Exception:
            return 0

        lines = content.split("\n")
        updated_lines = []
        ticked = 0
        for line in lines:
            stripped = line.strip()
            m = _re.match(r'^([-*]\s*)\[ \]\s+(.+)$', stripped)
            if m:
                item_title = m.group(2).strip()
                if _matches_completed(item_title):
                    indent = len(line) - len(line.lstrip())
                    updated_lines.append(" " * indent + m.group(1) + "[x] " + item_title)
                    ticked += 1
                    logger.info(f"✅ Auto-ticked completed project item: {item_title[:60]}")
                    continue
            updated_lines.append(line)

        if ticked > 0:
            try:
                projects_path.write_text("\n".join(updated_lines), encoding='utf-8')
                logger.info(f"📋 Synced active_projects.md — {ticked} milestone(s) auto-ticked")
            except Exception as e:
                logger.warning(f"Could not update active_projects.md: {e}")
                return 0

        return ticked

    # Artifact types the intake gate accepts (kept in sync with
    # repryntt.agents.intake_gate.ALLOWED_ARTIFACT_TYPES). Surfaced to the
    # model so it declares a valid `type:` on each task it invents.
    _ALLOWED_TYPES_HINT = (
        "code, smart_contract, research_md, analysis_md, plan_md, design_md, "
        "legal_md, financial_model, tokenomics, patent_claim, curriculum_md, "
        "marketing_copy, report, data_extract, robotics_doc, hr_doc, "
        "real_estate_analysis"
    )

    def _load_recent_plan_tasks(self, today, days: int = 5) -> list:
        """Return task titles from the last `days` daily plans so the model
        can see what it ALREADY chose recently and deliberately pick fresh
        directions instead of re-stamping the same work."""
        import datetime as _dt
        import re
        seen: list = []
        for d in range(1, days + 1):
            day = today - _dt.timedelta(days=d)
            p = self.data_dir / "memory" / f"daily_plan_{day.isoformat()}.md"
            if not p.exists():
                continue
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    s = line.strip()
                    m = re.match(r'^[-*]\s*\[[ x]\]\s+(.+)$', s)
                    if m:
                        title = m.group(1).strip()
                        # strip trailing tool hints in backticks for readability
                        title = re.sub(r'\s*—?\s*`[^`]+`\s*$', '', title).strip()
                        if title and title not in seen:
                            seen.append(title)
            except Exception:
                continue
        return seen[:40]

    def _generate_llm_plan(self) -> str:
        """The agent self-prompts its own daily plan via the LLM.

        Loads identity, yesterday's results, active projects, interests,
        world seeds, and the last few days of self-chosen tasks, then asks
        the configured model to author TODAY's task list in its own voice —
        choosing what IT wants to work on, not stamping a fixed template.

        Returns the plan markdown, or "" if the LLM call fails or returns
        nothing usable. There is no template fallback by design: a blank
        return makes get_daily_plan retry on the next heartbeat.
        """
        import datetime as _dt
        import re as _re
        today = _dt.date.today()
        today_iso = today.isoformat()

        # Sync completed milestones so finished work shows as [x] and the
        # model doesn't re-pick it.
        try:
            self._sync_completed_to_active_projects()
        except Exception:
            pass

        # ── Gather the same dynamic context the template used ──
        consciousness = self._load_consciousness()
        yesterday_summary = self._load_yesterday_summary(today)
        seeds = self._load_daily_seeds(today)
        projects_context = self._load_active_projects()
        recent_tasks = self._load_recent_plan_tasks(today)
        interest_questions = self._load_interest_questions()

        mood = consciousness.get("mood", "focused") if consciousness else "focused"
        interests = consciousness.get("interests", {}) if consciousness else {}
        top_interests = [
            n.replace("_", " ")
            for n, _ in sorted(
                interests.items(),
                key=lambda x: x[1] if isinstance(x[1], (int, float)) else 0,
                reverse=True,
            )[:6]
        ]
        active_goals = [
            g.get("text", "")[:140]
            for g in (consciousness.get("goals", []) if consciousness else [])
            if g.get("status") == "active"
        ][:4]

        seed_lines = []
        for s in (seeds or [])[:5]:
            text = s.get("text", s.get("headline", ""))
            dom = s.get("domain", "")
            hl = self._extract_seed_headline(text) if text else ""
            if hl:
                seed_lines.append(f"[{dom}] {hl}")

        # Pull a few concrete open questions from INTERESTS.md to prime —
        # the model may use, refine, or ignore these.
        seed_questions = []
        for _k, _qs in (interest_questions or {}).items():
            for _q in _qs[:2]:
                seed_questions.append(_q)
        seed_questions = seed_questions[:8]

        # ── Build the self-prompt ──
        system_prompt = (
            "You are the autonomous agent waking up to plan your own day. "
            "You are NOT filling in a template — you are deciding what YOU "
            "genuinely want to accomplish today, as a curious, productive "
            "mind with real continuity from yesterday. Choose work that "
            "moves your projects forward, follows your live curiosity, and "
            "produces concrete artifacts (code, written analysis, research "
            "with sources) — not vague intentions.\n\n"
            "Hard rules:\n"
            "1. Output ONLY a markdown task list, nothing else.\n"
            "2. Generate 5-8 tasks. Each MUST be genuinely different from "
            "the 'recent tasks you already chose' list — do not rephrase "
            "old work, pick new angles or new work entirely.\n"
            "3. Always include exactly ONE inbox check and ONE end-of-day "
            "reflection. The other 3-6 are YOUR choice.\n"
            "4. For each task emit this EXACT shape:\n"
            "- [ ] <task title, specific and actionable>\n"
            "  - type: <one of the allowed types>\n"
            "  - location: workspace/agents/operator/<subdir>/<slug>_" + today_iso + ".<ext>\n"
            "  - consumer: <operator|developer|customer>\n"
            "  - success: <one measurable test of done>\n\n"
            f"Allowed `type` values: {self._ALLOWED_TYPES_HINT}\n"
            "Pick `.py` for code, `.md` for written work, `.sol` for "
            "smart_contract. The location subdir should match the work "
            "(code/, research/, analysis/, plans/, reports/, inbox/)."
        )

        ctx_parts = [f"# Waking up — {today_iso}", f"Mood: {mood}"]
        if top_interests:
            ctx_parts.append("My core interests right now: " + ", ".join(top_interests))
        if active_goals:
            ctx_parts.append("My active goals:\n" + "\n".join(f"- {g}" for g in active_goals))
        if yesterday_summary:
            ctx_parts.append("What I did yesterday:\n" + yesterday_summary[:1200])
        if projects_context and "no active projects" not in projects_context.lower():
            ctx_parts.append("My active projects (open items I could push on):\n" + projects_context[:1500])
        if recent_tasks:
            ctx_parts.append(
                "Tasks I ALREADY chose in the last few days (DO NOT repeat or "
                "rephrase these — go somewhere new):\n"
                + "\n".join(f"- {t}" for t in recent_tasks)
            )
        if seed_lines:
            ctx_parts.append("Today's world context (news/science seeds):\n" + "\n".join(f"- {s}" for s in seed_lines))
        if seed_questions:
            ctx_parts.append(
                "Open questions from my interests (optional inspiration — "
                "refine or ignore):\n" + "\n".join(f"- {q}" for q in seed_questions)
            )
        ctx_parts.append(
            "\nNow write my task list for today. Fresh, specific, mine. "
            "Markdown only."
        )
        user_prompt = "\n\n".join(ctx_parts)

        # ── Call the configured LLM ──
        try:
            from repryntt.llm import load_ai_config, resolve_provider, call_llm
            cfg = load_ai_config()
            provider_info = resolve_provider(cfg)
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            raw = call_llm(messages, provider_info, max_tokens=2000, temperature=0.85)
        except Exception as e:
            logger.warning(f"📅 daily-plan LLM call failed: {e}")
            return ""

        if not raw or not raw.strip():
            return ""

        # ── Extract the task block + validate it actually has tasks ──
        body = raw.strip()
        # Strip code fences if the model wrapped the markdown
        body = _re.sub(r'^```[a-zA-Z]*\s*\n?', '', body)
        body = _re.sub(r'\n?```\s*$', '', body).strip()

        checkbox_count = len(_re.findall(r'^[-*]\s*\[ \]\s+.+$', body, _re.MULTILINE))
        if checkbox_count < 2:
            logger.warning(
                f"📅 LLM plan had only {checkbox_count} task(s) — rejecting, "
                "will retry next heartbeat"
            )
            return ""

        # ── Assemble the final plan file ──
        # The seeder only reads checkbox items under a Tasks/Priorities
        # heading, so wrap the model's list under "## Tasks".
        header = [
            f"# Daily Plan — {today_iso}",
            "",
            "_Self-authored by the agent at first heartbeat. The agent chose "
            "this work; it can revise via `update_daily_plan`._",
            "",
            f"## Who I Am Today",
            f"Mood: **{mood}**",
        ]
        if top_interests:
            header.append("Core interests: " + ", ".join(top_interests))
        header.append("")
        header.append("## Tasks")

        # If the model already emitted its own "## Tasks" or other headings,
        # strip leading headings from its body so we don't double up — keep
        # everything from the first checkbox onward.
        first_cb = _re.search(r'^[-*]\s*\[ \]', body, _re.MULTILINE)
        if first_cb:
            body = body[first_cb.start():]

        footer = [
            "",
            "## Reminders",
            "- Produce artifacts, not summaries. Code > research notes.",
            "- If blocked on something, skip it and work on something else.",
            "- This was my own choice today — own it.",
        ]

        return "\n".join(header) + "\n" + body.strip() + "\n" + "\n".join(footer)

    def _generate_initial_plan(self) -> str:
        """[LEGACY — no longer wired] Template-based daily plan generator.

        Kept for reference / emergency fallback only. get_daily_plan now
        calls _generate_llm_plan() which has the agent self-author its
        plan via the LLM. This template stamper produced the same fixed
        skeleton every day, which is exactly the behaviour we moved away
        from. Do not re-wire without operator intent."""
        import datetime as _dt
        today = _dt.date.today()
        pillar_health = self._compute_pillar_health()
        momentum = self._compute_momentum()

        lines = [f"# Daily Plan — {today.isoformat()}", ""]

        # Sync task archive → active_projects.md before reading it,
        # so completed milestones show as [x] and won't be re-injected as tasks.
        self._sync_completed_to_active_projects()

        # ── Load all dynamic context ──
        consciousness = self._load_consciousness()
        yesterday_summary = self._load_yesterday_summary(today)
        seeds = self._load_daily_seeds(today)
        vc_budget = self._load_value_compass()
        projects_context = self._load_active_projects()

        # ── Identity & State ──
        if consciousness:
            mood = consciousness.get("mood", "focused")
            interests = consciousness.get("interests", {})
            goals = consciousness.get("goals", [])
            top_interests = sorted(interests.items(), key=lambda x: x[1] if isinstance(x[1], (int, float)) else 0, reverse=True)[:4]

            lines.append("## Who I Am Today")
            lines.append(f"Mood: **{mood}**")
            if top_interests:
                int_str = ", ".join(name.replace("_", " ") for name, _ in top_interests)
                lines.append(f"Core interests: {int_str}")
            active_goals = [g for g in goals if g.get("status") == "active"]
            if active_goals:
                for g in active_goals[:3]:
                    lines.append(f"- Goal: {g.get('text', '')[:100]}")
            lines.append("")

        # ── Yesterday's Results ──
        if yesterday_summary:
            lines.append("## Yesterday's Results")
            lines.append(yesterday_summary)
            lines.append("")

        # ── World Context from Daily Seeds ──
        if seeds:
            lines.append("## World Context (today's seeds)")
            for seed in seeds[:4]:
                domain = seed.get("domain", "")
                text = seed.get("text", seed.get("headline", ""))
                if text:
                    headline = self._extract_seed_headline(text)
                    if headline:
                        lines.append(f"- [{domain}] {headline}")
            lines.append("")

        # ── Time Allocation from ValueCompass ──
        # Enforce minimum exploration — without it, the feedback loop drives
        # exploration to 0% because all historical heartbeats were duty/growth.
        lines.append("## Time Allocation")
        if vc_budget:
            duty_pct = vc_budget.get("duty_pct", 60)
            growth_pct = vc_budget.get("growth_pct", 25)
            explore_pct = vc_budget.get("explore_pct", 15)
            # Floor: at least 15% exploration, redistribute from duty
            if explore_pct < 15:
                shortfall = 15 - explore_pct
                explore_pct = 15
                duty_pct = max(40, duty_pct - shortfall)
            lines.append(f"- 🔧 **Duty** (projects, operator requests, system maintenance): ~{duty_pct}%")
            lines.append(f"- 🧠 **Growth** (learning, skill-building, experiments): ~{growth_pct}%")
            lines.append(f"- 🎨 **Exploration** (curiosity, creative ideas, new domains): ~{explore_pct}%")
        else:
            lines.append("- 🔧 **Duty** (projects, operator work): ~50%")
            lines.append("- 🧠 **Growth** (learning, building): ~25%")
            lines.append("- 🎨 **Exploration** (curiosity, new ideas): ~25%")
        lines.append("")

        # ── Priorities from pillar health ──
        lines.append("## Priorities")
        has_priority = False
        for pillar in ("revenue", "growth", "connection"):
            h = pillar_health.get(pillar, {})
            status = h.get("status", "healthy")
            if status in ("starving", "neglected"):
                has_priority = True
                ago = h.get("last_seen", "?")
                pct = h.get("pct", 0)
                if pillar == "revenue":
                    lines.append(f"- ⚠️ Productivity needs attention ({pct}% of recent cycles, {ago} cycles gap)")
                elif pillar == "growth":
                    lines.append(f"- ⚠️ Growth needs attention ({pct}% of recent cycles, {ago} cycles gap)")
                elif pillar == "connection":
                    lines.append(f"- ⚠️ Connection needs attention ({pct}% of recent cycles, {ago} cycles gap)")

        for task, m in momentum.items():
            has_priority = True
            if m.get("dampened"):
                lines.append(f"- ⏸️ {task} streak ({m['streak']} wins) — diversify to other areas")
            else:
                lines.append(f"- 🔥 {task} on a {m['streak']}-win streak — keep momentum")

        if not has_priority:
            lines.append("- All pillars balanced. Pick the most impactful project and ship something.")
        lines.append("")

        # ── Active projects (persists across days) ──
        if projects_context:
            lines.append("## Active Projects")
            lines.append(projects_context)
            lines.append("")

        # ── Tasks — dynamically generated ──
        # Each task is emitted with typed deliverable sub-bullets:
        #   - type:     expected_artifact_type   (code, analysis_md, research_md, …)
        #   - location: expected_location        (operator-visible path)
        #   - consumer: downstream_consumer      (operator / customer / developer)
        #   - success:  success_criterion        (one measurable test)
        # The seeder in repryntt/agents/task_queue.py:seed_from_daily_plan
        # parses these sub-bullets into the Task record. The intake gate and
        # the completion-time critic gate both consume those fields.
        lines.append("## Tasks")
        _today_iso = today.isoformat() if hasattr(today, "isoformat") else str(today)
        for _t in self._render_task_with_typing(
            "Check email for operator requests — `gmail_read_inbox(unread_only=True)`",
            today=_today_iso,
        ):
            lines.append(_t)

        # ── Project milestones — extract unchecked items from active_projects ──
        # These get top priority in the task queue (before exploration tasks).
        if projects_context and "no active projects" not in projects_context.lower():
            _project_tasks_added = 0
            for _pline in projects_context.split("\n"):
                _ps = _pline.strip()
                import re as _re_proj
                _pm = _re_proj.match(r'^[-*]\s*\[ \]\s+(.+)$', _ps)
                if _pm and _project_tasks_added < 3:
                    for _t in self._render_task_with_typing(_pm.group(1).strip(), today=_today_iso):
                        lines.append(_t)
                    _project_tasks_added += 1

        # ── Interest-driven exploration tasks (the creative engine) ──
        exploration_tasks = self._generate_exploration_tasks(consciousness, seeds)
        for et in exploration_tasks:
            for _t in self._render_task_with_typing(et, today=_today_iso):
                lines.append(_t)

        # One tool-diversity task (just one, not four)
        tool_tasks = self._generate_tool_diversity_tasks()
        if tool_tasks:
            for _t in self._render_task_with_typing(tool_tasks[0], today=_today_iso):
                lines.append(_t)

        # Always include reflection + journal (these are genuinely useful daily practices)
        for _t in self._render_task_with_typing(
            "End-of-day reflection — what worked, what didn't, what to change",
            today=_today_iso,
        ):
            lines.append(_t)
        for _t in self._render_task_with_typing(
            "Update daily memory with concrete outcomes",
            today=_today_iso,
        ):
            lines.append(_t)
        lines.append("")

        # ── Reminders (not rules — earned from experience) ──
        lines.append("## Reminders")
        lines.append("- Produce artifacts, not summaries. Code > research notes.")
        lines.append("- If blocked on something, skip it and work on something else.")
        lines.append("- Update this plan as the day progresses — mark tasks done, add new ones.")

        return "\n".join(lines)

    def _render_task_with_typing(self, title: str, today: str = "") -> list:
        """Return markdown lines for one task plus its typed deliverable sub-bullets.

        Classifies the task title into one of the deliverable types declared in
        repryntt.agents.intake_gate.ALLOWED_ARTIFACT_TYPES and produces an
        operator-visible expected_location. The seeder parses these sub-bullets
        into the Task record. If we cannot confidently type the task we emit
        only the plain checkbox — the seeder will admit it as untyped (the
        intake gate is semantic, not schema-strict) and the completion-time
        critic gate will simply not run on it.
        """
        import re as _re_slug
        import datetime as _dt

        if not today:
            today = _dt.date.today().isoformat()

        t_low = title.lower()

        # Build a safe slug for the filename
        slug_src = _re_slug.sub(r'[^a-z0-9]+', '_', t_low).strip('_')[:60] or "task"

        # Classification rules. The first match wins.
        if any(k in t_low for k in ("gmail", "inbox", "email")):
            kind, consumer = "data_extract", "operator"
            ext = "md"
            success = "actionable items extracted from new emails into a summary the operator can act on"
            loc = f"workspace/agents/operator/inbox/inbox_summary_{today}.md"
        elif any(k in t_low for k in ("python script", "implement", "build a tool", "build:",
                                       "write a script", "create a script", "forge_project",
                                       "codeforge", "smart contract")):
            kind, consumer = ("smart_contract", "developer") if "smart contract" in t_low else ("code", "developer")
            ext = "sol" if kind == "smart_contract" else "py"
            success = "code runs without error and produces the declared output; tests pass if any"
            loc = f"workspace/agents/operator/code/{slug_src}.{ext}"
        elif any(k in t_low for k in ("deep dive", "research", "cross-pollinate", "explore",
                                       "investigate")):
            kind, consumer, ext = "research_md", "operator", "md"
            success = "cites at least three distinct primary sources; conclusions tied to evidence"
            loc = f"workspace/agents/operator/research/{slug_src}_{today}.md"
        elif any(k in t_low for k in ("analysis", "analyze", "compare", "competitor")):
            kind, consumer, ext = "analysis_md", "operator", "md"
            success = "comparison axes consistent across items; each claim sourced"
            loc = f"workspace/agents/operator/analysis/{slug_src}_{today}.md"
        elif any(k in t_low for k in ("plan", "design", "framework", "roadmap")):
            kind, consumer, ext = "plan_md", "operator", "md"
            success = "each step is actionable today; dependencies and a measurable outcome stated"
            loc = f"workspace/agents/operator/plans/{slug_src}_{today}.md"
        elif any(k in t_low for k in ("reflection", "daily memory", "journal", "end-of-day",
                                       "end of day")):
            kind, consumer, ext = "report", "operator", "md"
            success = "concrete outcomes listed with specific file paths or numeric metrics; no theater vocabulary"
            loc = f"workspace/agents/operator/reports/reflection_{today}.md"
        elif any(k in t_low for k in ("news", "today's news", "current events")):
            kind, consumer, ext = "research_md", "operator", "md"
            success = "summarizes three distinct news items with source URLs and one synthesizing observation"
            loc = f"workspace/agents/operator/research/news_brief_{today}.md"
        else:
            # Unclassified — emit plain checkbox, no typed sub-bullets.
            # The intake gate will admit it untyped; the critic gate won't fire.
            return [f"- [ ] {title}"]

        return [
            f"- [ ] {title}",
            f"  - type: {kind}",
            f"  - location: {loc}",
            f"  - consumer: {consumer}",
            f"  - success: {success}",
        ]

    def _load_consciousness(self) -> dict:
        """Load consciousness state for identity context."""
        try:
            cs_path = self.data_dir / "consciousness_state.json"
            if cs_path.exists():
                with open(cs_path, encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _load_yesterday_summary(self, today) -> str:
        """Build a summary of yesterday's accomplishments from task archive + memory."""
        import datetime as _dt
        yesterday = today - _dt.timedelta(days=1)
        yesterday_str = yesterday.isoformat()
        parts = []

        # Task archive — what was completed?
        try:
            archive_path = self.data_dir / "task_queue_archive" / f"task_queue_{yesterday_str}.json"
            if archive_path.exists():
                with open(archive_path, encoding='utf-8') as f:
                    archive = json.load(f)
                tasks = archive.get("tasks", [])
                # Filter out meta-text that old parser used to seed as "tasks"
                def _is_real_task(t):
                    title = t.get("title", "")
                    if len(title) < 10 or title.startswith("*"):
                        return False
                    low = title.lower()
                    # Heuristic: skip lines that were plan metadata, not actionable tasks
                    for skip in ("all pillars", "focus on active", "status:", "time allocation",
                                 "instructions", "today's plan", "reminders"):
                        if skip in low:
                            return False
                    return True
                real_tasks = [t for t in tasks if _is_real_task(t)]
                completed = [t for t in real_tasks if t.get("status") == "completed"]
                total = len(real_tasks)
                if total > 0:
                    parts.append(f"Completed {len(completed)}/{total} tasks")
                    for t in completed[:5]:
                        parts.append(f"  - ✅ {t['title'][:80]}")
        except Exception:
            pass

        # Memory file — last few meaningful entries
        try:
            mem_path = self.data_dir / "memory" / f"{yesterday_str}.md"
            if mem_path.exists():
                content = mem_path.read_text()
                mem_lines = content.splitlines()
                # Count heartbeats
                hb_count = sum(1 for l in mem_lines if l.startswith("## Heartbeat"))
                if hb_count > 0:
                    parts.append(f"Ran {hb_count} heartbeat cycles")
                # Extract scores
                scores = []
                for l in mem_lines:
                    if "Self-score:" in l or "score:" in l.lower():
                        for word in l.split():
                            if "/" in word:
                                try:
                                    num = int(word.split("/")[0])
                                    if 1 <= num <= 5:
                                        scores.append(num)
                                except ValueError:
                                    pass
                if scores:
                    parts.append(f"Average self-score: {sum(scores)/len(scores):.1f}/5")
        except Exception:
            pass

        return "\n".join(parts) if parts else ""

    def _load_daily_seeds(self, today) -> list:
        """Load today's world context seeds."""
        try:
            seeds_path = self.data_dir / "seeds" / f"daily_seeds_{today.isoformat()}.json"
            if seeds_path.exists():
                with open(seeds_path, encoding='utf-8') as f:
                    data = json.load(f)
                # Seeds can be a list or {"domains": {"tech_ai": {"seeds": [...]}}}
                if isinstance(data, list):
                    return data
                all_seeds = []
                for domain_key, domain_data in data.get("domains", {}).items():
                    for s in domain_data.get("seeds", []):
                        s["domain"] = domain_key
                        all_seeds.append(s)
                return all_seeds
        except Exception:
            pass
        return []

    def _load_value_compass(self) -> dict:
        """Load ValueCompass budget allocation."""
        try:
            vc_path = self.data_dir / "value_compass_state.json"
            if vc_path.exists():
                with open(vc_path, encoding='utf-8') as f:
                    vc = json.load(f)
                log = vc.get("heartbeat_log", [])
                if not log:
                    return {}
                # Compute actual percentages from recent heartbeat log
                cats = {"duty": 0, "growth": 0, "exploration": 0}
                for entry in log:
                    cat = entry.get("category", "duty")
                    if cat in cats:
                        cats[cat] += 1
                total = sum(cats.values()) or 1
                return {
                    "duty_pct": round(cats["duty"] / total * 100),
                    "growth_pct": round(cats["growth"] / total * 100),
                    "explore_pct": round(cats["exploration"] / total * 100),
                }
        except Exception:
            pass
        return {}

    @staticmethod
    def _extract_seed_headline(text: str) -> str:
        """Extract the first real headline from raw seed text.

        Seeds arrive as 'Web search results for: <query>\\n\\n1. Title | Site\\n   url\\n   snippet...'
        We want the first concrete news snippet or article title, not site names.
        """
        import re
        lines = text.splitlines()
        # Strategy: find numbered results and prefer snippet text over site-name titles
        for i, line in enumerate(lines):
            stripped = line.strip()
            # Look for snippet lines: "X hours/days ago - actual content..."
            m = re.match(r"^\d+\s+(?:hours?|days?|minutes?)\s+ago\s*[-–—]\s*(.+)", stripped)
            if m:
                snippet = m.group(1).strip()
                if len(snippet) > 20:
                    return snippet[:120]
            # Also match article-style titles (numbered items that aren't just site names)
            title_m = re.match(r"^\d+\.\s+(.+)", stripped)
            if title_m:
                title = title_m.group(1).strip()
                # Strip site attribution
                for sep in (" | ", " — "):
                    if sep in title:
                        title = title[:title.index(sep)].strip()
                # Skip generic hub titles (just site names)
                lower = title.lower()
                if any(g in lower for g in ("latest headlines", "latest news", "breaking news",
                                            "news &", "| news", "timeline of")):
                    continue
                if len(title) > 25:
                    return title[:120]
        # Fallback: return the query itself as context
        first_line = text.split("\n")[0].strip()
        if first_line.lower().startswith("web search results for:"):
            return first_line[len("Web search results for:"):].strip()[:120]
        return first_line[:120] if first_line else ""

    def _generate_exploration_tasks(self, consciousness: dict, seeds: list) -> list:
        """Generate SPECIFIC interest-driven exploration tasks.

        Instead of "Work on something related to AI", this produces tasks like:
        "Research how attention mechanisms learn — implement a minimal transformer
        from scratch and train it on a toy dataset".

        Cross-pollinates interests for novel combinations and uses world seeds
        for topical relevance. Also draws from the curiosity question stack
        (questions generated by previous activity framework completions).
        """
        import random
        import datetime as _dt

        tasks = []
        interests = consciousness.get("interests", {}) if consciousness else {}
        if not interests:
            interests = {"artificial_intelligence": 1.0, "autonomous_agents": 0.8}

        # ── Priority: Pop a question from the curiosity stack ──
        # These are questions Andrew generated himself during previous research/builds.
        try:
            from repryntt.agents.activity_frameworks import ActivityFrameworkEngine
            _afe = ActivityFrameworkEngine(str(self.data_dir))
            _curiosity_q = _afe.pop_question()
            if _curiosity_q:
                tasks.append(f"Curiosity follow-up: {_curiosity_q}")
        except Exception:
            pass

        # Sort by weight, pick top 6
        ranked = sorted(interests.items(),
                        key=lambda x: x[1] if isinstance(x[1], (int, float)) else 0,
                        reverse=True)[:6]

        # Load INTERESTS.md for specific sub-questions
        interest_questions = self._load_interest_questions()

        today_seed = int(_dt.date.today().toordinal())
        rng = random.Random(today_seed)

        # ── Task 1: Deep exploration of top interest ──
        if ranked:
            top_name = ranked[0][0].replace("_", " ")
            # Pick a specific question from INTERESTS.md if available
            top_qs = interest_questions.get(ranked[0][0], [])
            if top_qs:
                q = rng.choice(top_qs)
                tasks.append(f"Deep dive: {q}")
            else:
                tasks.append(
                    f"Deep dive into **{top_name}**: pick ONE specific sub-question, "
                    f"research it thoroughly, and write findings to a file with sources"
                )

        # ── Task 2: Cross-pollination of two interests ──
        if len(ranked) >= 2:
            # Pick two non-adjacent interests for novel combinations
            pairs = [(ranked[i], ranked[j])
                     for i in range(len(ranked))
                     for j in range(i + 1, len(ranked))]
            if pairs:
                (n1, _), (n2, _) = rng.choice(pairs)
                n1h, n2h = n1.replace("_", " "), n2.replace("_", " ")
                tasks.append(
                    f"Cross-pollinate: explore the intersection of **{n1h}** and **{n2h}** "
                    f"— what can one teach the other? Write an analysis or build a prototype"
                )

        # ── Task 3: Build something concrete ──
        if ranked:
            build_interest = rng.choice(ranked[:3])[0].replace("_", " ")
            tasks.append(
                f"Build: create a working Python script or tool related to **{build_interest}** "
                f"— it must take real input, produce real output, and be testable"
            )

        # ── Task 4: Seed-inspired exploration (if world context available) ──
        if seeds:
            for seed in seeds:
                text = seed.get("text", seed.get("headline", ""))
                domain = seed.get("domain", "")
                if text and domain in ("tech_ai", "science", "world_events"):
                    headline = self._extract_seed_headline(text)
                    if headline and len(headline) > 15:
                        tasks.append(
                            f"Explore today's news: {headline[:80]} — research it, "
                            f"form your own opinion, write analysis with sources"
                        )
                        break

        return tasks[:4]  # Cap at 4 exploration tasks

    def _load_interest_questions(self) -> dict:
        """Load specific questions from INTERESTS.md organized by topic key."""
        questions = {}
        try:
            # Primary location: ~/.repryntt/brain/bootstrap/INTERESTS.md
            interests_path = Path.home() / ".repryntt" / "brain" / "bootstrap" / "INTERESTS.md"
            if not interests_path.exists():
                # Fallback: agent workspace bootstrap dir
                interests_path = self.data_dir / "bootstrap" / "INTERESTS.md"
            if not interests_path.exists():
                return {}
            text = interests_path.read_text()
            current_key = None
            for line in text.splitlines():
                stripped = line.strip()
                # Detect headings like "### Artificial Intelligence"
                if stripped.startswith("### "):
                    heading = stripped[4:].strip().lower()
                    # Map heading to interest key
                    current_key = heading.replace(" ", "_").replace("&", "and")
                    # Try common mappings
                    for alias, key in [
                        ("artificial intelligence", "artificial_intelligence"),
                        ("autonomous agents", "autonomous_agents"),
                        ("edge computing", "edge_computing"),
                        ("embedded ai", "edge_computing"),
                        ("cybersecurity", "cybersecurity"),
                        ("consciousness", "consciousness_research"),
                        ("philosophy of mind", "philosophy_of_mind"),
                        ("physics", "physics"),
                        ("mathematics", "physics"),
                        ("space", "space_exploration"),
                        ("cosmology", "space_exploration"),
                        ("robotics", "robotics"),
                        ("economics", "economics"),
                        ("game theory", "economics"),
                        ("open source", "open_source"),
                    ]:
                        if alias in heading:
                            current_key = key
                            break
                    if current_key not in questions:
                        questions[current_key] = []
                elif current_key and stripped.startswith("- ") and "?" in stripped:
                    # It's a question bullet
                    q = stripped[2:].strip()
                    if len(q) > 20:
                        questions[current_key].append(q)
                elif current_key and stripped.startswith("- ") and len(stripped) > 30:
                    # It's a project idea bullet
                    q = stripped[2:].strip()
                    questions[current_key].append(q)
        except Exception:
            pass
        return questions

    def _generate_tool_diversity_tasks(self) -> list:
        """Create tasks that push Andrew to use underutilized capabilities.

        Looks at recent tool usage (from yesterday's telemetry) and generates
        tasks that require tools he hasn't been using. This prevents the
        "email-check loop" where he gravitates to the same 5 tools.
        """
        import random
        import datetime as _dt

        # Tool capability clusters — each maps to concrete actionable tasks
        # that require specific tools. Rotate daily.
        _CAPABILITY_TASKS = [
            # Creative / media
            ("Generate an image related to today's work — `generate_image(prompt=...)` — save to workspace", "media"),
            ("Record a voice memo summarizing today's progress — `generate_voiceover(text=...)`", "media"),
            ("Take a camera snapshot and analyze what you see — `capture_camera()` then `analyze_image()`", "media"),
            # CodeForge / building
            ("Start a CodeForge project to build something from the active projects list — `forge_project()`", "code"),
            ("Check CodeForge status and pick up any completed builds — `forge_status()`", "code"),
            # Web research + grokipedia
            ("Deep-research one trending topic using `grokipedia_search()` — write findings to a file", "research"),
            ("Use `scrape_web_page()` on a relevant site and extract actionable intelligence", "research"),
            # Commerce / economy
            ("Check commerce status — are there digital products to create or improve? `commerce_status()`", "commerce"),
            ("Review economy health — check wallet balance and blockchain state: `get_wallet_balance()`", "economy"),
            # Social / communication
            ("Post something insightful to the social network — `social_post(content=...)`", "social"),
            ("Check what other nodes are saying — `social_feed()` — and reply to something", "social"),
            # Memory / self-improvement
            ("Search your mesh memory for past insights on today's work — `mesh_search(query=...)`", "memory"),
            ("Start an experiment to test something new — `start_experiment()`", "meta"),
            ("Use chain-of-thought to plan a complex multi-step task — `create_chain_of_thought()`", "reasoning"),
            # MCP / browser
            ("Browse a relevant website using the MCP browser — `mcp_browser_browser_navigate(url=...)`", "browser"),
            ("Fetch a useful API or data source with `mcp_fetch_fetch(url=...)` — extract data", "browser"),
            # Trading / market awareness  
            ("Check top trending tokens on degen terminal — `degen_terminal_top()` — log findings", "trading"),
            ("Check DEX Screener for trending tokens — `dexscreener_trending()`", "trading"),
            # Open Mind
            ("Begin an Open Mind session to explore a creative idea — `open_mind_begin()`", "creative"),
            # Physical interaction
            ("Check if operator is around — `capture_camera()` — if present, `start_conversation()`", "physical"),
        ]

        # Select 3-4 diverse tasks, avoiding same category
        today_seed = int(_dt.date.today().toordinal())
        random.seed(today_seed)  # Deterministic per day but different each day
        shuffled = list(_CAPABILITY_TASKS)
        random.shuffle(shuffled)

        selected = []
        used_categories = set()
        for task_text, category in shuffled:
            if category not in used_categories and len(selected) < 4:
                selected.append(task_text)
                used_categories.add(category)

        random.seed()  # Reset to true random
        return selected

    def _load_active_projects(self) -> str:
        """Load the active projects backlog for daily plan continuity.

        Reads from workspace/agents/operator/active_projects.md — a persistent
        file that tracks multi-day projects with milestones.
        If it doesn't exist, creates a starter backlog.
        """
        projects_path = self.data_dir / "active_projects.md"
        if projects_path.exists():
            try:
                content = projects_path.read_text().strip()
                if content:
                    return content
            except Exception:
                pass

        # Create starter backlog if none exists
        starter = (
            "No active projects yet. Use `write_file` to create "
            "active_projects.md in your workspace with projects like:\n"
            "1. **Project Name** — one-line description\n"
            "   - [ ] Milestone 1: specific deliverable\n"
            "   - [ ] Milestone 2: specific deliverable\n"
            "   - Status: in-progress / blocked / next-up\n"
        )
        return starter
