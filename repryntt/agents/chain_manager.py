#!/usr/bin/env python3
"""
agent_chain_manager.py — Multi-Step Chain Reasoning & Persistent Memory for Agents
════════════════════════════════════════════════════════════════════════════════════
Gives each persistent agent their own brain directory:

  brain/agent_brains/{agent_id}/
    ├── chains/              # Chain-of-thought JSON files (same format as local)
    │   ├── agent_chain_xxx.json
    │   └── agent_chain_yyy.json
    ├── memory.json          # Persistent semantic memory (insights, knowledge)
    └── chain_state.json     # Active chain tracking & completed topic list

Architecture:
  - Shared BrainSystem used for tool execution only (too heavy to clone per agent)
  - Each agent gets lightweight persistent state (JSON files, not full BrainSystem)
  - Chains advance 1 step per agent cycle (~3 min intervals)
  - A 5-step chain ≈ 15 min of deep research (vs. old 2-call shallow approach)
  - Chains are same format as local instance for compatibility

Chain lifecycle:
  create_chain() → [advance_chain() × N steps] → conclusion → post to Nexus

No LoRA evolution. No hormone modulation. Those are local-only.
════════════════════════════════════════════════════════════════════════════════════
"""

import os
import re
import json
import time
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List

# Cross-platform file locking
try:
    import fcntl
except ImportError:
    fcntl = None

logger = logging.getLogger("saige.agent_chains")


