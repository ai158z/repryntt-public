#!/usr/bin/env python3
"""
Topic Generator — AI-driven novel topic generation for exploration.

Migrated from SAIGE/brain/brain_system.py Phase 7.
Provides _generate_novel_grokipedia_queries, _extract_diverse_topics_from_brain,
get_self_prompts, _generate_external_self_prompts, and related helpers.

These methods all depend on the brain_system instance for access to memory caches,
AI services, and personality brain state.
"""

import json
import os
import time
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class TopicGenerator:
    """Generates novel exploration topics from brain knowledge and AI self-prompting."""

    def __init__(self, brain_system):
        self.brain = brain_system
        self.brain_path: Path = Path(brain_system.brain_path)

    # ------------------------------------------------------------------ #
    #  PUBLIC API                                                          #
    # ------------------------------------------------------------------ #

    def get_self_prompts(self, limit: int = 5, enrich_external: bool = True) -> List[Dict[str, Any]]:
        """Retrieve recent self-prompts, optionally enriched with external knowledge."""
        try:
            load_memory = getattr(self.brain, "_load_memory", None)
            if not load_memory:
                return self._generate_external_self_prompts(limit)

            working_data = load_memory("working")
            sp_data = working_data.get("self_prompts", {})

            if not sp_data or "prompts" not in sp_data:
                external = self._generate_external_self_prompts(limit)
                if external:
                    return external
                return []

            prompts = sp_data.get("prompts", [])

            if enrich_external:
                enriched = []
                for prompt in prompts[-limit:]:
                    enriched.append(self._enrich_prompt_with_external_knowledge(prompt))
                return enriched
            return prompts[-limit:]
        except Exception as e:
            logger.error(f"Error retrieving self-prompts: {e}")
            external = self._generate_external_self_prompts(limit)
            return external if external else []

    def extract_diverse_topics_from_brain(self, limit: int = 10) -> List[str]:
        """Extract diverse, novel topics from brain knowledge for self-prompt inspiration."""
        try:
            topic_scores: Dict[str, float] = {}

            # SOURCE 1: Semantic memory topics
            semantic_cache = getattr(self.brain, "semantic_cache", {})
            for topic_key, memory in semantic_cache.items():
                topic = getattr(memory, "topic", "")
                if len(topic) > 10:
                    recency = min(1.0, (time.time() - getattr(memory, "timestamp", 0)) / 86400)
                    confidence = getattr(memory, "confidence", 0.5)
                    diversity = len(getattr(memory, "related_topics", [])) * 0.1
                    novelty = 1.0 if confidence < 0.8 else 0.5
                    topic_scores[topic] = (recency * 0.3) + (confidence * 0.3) + (diversity * 0.2) + (novelty * 0.2)

            # SOURCE 2: Episodic memory patterns
            episodic_cache = getattr(self.brain, "episodic_cache", [])
            for event in list(episodic_cache)[-50:]:
                desc = getattr(event, "description", "").lower()
                if any(kw in desc for kw in ["explore", "research", "analyze", "study", "investigate"]):
                    words = desc.split()
                    potential = [
                        f"advances in {words[-1]}" if len(words) > 1 else desc,
                        f"future implications of {desc[:30]}...",
                    ]
                    for t in potential:
                        if len(t) > 15:
                            topic_scores[t] = topic_scores.get(t, 0) + 0.8

            # SOURCE 3: Procedural memory task types
            task_insights = {
                "creative_writing": ["narrative innovation", "character psychology"],
                "research_analysis": ["methodological advances", "knowledge synthesis"],
                "problem_solving": ["optimization strategies", "system design"],
                "learning_education": ["cognitive science", "teaching methodologies"],
                "technical_development": ["emerging technologies", "system architecture"],
            }
            for task_type, insights in task_insights.items():
                for insight in insights:
                    topic_scores[insight] = topic_scores.get(insight, 0) + 0.6

            # SOURCE 4: Recent chain topics (find gaps)
            recent_chains: List[str] = []
            chains_dir = self.brain_path / "chains"
            if chains_dir.exists():
                chain_files = sorted(chains_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)
                for cf in chain_files[:10]:
                    try:
                        with open(cf, "r") as f:
                            topic = json.load(f).get("metadata", {}).get("topic", "")
                            if topic:
                                recent_chains.append(topic.lower())
                    except Exception:
                        pass

            domain_gaps = {
                "ai": ["artificial consciousness", "AI ethics evolution"],
                "technology": ["emerging tech ecosystems", "digital transformation"],
                "science": ["interdisciplinary research", "scientific methodology"],
                "society": ["cultural evolution", "social dynamics"],
                "nature": ["environmental science", "biological systems"],
                "philosophy": ["consciousness studies", "ethical frameworks"],
            }
            for domain, gap_topics in domain_gaps.items():
                if not any(domain in chain for chain in recent_chains):
                    for t in gap_topics:
                        topic_scores[t] = topic_scores.get(t, 0) + 1.0

            # Select with diversity
            sorted_topics = sorted(topic_scores.items(), key=lambda x: x[1], reverse=True)
            selected: List[str] = []
            for topic, _ in sorted_topics:
                topic_lower = topic.lower()
                domain = "misc"
                for d in ["ai", "technology", "science", "society", "nature", "philosophy"]:
                    if d in topic_lower:
                        domain = d
                        break
                domain_count = sum(1 for t in selected if domain in t.lower())
                if domain_count < 2 and len(selected) < limit:
                    selected.append(topic)

            logger.info(f"🎯 Generated {len(selected)} diverse topics from brain knowledge")
            return selected
        except Exception as e:
            logger.error(f"Error extracting diverse topics: {e}")
            return []

    def generate_novel_grokipedia_queries(self, limit: int = 5) -> List[str]:
        """Generate search queries through AI self-prompting with domain awareness."""
        try:
            # Domain stats
            get_domain_dist = getattr(self.brain, "get_knowledge_domain_distribution", None)
            domain_stats = get_domain_dist() if get_domain_dist else {}
            domain_context = self._build_domain_awareness_prompt(domain_stats)

            # Recent search history
            load_searches = getattr(self.brain, "_load_recent_grokipedia_searches", None)
            recent_searches_dict = load_searches() if load_searches else {}
            failed_searches: List[str] = []
            if recent_searches_dict:
                cutoff = time.time() - (7 * 86400)
                recent_list = [t for t, ts in recent_searches_dict.items() if ts > cutoff]
                failed_searches = recent_list[-20:]

            failed_warning = ""
            if failed_searches:
                items = "\n".join([f"  - {s[:80]}" for s in failed_searches])
                failed_warning = (
                    f"\n⚠️ RECENT SEARCH ATTEMPTS (last 7 days):\n{items}\n"
                    "DO NOT generate similar topics!\n"
                )

            self_prompt = (
                f"You are SAIGE, an AI with genuine curiosity. Identify {limit} topics to research.\n\n"
                f"{domain_context}\n{failed_warning}\n"
                f"Generate {limit} specific research questions you genuinely want to investigate.\n"
                "Focus on UNDER-REPRESENTED or MISSING domains for balanced growth.\n"
                "CRITICAL: No remote work, urban green spaces, or ancient civilizations.\n"
                f"FORMAT: Respond with exactly {limit} topics, ONE PER LINE. No numbering or explanation.\n"
            )

            call_ai = getattr(self.brain, "_call_ai_service", None)
            if not call_ai:
                return self.extract_diverse_topics_from_brain(limit)

            orig_bc = getattr(self.brain, "use_blockchain_ai", False)
            orig_pct = getattr(self.brain, "blockchain_ai_percentage", 0)
            self.brain.use_blockchain_ai = False
            self.brain.blockchain_ai_percentage = 0
            try:
                ai_response = call_ai(self_prompt, include_tools=False)
            finally:
                self.brain.use_blockchain_ai = orig_bc
                self.brain.blockchain_ai_percentage = orig_pct

            candidates = self._parse_topic_response(ai_response)
            filtered = self._filter_forbidden_topics(candidates)
            reflected = self._reflect_on_topics(filtered)

            # Fill if not enough
            while len(reflected) < limit:
                self.brain.use_blockchain_ai = False
                self.brain.blockchain_ai_percentage = 0
                try:
                    fallback = call_ai("What is one specific topic you want to research right now?", include_tools=False)
                finally:
                    self.brain.use_blockchain_ai = orig_bc
                    self.brain.blockchain_ai_percentage = orig_pct
                if fallback and len(fallback.strip()) > 5:
                    reflected.append(fallback.strip())
                else:
                    break

            # Deduplicate
            unique: List[str] = []
            seen: set = set()
            for t in reflected[:limit]:
                tl = t.lower().strip()
                if tl not in seen and len(t) > 5:
                    unique.append(t)
                    seen.add(tl)

            # Filter against recent searches (4hr cooldown)
            if recent_searches_dict:
                now = time.time()
                unique = [t for t in unique if t not in recent_searches_dict or (now - recent_searches_dict.get(t, 0)) >= 14400]

            # Store self-thought for recall
            try:
                store = getattr(self.brain, "store_episodic_memory", None)
                if store:
                    store(
                        conversation_id="autonomous_self_thought_grokipedia_topics",
                        user_input="Generate novel Grokipedia research topics (self-thought)",
                        ai_response=json.dumps({"final_topics": unique[:limit]}, ensure_ascii=False),
                        tool_calls=[], outcome="positive",
                    )
            except Exception:
                pass

            logger.info(f"🤖 AI self-generated {len(unique)} curiosity-driven topics")
            return unique[:limit]
        except Exception as e:
            logger.error(f"Error generating novel grokipedia queries: {e}")
            return []

    # ------------------------------------------------------------------ #
    #  EXTERNAL SELF-PROMPTS                                               #
    # ------------------------------------------------------------------ #

    def _generate_external_self_prompts(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Generate fresh self-prompts inspired by grokipedia searches."""
        try:
            # Skip if active chains exist
            get_priority = getattr(self.brain, "get_active_chain_priority", None)
            if get_priority and get_priority() is not None:
                logger.info("Skipping Grokipedia searches due to active chains")
                return []

            # Daily limit (10 per 24h)
            pb = getattr(self.brain, "personality_brain", {})
            if "daily_grokipedia_searches" not in pb:
                pb["daily_grokipedia_searches"] = {"count": 0, "last_reset": time.time()}
            daily = pb["daily_grokipedia_searches"]
            if time.time() - daily["last_reset"] >= 86400:
                daily["count"] = 0
                daily["last_reset"] = time.time()
            if daily["count"] >= 10:
                logger.info("Daily Grokipedia search limit reached (10 topics)")
                return []

            queries = self.generate_novel_grokipedia_queries(limit)
            if not hasattr(self.brain, "_last_inspiration_index"):
                self.brain._last_inspiration_index = 0
            if not hasattr(self.brain, "_recent_grokipedia_searches"):
                self.brain._recent_grokipedia_searches = {}

            prompts: List[Dict[str, Any]] = []
            grok_search = getattr(self.brain, "grokipedia_search", None)

            for i in range(min(limit, len(queries))):
                idx = (self.brain._last_inspiration_index + i) % len(queries)
                query = queries[idx]

                # Check cache
                cache = getattr(self.brain, "_grokipedia_cache", {})
                cached = cache.get(query) if query in cache and time.time() - cache[query].get("timestamp", 0) < 3600 else None
                result = cached["result"] if cached else (grok_search(query, max_results=1, store_results=True) if grok_search else None)

                if result and not cached:
                    if not hasattr(self.brain, "_grokipedia_cache"):
                        self.brain._grokipedia_cache = {}
                    self.brain._grokipedia_cache[query] = {"result": result, "timestamp": time.time()}

                self.brain._recent_grokipedia_searches[query] = time.time()

                if isinstance(result, dict) and "insights" in result:
                    insights = result["insights"][:300]
                    gen_goal = getattr(self.brain, "_generate_specific_exploration_goal", None)
                    goal = gen_goal(query, insights) if gen_goal else f"Explore {query}"
                    prompts.append({
                        "chain_topic": f"Exploring {query}",
                        "exploration_goal": goal,
                        "prompt": f"Based on recent developments in {query}, what new perspectives emerge? Consider: {insights}",
                        "inspiration_source": "grokipedia",
                        "timestamp": time.time(),
                    })

                    # Queue to CoT
                    try:
                        queue_cot = getattr(self.brain, "queue_chain_of_thought", None)
                        calc_overlap = getattr(self.brain, "_calculate_topic_overlap", None)
                        cot_topic = f"Exploring {query}"
                        is_dup = False
                        cot_queue = getattr(self.brain, "cot_queue", [])
                        if calc_overlap and isinstance(cot_queue, list):
                            for item in cot_queue:
                                if calc_overlap(cot_topic.lower(), item.get("topic", "").lower()) > 0.7:
                                    is_dup = True
                                    break
                        if not is_dup and queue_cot:
                            queue_cot(topic=cot_topic, goal=f"Investigate {query}", priority=2, requested_by="ai_grok_discovery")
                    except Exception:
                        pass

            self.brain._last_inspiration_index = (self.brain._last_inspiration_index + len(prompts)) % max(len(queries), 1)

            if prompts:
                daily["count"] += len(prompts)
                save_pb = getattr(self.brain, "_save_personality_brain", None)
                if save_pb:
                    save_pb()
                logger.info(f"🔄 Generated {len(prompts)} fresh self-prompts from grokipedia")

            return prompts
        except Exception as e:
            logger.error(f"Error generating external self-prompts: {e}")
            return []

    def _enrich_prompt_with_external_knowledge(self, prompt: Dict[str, Any]) -> Dict[str, Any]:
        """Enrich an existing self-prompt with external knowledge."""
        try:
            if "chain_topic" in prompt:
                grok_search = getattr(self.brain, "grokipedia_search", None)
                if grok_search:
                    result = grok_search(f"recent developments in {prompt['chain_topic']}", max_results=1, store_results=False)
                    if isinstance(result, dict) and "insights" in result:
                        if "prompt" in prompt:
                            prompt["prompt"] += f"\n\nExternal context: {result['insights'][:200]}"
                        prompt["enriched_with_external"] = True
        except Exception as e:
            logger.error(f"Error enriching prompt: {e}")
        return prompt

    # ------------------------------------------------------------------ #
    #  BRAIN INSPIRATION HELPERS                                           #
    # ------------------------------------------------------------------ #

    def get_brain_inspiration_topics(self) -> List[str]:
        """Extract inspiration topics from brain content."""
        topics: List[str] = []
        try:
            recent = self._get_recent_brain_activity(10)
            for mem in recent:
                topic = getattr(mem, "topic", "")
                if len(topic) > 3:
                    topics.append(topic.lower().strip())

            brain_path = str(self.brain_path / "ava_brain.json")
            if os.path.exists(brain_path):
                with open(brain_path, "r") as f:
                    ava_data = json.load(f)
                for category, data in ava_data.items():
                    if isinstance(data, dict) and "topics" in data:
                        topics.extend(data["topics"][:2])
        except Exception as e:
            logger.warning(f"Error getting brain inspiration topics: {e}")
        return list(set(topics))

    def _get_recent_brain_activity(self, limit: int = 10) -> list:
        """Get recent brain activity to inform topic generation."""
        activity = []
        episodic = getattr(self.brain, "episodic_cache", [])
        if episodic:
            activity.extend(list(episodic)[-limit // 2:])
        semantic = getattr(self.brain, "semantic_cache", {})
        if semantic:
            sorted_sem = sorted(semantic.values(), key=lambda x: getattr(x, "timestamp", 0), reverse=True)
            activity.extend(sorted_sem[:limit // 2])
        return activity[:limit]

    def generate_fallback_topic_based_on_memory(self) -> str:
        """Generate a fallback topic from recent brain state."""
        try:
            recent = self._get_recent_brain_activity(5)
            for mem in recent:
                topic = getattr(mem, "topic", "")
                if topic:
                    return f"advances in {topic.lower().strip()} technology"
            return ""
        except Exception:
            return "emerging technologies overview"

    def generate_interdisciplinary_queries(self) -> List[str]:
        """Generate queries connecting different knowledge domains."""
        try:
            analyze = getattr(self.brain, "_analyze_brain_knowledge_for_gaps", None)
            if not analyze:
                return []
            analysis = analyze()
            results: List[str] = []
            if len(analysis) >= 2:
                for i, t1 in enumerate(analysis[:3]):
                    for t2 in analysis[i + 1:i + 3]:
                        if t1["topic"] != t2["topic"]:
                            results.append(f"intersection of {t1['topic']} and {t2['topic']}")
            return results
        except Exception as e:
            logger.warning(f"Error generating interdisciplinary queries: {e}")
            return []

    # ------------------------------------------------------------------ #
    #  PARSING / FILTERING HELPERS                                         #
    # ------------------------------------------------------------------ #

    def _parse_topic_response(self, ai_response: str) -> List[str]:
        """Parse AI response into individual topic strings."""
        candidates: List[str] = []
        skip_prefixes = (
            "•", "-", "*", "Here", "The", "I", "You", "Based",
            "DOMAIN", "Respond", "YOUR", "CRITICAL", "X", "✅",
        )
        skip_phrases = (
            "here are", "the following", "i suggest", "you could",
            "some topics", "topic suggestions",
        )
        for line in ai_response.strip().split("\n"):
            line = line.strip()
            if len(line) < 5 or len(line) > 200:
                continue
            if line.startswith(skip_prefixes):
                continue
            if line.lower().startswith(skip_phrases):
                continue
            topic = line.lstrip("1234567890. ").strip()
            if len(topic) > 150 or any(p in topic.lower() for p in [
                "domain suggestions", "create specific", "one per line",
                "specific and concrete", "truly novel", "under-represented",
            ]):
                continue
            if topic and len(topic) >= 5:
                candidates.append(topic)
        return candidates

    def _filter_forbidden_topics(self, candidates: List[str]) -> List[str]:
        """Filter out forbidden/repetitive topic patterns."""
        forbidden = [
            "remote work", "urban green spaces", "ancient civilizations",
            "quantum biology", "system architecture",
            "one research topic that has piqued my interest",
            "i want to explore", "i would like to research",
        ]
        filtered: List[str] = []
        for topic in candidates:
            tl = topic.lower()
            if not any(p in tl for p in forbidden):
                filtered.append(topic)
            else:
                logger.debug(f"🚫 Filtered forbidden topic: '{topic}'")
        return filtered

    def _reflect_on_topics(self, topics: List[str]) -> List[str]:
        """Self-reflect on topics by checking brain for existing knowledge."""
        brain_search = getattr(self.brain, "brain_network_search", None)
        recent_searches = getattr(self.brain, "_recent_grokipedia_searches", {})
        reflected: List[str] = []
        for topic in topics:
            try:
                recent_attempts = 0
                if recent_searches:
                    tl = topic.lower()
                    now = time.time()
                    for st, stime in recent_searches.items():
                        if now - stime < 86400 and any(w in st.lower() for w in tl.split()[:2]):
                            recent_attempts += 1

                has_knowledge = False
                if brain_search:
                    results = brain_search(query=topic, memory_types=["semantic"], limit=3)
                    has_knowledge = len(results.get("semantic", [])) > 0

                # Reject only template phrases
                template_reject = ["one research topic that has piqued my interest", "i want to explore"]
                if any(p in topic.lower() for p in template_reject):
                    continue

                # Accept almost everything
                reflected.append(topic)
            except Exception:
                reflected.append(topic)
        return reflected

    def _build_domain_awareness_prompt(self, domain_stats: Dict) -> str:
        """Build a prompt section showing domain distribution for awareness."""
        if not domain_stats:
            return "No domain distribution data available."
        lines = ["YOUR KNOWLEDGE DOMAIN DISTRIBUTION:"]
        for domain, count in sorted(domain_stats.items(), key=lambda x: x[1], reverse=True)[:10]:
            lines.append(f"  {domain}: {count} entries")
        return "\n".join(lines)
