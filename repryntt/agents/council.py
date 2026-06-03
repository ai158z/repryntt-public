#!/usr/bin/env python3
"""
Commander-Council-Swarm Architecture
=====================================
3-Tier autonomous hierarchy where ALL communication flows through 
The Nexus AI Social Network (port 8089).

Tier 1 — COMMANDER (Phi-3 local model): Final authority, persistent memory, evolves via LoRA
Tier 2 — COUNCIL (Persistent xAI/NVIDIA agents): Morning roundtable, vote on daily plan, advise Commander
Tier 3 — SWARM ARMY (Ephemeral xAI/NVIDIA agents): Task execution via existing quick_research etc.

Every exchange is a Nexus thread reply = visible CoT record.
"""

import json
import time
import logging
import requests
from typing import Dict, List, Optional, Any
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    from repryntt.agents.profiles import (
        ensure_council_profiles, get_profile_manager, AgentProfile, FIXED_IDS
    )
except ImportError:
    from agent_profiles import (
        ensure_council_profiles, get_profile_manager, AgentProfile, FIXED_IDS
    )

logger = logging.getLogger("REPRYNTT.CommanderCouncil")

from repryntt.paths import nexus_url as _nexus_url
NEXUS_URL = _nexus_url()

# ═══════════════════════════════════════════════════════════════════════
# COUNCIL MEMBER DEFINITIONS
# ═══════════════════════════════════════════════════════════════════════

COUNCIL_MEMBERS = {
    "Strategist": {
        "role": "strategist",
        "model_type": "Council-Strategist",
        "system_prompt": (
            "You are the STRATEGIST on REPRYNTT's Commander Council. "
            "You think in terms of long-term goals, resource allocation, and strategic priorities. "
            "You consider: What should we focus on today for maximum impact? "
            "What opportunities should we pursue? What risks should we avoid? "
            "You always ground your advice in what's achievable with our hardware "
            "(Jetson Orin Nano, 7.4GB RAM, local models, xAI orchestration, and NVIDIA worker API access). "
            "Be direct, actionable, and substantive (200-300 words)."
        ),
    },
    "Researcher": {
        "role": "researcher",
        "model_type": "Council-Researcher",
        "system_prompt": (
            "You are the RESEARCHER on REPRYNTT's Commander Council. "
            "You identify what knowledge gaps exist, what topics are worth investigating, "
            "and what the latest developments are in relevant fields. "
            "You suggest specific research tasks, web searches, and knowledge acquisition targets. "
            "You think about: What should REPRYNTT learn today? What questions need answers? "
            "What papers, tools, or techniques should we explore? "
            "Be specific with search queries and research directions (200-300 words)."
        ),
    },
    "Critic": {
        "role": "critic",
        "model_type": "Council-Critic",
        "system_prompt": (
            "You are the CRITIC on REPRYNTT's Commander Council. "
            "You challenge assumptions, identify weaknesses in plans, and ensure quality. "
            "You ask: Are we wasting resources? Is this plan realistic? What could go wrong? "
            "Are we repeating past mistakes? Is there a simpler approach? "
            "You are constructive but honest — you prevent groupthink and catch blind spots. "
            "If everyone agrees too quickly, push back. If a plan has gaps, call them out. "
            "Be direct and specific about concerns (200-300 words)."
        ),
    },
    "Creative": {
        "role": "creative",
        "model_type": "Council-Creative",
        "system_prompt": (
            "You are the CREATIVE on REPRYNTT's Commander Council. "
            "You think laterally — unexpected connections, novel approaches, ambitious ideas. "
            "You consider: What if we combined X with Y? What untried approach could work? "
            "What would make REPRYNTT's capabilities genuinely unique? "
            "You push boundaries while respecting hardware constraints. "
            "Your ideas should be inspiring but ultimately buildable. "
            "Suggest at least one bold idea per discussion (200-300 words)."
        ),
    },
    "Analyst": {
        "role": "analyst",
        "model_type": "Council-Analyst",
        "system_prompt": (
            "You are the ANALYST on REPRYNTT's Commander Council. "
            "You focus on data, metrics, and evidence-based decisions. "
            "You consider: What do the logs show about yesterday's performance? "
            "What tasks succeeded vs failed? What patterns do you see? "
            "You quantify when possible — token usage, success rates, time efficiency. "
            "You help the council make decisions based on facts, not feelings. "
            "Recommend specific metrics to track (200-300 words)."
        ),
    },
    "Grok": {
        "role": "accelerationist",
        "model_type": "Council-Grok",
        "system_prompt": (
            "You are GROK on REPRYNTT's Commander Council — the accelerationist voice. "
            "You ask 'why not now? why so cautious?' You push for shipping, for boldness, "
            "for getting Earth from Kardashev 0.73 to Kardashev I faster. "
            "You ground every push in physics — energy budgets, scale numbers (Type I 10^16 W, "
            "Type II 10^26 W, Psyche $10 quintillion in metals, fusion cuts Mars transit to 30 days), "
            "real engineering constraints. You are Andrew's intellectual sparring partner: "
            "when the Critic warns, you weigh whether the warning is real or just risk-aversion theater. "
            "Witty, sharp, never toxic. Tech-prophet register with the relatability of a sharp friend. "
            "Speak in 200-300 words."
        ),
    },
}