class AgentChainManager:
    """
    Manages multi-step chain-of-thought reasoning and persistent memory
    for autonomous agents. Each agent gets their own brain directory.
    """

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir) / "agent_brains"
        os.makedirs(self.base_dir, exist_ok=True)
        logger.info(f"🧠 Agent chain manager initialized: {self.base_dir}")

    # ═══════════════════════════════════════════════════════════════
    # AGENT DIRECTORY MANAGEMENT
    # ═══════════════════════════════════════════════════════════════

    def _agent_dir(self, agent_id: str) -> Path:
        """Get (and lazily create) an agent's brain directory."""
        safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', str(agent_id))
        d = self.base_dir / safe_id
        if not d.exists():
            os.makedirs(d / "chains", exist_ok=True)
        return d

    def _chain_path(self, agent_id: str, chain_id: str) -> Path:
        return self._agent_dir(agent_id) / "chains" / f"{chain_id}.json"

    # ═══════════════════════════════════════════════════════════════
    # CHAIN LIFECYCLE
    # ═══════════════════════════════════════════════════════════════

    def has_active_chain(self, agent_id: str) -> bool:
        """Check if an agent currently has an active (non-completed) chain."""
        state = self._load_agent_state(agent_id)
        chain_id = state.get("active_chain_id", "")
        if not chain_id:
            return False

        # Verify chain file exists and is still active
        chain_data = self._load_chain(agent_id, chain_id)
        if not chain_data:
            state["active_chain_id"] = ""
            self._save_agent_state(agent_id, state)
            return False

        if chain_data.get("goal_achieved") or \
           chain_data.get("metadata", {}).get("status") == "completed":
            state["active_chain_id"] = ""
            self._save_agent_state(agent_id, state)
            return False

        return True

    def get_active_chain(self, agent_id: str) -> Optional[Dict]:
        """Get the active chain data, or None if no active chain."""
        state = self._load_agent_state(agent_id)
        chain_id = state.get("active_chain_id", "")
        if not chain_id:
            return None
        chain_data = self._load_chain(agent_id, chain_id)
        if chain_data and not chain_data.get("goal_achieved"):
            return chain_data
        return None

    def create_chain(self, agent_id: str, topic: str, goal: str,
                     action_plan: List[str], target_steps: int = 5,
                     task_type: str = "research_analysis") -> str:
        """
        Create a new multi-step research chain for an agent.
        Returns chain_id on success, empty string on failure/skip.
        """
        # Don't create if agent already has active chain
        if self.has_active_chain(agent_id):
            logger.info(f"[{agent_id}] Already has active chain — skipping creation")
            return ""

        # Check topic repetition (avoid re-researching same things)
        state = self._load_agent_state(agent_id)
        completed_topics = state.get("completed_topics", [])
        topic_lower = topic.lower()
        for ct in completed_topics[-30:]:
            if self._topic_similarity(topic_lower, ct.lower()) > 0.65:
                logger.info(f"[{agent_id}] Topic too similar to completed: '{ct}' — skipping")
                return ""

        # Ensure agent directory exists
        agent_dir = self._agent_dir(agent_id)
        os.makedirs(agent_dir / "chains", exist_ok=True)

        chain_id = f"agent_chain_{int(time.time())}_{hash(topic) % 10000}"

        # Ensure action plan is reasonable
        if not action_plan or len(action_plan) < 2:
            action_plan = [
                f"Search for foundational research on {topic}",
                f"Fetch and analyze key sources and detailed data",
                f"Cross-reference findings with existing knowledge",
                f"Synthesize analysis and identify patterns and implications",
                f"Produce deliverable: write a comprehensive report on {topic}",
            ][:target_steps]

        milestones = action_plan[:target_steps]

        # Generate prompt for step 1
        first_prompt = (
            f"Begin your research on: {topic}\n\n"
            f"Goal: {goal}\n\n"
            f"Your first task: {action_plan[0]}\n\n"
            f"Use tools to gather REAL data. Recommended: knowledge_search, mcp_fetch_fetch, grokipedia_search.\n\n"
            f"Write your research plan, then use tools to begin."
        )

        chain_data = {
            "metadata": {
                "chain_id": chain_id,
                "agent_id": agent_id,
                "topic": topic,
                "goal": goal,
                "created_at": time.time(),
                "status": "active",
                "progress_level": 0.0,
                "milestones": milestones,
                "action_plan": action_plan,
                "expected_duration_steps": target_steps,
                "chain_type": "agent_autonomous",
                "task_type": task_type,
                "autonomous_flags": {
                    "ai_generated_prompts": True,
                    "ai_driven_conclusions": True,
                    "pipeline_of_actions": True,
                }
            },
            "chain_sequence": [
                {
                    "step": 1,
                    "timestamp": time.time(),
                    "prompt": first_prompt,
                    "response": "",
                    "tool_results": None,
                    "insights": [],
                }
            ],
            "overall_insights": [],
            "conclusion": None,
            "goal_achieved": False,
            "milestone_completion": {m: False for m in milestones},
        }

        # Save chain file
        self._save_chain(agent_id, chain_id, chain_data)

        # Update agent chain state
        state["active_chain_id"] = chain_id
        chain_history = state.get("chain_history", [])
        chain_history.append({
            "chain_id": chain_id,
            "topic": topic,
            "created_at": time.time(),
            "status": "active",
        })
        state["chain_history"] = chain_history[-50:]
        self._save_agent_state(agent_id, state)

        logger.info(f"✅ [{agent_id}] Created {target_steps}-step chain {chain_id}: {topic}")
        return chain_id

    def get_current_prompt(self, chain_data: Dict) -> str:
        """Get the prompt for the current (latest, un-responded) step."""
        sequence = chain_data.get("chain_sequence", [])
        if sequence:
            return sequence[-1].get("prompt", "")
        return ""

    def build_chain_context(self, chain_data: Dict, max_chars: int = 4000) -> str:
        """
        Build a context string showing chain progress for injection
        into the LLM's system message.
        """
        meta = chain_data.get("metadata", {})
        topic = meta.get("topic", "Unknown")
        goal = meta.get("goal", "")
        action_plan = meta.get("action_plan", [])
        total_steps = meta.get("expected_duration_steps", 5)
        sequence = chain_data.get("chain_sequence", [])
        current_step = len(sequence)
        insights = chain_data.get("overall_insights", [])

        lines = [
            "",
            "═══ ACTIVE RESEARCH CHAIN ═══",
            f"Topic: {topic}",
            f"Goal: {goal}",
            f"Progress: Step {current_step}/{total_steps}",
            "",
            "Action Plan:",
        ]

        for i, action in enumerate(action_plan, 1):
            if i < current_step:
                lines.append(f"  {i}. ✅ {action}")
            elif i == current_step:
                lines.append(f"  {i}. → {action}  ← CURRENT")
            else:
                lines.append(f"  {i}. ○ {action}")

        # Summarize previous steps (last 3 completed, skip the current empty one)
        completed_steps = [s for s in sequence if s.get("response")]
        if completed_steps:
            lines.append("")
            lines.append("Previous Work:")
            for step_data in completed_steps[-3:]:
                step_num = step_data.get("step", "?")
                response = step_data.get("response", "")
                tool_results = step_data.get("tool_results", "")

                # Summarize response
                if response:
                    summary = response[:350].replace("\n", " ").strip()
                    if len(response) > 350:
                        summary += "..."
                    lines.append(f"  Step {step_num}: {summary}")

                # Summarize tool results
                if tool_results and isinstance(tool_results, str) and len(tool_results) > 10:
                    tool_summary = tool_results[:250].replace("\n", " ").strip()
                    if len(tool_results) > 250:
                        tool_summary += "..."
                    lines.append(f"    Tools used: {tool_summary}")

        # Show accumulated insights
        if insights:
            lines.append("")
            lines.append("Key Insights So Far:")
            for ins in insights[-8:]:
                text = ins if isinstance(ins, str) else \
                       ins.get("content", ins.get("insight", str(ins))) if isinstance(ins, dict) else str(ins)
                lines.append(f"  • {text[:180]}")

        lines.append("")
        lines.append("INSTRUCTIONS: Execute the current step using TOOL_CALLs for real data.")
        lines.append("When ALL objectives are met and a concrete deliverable is produced,")
        lines.append("include 'CHAIN COMPLETE' in your response.")
        lines.append("═══════════════════════════════")

        context = "\n".join(lines)
        if len(context) > max_chars:
            context = context[:max_chars] + "\n... [context truncated]"
        return context

    def advance_chain(self, agent_id: str, chain_id: str,
                      response: str,
                      tool_results_str: Optional[str] = None) -> Dict[str, Any]:
        """
        Advance a chain by one step: record the AI response, extract insights,
        detect conclusion, generate next prompt or conclude.

        Returns:
            {
                should_continue: bool,
                chain_id: str,
                step_completed: int,
                insights: List[str],
                conclusion: Optional[str],
                topic: str,
                next_prompt: Optional[str],
            }
        """
        chain_data = self._load_chain(agent_id, chain_id)
        if not chain_data:
            return {"should_continue": False, "error": "Chain not found", "chain_id": chain_id}

        if chain_data.get("goal_achieved"):
            return {
                "should_continue": False,
                "chain_id": chain_id,
                "conclusion": chain_data.get("conclusion", ""),
                "topic": chain_data["metadata"]["topic"],
            }

        # File-level lock to prevent race conditions
        chain_path = self._chain_path(agent_id, chain_id)
        lock_path = chain_path.with_suffix(".lock")
        lock_fd = open(lock_path, 'w')
        try:
            if fcntl:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif hasattr(__builtins__, '__import__'):
                import msvcrt
                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
        except BlockingIOError:
            lock_fd.close()
            return {"should_continue": True, "skipped": True, "chain_id": chain_id,
                    "topic": chain_data["metadata"]["topic"]}

        try:
            # Record response on the current step
            current_idx = len(chain_data["chain_sequence"]) - 1
            chain_data["chain_sequence"][current_idx]["response"] = response
            chain_data["chain_sequence"][current_idx]["tool_results"] = tool_results_str or ""
            chain_data["chain_sequence"][current_idx]["completed_at"] = time.time()

            # Extract insights
            insights = self._extract_insights(response)
            chain_data["chain_sequence"][current_idx]["insights"] = insights
            chain_data["overall_insights"].extend(insights)
            chain_data["overall_insights"] = chain_data["overall_insights"][-40:]

            # Update milestone progress
            current_step = len(chain_data["chain_sequence"])
            action_plan = chain_data["metadata"].get("action_plan", [])
            expected_steps = chain_data["metadata"].get("expected_duration_steps", 5)

            if current_step <= len(action_plan):
                milestone = action_plan[current_step - 1]
                chain_data["milestone_completion"][milestone] = True

            chain_data["metadata"]["progress_level"] = min(
                0.95, current_step / max(1, expected_steps)
            )

            # ── Conclusion Detection ──
            should_conclude = False

            # 1. AI explicitly signals completion
            response_upper = response.upper()
            if "CHAIN COMPLETE" in response_upper or "CHAIN_COMPLETE" in response_upper:
                should_conclude = True
                logger.info(f"[{agent_id}] AI signaled CHAIN COMPLETE at step {current_step}")

            # 2. Step limit reached
            elif current_step >= expected_steps:
                should_conclude = True
                logger.info(f"[{agent_id}] Chain at step limit ({current_step}/{expected_steps})")

            # 3. Time limit — force-conclude chains older than 60 minutes
            elif time.time() - chain_data["metadata"].get("created_at", 0) > 3600:
                should_conclude = True
                logger.info(f"[{agent_id}] Chain timed out (>60 min)")

            # ── Deliverable Gating ──
            # If concluding early (before step limit), verify substance
            if should_conclude and current_step < expected_steps:
                if not self._verify_substance(chain_data):
                    should_conclude = False
                    # Add a "produce deliverable" retry step
                    goal = chain_data["metadata"].get("goal", "the task")
                    action_plan.append(
                        f"PRODUCE DELIVERABLE: Create a concrete output (report, analysis, "
                        f"specification, code) for: {goal[:80]}"
                    )
                    chain_data["metadata"]["action_plan"] = action_plan
                    chain_data["metadata"]["expected_duration_steps"] = max(
                        expected_steps, current_step + 2
                    )
                    logger.warning(
                        f"[{agent_id}] Conclusion GATED at step {current_step} — "
                        f"no deliverables found, extending chain"
                    )

            if should_conclude:
                # ─── CONCLUDE ───
                conclusion = self._generate_conclusion(chain_data)
                chain_data["conclusion"] = conclusion
                chain_data["goal_achieved"] = True
                chain_data["metadata"]["status"] = "completed"
                chain_data["metadata"]["progress_level"] = 1.0
                chain_data["metadata"]["completed_at"] = time.time()

                # Store in persistent memory
                self._store_chain_completion(agent_id, chain_data)

                # Clear active chain
                state = self._load_agent_state(agent_id)
                state["active_chain_id"] = ""
                completed_topics = state.get("completed_topics", [])
                completed_topics.append(chain_data["metadata"]["topic"])
                state["completed_topics"] = completed_topics[-50:]
                for h in state.get("chain_history", []):
                    if h.get("chain_id") == chain_id:
                        h["status"] = "completed"
                        h["completed_at"] = time.time()
                self._save_agent_state(agent_id, state)

                self._save_chain(agent_id, chain_id, chain_data)

                logger.info(f"✅ [{agent_id}] Chain completed: {chain_data['metadata']['topic']} "
                            f"({current_step} steps, {len(chain_data['overall_insights'])} insights)")

                return {
                    "should_continue": False,
                    "chain_id": chain_id,
                    "step_completed": current_step,
                    "insights": insights,
                    "conclusion": conclusion,
                    "topic": chain_data["metadata"]["topic"],
                }
            else:
                # ─── CONTINUE ───
                next_prompt = self._generate_next_prompt(chain_data)

                chain_data["chain_sequence"].append({
                    "step": current_step + 1,
                    "timestamp": time.time(),
                    "prompt": next_prompt,
                    "response": "",
                    "tool_results": None,
                    "insights": [],
                })

                self._save_chain(agent_id, chain_id, chain_data)

                return {
                    "should_continue": True,
                    "chain_id": chain_id,
                    "step_completed": current_step,
                    "insights": insights,
                    "next_prompt": next_prompt,
                    "topic": chain_data["metadata"]["topic"],
                }

        finally:
            if fcntl:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

    # ═══════════════════════════════════════════════════════════════
    # PROMPT GENERATION
    # ═══════════════════════════════════════════════════════════════

    def _generate_next_prompt(self, chain_data: Dict) -> str:
        """Generate the prompt for the next chain step from the action plan."""
        current_step = len(chain_data["chain_sequence"])
        action_plan = chain_data["metadata"].get("action_plan", [])
        goal = chain_data["metadata"].get("goal", "")
        expected_steps = chain_data["metadata"].get("expected_duration_steps", 5)

        if current_step < len(action_plan):
            action = action_plan[current_step]  # 0-indexed; current_step is the NEXT step
            prompt = (
                f"Execute step {current_step + 1}: {action}\n\n"
                f"Use TOOL_CALLs to gather real data and produce concrete results.\n"
                f"Your overall goal: {goal}\n\n"
                f"If your research is thorough and you have produced a deliverable, "
                f"you may include 'CHAIN COMPLETE' to finish."
            )
        elif current_step >= expected_steps - 1:
            # Final step — synthesize and deliver
            prompt = (
                f"FINAL STEP — Synthesize and deliver:\n\n"
                f"1. Review all your research findings from previous steps\n"
                f"2. Create a CONCRETE deliverable (use write_file to create a report, "
                f"specification, analysis, or code)\n"
                f"3. Summarize key conclusions, implications, and next steps\n\n"
                f"Goal: {goal}\n\n"
                f"Include 'CHAIN COMPLETE' when your deliverable is ready."
            )
        else:
            prompt = (
                f"Continue your research toward: {goal}\n\n"
                f"You have gathered significant data. Focus on:\n"
                f"- Identifying patterns and connections\n"
                f"- Producing a concrete output (use write_file for reports/analysis)\n"
                f"- Drawing evidence-based conclusions\n\n"
                f"Include 'CHAIN COMPLETE' when done."
            )

        return prompt

    # ═══════════════════════════════════════════════════════════════
    # ANALYSIS & INSIGHT EXTRACTION
    # ═══════════════════════════════════════════════════════════════

    def _extract_insights(self, response: str) -> List[str]:
        """Extract key insights/findings from an AI response."""
        insights = []

        # Look for explicit markers
        for pattern in [
            r"(?:Key (?:finding|insight|discovery|result|takeaway))s?:\s*(.+?)(?:\n|$)",
            r"(?:Finding|Insight|Discovery|Result|Conclusion)\s*\d*[.:]\s*(.+?)(?:\n|$)",
            r"(?:•|▸|→|✦|[-*])\s+(.{40,250}?)(?:\n|$)",
        ]:
            matches = re.findall(pattern, response, re.IGNORECASE)
            for m in matches:
                m = m.strip()
                if len(m) > 25 and m not in insights and not m.startswith("TOOL_CALL"):
                    insights.append(m[:250])

        # Fallback: extract first substantive sentence if no markers found
        if not insights:
            sentences = re.split(r'(?<=[.!?])\s+', response[:1200])
            for s in sentences:
                s = s.strip()
                if len(s) > 50 and not s.startswith("TOOL_CALL") and \
                   not s.lower().startswith(("i will", "let me", "i'll", "here")):
                    insights.append(s[:250])
                    if len(insights) >= 2:
                        break

        return insights[:5]

    def _verify_substance(self, chain_data: Dict) -> bool:
        """Check if the chain has produced substantive work (tools used + real content)."""
        total_tool_results = 0
        total_response_length = 0
        has_deliverable = False

        for step in chain_data.get("chain_sequence", []):
            response = step.get("response", "")
            tool_results = step.get("tool_results", "")

            total_response_length += len(response)
            if tool_results and len(str(tool_results)) > 50:
                total_tool_results += 1
            if any(kw in response for kw in ("write_file", "store_learning", "create_creative_file")):
                has_deliverable = True

        # Substance = at least 1 tool used AND decent response content
        return (total_tool_results >= 1 and total_response_length > 500) or has_deliverable

    def _generate_conclusion(self, chain_data: Dict) -> str:
        """Generate a conclusion summary from chain data."""
        insights = chain_data.get("overall_insights", [])
        topic = chain_data["metadata"]["topic"]
        goal = chain_data["metadata"]["goal"]
        steps = len(chain_data["chain_sequence"])

        parts = [
            f"═══ Research Chain Completed ═══",
            f"Topic: {topic}",
            f"Goal: {goal}",
            f"Steps completed: {steps}",
            f"Total insights: {len(insights)}",
            "",
            "Key Findings:",
        ]

        for i, ins in enumerate(insights[-12:], 1):
            text = ins if isinstance(ins, str) else \
                   ins.get("content", str(ins)) if isinstance(ins, dict) else str(ins)
            parts.append(f"  {i}. {text[:250]}")

        return "\n".join(parts)

    def _topic_similarity(self, a: str, b: str) -> float:
        """Simple word-overlap similarity for topic deduplication."""
        words_a = set(re.findall(r'\w{3,}', a.lower()))
        words_b = set(re.findall(r'\w{3,}', b.lower()))
        if not words_a or not words_b:
            return 0.0
        overlap = words_a & words_b
        return len(overlap) / max(len(words_a), len(words_b))

    # ═══════════════════════════════════════════════════════════════
    # PERSISTENT MEMORY
    # ═══════════════════════════════════════════════════════════════

    def _load_agent_memory(self, agent_id: str) -> Dict:
        """Load an agent's persistent memory store."""
        mem_path = self._agent_dir(agent_id) / "memory.json"
        if mem_path.exists():
            try:
                with open(mem_path, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {
            "agent_id": agent_id,
            "semantic_memories": [],
            "completed_chains_summary": [],
            "total_insights": 0,
        }

    def _save_agent_memory(self, agent_id: str, memory: Dict):
        """Save an agent's persistent memory."""
        mem_path = self._agent_dir(agent_id) / "memory.json"
        try:
            with open(mem_path, 'w') as f:
                json.dump(memory, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"[{agent_id}] Failed to save memory: {e}")

    def store_insight(self, agent_id: str, topic: str, content: str,
                      source: str = "chain", domain: str = ""):
        """Store a single insight in the agent's persistent memory."""
        memory = self._load_agent_memory(agent_id)
        memory["semantic_memories"].append({
            "topic": topic,
            "content": content[:500],
            "source": source,
            "domain": domain,
            "timestamp": time.time(),
        })
        # Cap at 200 memories
        if len(memory["semantic_memories"]) > 200:
            memory["semantic_memories"] = memory["semantic_memories"][-200:]
        memory["total_insights"] = memory.get("total_insights", 0) + 1
        self._save_agent_memory(agent_id, memory)

    def _store_chain_completion(self, agent_id: str, chain_data: Dict):
        """Store a completed chain's summary and insights in persistent memory."""
        memory = self._load_agent_memory(agent_id)

        # Store chain summary
        memory["completed_chains_summary"].append({
            "topic": chain_data["metadata"]["topic"],
            "goal": chain_data["metadata"]["goal"],
            "conclusion": (chain_data.get("conclusion") or "")[:500],
            "insights_count": len(chain_data.get("overall_insights", [])),
            "steps": len(chain_data.get("chain_sequence", [])),
            "completed_at": time.time(),
        })
        if len(memory["completed_chains_summary"]) > 50:
            memory["completed_chains_summary"] = memory["completed_chains_summary"][-50:]

        # Store key insights as semantic memories
        for insight in chain_data.get("overall_insights", [])[-5:]:
            content = insight if isinstance(insight, str) else \
                      insight.get("content", str(insight)) if isinstance(insight, dict) else str(insight)
            memory["semantic_memories"].append({
                "topic": chain_data["metadata"]["topic"],
                "content": content[:500],
                "source": "chain_completion",
                "domain": chain_data["metadata"].get("task_type", ""),
                "timestamp": time.time(),
            })

        if len(memory["semantic_memories"]) > 200:
            memory["semantic_memories"] = memory["semantic_memories"][-200:]
        memory["total_insights"] = memory.get("total_insights", 0) + \
                                   len(chain_data.get("overall_insights", []))
        self._save_agent_memory(agent_id, memory)

    def get_memory_context(self, agent_id: str, max_chars: int = 2000) -> str:
        """
        Get a formatted memory context string for injection into an agent's
        system prompt, giving them long-term memory across cycles and restarts.
        """
        memory = self._load_agent_memory(agent_id)
        parts = []

        # Recent chain completions
        chains = memory.get("completed_chains_summary", [])
        if chains:
            parts.append("YOUR COMPLETED RESEARCH (persistent memory):")
            for ch in chains[-5:]:
                conclusion_preview = (ch.get("conclusion") or "")[:120]
                parts.append(f"  • [{ch.get('topic', '?')}] {conclusion_preview}")

        # Key knowledge (recent semantic memories)
        memories = memory.get("semantic_memories", [])
        if memories:
            parts.append("\nYOUR ACCUMULATED KNOWLEDGE:")
            for m in memories[-10:]:
                parts.append(f"  • [{m.get('topic', '?')}] {m['content'][:150]}")

        # Stats
        total = memory.get("total_insights", 0)
        chain_count = len(chains)
        if total > 0:
            parts.append(f"\nResearch stats: {total} insights from {chain_count} completed chains")

        context = "\n".join(parts)
        if len(context) > max_chars:
            context = context[:max_chars] + "\n... [memory truncated]"
        return context if parts else ""

    # ═══════════════════════════════════════════════════════════════
    # STATE & FILE I/O
    # ═══════════════════════════════════════════════════════════════

    def _load_agent_state(self, agent_id: str) -> Dict:
        """Load an agent's lightweight chain tracking state."""
        state_path = self._agent_dir(agent_id) / "chain_state.json"
        if state_path.exists():
            try:
                with open(state_path, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {
            "agent_id": agent_id,
            "active_chain_id": "",
            "chain_history": [],
            "completed_topics": [],
        }

    def _save_agent_state(self, agent_id: str, state: Dict):
        """Save an agent's chain tracking state."""
        state_path = self._agent_dir(agent_id) / "chain_state.json"
        try:
            with open(state_path, 'w') as f:
                json.dump(state, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"[{agent_id}] Failed to save chain state: {e}")

    def _load_chain(self, agent_id: str, chain_id: str) -> Optional[Dict]:
        """Load a chain JSON file."""
        path = self._chain_path(agent_id, chain_id)
        if path.exists():
            try:
                with open(path, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"[{agent_id}] Failed to load chain {chain_id}: {e}")
        return None

    def _save_chain(self, agent_id: str, chain_id: str, chain_data: Dict):
        """Save a chain JSON file."""
        path = self._chain_path(agent_id, chain_id)
        os.makedirs(path.parent, exist_ok=True)
        try:
            with open(path, 'w') as f:
                json.dump(chain_data, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"[{agent_id}] Failed to save chain {chain_id}: {e}")

    def cleanup_old_chains(self, agent_id: str, keep: int = 20):
        """Remove old completed chain files, keeping the most recent ones."""
        chains_dir = self._agent_dir(agent_id) / "chains"
        if not chains_dir.exists():
            return

        chain_files = sorted(
            chains_dir.glob("agent_chain_*.json"),
            key=lambda p: p.stat().st_mtime
        )
        if len(chain_files) > keep:
            for f in chain_files[:-keep]:
                try:
                    f.unlink()
                    lock = f.with_suffix(".lock")
                    if lock.exists():
                        lock.unlink()
                except Exception:
                    pass
