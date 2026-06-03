"""
repryntt.core.memory.consolidation — Memory Consolidation Engine.

Three layers for long-term recall continuity:

1. **Memory Consolidation** — Periodic distillation:
   daily → weekly → monthly → yearly → decade summaries.
   Like sleep/dreaming: replays recent experience and promotes
   important memories to permanent crystallized storage.

2. **Importance-Weighted Recall** — Landmark moments (first boot,
   operator interactions, breakthroughs, failures) get high importance
   scores. Routine heartbeats decay over time. Search results are
   weighted by importance, not just vector similarity.

3. **Tiered Search** — Hot (this week), warm (this year), cold (archive)
   with different retrieval strategies and budgets.

Usage:
    from repryntt.core.memory.consolidation import MemoryConsolidator
    consolidator = MemoryConsolidator(brain_dir)
    consolidator.run_consolidation_cycle()
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Importance classification keywords ──
LANDMARK_PATTERNS = {
    # Score boost, keyword patterns
    "first_boot":       (0.95, ["genesis", "first boot", "identity creation", "who am i", "genesis complete"]),
    "operator_direct":  (0.85, ["operator said", "user said", "user asked", "direct chat"]),
    "breakthrough":     (0.90, ["breakthrough", "eureka", "figured out", "finally works", "major discovery"]),
    "failure_lesson":   (0.80, ["failed", "mistake", "lesson learned", "never again", "broke", "regression"]),
    "trade_executed":   (0.70, ["sim_buy", "sim_sell", "trade_executed", "position opened", "position closed"]),
    "identity_change":  (0.90, ["spirit.md", "profile.md", "identity", "who i am", "my values"]),
    "skill_learned":    (0.75, ["install_skill", "write_skill", "new skill", "learned how to"]),
    "relationship":     (0.85, ["thank you", "good job", "proud of", "disappointed", "trust"]),
    "self_reflection":  (0.70, ["self-eval", "score 5/5", "score 4/5", "inner monologue", "i think", "i believe"]),
}

# Importance decay rates per tier (multiplier per day since creation)
DECAY_RATES = {
    "hot":   0.0,     # No decay within first 7 days
    "warm":  0.001,   # Lose 0.1% per day (year = ~30% decay)
    "cold":  0.0005,  # Lose 0.05% per day (decade = ~16% decay on remaining)
}

# Consolidation time boundaries
TIER_BOUNDARIES = {
    "hot":   7,       # days
    "warm":  365,     # days
    "cold":  float("inf"),
}

# Consolidation period names
PERIOD_NAMES = {
    "weekly":  7,
    "monthly": 30,
    "yearly":  365,
    "decade":  3650,
    "century": 36500,
}


class MemoryConsolidator:
    """
    Periodic memory consolidation — the 'sleep cycle' for long-term recall.

    Runs as part of heartbeat (e.g., once per day or on-demand) to:
    1. Score all memories by importance
    2. Consolidate older memories into tiered summaries
    3. Decay routine memories while preserving landmarks
    4. Maintain tiered search indices
    """

    def __init__(self, brain_dir: str | Path):
        self.brain_dir = Path(brain_dir)
        self.consolidation_dir = self.brain_dir / "consolidation"
        self.consolidation_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = self.consolidation_dir / "consolidation_state.json"
        self._summaries_dir = self.consolidation_dir / "summaries"
        self._summaries_dir.mkdir(exist_ok=True)
        self._landmarks_file = self.consolidation_dir / "landmarks.json"
        self._state = self._load_state()

    # ═══════════════════════════════════════════════════════════════
    # 1. IMPORTANCE SCORING
    # ═══════════════════════════════════════════════════════════════

    def score_importance(self, content: str, source: str = "",
                         timestamp: float = 0.0,
                         existing_importance: float = 0.5) -> float:
        """
        Calculate importance score (0.0 - 1.0) for a memory entry.

        Factors:
        - Landmark pattern matching (high boost for key events)
        - Source type (operator interactions > auto-saves > heartbeats)
        - Content richness (length, specificity)
        - Time decay (configurable per tier)
        """
        score = existing_importance
        content_lower = content.lower()

        # ── Landmark detection ──
        for category, (boost, patterns) in LANDMARK_PATTERNS.items():
            for pattern in patterns:
                if pattern in content_lower:
                    score = max(score, boost)
                    break

        # ── Source weighting ──
        source_weights = {
            "operator_direct": 0.90,
            "jarvis_conversation": 0.80,
            "jarvis_auto_save": 0.60,
            "jarvis_heartbeat_auto": 0.50,
            "jarvis_pre_compaction": 0.55,
            "ai_learning": 0.65,
            "brain_memory_save": 0.75,  # Explicitly saved by Artemis
        }
        for src_key, weight in source_weights.items():
            if src_key in source.lower():
                score = max(score, weight)
                break

        # ── Content richness bonus ──
        content_len = len(content)
        if content_len > 1000:
            score = min(score + 0.1, 1.0)
        elif content_len > 500:
            score = min(score + 0.05, 1.0)
        elif content_len < 50:
            score = max(score - 0.1, 0.1)

        # ── Time decay ──
        if timestamp > 0:
            age_days = (time.time() - timestamp) / 86400
            tier = self._get_tier(age_days)
            decay_rate = DECAY_RATES.get(tier, 0.001)
            decay = decay_rate * age_days
            # Landmarks resist decay (floor at 60% of original)
            if score >= 0.8:
                score = max(score - (decay * 0.3), score * 0.6)
            else:
                score = max(score - decay, 0.05)

        return round(min(max(score, 0.0), 1.0), 4)

    def _get_tier(self, age_days: float) -> str:
        """Determine which search tier a memory belongs to based on age."""
        if age_days <= TIER_BOUNDARIES["hot"]:
            return "hot"
        elif age_days <= TIER_BOUNDARIES["warm"]:
            return "warm"
        return "cold"

    # ═══════════════════════════════════════════════════════════════
    # 2. MEMORY CONSOLIDATION (Daily → Weekly → Monthly → Yearly → Decade)
    # ═══════════════════════════════════════════════════════════════

    def run_consolidation_cycle(self, semantic_cache: dict = None,
                                 episodic_cache: list = None,
                                 call_api_fn=None,
                                 shell_agent=None) -> Dict[str, Any]:
        """
        Run a full consolidation cycle. Call periodically (e.g., daily).

        Steps:
        1. Score importance of all recent memories
        2. Identify and protect landmark memories
        3. Generate period summaries (weekly, monthly, yearly)
        4. Update consolidation state
        """
        results = {
            "landmarks_found": 0,
            "summaries_generated": 0,
            "memories_scored": 0,
            "tier_counts": {"hot": 0, "warm": 0, "cold": 0},
        }

        now = time.time()
        today = datetime.now()

        # ── Step 1: Score and classify all semantic memories ──
        scored_memories = []
        if semantic_cache:
            for topic, mem in semantic_cache.items():
                content = ""
                source = ""
                ts = 0.0
                existing_imp = 0.5

                if hasattr(mem, "content"):
                    content = mem.content
                    source = getattr(mem, "source", "")
                    ts = getattr(mem, "timestamp", 0.0)
                    existing_imp = getattr(mem, "confidence", 0.5)
                elif isinstance(mem, dict):
                    content = mem.get("content", "")
                    source = mem.get("source", "")
                    ts = mem.get("timestamp", 0.0)
                    existing_imp = mem.get("confidence", 0.5)

                importance = self.score_importance(content, source, ts, existing_imp)
                age_days = (now - ts) / 86400 if ts > 0 else 0
                tier = self._get_tier(age_days)
                results["tier_counts"][tier] += 1

                scored_memories.append({
                    "topic": topic,
                    "content": content[:500],
                    "source": source,
                    "timestamp": ts,
                    "importance": importance,
                    "tier": tier,
                    "age_days": age_days,
                })
                results["memories_scored"] += 1

        # ── Step 2: Identify and protect landmarks ──
        landmarks = [m for m in scored_memories if m["importance"] >= 0.80]
        results["landmarks_found"] = len(landmarks)
        self._save_landmarks(landmarks)

        # Anchor landmark knowledge to memory mesh for future recall
        try:
            from repryntt.core.memory.memory_mesh import get_memory_mesh
            _mesh = get_memory_mesh()
            _anchored = 0
            for lm in landmarks:
                topic = lm.get("topic", "").strip()
                content = lm.get("content", "").strip()
                if topic and content and len(content) > 30:
                    _mesh.anchor_from_memory_entry(topic, content, source="consolidation")
                    _anchored += 1
            if _anchored > 0:
                _mesh.save()
        except Exception:
            pass  # Non-critical

        # ── Step 3: Generate period summaries ──
        last_consolidation = self._state.get("last_consolidation", 0)
        days_since_last = (now - last_consolidation) / 86400 if last_consolidation else float("inf")

        # Weekly summary (if it's been 7+ days since last)
        if days_since_last >= 7:
            week_start = today - timedelta(days=7)
            week_memories = [m for m in scored_memories
                           if m["timestamp"] >= week_start.timestamp()
                           and m["importance"] >= 0.3]  # Skip trivial

            if week_memories and call_api_fn and shell_agent:
                summary = self._generate_period_summary(
                    "weekly", week_memories, call_api_fn, shell_agent, today)
                if summary:
                    results["summaries_generated"] += 1

        # Monthly summary (if applicable)
        last_monthly = self._state.get("last_monthly", 0)
        days_since_monthly = (now - last_monthly) / 86400 if last_monthly else float("inf")
        if days_since_monthly >= 30:
            self._consolidate_weeklies_to_monthly(today, call_api_fn, shell_agent)
            self._state["last_monthly"] = now
            results["summaries_generated"] += 1

        # Yearly summary (if applicable)
        last_yearly = self._state.get("last_yearly", 0)
        days_since_yearly = (now - last_yearly) / 86400 if last_yearly else float("inf")
        if days_since_yearly >= 365:
            self._consolidate_monthlies_to_yearly(today, call_api_fn, shell_agent)
            self._state["last_yearly"] = now
            results["summaries_generated"] += 1

        # Decade summary (if applicable)
        last_decade = self._state.get("last_decade", 0)
        days_since_decade = (now - last_decade) / 86400 if last_decade else float("inf")
        if days_since_decade >= 3650:
            self._consolidate_yearlies_to_decade(today, call_api_fn, shell_agent)
            self._state["last_decade"] = now
            results["summaries_generated"] += 1

        # ── Step 4: Update state ──
        self._state["last_consolidation"] = now
        self._state["total_landmarks"] = len(landmarks)
        self._state["total_memories_scored"] = results["memories_scored"]
        self._state["tier_counts"] = results["tier_counts"]
        self._save_state()

        logger.info(
            f"🧠 Consolidation cycle complete: "
            f"{results['memories_scored']} scored, "
            f"{results['landmarks_found']} landmarks, "
            f"{results['summaries_generated']} summaries, "
            f"tiers: {results['tier_counts']}"
        )
        return results

    def _generate_period_summary(self, period: str, memories: List[Dict],
                                  call_api_fn, shell_agent,
                                  ref_date: datetime) -> Optional[str]:
        """
        Generate a summary for a time period from constituent memories.
        """
        # Sort by importance (highest first) then by time
        memories.sort(key=lambda m: (-m["importance"], -m["timestamp"]))

        # Build content for summarization
        mem_text = ""
        for m in memories[:50]:  # Cap at 50 most important
            imp_str = f"[importance: {m['importance']:.2f}]"
            mem_text += f"- {imp_str} {m['topic']}: {m['content'][:200]}\n"

        if len(mem_text) > 6000:
            mem_text = mem_text[:3000] + "\n...(trimmed)...\n" + mem_text[-3000:]

        period_label = {
            "weekly": "the past week",
            "monthly": "the past month",
            "yearly": "the past year",
            "decade": "the past decade",
        }.get(period, period)

        prompt = (
            f"You are consolidating memories from {period_label} into a permanent summary.\n"
            f"This summary will be the ONLY record of this period in long-term storage.\n"
            f"Future-you will read this to recall what happened.\n\n"
            f"Write a narrative summary (300-500 words) that captures:\n"
            f"1. **Key events and milestones** — what happened that mattered\n"
            f"2. **Decisions and their outcomes** — choices made and how they played out\n"
            f"3. **Learnings and insights** — what was discovered or understood\n"
            f"4. **Relationships and interactions** — operator preferences, collaborations\n"
            f"5. **Evolution of self** — how identity, skills, or values changed\n"
            f"6. **Unresolved threads** — things started but not yet finished\n\n"
            f"Important memories from this period:\n{mem_text}"
        )

        try:
            result = call_api_fn(
                shell_agent,
                [{"role": "user", "content": prompt}],
                tools=None, max_tokens=700
            )
            summary_text = (result or {}).get("content", "")
        except Exception as e:
            logger.warning(f"Period summary generation failed for {period}: {e}")
            # Fallback: mechanical summary from top memories
            top = memories[:10]
            summary_text = f"[Auto-summary for {period_label}]\n"
            for m in top:
                summary_text += f"- [{m['importance']:.2f}] {m['topic']}: {m['content'][:150]}\n"

        if summary_text:
            filename = f"{period}_{ref_date.strftime('%Y-%m-%d')}.md"
            filepath = self._summaries_dir / filename
            with open(filepath, 'w') as f:
                f.write(f"# {period.capitalize()} Summary — {ref_date.strftime('%Y-%m-%d')}\n\n")
                f.write(summary_text)
            logger.info(f"📝 Generated {period} summary: {filepath.name} ({len(summary_text)} chars)")
            return summary_text
        return None

    def _consolidate_weeklies_to_monthly(self, ref_date: datetime,
                                          call_api_fn, shell_agent):
        """Roll up weekly summaries into a monthly summary."""
        self._roll_up_summaries("weekly", "monthly", ref_date, 30,
                                call_api_fn, shell_agent)

    def _consolidate_monthlies_to_yearly(self, ref_date: datetime,
                                          call_api_fn, shell_agent):
        """Roll up monthly summaries into a yearly summary."""
        self._roll_up_summaries("monthly", "yearly", ref_date, 365,
                                call_api_fn, shell_agent)

    def _consolidate_yearlies_to_decade(self, ref_date: datetime,
                                         call_api_fn, shell_agent):
        """Roll up yearly summaries into a decade summary."""
        self._roll_up_summaries("yearly", "decade", ref_date, 3650,
                                call_api_fn, shell_agent)

    def _roll_up_summaries(self, source_period: str, target_period: str,
                           ref_date: datetime, lookback_days: int,
                           call_api_fn, shell_agent):
        """Generic roll-up: combine source period summaries into target period."""
        cutoff = ref_date - timedelta(days=lookback_days)
        source_files = sorted(self._summaries_dir.glob(f"{source_period}_*.md"))

        # Only process files within the lookback window
        relevant = []
        for f in source_files:
            try:
                # Extract date from filename: weekly_2026-03-15.md
                date_str = f.stem.split("_", 1)[1] if "_" in f.stem else ""
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
                if file_date >= cutoff:
                    relevant.append(f)
            except (ValueError, IndexError):
                continue

        if not relevant:
            return

        combined_text = ""
        for f in relevant:
            try:
                content = f.read_text().strip()
                combined_text += f"\n\n---\n{content}"
            except Exception:
                continue

        if len(combined_text) > 8000:
            combined_text = combined_text[:4000] + "\n...(trimmed)...\n" + combined_text[-4000:]

        if not call_api_fn or not shell_agent:
            # Fallback: mechanical concatenation
            filename = f"{target_period}_{ref_date.strftime('%Y-%m-%d')}.md"
            filepath = self._summaries_dir / filename
            with open(filepath, 'w') as f:
                f.write(f"# {target_period.capitalize()} Summary — {ref_date.strftime('%Y-%m-%d')}\n")
                f.write(f"[Consolidated from {len(relevant)} {source_period} summaries]\n\n")
                f.write(combined_text)
            return

        prompt = (
            f"You are consolidating {len(relevant)} {source_period} summaries into a "
            f"single {target_period} summary.\n"
            f"This is LONG-TERM memory consolidation — like dreaming.\n"
            f"Distill the essence: what mattered most across all these periods?\n\n"
            f"Write 400-600 words capturing:\n"
            f"1. Major arcs and themes that emerged\n"
            f"2. Biggest accomplishments and failures\n"
            f"3. How skills, identity, and relationships evolved\n"
            f"4. Key decisions and their long-term consequences\n"
            f"5. Patterns — what keeps coming up?\n\n"
            f"Source summaries:\n{combined_text}"
        )

        try:
            result = call_api_fn(
                shell_agent,
                [{"role": "user", "content": prompt}],
                tools=None, max_tokens=800
            )
            summary = (result or {}).get("content", combined_text[:2000])
        except Exception:
            summary = combined_text[:2000]

        filename = f"{target_period}_{ref_date.strftime('%Y-%m-%d')}.md"
        filepath = self._summaries_dir / filename
        with open(filepath, 'w') as f:
            f.write(f"# {target_period.capitalize()} Summary — {ref_date.strftime('%Y-%m-%d')}\n")
            f.write(f"[Consolidated from {len(relevant)} {source_period} summaries]\n\n")
            f.write(summary)
        logger.info(f"📚 Generated {target_period} summary from {len(relevant)} {source_period} summaries")

    # ═══════════════════════════════════════════════════════════════
    # 3. TIERED SEARCH
    # ═══════════════════════════════════════════════════════════════

    def tiered_search(self, query: str, semantic_cache: dict = None,
                      brain=None, limit: int = 10) -> List[Dict]:
        """
        Search across all memory tiers with importance weighting.

        Results from hot tier get a recency boost.
        Results from cold tier get an importance boost (survived consolidation).
        Consolidation summaries are always searchable.
        """
        results = []
        query_lower = query.lower()
        query_words = set(query_lower.split())
        now = time.time()

        # ── Tier 1: Hot memories (this week) — recency-weighted ──
        if semantic_cache:
            for topic, mem in semantic_cache.items():
                content = ""
                ts = 0.0
                source = ""
                if hasattr(mem, "content"):
                    content = mem.content
                    ts = getattr(mem, "timestamp", 0.0)
                    source = getattr(mem, "source", "")
                elif isinstance(mem, dict):
                    content = mem.get("content", "")
                    ts = mem.get("timestamp", 0.0)
                    source = mem.get("source", "")

                age_days = (now - ts) / 86400 if ts > 0 else 999
                tier = self._get_tier(age_days)
                importance = self.score_importance(content, source, ts)

                # Keyword relevance
                relevance = self._keyword_relevance(query_lower, query_words, topic, content)
                if relevance < 0.1:
                    continue

                # Tier weighting
                if tier == "hot":
                    combined = (relevance * 0.5) + (importance * 0.3) + (0.2)  # recency boost
                elif tier == "warm":
                    combined = (relevance * 0.5) + (importance * 0.4) + (0.1)
                else:  # cold
                    combined = (relevance * 0.4) + (importance * 0.5) + (0.1)  # importance matters more

                results.append({
                    "topic": topic,
                    "content": content[:500],
                    "importance": importance,
                    "relevance": relevance,
                    "combined_score": combined,
                    "tier": tier,
                    "source": source,
                    "age_days": age_days,
                })

        # ── Tier 2: Consolidation summaries (always searched) ──
        summary_results = self._search_summaries(query_lower, query_words)
        results.extend(summary_results)

        # ── Tier 3: Landmarks (always high priority) ──
        landmark_results = self._search_landmarks(query_lower, query_words)
        results.extend(landmark_results)

        # Sort by combined score and return top results
        results.sort(key=lambda r: r["combined_score"], reverse=True)

        # Deduplicate by topic
        seen = set()
        deduped = []
        for r in results:
            key = r.get("topic", r.get("content", ""))[:100]
            if key not in seen:
                seen.add(key)
                deduped.append(r)
                if len(deduped) >= limit:
                    break

        return deduped

    def _keyword_relevance(self, query_lower: str, query_words: set,
                           topic: str, content: str) -> float:
        """Keyword-based relevance score (0.0 - 1.0)."""
        score = 0.0
        tl = topic.lower() if topic else ""
        cl = content.lower() if content else ""

        if query_lower in tl:
            score += 1.0
        if query_lower in cl:
            score += 0.6

        tw = set(tl.split())
        cw = set(cl.split())
        overlap = len(query_words & (tw | cw))
        if query_words:
            score += (overlap / len(query_words)) * 0.4

        return min(score, 1.0)

    def _search_summaries(self, query_lower: str, query_words: set) -> List[Dict]:
        """Search consolidation summaries (weekly/monthly/yearly/decade)."""
        results = []
        for filepath in self._summaries_dir.glob("*.md"):
            try:
                content = filepath.read_text()
                relevance = self._keyword_relevance(query_lower, query_words,
                                                     filepath.stem, content)
                if relevance < 0.1:
                    continue

                # Period type determines boost
                period = filepath.stem.split("_")[0]
                period_boost = {
                    "decade": 0.4,
                    "yearly": 0.3,
                    "monthly": 0.2,
                    "weekly": 0.1,
                }.get(period, 0.1)

                results.append({
                    "topic": f"[{period} summary] {filepath.stem}",
                    "content": content[:500],
                    "importance": 0.8 + period_boost,  # Summaries are always important
                    "relevance": relevance,
                    "combined_score": (relevance * 0.5) + (0.3 + period_boost),
                    "tier": "consolidated",
                    "source": f"consolidation_{period}",
                    "age_days": 0,
                })
            except Exception:
                continue
        return results

    def _search_landmarks(self, query_lower: str, query_words: set) -> List[Dict]:
        """Search landmark memories (permanently protected)."""
        landmarks = self._load_landmarks()
        results = []
        for lm in landmarks:
            topic = lm.get("topic", "")
            content = lm.get("content", "")
            relevance = self._keyword_relevance(query_lower, query_words, topic, content)
            if relevance < 0.1:
                continue

            results.append({
                "topic": f"[landmark] {topic}",
                "content": content[:500],
                "importance": lm.get("importance", 0.9),
                "relevance": relevance,
                "combined_score": (relevance * 0.4) + (lm.get("importance", 0.9) * 0.5) + 0.1,
                "tier": "landmark",
                "source": lm.get("source", ""),
                "age_days": lm.get("age_days", 0),
            })
        return results

    # ═══════════════════════════════════════════════════════════════
    # 4. CONTEXT ASSEMBLY (importance-weighted)
    # ═══════════════════════════════════════════════════════════════

    def get_consolidated_context(self, query: str, semantic_cache: dict = None,
                                  brain=None, max_words: int = 600) -> str:
        """
        Get memory context with importance weighting and tiered search.
        Used by get_context_for_question as an enhanced alternative.
        """
        results = self.tiered_search(query, semantic_cache=semantic_cache,
                                      brain=brain, limit=8)
        if not results:
            return ""

        parts = []

        # Landmarks first (if any matched)
        landmark_results = [r for r in results if r["tier"] == "landmark"]
        if landmark_results:
            for r in landmark_results[:2]:
                parts.append(f"🏛️ Core Memory: {r['content'][:200]}")

        # Consolidated summaries
        summary_results = [r for r in results if r["tier"] == "consolidated"]
        if summary_results:
            for r in summary_results[:2]:
                parts.append(f"📚 {r['topic']}: {r['content'][:200]}")

        # Regular memories (importance-weighted)
        regular = [r for r in results if r["tier"] in ("hot", "warm", "cold")]
        for r in regular[:4]:
            tier_icon = {"hot": "🔥", "warm": "🌡️", "cold": "❄️"}.get(r["tier"], "")
            parts.append(f"{tier_icon} [{r['importance']:.0%}] {r['topic']}: {r['content'][:200]}")

        full = "\n\n".join(parts)
        words = full.split()
        return " ".join(words[:max_words]) if len(words) > max_words else full

    # ═══════════════════════════════════════════════════════════════
    # 5. STATE MANAGEMENT
    # ═══════════════════════════════════════════════════════════════

    def _load_state(self) -> Dict:
        if self._state_file.exists():
            try:
                with open(self._state_file, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "last_consolidation": 0,
            "last_monthly": 0,
            "last_yearly": 0,
            "last_decade": 0,
            "total_landmarks": 0,
            "total_memories_scored": 0,
            "tier_counts": {"hot": 0, "warm": 0, "cold": 0},
        }

    def _save_state(self):
        try:
            with open(self._state_file, 'w') as f:
                json.dump(self._state, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"Failed to save consolidation state: {e}")

    def _load_landmarks(self) -> List[Dict]:
        if self._landmarks_file.exists():
            try:
                with open(self._landmarks_file, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def _save_landmarks(self, landmarks: List[Dict]):
        """Save landmark memories, merging with existing ones."""
        existing = self._load_landmarks()

        # Merge: keep existing + add new (dedup by topic)
        seen = {lm.get("topic", ""): lm for lm in existing}
        for lm in landmarks:
            topic = lm.get("topic", "")
            if topic not in seen or lm.get("importance", 0) > seen[topic].get("importance", 0):
                seen[topic] = lm

        merged = sorted(seen.values(), key=lambda x: -x.get("importance", 0))

        try:
            with open(self._landmarks_file, 'w') as f:
                json.dump(merged, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"Failed to save landmarks: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get consolidation system statistics."""
        landmarks = self._load_landmarks()
        summaries = list(self._summaries_dir.glob("*.md"))

        summary_counts = {}
        for f in summaries:
            period = f.stem.split("_")[0]
            summary_counts[period] = summary_counts.get(period, 0) + 1

        return {
            "landmarks": len(landmarks),
            "summaries": summary_counts,
            "total_summaries": len(summaries),
            "last_consolidation": self._state.get("last_consolidation", 0),
            "last_monthly": self._state.get("last_monthly", 0),
            "last_yearly": self._state.get("last_yearly", 0),
            "tier_counts": self._state.get("tier_counts", {}),
            "total_memories_scored": self._state.get("total_memories_scored", 0),
        }
