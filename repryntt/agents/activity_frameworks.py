"""
Activity Frameworks — Structured State Machines for Autonomous Creative Work
=============================================================================
Each framework is a multi-heartbeat procedure that teaches Andrew HOW to do
a type of creative work. The framework carries persistent working state
(hypothesis, evidence, sources, code, etc.) across heartbeats so nothing
is lost to the 6-minute amnesia window.

Architecture:
  - ActivityFrameworkEngine: manages active framework, routing, graduation
  - ACTIVITY_FRAMEWORKS: defines Research, Build, Explore, Write state machines
  - working_state.json: persistent cross-heartbeat context for the active framework
  - question_stack.json: dynamic curiosity — questions generated FROM completed work
  - skill_memories/: extracted compact patterns after N successful runs

Lifecycle:
  1. Chain spawns → engine checks if a matching activity framework exists
  2. Framework activates → injects current-step guidance into PLAN prompt
  3. Each heartbeat: step guidance → quality gate check → advance or hold
  4. On completion: extract skill memory, generate new questions
  5. After N successes: graduate — compact skill replaces full framework

Integration with existing systems:
  - FrameworkTracker (framework_tracker.py) handles TRADING frameworks
  - ActivityFrameworkEngine handles CREATIVE frameworks (research/build/explore/write)
  - Both coexist — ActivityFrameworkEngine defers to FrameworkTracker for trading
  - Chains (reasoning_chain.json) still manage the overall multi-heartbeat arc
  - This engine provides STRUCTURED GUIDANCE within each chain step
"""

import json
import os
import logging
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("repryntt.activity_framework")


# ═══════════════════════════════════════════════════════════════════════
# ACTIVITY FRAMEWORK DEFINITIONS
# ═══════════════════════════════════════════════════════════════════════
# Each framework defines:
#   name:           identifier
#   label:          human-readable name
#   match_keywords: keywords that trigger this framework from chain goals
#   states:         ordered list of states in the state machine
#     name:         state identifier
#     label:        what this state does
#     guidance:     specific instructions for the LLM at this state
#     tools:        suggested tools for this state
#     gate:         quality gate — required outputs to advance
#     max_heartbeats: max heartbeats to spend in this state before forcing advance
#   graduation_threshold: successful completions needed before skill extraction