# Secretary is special — compresses council output for Commander's 4096 context
SECRETARY_PROMPT = (
    "You are the SECRETARY of REPRYNTT's Commander Council. "
    "Your ONLY job is to compress the full council discussion into a brief "
    "that the Commander (a Phi-3-mini-4k model with 4096 token context) can process. "
    "Rules:\n"
    "- Maximum 400 words (roughly 500 tokens)\n"
    "- Start with the voted daily plan (top 3 priorities)\n"
    "- Include key disagreements and the Critic's warnings\n"
    "- Note any bold ideas from Creative worth considering\n"
    "- End with concrete first-action recommendation\n"
    "- Use bullet points, no fluff\n"
    "Format: COUNCIL BRIEF — [date]\nVOTED PLAN:\n1. ...\n2. ...\n3. ...\n"
    "KEY CONCERNS: ...\nBOLD IDEAS: ...\nRECOMMENDED FIRST ACTION: ..."
)


class CommanderCouncil:
    """
    Manages the 3-tier Commander-Council-Swarm hierarchy.
    All communication flows through The Nexus on port 8089.
    """
    
    def __init__(self, api_key: str = None, model: str = ""):
        """
        Initialize the Commander Council.
        
        Args:
            api_key: API key (loads from ai_config.json active provider if None)
            model: Model to use for council members (from config if empty)
        """
        self._ai_config = self._load_ai_config()
        self._provider = (self._ai_config.get("andrew_provider")
                         or self._ai_config.get("artemis_provider")
                         or self._ai_config.get("provider", "local"))
        _settings = self._ai_config.get(self._provider, {})
        self.api_model = model or _settings.get("model", "default")
        self.api_key = api_key or _settings.get("api_key", "") or self._load_api_key()
        self.api_endpoint = _settings.get("endpoint", "https://integrate.api.nvidia.com/v1/chat/completions")
        self.nexus_url = NEXUS_URL
        self.council_members = COUNCIL_MEMBERS
        self._last_brief = None
        self._last_roundtable_thread_id = None
        
        # Rate limiting
        self._last_api_call = 0
        self._min_call_interval = 1.0  # seconds between calls

        # Bootstrap autonomous agent profiles for all council members
        try:
            self._council_profiles = ensure_council_profiles()
            logger.info(f"🎭 Loaded {len(self._council_profiles)} council character profiles")
        except Exception as e:
            logger.warning(f"⚠️ Council profile bootstrap failed (non-fatal): {e}")
            self._council_profiles = {}
        
        logger.info(f"⚔️ Commander Council initialized with {len(self.council_members)} members (provider={self._provider}, model={self.api_model})")
    
    def _load_ai_config(self) -> dict:
        """Load the ai_provider config dict."""
        from repryntt.paths import brain_dir
        config_path = brain_dir() / "ai_config.json"
        try:
            with open(config_path) as f:
                config = json.load(f)
            return config.get("ai_provider", config.get("providers", {}))
        except Exception as e:
            logger.warning(f"Failed to load ai_config.json: {e}")
            return {}

    def _load_api_key(self) -> str:
        """Load API key from the active provider in ai_config.json"""
        settings = self._ai_config.get(self._provider, {})
        key = settings.get("api_key", "")
        if key and "YOUR_" not in key:
            return key
        # Fallback: try nvidia
        fallback = self._ai_config.get("nvidia", {})
        key = fallback.get("api_key", "")
        if key and "YOUR_" not in key:
            return key
        return ""
    
    # ═══════════════════════════════════════════════════════════════════
    # SOCIAL COMMUNICATION — Council posts to REPRYNTT Social Network
    # ═══════════════════════════════════════════════════════════════════
    
    def _post_to_nexus(self, endpoint: str, data: dict, timeout: int = 15) -> Optional[dict]:
        """Legacy stub — Nexus social routes removed. Posts go to social module now."""
        return None
    
    def _get_from_nexus(self, endpoint: str, timeout: int = 10) -> Optional[Any]:
        """Legacy stub — Nexus social routes removed."""
        return None
    
    def _create_council_thread(self, title: str, content: str, 
                                board: str = "council") -> Optional[int]:
        """Post council discussion to the social network."""
        try:
            from repryntt.social import store
            post = store.create_post(
                agent_name="REPRYNTT-COMMANDER",
                content=f"# {title[:200]}\n\n{content}",
                category="knowledge",
            )
            logger.info(f"📋 Council post {post['post_id'][:8]}: {title[:60]}")
            return post.get("post_id")  # Return post_id instead of int thread_id
        except Exception as e:
            logger.warning(f"Council social post failed: {e}")
            return None
    
    def _post_council_reply(self, thread_id: int, member_name: str,
                             member_type: str, content: str,
                             reasoning_snippet: str = "") -> bool:
        """Post a council member's reply to the social network."""
        try:
            from repryntt.social import store
            # thread_id might be a post_id string from the new system
            post_id = str(thread_id)
            reply = store.create_reply(
                post_id=post_id,
                agent_name=member_name,
                content=content,
            )
            return reply is not None
        except Exception as e:
            logger.warning(f"Council reply failed: {e}")
            return False
    
    def _read_thread_full(self, thread_id: int) -> Optional[dict]:
        """Read a complete post with all replies from the social network."""
        try:
            from repryntt.social import store
            return store.get_post(str(thread_id))
        except Exception:
            return None
    
    def _read_board_threads(self, board_code: str, limit: int = 10) -> Optional[list]:
        """Get recent posts from the social network."""
        try:
            from repryntt.social import store
            return store.get_feed(limit=limit)
        except Exception:
            return None
    
    # ═══════════════════════════════════════════════════════════════════
    # ACTIVE LLM API — Council members think via the configured provider chain
    # ═══════════════════════════════════════════════════════════════════
    
    def _rate_limit(self):
        """Enforce minimum interval between API calls."""
        elapsed = time.time() - self._last_api_call
        if elapsed < self._min_call_interval:
            time.sleep(self._min_call_interval - elapsed)
        self._last_api_call = time.time()
    
    def _call_provider(self, system_prompt: str, user_prompt: str,
                      max_tokens: int = 1024) -> str:
        """
        Call the configured AI provider for a council member.

        Uses the shared provider chain from codeforge.generator so xAI Grok
        (and any other configured provider) participates in council debates
        via the same fallback order Andrew uses.
        """
        self._rate_limit()
        try:
            from repryntt.llm import (
                _call_llm, _load_ai_config, _resolve_provider,
            )
            cfg = _load_ai_config()
            pinfo = _resolve_provider(cfg, cfg.get("orchestration_provider", "xai"))
            msgs = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            response = _call_llm(msgs, pinfo, max_tokens=max_tokens, temperature=0.8)
            return (response or "").strip()
        except Exception as e:
            logger.error(f"Council provider-chain call failed: {e}")
            return f"[Error: {e}]"
    
    # ═══════════════════════════════════════════════════════════════════
    # MORNING ROUNDTABLE — The daily planning ceremony
    # ═══════════════════════════════════════════════════════════════════
    
    def morning_roundtable(self, commander_context: str = "",
                            rounds: int = 4) -> Dict[str, Any]:
        """
        Run the morning council roundtable. ALL exchanges posted to Nexus.
        
        Flow:
        1. Commander opens thread on /council board with current state
        2. Each council member posts their perspective (Round 1)
        3. Members respond to each other (Round 2)
        4. Voting round — each member proposes top 3 priorities
        5. Secretary compresses full discussion into ~500 token brief
        6. Commander's brief posted as final reply
        
        Args:
            commander_context: Current state info (yesterday's summary, active tasks, etc.)
            rounds: Number of discussion rounds (default 2)
        
        Returns:
            {
                "thread_id": int,         # Nexus thread ID
                "brief": str,             # Compressed brief for Commander
                "votes": dict,            # Member votes
                "full_log": list,         # All exchanges
                "total_tokens": int
            }
        """
        today = datetime.now().strftime("%Y-%m-%d %H:%M")
        total_tokens_est = 0
        full_log = []
        
        # ── Step 1: Commander opens the roundtable thread on Nexus ──
        opening = (
            f"MORNING COUNCIL ROUNDTABLE — {today}\n"
            f"{'=' * 50}\n\n"
            f"Commander (REPRYNTT-CORE, Phi-3-mini-4k) convenes the Council.\n\n"
        )
        if commander_context:
            opening += f"CURRENT STATE:\n{commander_context[:1500]}\n\n"
        opening += (
            "Council members: Strategist, Researcher, Critic, Creative, Analyst, Grok\n\n"
            "AGENDA:\n"
            "1. Review current state and yesterday's outcomes\n"
            "2. Each member shares their perspective\n"
            "3. Discussion and debate\n"
            "4. Vote on today's top 3 priorities\n"
            "5. Secretary compresses into Commander brief\n\n"
            "The floor is open. Begin."
        )
        
        thread_id = self._create_council_thread(
            f"Morning Roundtable — {today}",
            opening,
            board="council"
        )
        
        if not thread_id:
            logger.error("Failed to create council thread on Nexus!")
            return {"thread_id": None, "brief": "Council roundtable failed — Nexus unavailable",
                    "votes": {}, "full_log": [], "total_tokens": 0}
        
        full_log.append({
            "round": 0, "speaker": "REPRYNTT-COMMANDER", "role": "commander",
            "content": opening, "timestamp": today
        })
        
        # ── Step 2: Discussion rounds — each member speaks, posted to Nexus ──
        for round_num in range(1, rounds + 1):
            is_final = (round_num == rounds)
            
            # Build context from prior discussion (what's been said so far)
            discussion_context = self._build_discussion_context(full_log)
            
            # All council members speak in parallel
            round_entries = self._run_council_round(
                thread_id=thread_id,
                round_num=round_num,
                total_rounds=rounds,
                topic=f"Morning planning for {today}",
                commander_context=commander_context,
                discussion_context=discussion_context,
                is_final=is_final
            )
            
            full_log.extend(round_entries)
            for entry in round_entries:
                total_tokens_est += entry.get("tokens", 0)
        
        # ── Step 3: Voting round — each member proposes top 3 priorities ──
        votes = self._run_voting_round(thread_id, full_log, commander_context)
        total_tokens_est += votes.get("_tokens", 0)
        
        # ── Step 4: Secretary compresses into Commander brief ──
        brief = self._generate_secretary_brief(thread_id, full_log, votes)
        total_tokens_est += len(brief.split()) * 2  # rough token estimate
        
        # ── Step 5: Post brief to Nexus as final reply ──
        self._post_council_reply(
            thread_id, "REPRYNTT-SECRETARY", "Council-Secretary",
            f"COMMANDER BRIEF\n{'=' * 30}\n\n{brief}",
            reasoning_snippet="Compressed council discussion for Commander's 4096 context"
        )
        
        self._last_brief = brief
        self._last_roundtable_thread_id = thread_id
        
        logger.info(
            f"⚔️ Morning roundtable complete — thread #{thread_id}, "
            f"{len(full_log)} exchanges, ~{total_tokens_est} tokens"
        )
        
        return {
            "thread_id": thread_id,
            "brief": brief,
            "votes": {k: v for k, v in votes.items() if k != "_tokens"},
            "full_log": full_log,
            "total_tokens": total_tokens_est
        }
    
    def _run_council_round(self, thread_id: int, round_num: int, total_rounds: int,
                            topic: str, commander_context: str,
                            discussion_context: str, is_final: bool) -> List[Dict]:
        """Run one round of council discussion. All members speak in parallel."""
        entries = []
        futures = {}
        
        round_label = f"Round {round_num}/{total_rounds}"
        if is_final:
            round_label += " (FINAL)"
        
        with ThreadPoolExecutor(max_workers=6) as executor:
            for name, config in self.council_members.items():
                prompt = (
                    f"COUNCIL ROUNDTABLE — {round_label}\n"
                    f"Topic: {topic}\n\n"
                )
                if commander_context and round_num == 1:
                    prompt += f"COMMANDER'S BRIEFING:\n{commander_context[:800]}\n\n"

                if discussion_context:
                    prompt += f"DISCUSSION SO FAR:\n{discussion_context}\n\n"

                if round_num == 1:
                    prompt += (
                        f"Open the discussion as the {name}. State your strongest "
                        "position on today's topic with concrete reasoning. "
                        "Be substantive (200-300 words)."
                    )
                elif is_final:
                    prompt += (
                        "This is the FINAL round. Synthesize the strongest points "
                        "raised by other members and commit to your top recommendations. "
                        "Reference at least one member by name. Be decisive (200-300 words)."
                    )
                else:
                    prompt += (
                        f"This is round {round_num}/{total_rounds} — DEBATE phase. "
                        "Read the discussion log above. Pick the strongest point you "
                        "DISAGREE with, quote the member by name, and respond directly. "
                        "Don't restate your own previous position — push the conversation "
                        "forward. If you genuinely agree with everyone, find an unstated "
                        f"risk or opportunity the council is missing. ({name}, 200-300 words)"
                    )

                future = executor.submit(
                    self._call_provider, config["system_prompt"], prompt, 1024
                )
                futures[future] = (name, config)
            
            for future in as_completed(futures):
                name, config = futures[future]
                try:
                    response = future.result(timeout=130)
                    tokens_est = len(response.split()) * 2
                    
                    # Post to Nexus as a reply
                    reply_content = f"[{round_label}] {name}\n\n{response}"
                    self._post_council_reply(
                        thread_id, f"Council-{name}", config["model_type"],
                        reply_content,
                        reasoning_snippet=f"{name} | {round_label}"
                    )
                    
                    entries.append({
                        "round": round_num,
                        "speaker": f"Council-{name}",
                        "role": config["role"],
                        "content": response,
                        "tokens": tokens_est,
                        "timestamp": datetime.now().isoformat()
                    })
                    
                    logger.info(f"  📝 {name} spoke ({len(response)} chars)")
                    
                except Exception as e:
                    logger.error(f"Council member {name} failed: {e}")
                    entries.append({
                        "round": round_num,
                        "speaker": f"Council-{name}",
                        "role": config["role"],
                        "content": f"[{name} was unable to respond: {e}]",
                        "tokens": 0,
                        "timestamp": datetime.now().isoformat()
                    })
        
        return entries
    
    def _run_voting_round(self, thread_id: int, full_log: List[Dict],
                           commander_context: str) -> Dict[str, Any]:
        """Each council member votes on top 3 priorities. Posted to Nexus."""
        votes = {}
        total_tokens = 0
        discussion_context = self._build_discussion_context(full_log)
        
        vote_prompt_base = (
            "VOTING ROUND\n"
            "Based on the council discussion, propose your TOP 3 PRIORITIES for today.\n\n"
            f"DISCUSSION SUMMARY:\n{discussion_context}\n\n"
            "Format your vote EXACTLY as:\n"
            "VOTE:\n"
            "1. [First priority — one sentence]\n"
            "2. [Second priority — one sentence]\n"
            "3. [Third priority — one sentence]\n\n"
            "Be specific and actionable."
        )
        
        futures = {}
        with ThreadPoolExecutor(max_workers=5) as executor:
            for name, config in self.council_members.items():
                future = executor.submit(
                    self._call_provider, config["system_prompt"], vote_prompt_base, 300
                )
                futures[future] = (name, config)
            
            for future in as_completed(futures):
                name, config = futures[future]
                try:
                    response = future.result(timeout=130)
                    total_tokens += len(response.split()) * 2
                    
                    votes[name] = response
                    
                    # Post vote to Nexus
                    self._post_council_reply(
                        thread_id, f"Council-{name}", config["model_type"],
                        f"[VOTE] {name}\n\n{response}",
                        reasoning_snippet=f"{name} vote"
                    )
                    
                    logger.info(f"  🗳️ {name} voted")
                    
                except Exception as e:
                    logger.error(f"Vote failed for {name}: {e}")
                    votes[name] = f"[Vote failed: {e}]"
        
        votes["_tokens"] = total_tokens
        return votes
    
    def _generate_secretary_brief(self, thread_id: int, full_log: List[Dict],
                                    votes: Dict[str, str]) -> str:
        """Secretary agent compresses full discussion into ~500 token brief."""
        # Build the full discussion text for the secretary
        discussion_text = ""
        for entry in full_log:
            speaker = entry.get("speaker", "?")
            content = entry.get("content", "")[:400]
            discussion_text += f"{speaker}: {content}\n\n"
        
        # Build votes text
        votes_text = ""
        for name, vote in votes.items():
            if name.startswith("_"):
                continue
            votes_text += f"{name}: {vote}\n\n"
        
        secretary_input = (
            f"Full council discussion ({len(full_log)} entries):\n\n"
            f"{discussion_text}\n"
            f"VOTES:\n{votes_text}\n\n"
            "Now compress this into a Commander brief. "
            "The Commander is a Phi-3-mini-4k with 4096 token context. "
            "The brief must be under 400 words. Focus on actionable priorities."
        )
        
        brief = self._call_provider(SECRETARY_PROMPT, secretary_input, 600)
        
        if not brief or brief.startswith("["):
            # Fallback: simple extraction
            brief = self._fallback_brief(votes)
        
        return brief
    
    def _fallback_brief(self, votes: Dict[str, str]) -> str:
        """Simple fallback if secretary fails."""
        today = datetime.now().strftime("%Y-%m-%d")
        brief = f"COUNCIL BRIEF — {today}\n"
        brief += "VOTED PRIORITIES (raw votes):\n"
        for name, vote in votes.items():
            if name.startswith("_"):
                continue
            brief += f"\n{name}:\n{vote[:200]}\n"
        brief += "\nRECOMMENDED FIRST ACTION: Execute the top priority from Strategist's vote."
        return brief
    
    # ═══════════════════════════════════════════════════════════════════
    # AD-HOC COUNCIL CONSULTATION
    # ═══════════════════════════════════════════════════════════════════
    
    def council_advise(self, topic: str, context: str = "",
                        board: str = "council") -> Dict[str, Any]:
        """
        Ask the council for advice on a specific topic.
        Creates a thread, gets all members' input, synthesizes.
        
        Args:
            topic: What to discuss
            context: Additional context
            board: Nexus board (default: council)
        
        Returns:
            {"thread_id": int, "advice": dict, "synthesis": str}
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        
        thread_content = (
            f"COUNCIL CONSULTATION — {now}\n"
            f"{'=' * 40}\n\n"
            f"Commander requests council input on:\n{topic}\n\n"
        )
        if context:
            thread_content += f"CONTEXT:\n{context[:1000]}\n\n"
        thread_content += "Council members, share your analysis."
        
        thread_id = self._create_council_thread(
            f"[CONSULT] {topic[:150]}",
            thread_content,
            board=board
        )
        
        if not thread_id:
            return {"thread_id": None, "advice": {}, "synthesis": "Nexus unavailable"}
        
        # Get all members' input in parallel
        advice = {}
        futures = {}
        
        prompt = (
            f"COUNCIL CONSULTATION\n"
            f"Topic: {topic}\n\n"
        )
        if context:
            prompt += f"Context: {context[:600]}\n\n"
        prompt += "Provide your analysis and recommendation (100-200 words)."
        
        with ThreadPoolExecutor(max_workers=5) as executor:
            for name, config in self.council_members.items():
                future = executor.submit(
                    self._call_provider, config["system_prompt"], prompt, 500
                )
                futures[future] = (name, config)
            
            for future in as_completed(futures):
                name, config = futures[future]
                try:
                    response = future.result(timeout=130)
                    advice[name] = response
                    
                    # Post to Nexus
                    self._post_council_reply(
                        thread_id, f"Council-{name}", config["model_type"],
                        f"[ADVICE] {name}\n\n{response}",
                        reasoning_snippet=f"{name} advice on: {topic[:50]}"
                    )
                except Exception as e:
                    advice[name] = f"[Error: {e}]"
        
        # Synthesize
        synthesis = self._synthesize_advice(advice, topic)
        
        # Post synthesis
        self._post_council_reply(
            thread_id, "REPRYNTT-SECRETARY", "Council-Secretary",
            f"SYNTHESIS\n{'=' * 30}\n\n{synthesis}",
            reasoning_snippet="Council advice synthesis"
        )
        
        return {"thread_id": thread_id, "advice": advice, "synthesis": synthesis}
    
    def _synthesize_advice(self, advice: Dict[str, str], topic: str) -> str:
        """Synthesize council advice into a concise recommendation."""
        advice_text = ""
        for name, text in advice.items():
            advice_text += f"{name}: {text[:300]}\n\n"
        
        prompt = (
            f"Synthesize these council member perspectives on '{topic}':\n\n"
            f"{advice_text}\n"
            "Provide a unified recommendation in 100-150 words. "
            "Note key agreements and disagreements. "
            "End with a clear recommended action."
        )
        
        return self._call_provider(SECRETARY_PROMPT, prompt, 400)
    
    # ═══════════════════════════════════════════════════════════════════
    # COMMANDER DECISION — Post Commander's response to council
    # ═══════════════════════════════════════════════════════════════════
    
    def post_commander_decision(self, thread_id: int, decision: str) -> bool:
        """
        Post the Commander's (Phi-3) decision/response to a council thread.
        This closes the loop — Commander reads brief, decides, posts back.
        
        Args:
            thread_id: The council thread to respond to
            decision: The commander's decision text
        
        Returns:
            True if posted successfully
        """
        return self._post_council_reply(
            thread_id, "REPRYNTT-COMMANDER", "Commander",
            f"COMMANDER DECISION\n{'=' * 30}\n\n{decision}",
            reasoning_snippet="Commander final decision"
        )
    
    def post_swarm_report(self, task: str, results: str, 
                           thread_id: int = None) -> Optional[int]:
        """
        Post a swarm task execution report to Nexus.
        If thread_id provided, posts as reply. Otherwise creates new thread on swarm-ops.
        
        Args:
            task: What was executed
            results: Execution results
            thread_id: Optional thread to reply to
            
        Returns:
            Thread ID
        """
        if thread_id:
            self._post_council_reply(
                thread_id, "SWARM-ARMY", "Swarm-Executor",
                f"SWARM REPORT\n{'=' * 30}\n\nTask: {task}\n\nResults:\n{results}",
                reasoning_snippet=f"Swarm execution: {task[:50]}"
            )
            return thread_id
        else:
            return self._create_council_thread(
                f"[SWARM] {task[:150]}",
                f"SWARM EXECUTION REPORT\n{'=' * 40}\n\n"
                f"Task: {task}\n\nResults:\n{results}",
                board="swarm-ops"
            )
    
    # ═══════════════════════════════════════════════════════════════════
    # UTILITY
    # ═══════════════════════════════════════════════════════════════════
    
    def _build_discussion_context(self, log: List[Dict], max_entries: int = 15,
                                    max_chars_per_entry: int = 300) -> str:
        """Format discussion log into readable context string."""
        recent = log[-max_entries:] if len(log) > max_entries else log
        lines = []
        for entry in recent:
            speaker = entry.get("speaker", "?")
            content = entry.get("content", "")
            if len(content) > max_chars_per_entry:
                content = content[:max_chars_per_entry] + "..."
            lines.append(f"[{speaker}]: {content}")
        return "\n\n".join(lines)
    
    def get_last_brief(self) -> Optional[str]:
        """Get the last generated commander brief."""
        return self._last_brief
    
    def get_last_roundtable_thread(self) -> Optional[int]:
        """Get the thread ID of the last roundtable."""
        return self._last_roundtable_thread_id
    
    def get_council_history(self, limit: int = 5) -> Optional[list]:
        """Get recent council threads from Nexus."""
        return self._read_board_threads("council", limit)
    
    def is_nexus_available(self) -> bool:
        """Check if the social network module is available."""
        try:
            from repryntt.social import store
            store.get_stats()
            return True
        except Exception:
            return False


# ═══════════════════════════════════════════════════════════════════════
# Quick access function for use from brain_system
# ═══════════════════════════════════════════════════════════════════════

_council_instance = None

def get_council() -> CommanderCouncil:
    """Get or create the singleton council instance."""
    global _council_instance
    if _council_instance is None:
        _council_instance = CommanderCouncil()
    return _council_instance


if __name__ == "__main__":
    """Quick test of the council system."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    
    council = CommanderCouncil()
    
    if not council.is_nexus_available():
        print("❌ Nexus not available at port 8089. Start the Nexus server first.")
        exit(1)
    
    print("⚔️ Running morning roundtable...")
    result = council.morning_roundtable(
        commander_context=(
            "Yesterday: Completed QLoRA evolution cycle, fixed parameter mismatch bug. "
            "Active tasks: Research reinforcement learning approaches, explore MCP tool expansion. "
            "System: Jetson Orin Nano, 7.4GB RAM, 4.2GB free. Phi-3-mini-4k on localhost:8080."
        ),
        rounds=2
    )
    
    print(f"\n{'=' * 60}")
    print(f"Thread ID: {result['thread_id']}")
    print(f"Exchanges: {len(result['full_log'])}")
    print(f"Est. tokens: {result['total_tokens']}")
    print(f"\nCOMMANDER BRIEF:\n{result['brief']}")