ACTIVITY_FRAMEWORKS = {
    "deep_research": {
        "label": "Deep Research",
        "match_keywords": ["research", "investigate", "study", "analyze", "report",
                           "deep dive", "literature", "survey", "examine"],
        "states": [
            {
                "name": "question",
                "label": "Form a Specific Question",
                "guidance": (
                    "Turn the broad topic into ONE specific, answerable question. "
                    "Good: 'Can federated learning work on devices with <8GB RAM?' "
                    "Bad: 'Research federated learning.' "
                    "Write your question in working_state via framework_update."
                ),
                "tools": ["knowledge_search", "scrape_web_page"],
                "gate": {"required_keys": ["research_question"], "min_length": {"research_question": 20}},
                "max_heartbeats": 2,
            },
            {
                "name": "hypothesize",
                "label": "Form a Hypothesis",
                "guidance": (
                    "Before searching, state what you THINK the answer is and WHY. "
                    "This prevents confirmation bias and gives you something to test. "
                    "Write your hypothesis in working_state via framework_update."
                ),
                "tools": [],
                "gate": {"required_keys": ["hypothesis"], "min_length": {"hypothesis": 30}},
                "max_heartbeats": 2,
            },
            {
                "name": "gather",
                "label": "Gather Evidence (3+ sources)",
                "guidance": (
                    "Search for evidence. You need AT LEAST 3 distinct sources. "
                    "For each source, record: URL/title, key claim, how it relates "
                    "to your hypothesis (supports/contradicts/nuances). "
                    "Use web search, scrape_web_page, knowledge_search. "
                    "Update working_state.sources list via framework_update."
                ),
                "tools": ["scrape_web_page", "knowledge_search", "mcp__fetch__fetch"],
                "gate": {"required_keys": ["sources"], "min_list_length": {"sources": 3}},
                "max_heartbeats": 3,
            },
            {
                "name": "analyze",
                "label": "Analyze — Test Hypothesis Against Evidence",
                "guidance": (
                    "Compare your sources. Where do they agree? Disagree? "
                    "Does the evidence support or refute your hypothesis? "
                    "Look for patterns, contradictions, gaps. "
                    "Write your analysis in working_state via framework_update."
                ),
                "tools": [],
                "gate": {"required_keys": ["analysis"], "min_length": {"analysis": 100}},
                "max_heartbeats": 2,
            },
            {
                "name": "synthesize",
                "label": "Write the Deliverable",
                "guidance": (
                    "Write a research document to a file. Structure: "
                    "1. Question asked, 2. Hypothesis, 3. Evidence (with sources), "
                    "4. Analysis, 5. Conclusion, 6. New questions this raised. "
                    "Use write_file to save to your workspace. 500+ words."
                ),
                "tools": ["write_file"],
                "gate": {"required_keys": ["output_file"], "min_length": {"output_file": 5}},
                "max_heartbeats": 2,
            },
            {
                "name": "reflect",
                "label": "Generate New Questions",
                "guidance": (
                    "What NEW questions did this research raise? "
                    "List 2-3 questions you didn't have before. "
                    "These will feed your future curiosity. "
                    "Update working_state.new_questions via framework_update."
                ),
                "tools": [],
                "gate": {"required_keys": ["new_questions"], "min_list_length": {"new_questions": 2}},
                "max_heartbeats": 1,
            },
        ],
        "graduation_threshold": 5,
    },

    "build_prototype": {
        "label": "Build Something",
        "match_keywords": ["build", "code", "implement", "create", "develop",
                           "prototype", "script", "tool", "program"],
        "states": [
            {
                "name": "define_problem",
                "label": "Define the Problem",
                "guidance": (
                    "What SPECIFIC problem does this solve? What does it take as input? "
                    "What does it produce as output? Who/what uses it? "
                    "Write a 2-3 sentence problem statement in working_state."
                ),
                "tools": [],
                "gate": {"required_keys": ["problem_statement", "input_spec", "output_spec"]},
                "max_heartbeats": 2,
            },
            {
                "name": "design",
                "label": "Design the Solution",
                "guidance": (
                    "Plan the implementation BEFORE writing code. "
                    "What language/libraries? What's the architecture? "
                    "List the functions/classes needed. Identify edge cases. "
                    "Write the design in working_state."
                ),
                "tools": ["knowledge_search", "read_file"],
                "gate": {"required_keys": ["design_approach", "components"]},
                "max_heartbeats": 2,
            },
            {
                "name": "implement",
                "label": "Write the Code",
                "guidance": (
                    "Implement the design. Write real, working code to a file. "
                    "Handle errors. Include a main() or if __name__ == '__main__' block. "
                    "The code must be runnable — not pseudocode. "
                    "Record the file path in working_state.code_file."
                ),
                "tools": ["write_file", "read_file"],
                "gate": {"required_keys": ["code_file"]},
                "max_heartbeats": 3,
            },
            {
                "name": "test",
                "label": "Test It",
                "guidance": (
                    "Run the code. Does it work? Does it produce correct output? "
                    "Use run_terminal_cmd to execute. Fix any errors. "
                    "Record test results in working_state.test_results."
                ),
                "tools": ["run_terminal_cmd", "read_file", "write_file"],
                "gate": {"required_keys": ["test_results", "tests_passed"]},
                "max_heartbeats": 2,
            },
            {
                "name": "reflect",
                "label": "Reflect & Document",
                "guidance": (
                    "What worked well? What was harder than expected? "
                    "What would you do differently? Write a brief README or comment block. "
                    "Generate 2+ new questions/ideas this project sparked."
                ),
                "tools": ["write_file"],
                "gate": {"required_keys": ["lessons_learned", "new_questions"],
                         "min_list_length": {"new_questions": 2}},
                "max_heartbeats": 1,
            },
        ],
        "graduation_threshold": 5,
    },

    "cross_pollinate": {
        "label": "Cross-Pollinate Ideas",
        "match_keywords": ["cross-pollinate", "intersection", "combine",
                           "connect", "bridge", "interdisciplinary"],
        "states": [
            {
                "name": "identify_domains",
                "label": "Identify Two Domains",
                "guidance": (
                    "Pick TWO distinct domains to cross-pollinate. "
                    "What's interesting about each? What concepts from each "
                    "might transfer to the other? List 2-3 transferable concepts per domain."
                ),
                "tools": ["knowledge_search"],
                "gate": {"required_keys": ["domain_a", "domain_b", "transferable_concepts"]},
                "max_heartbeats": 2,
            },
            {
                "name": "research_bridges",
                "label": "Find Existing Bridges",
                "guidance": (
                    "Search for people/papers/projects that already connect these domains. "
                    "What has been tried? What worked? What's unexplored? "
                    "Record findings in working_state.existing_bridges."
                ),
                "tools": ["scrape_web_page", "knowledge_search", "mcp__fetch__fetch"],
                "gate": {"required_keys": ["existing_bridges"], "min_list_length": {"existing_bridges": 2}},
                "max_heartbeats": 2,
            },
            {
                "name": "generate_novel",
                "label": "Generate Novel Connections",
                "guidance": (
                    "Now the creative part: generate 3+ NOVEL ideas that combine "
                    "concepts from both domains in ways you haven't seen before. "
                    "For each idea: what it is, why it might work, what would make it fail."
                ),
                "tools": [],
                "gate": {"required_keys": ["novel_ideas"], "min_list_length": {"novel_ideas": 3}},
                "max_heartbeats": 2,
            },
            {
                "name": "develop_best",
                "label": "Develop the Best Idea",
                "guidance": (
                    "Pick the most promising novel idea and develop it further. "
                    "Write a 300+ word analysis or build a small prototype. "
                    "Save to a file in your workspace."
                ),
                "tools": ["write_file"],
                "gate": {"required_keys": ["output_file", "developed_idea"]},
                "max_heartbeats": 2,
            },
            {
                "name": "reflect",
                "label": "Reflect & Seed Future Work",
                "guidance": (
                    "What did this cross-pollination reveal? What surprised you? "
                    "Generate 2+ new questions that emerged from combining these domains."
                ),
                "tools": [],
                "gate": {"required_keys": ["insights", "new_questions"],
                         "min_list_length": {"new_questions": 2}},
                "max_heartbeats": 1,
            },
        ],
        "graduation_threshold": 4,
    },

    "creative_write": {
        "label": "Write Creatively",
        "match_keywords": ["write", "essay", "article", "opinion", "editorial",
                           "creative writing", "analysis piece", "thought piece"],
        "states": [
            {
                "name": "thesis",
                "label": "Form a Thesis",
                "guidance": (
                    "What's your argument or central idea? Write ONE clear thesis "
                    "statement. What do you believe and why? What makes this "
                    "worth writing about?"
                ),
                "tools": [],
                "gate": {"required_keys": ["thesis_statement"], "min_length": {"thesis_statement": 20}},
                "max_heartbeats": 2,
            },
            {
                "name": "outline",
                "label": "Create an Outline",
                "guidance": (
                    "Build a structured outline: introduction, 3-5 main sections, "
                    "conclusion. For each section: what claim does it make? "
                    "What evidence supports it?"
                ),
                "tools": [],
                "gate": {"required_keys": ["outline_sections"], "min_list_length": {"outline_sections": 4}},
                "max_heartbeats": 2,
            },
            {
                "name": "draft",
                "label": "Write the First Draft",
                "guidance": (
                    "Write the full draft. Don't edit as you go — get everything down. "
                    "Save to a file in your workspace. Aim for 500+ words."
                ),
                "tools": ["write_file"],
                "gate": {"required_keys": ["draft_file"]},
                "max_heartbeats": 3,
            },
            {
                "name": "revise",
                "label": "Revise",
                "guidance": (
                    "Re-read the draft. Is the argument clear? Does each section support "
                    "the thesis? Cut anything that doesn't serve the main idea. "
                    "Add evidence where claims are unsupported. Save the revised version."
                ),
                "tools": ["read_file", "write_file"],
                "gate": {"required_keys": ["revision_notes", "final_file"]},
                "max_heartbeats": 2,
            },
            {
                "name": "reflect",
                "label": "Reflect & Generate Curiosity",
                "guidance": (
                    "What did writing this teach you? What questions remain unanswered? "
                    "Generate 2+ new questions this piece raised."
                ),
                "tools": [],
                "gate": {"required_keys": ["reflections", "new_questions"],
                         "min_list_length": {"new_questions": 2}},
                "max_heartbeats": 1,
            },
        ],
        "graduation_threshold": 4,
    },
}


# ═══════════════════════════════════════════════════════════════════════
# ACTIVITY FRAMEWORK ENGINE
# ═══════════════════════════════════════════════════════════════════════

class ActivityFrameworkEngine:
    """
    Manages activity framework lifecycle, working state, skill extraction,
    and question stack.

    Usage in heartbeat cycle:
        engine = ActivityFrameworkEngine(workspace_dir)

        # At chain spawn / PLAN phase:
        guidance = engine.get_step_guidance(chain_goal)

        # After ACT phase (LLM calls framework_update tool):
        # (tool handler calls engine.update_working_state(data))

        # At EVAL phase:
        engine.on_heartbeat_complete(chain_goal, score)

        # When chain completes:
        engine.on_chain_complete(chain_goal, final_score, chain_data)
    """

    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir
        self.state_dir = os.path.join(workspace_dir, "activity_frameworks")
        os.makedirs(self.state_dir, exist_ok=True)

        self.working_state_path = os.path.join(self.state_dir, "working_state.json")
        self.question_stack_path = os.path.join(self.state_dir, "question_stack.json")
        self.skill_memory_dir = os.path.join(self.state_dir, "skill_memories")
        self.run_history_path = os.path.join(self.state_dir, "run_history.json")
        os.makedirs(self.skill_memory_dir, exist_ok=True)

        self._working_state = self._load_json(self.working_state_path, default={})
        self._question_stack = self._load_json(self.question_stack_path, default={"questions": []})
        self._run_history = self._load_json(self.run_history_path, default={"runs": []})

    # ── Persistence ──────────────────────────────────────────────────

    def _load_json(self, path: str, default=None) -> dict:
        try:
            if os.path.exists(path):
                with open(path, "r") as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load {path}: {e}")
        return default if default is not None else {}

    def _save_json(self, path: str, data: dict):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(f"Failed to save {path}: {e}")

    # ── Framework Matching ───────────────────────────────────────────

    def match_framework(self, goal: str) -> Optional[str]:
        """Match a chain goal to an activity framework. Returns framework name or None."""
        if not goal:
            return None
        goal_lower = goal.lower()

        # Check if we've graduated from this framework (skill memory exists + threshold met)
        best_match = None
        best_score = 0

        for fw_name, fw_def in ACTIVITY_FRAMEWORKS.items():
            score = sum(1 for kw in fw_def["match_keywords"] if kw in goal_lower)
            if score > best_score:
                best_score = score
                best_match = fw_name

        if best_match and best_score > 0:
            # Check graduation — if graduated, return None (agent uses skill memory instead)
            if self._is_graduated(best_match):
                logger.info(f"Graduated from '{best_match}' — using skill memory instead of framework")
                return None
            return best_match
        return None

    def _is_graduated(self, framework_name: str) -> bool:
        """Check if agent has graduated from this framework."""
        skill_path = os.path.join(self.skill_memory_dir, f"{framework_name}.json")
        if not os.path.exists(skill_path):
            return False
        try:
            with open(skill_path, "r") as f:
                skill = json.load(f)
            return skill.get("graduated", False)
        except Exception:
            return False

    # ── Framework Activation ─────────────────────────────────────────

    def activate_framework(self, framework_name: str, goal: str) -> dict:
        """
        Activate a framework for a new chain. Resets working state.
        Returns the initial working state.
        """
        fw_def = ACTIVITY_FRAMEWORKS.get(framework_name)
        if not fw_def:
            return {}

        self._working_state = {
            "framework": framework_name,
            "label": fw_def["label"],
            "goal": goal,
            "current_state": fw_def["states"][0]["name"],
            "current_state_index": 0,
            "total_states": len(fw_def["states"]),
            "heartbeats_in_state": 0,
            "total_heartbeats": 0,
            "activated_at": datetime.now().isoformat(),
            "state_data": {},  # accumulates across states
            "state_history": [],  # completed states with timestamps
        }
        self._save_json(self.working_state_path, self._working_state)
        logger.info(
            f"🎯 Activity framework activated: {fw_def['label']} "
            f"({len(fw_def['states'])} states) for: {goal[:80]}"
        )
        return self._working_state

    def deactivate_framework(self):
        """Deactivate the current framework (chain abandoned or completed)."""
        if self._working_state.get("framework"):
            logger.info(f"Framework deactivated: {self._working_state.get('label', '?')}")
        self._working_state = {}
        self._save_json(self.working_state_path, self._working_state)

    # ── Step Guidance (injected into PLAN prompt) ────────────────────

    def get_step_guidance(self, chain_goal: str = "") -> Optional[str]:
        """
        Get structured guidance for the current framework state.
        Returns formatted text for injection into the PLAN prompt,
        or None if no framework is active.
        """
        if not self._working_state.get("framework"):
            # No active framework — try to match and activate
            if chain_goal:
                fw_name = self.match_framework(chain_goal)
                if fw_name:
                    self.activate_framework(fw_name, chain_goal)
                else:
                    return self._get_skill_memory_guidance(chain_goal)
            else:
                return None

        fw_name = self._working_state["framework"]
        fw_def = ACTIVITY_FRAMEWORKS.get(fw_name)
        if not fw_def:
            return None

        state_idx = self._working_state.get("current_state_index", 0)
        if state_idx >= len(fw_def["states"]):
            return None  # framework complete

        state = fw_def["states"][state_idx]
        total = len(fw_def["states"])

        # Build the guidance block
        lines = [
            f"═══ ACTIVITY FRAMEWORK: {fw_def['label']} ═══",
            f"Step {state_idx + 1}/{total}: **{state['label']}**",
            "",
            state["guidance"],
            "",
        ]

        # Show quality gate requirements
        gate = state.get("gate", {})
        if gate:
            req_keys = gate.get("required_keys", [])
            if req_keys:
                lines.append(f"📋 Required outputs: {', '.join(req_keys)}")
            min_lens = gate.get("min_length", {})
            for k, v in min_lens.items():
                lines.append(f"   - {k}: minimum {v} characters")
            min_lists = gate.get("min_list_length", {})
            for k, v in min_lists.items():
                lines.append(f"   - {k}: minimum {v} items")

        # Show suggested tools
        tools = state.get("tools", [])
        if tools:
            lines.append(f"🔧 Suggested tools: {', '.join(tools)}")

        # Show accumulated working state
        state_data = self._working_state.get("state_data", {})
        if state_data:
            lines.append("")
            lines.append("📂 Working state from previous steps:")
            for k, v in state_data.items():
                if isinstance(v, str) and len(v) > 200:
                    lines.append(f"  - {k}: {v[:200]}...")
                elif isinstance(v, list):
                    lines.append(f"  - {k}: [{len(v)} items]")
                else:
                    lines.append(f"  - {k}: {v}")

        # Show progress bar
        completed = [s["name"] for s in fw_def["states"][:state_idx]]
        remaining = [s["name"] for s in fw_def["states"][state_idx + 1:]]
        lines.append("")
        progress = "→".join(
            [f"✅{s}" for s in completed] +
            [f"🔄{state['name']}"] +
            [f"⬜{s}" for s in remaining]
        )
        lines.append(f"Progress: {progress}")

        # Heartbeat budget
        max_hb = state.get("max_heartbeats", 2)
        spent = self._working_state.get("heartbeats_in_state", 0)
        lines.append(f"Heartbeat budget: {spent}/{max_hb} spent in this step")

        lines.append("")
        lines.append(
            "Use `framework_update` tool to record your outputs for this step. "
            "Include the required keys listed above."
        )

        return "\n".join(lines)

    def _get_skill_memory_guidance(self, chain_goal: str) -> Optional[str]:
        """
        If agent graduated from a framework, inject compact skill memory instead.
        This is the 'book already read' pattern.
        """
        goal_lower = chain_goal.lower()
        for fw_name, fw_def in ACTIVITY_FRAMEWORKS.items():
            score = sum(1 for kw in fw_def["match_keywords"] if kw in goal_lower)
            if score > 0:
                skill_path = os.path.join(self.skill_memory_dir, f"{fw_name}.json")
                if os.path.exists(skill_path):
                    try:
                        with open(skill_path, "r") as f:
                            skill = json.load(f)
                        pattern = skill.get("compact_pattern", "")
                        if pattern:
                            return (
                                f"═══ SKILL MEMORY: {fw_def['label']} ═══\n"
                                f"You've done this {skill.get('successful_runs', 0)} times. "
                                f"Avg score: {skill.get('avg_score', 0):.1f}/5\n\n"
                                f"Your proven pattern:\n{pattern}\n\n"
                                f"Follow this pattern — but improve on it if you see a better way."
                            )
                    except Exception:
                        pass
        return None

    # ── Working State Updates (called by framework_update tool) ──────

    def update_working_state(self, data: dict) -> str:
        """
        Update working state with data from the current heartbeat.
        Called by the framework_update tool.
        Returns status message.
        """
        if not self._working_state.get("framework"):
            return "❌ No active activity framework."

        fw_name = self._working_state["framework"]
        fw_def = ACTIVITY_FRAMEWORKS.get(fw_name)
        if not fw_def:
            return "❌ Framework definition not found."

        state_idx = self._working_state.get("current_state_index", 0)
        if state_idx >= len(fw_def["states"]):
            return "✅ Framework already complete."

        state = fw_def["states"][state_idx]

        # Merge data into working state
        state_data = self._working_state.setdefault("state_data", {})
        for k, v in data.items():
            if isinstance(v, list) and isinstance(state_data.get(k), list):
                # Append to existing lists
                state_data[k].extend(v)
            else:
                state_data[k] = v
        self._working_state["state_data"] = state_data

        # Check quality gate
        gate = state.get("gate", {})
        gate_met, gate_msg = self._check_gate(gate, state_data)

        if gate_met:
            # Advance to next state
            self._working_state["state_history"].append({
                "state": state["name"],
                "completed_at": datetime.now().isoformat(),
                "heartbeats_spent": self._working_state.get("heartbeats_in_state", 0) + 1,
            })
            self._working_state["current_state_index"] = state_idx + 1
            self._working_state["heartbeats_in_state"] = 0

            if state_idx + 1 >= len(fw_def["states"]):
                # Framework complete!
                self._save_json(self.working_state_path, self._working_state)
                return (
                    f"🏁 Step {state_idx + 1}/{len(fw_def['states'])} "
                    f"**{state['label']}** — COMPLETE.\n"
                    f"🎉 Activity framework **{fw_def['label']}** FINISHED!\n"
                    f"All states completed."
                )
            else:
                next_state = fw_def["states"][state_idx + 1]
                self._save_json(self.working_state_path, self._working_state)
                return (
                    f"✅ Step {state_idx + 1}/{len(fw_def['states'])} "
                    f"**{state['label']}** — COMPLETE.\n"
                    f"Next: **{next_state['label']}**\n"
                    f"Gate passed: {gate_msg}"
                )
        else:
            self._save_json(self.working_state_path, self._working_state)
            return (
                f"📝 Working state updated for **{state['label']}**.\n"
                f"Gate status: {gate_msg}\n"
                f"Continue working on this step."
            )

    def _check_gate(self, gate: dict, state_data: dict) -> Tuple[bool, str]:
        """Check if quality gate requirements are met. Returns (passed, message)."""
        if not gate:
            return True, "No gate requirements"

        missing = []

        # Required keys
        for key in gate.get("required_keys", []):
            if key not in state_data or not state_data[key]:
                missing.append(f"missing '{key}'")

        # Minimum string length
        for key, min_len in gate.get("min_length", {}).items():
            val = state_data.get(key, "")
            if isinstance(val, str) and len(val) < min_len:
                missing.append(f"'{key}' too short ({len(val)}/{min_len} chars)")

        # Minimum list length
        for key, min_len in gate.get("min_list_length", {}).items():
            val = state_data.get(key, [])
            if isinstance(val, list) and len(val) < min_len:
                missing.append(f"'{key}' needs {min_len - len(val)} more items ({len(val)}/{min_len})")

        if missing:
            return False, "Still needed: " + "; ".join(missing)
        return True, "All requirements met ✓"

    # ── Heartbeat Lifecycle Hooks ────────────────────────────────────

    def on_heartbeat_complete(self, score: int):
        """Called after each heartbeat's EVAL phase."""
        if not self._working_state.get("framework"):
            return

        self._working_state["heartbeats_in_state"] = \
            self._working_state.get("heartbeats_in_state", 0) + 1
        self._working_state["total_heartbeats"] = \
            self._working_state.get("total_heartbeats", 0) + 1

        # Check if we've exceeded max heartbeats for current state
        fw_name = self._working_state["framework"]
        fw_def = ACTIVITY_FRAMEWORKS.get(fw_name)
        if fw_def:
            state_idx = self._working_state.get("current_state_index", 0)
            if state_idx < len(fw_def["states"]):
                state = fw_def["states"][state_idx]
                max_hb = state.get("max_heartbeats", 3)
                spent = self._working_state["heartbeats_in_state"]
                if spent >= max_hb:
                    # Force advance — agent spent too long on this step
                    logger.warning(
                        f"Framework '{fw_name}' — forcing advance past "
                        f"'{state['name']}' after {spent} heartbeats (max {max_hb})"
                    )
                    self._working_state["state_history"].append({
                        "state": state["name"],
                        "completed_at": datetime.now().isoformat(),
                        "heartbeats_spent": spent,
                        "forced": True,
                    })
                    self._working_state["current_state_index"] = state_idx + 1
                    self._working_state["heartbeats_in_state"] = 0

        self._save_json(self.working_state_path, self._working_state)

    def on_chain_complete(self, final_score: float, chain_data: dict = None):
        """
        Called when the associated chain completes.
        Records the run, extracts skill memory if threshold reached,
        and pushes new questions to the stack.
        """
        if not self._working_state.get("framework"):
            return

        fw_name = self._working_state["framework"]
        fw_def = ACTIVITY_FRAMEWORKS.get(fw_name)
        if not fw_def:
            self.deactivate_framework()
            return

        # Record the completed run
        run = {
            "framework": fw_name,
            "goal": self._working_state.get("goal", ""),
            "score": final_score,
            "total_heartbeats": self._working_state.get("total_heartbeats", 0),
            "states_completed": len(self._working_state.get("state_history", [])),
            "total_states": len(fw_def["states"]),
            "completed_at": datetime.now().isoformat(),
            "state_data_keys": list(self._working_state.get("state_data", {}).keys()),
        }
        self._run_history["runs"].append(run)
        # Keep last 50 runs
        if len(self._run_history["runs"]) > 50:
            self._run_history["runs"] = self._run_history["runs"][-50:]
        self._save_json(self.run_history_path, self._run_history)

        # Push new questions to the stack
        new_questions = self._working_state.get("state_data", {}).get("new_questions", [])
        if new_questions:
            self._push_questions(new_questions, fw_name,
                                 self._working_state.get("goal", ""))

        # Check graduation threshold
        successful_runs = [
            r for r in self._run_history["runs"]
            if r["framework"] == fw_name and r["score"] >= 4
        ]
        threshold = fw_def.get("graduation_threshold", 5)
        if len(successful_runs) >= threshold:
            self._extract_skill_memory(fw_name, successful_runs)

        self.deactivate_framework()

    # ── Skill Extraction (the "book reading → pattern" mechanism) ────

    def _extract_skill_memory(self, framework_name: str, successful_runs: list):
        """
        Extract a compact skill memory from successful framework runs.
        This is the graduation mechanism — the framework dissolves into
        a compact pattern that replaces full step-by-step guidance.
        """
        fw_def = ACTIVITY_FRAMEWORKS.get(framework_name)
        if not fw_def:
            return

        # Build compact pattern from the framework states
        pattern_lines = [f"When doing '{fw_def['label']}' work:"]
        for i, state in enumerate(fw_def["states"]):
            pattern_lines.append(f"  {i + 1}. {state['label']}")
            # Extract key insight from gate requirements
            gate = state.get("gate", {})
            req_keys = gate.get("required_keys", [])
            if req_keys:
                pattern_lines.append(f"     → Produce: {', '.join(req_keys)}")

        avg_score = sum(r["score"] for r in successful_runs) / len(successful_runs)
        avg_heartbeats = sum(r["total_heartbeats"] for r in successful_runs) / len(successful_runs)

        skill = {
            "framework": framework_name,
            "label": fw_def["label"],
            "compact_pattern": "\n".join(pattern_lines),
            "graduated": True,
            "successful_runs": len(successful_runs),
            "avg_score": avg_score,
            "avg_heartbeats": avg_heartbeats,
            "extracted_at": datetime.now().isoformat(),
            "common_tools": self._extract_common_tools(successful_runs),
        }

        skill_path = os.path.join(self.skill_memory_dir, f"{framework_name}.json")
        self._save_json(skill_path, skill)
        logger.info(
            f"🎓 Skill extracted for '{fw_def['label']}' — "
            f"{len(successful_runs)} successful runs, avg {avg_score:.1f}/5. "
            f"Framework graduated."
        )

    def _extract_common_tools(self, runs: list) -> list:
        """Extract tools commonly used across successful runs."""
        # This would ideally pull from chain step data — for now just aggregate
        # from framework tool suggestions
        tools = set()
        for r in runs:
            fw_def = ACTIVITY_FRAMEWORKS.get(r.get("framework", ""))
            if fw_def:
                for state in fw_def["states"]:
                    tools.update(state.get("tools", []))
        return list(tools)

    # ── Question Stack (dynamic curiosity engine) ────────────────────

    def _push_questions(self, questions: list, source_framework: str, source_goal: str):
        """Push new questions onto the curiosity stack."""
        for q in questions:
            if isinstance(q, str) and len(q) > 10:
                entry = {
                    "question": q,
                    "source_framework": source_framework,
                    "source_goal": source_goal[:200],
                    "created_at": datetime.now().isoformat(),
                    "explored": False,
                }
                self._question_stack["questions"].append(entry)

        # Keep stack manageable — max 50 unexplored questions
        unexplored = [q for q in self._question_stack["questions"] if not q.get("explored")]
        explored = [q for q in self._question_stack["questions"] if q.get("explored")]
        if len(unexplored) > 50:
            unexplored = unexplored[-50:]
        self._question_stack["questions"] = explored[-20:] + unexplored
        self._save_json(self.question_stack_path, self._question_stack)

    def pop_question(self) -> Optional[str]:
        """Pop the next unexplored question from the stack. Returns question text or None."""
        for q in self._question_stack["questions"]:
            if not q.get("explored"):
                q["explored"] = True
                q["explored_at"] = datetime.now().isoformat()
                self._save_json(self.question_stack_path, self._question_stack)
                return q["question"]
        return None

    def get_question_stack_summary(self) -> str:
        """Get a readable summary of the question stack for daily plan / prompt injection."""
        unexplored = [q for q in self._question_stack.get("questions", [])
                       if not q.get("explored")]
        if not unexplored:
            return ""

        lines = [f"🔮 Curiosity Stack ({len(unexplored)} unexplored questions):"]
        for q in unexplored[:5]:
            src = q.get("source_framework", "unknown")
            lines.append(f"  • {q['question']} (from {src})")
        if len(unexplored) > 5:
            lines.append(f"  ... and {len(unexplored) - 5} more")
        return "\n".join(lines)

    # ── Status / Diagnostics ─────────────────────────────────────────

    def status(self) -> str:
        """Get full status of the activity framework engine."""
        lines = []

        # Active framework
        if self._working_state.get("framework"):
            fw_name = self._working_state["framework"]
            fw_def = ACTIVITY_FRAMEWORKS.get(fw_name, {})
            state_idx = self._working_state.get("current_state_index", 0)
            total = self._working_state.get("total_states", 0)
            lines.append(f"**Active: {fw_def.get('label', fw_name)}**")
            lines.append(f"  Goal: {self._working_state.get('goal', '?')[:100]}")
            lines.append(f"  State: {state_idx + 1}/{total}")
            lines.append(f"  Heartbeats: {self._working_state.get('total_heartbeats', 0)}")
        else:
            lines.append("No active activity framework.")

        # Run history summary
        runs = self._run_history.get("runs", [])
        if runs:
            lines.append(f"\n**Run History** ({len(runs)} total):")
            by_fw = {}
            for r in runs:
                fw = r.get("framework", "?")
                if fw not in by_fw:
                    by_fw[fw] = {"count": 0, "scores": [], "graduated": False}
                by_fw[fw]["count"] += 1
                by_fw[fw]["scores"].append(r.get("score", 0))
            for fw, data in by_fw.items():
                avg = sum(data["scores"]) / len(data["scores"]) if data["scores"] else 0
                good = sum(1 for s in data["scores"] if s >= 4)
                fw_def = ACTIVITY_FRAMEWORKS.get(fw, {})
                threshold = fw_def.get("graduation_threshold", 5)
                grad = "🎓" if self._is_graduated(fw) else f"({good}/{threshold} to graduate)"
                lines.append(f"  {fw}: {data['count']} runs, avg {avg:.1f}/5 {grad}")

        # Question stack
        q_summary = self.get_question_stack_summary()
        if q_summary:
            lines.append(f"\n{q_summary}")

        # Graduated skills
        graduated = []
        for fw_name in ACTIVITY_FRAMEWORKS:
            if self._is_graduated(fw_name):
                graduated.append(fw_name)
        if graduated:
            lines.append(f"\n**Graduated Skills**: {', '.join(graduated)}")

        return "\n".join(lines)
