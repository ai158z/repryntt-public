#!/usr/bin/env python3
"""
Tool Chain Executor - Chain-of-Thought for Tool Execution

Similar to how CoT chains reasoning steps, this chains tool executions:
1. Execute tool → Get result
2. Analyze result → Determine next tool
3. Execute next tool → Get result
4. Continue until goal achieved

Each tool execution is a "step" with context maintained across the chain.
"""

import json
import time
import logging
from typing import Dict, List, Any, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class ToolChainExecutor:
    """
    Manages multi-step tool execution chains similar to Chain-of-Thought reasoning.
    
    Structure:
    - Chain ID: Unique identifier
    - Goal: What we're trying to accomplish
    - Steps: Each tool execution with results
    - Context: Accumulated knowledge from previous steps
    - Next Action: AI-suggested next tool based on results
    """
    
    def __init__(self, brain_system):
        self.brain = brain_system
        self.active_chains: Dict[str, Dict] = {}
        self.chains_dir = Path(brain_system.brain_path) / "tool_chains"
        self.chains_dir.mkdir(exist_ok=True)
    
    def start_tool_chain(self, goal: str, initial_tool: str, initial_params: Dict) -> str:
        """
        Start a new tool execution chain.
        
        Args:
            goal: High-level goal (e.g., "Research quantum computing and create summary")
            initial_tool: First tool to execute
            initial_params: Parameters for first tool
            
        Returns:
            Chain ID
        """
        chain_id = f"tool_chain_{int(time.time())}_{hash(goal) % 10000}"
        
        chain_data = {
            "chain_id": chain_id,
            "goal": goal,
            "created_at": time.time(),
            "status": "active",
            "steps": [],
            "accumulated_context": "",
            "goal_achieved": False
        }
        
        self.active_chains[chain_id] = chain_data
        
        # Send signal to consciousness nervous system
        if hasattr(self.brain, 'consciousness') and self.brain.consciousness:
            self.brain.consciousness.send_signal("tool_chain_started", {
                "chain_id": chain_id,
                "goal": goal
            })
        
        # Execute first tool
        self._execute_step(chain_id, initial_tool, initial_params, "Initial step to begin goal")
        
        logger.info(f"🔗 Started tool chain: {chain_id} - Goal: {goal}")
        return chain_id
    
    def _execute_step(self, chain_id: str, tool_name: str, params: Dict, reasoning: str) -> Dict:
        """
        Execute a single tool in the chain and record results.
        
        Returns:
            Step result with next_action suggestion
        """
        chain = self.active_chains.get(chain_id)
        if not chain:
            return {"error": "Chain not found"}
        
        step_num = len(chain["steps"]) + 1
        logger.info(f"🔧 Tool Chain Step {step_num}: {tool_name}")
        
        # Execute the tool
        try:
            tool_func = self.brain.available_tools.get(tool_name)
            if not tool_func:
                result = f"Error: Tool '{tool_name}' not found"
                success = False
            else:
                # Call the tool with parameters
                if params:
                    result = tool_func(**params)
                else:
                    result = tool_func()
                success = True
        except Exception as e:
            result = f"Error executing {tool_name}: {str(e)}"
            success = False
            logger.error(result)
        
        # Get tool details for chain suggestions
        from repryntt.tools.discovery import TOOL_DETAILS
        tool_info = TOOL_DETAILS.get(tool_name, {})
        chain_suggestion = tool_info.get("chain_next", "No suggestion available")
        
        # Build step record
        step_data = {
            "step": step_num,
            "tool": tool_name,
            "parameters": params,
            "reasoning": reasoning,
            "result": str(result)[:1000],  # Truncate long results
            "success": success,
            "chain_suggestion": chain_suggestion,
            "timestamp": time.time()
        }
        
        chain["steps"].append(step_data)
        
        # Send signal to consciousness nervous system
        if hasattr(self.brain, 'consciousness') and self.brain.consciousness:
            self.brain.consciousness.send_signal("tool_chain_step", {
                "chain_id": chain_id,
                "tool": tool_name,
                "result": str(result)[:200]
            })
        
        # Update accumulated context
        context_update = f"\nStep {step_num}: Used {tool_name} - {reasoning}\nResult: {str(result)[:200]}...\n"
        chain["accumulated_context"] += context_update
        
        # Generate next action using AI
        next_action = self._generate_next_action(chain)
        step_data["next_action"] = next_action
        
        # Save chain to disk
        self._save_chain(chain_id)
        
        return step_data
    
    def _generate_next_action(self, chain: Dict) -> Dict[str, Any]:
        """
        Use AI to determine next tool based on current progress.
        
        Similar to CoT generating next questions, this generates next tool call.
        """
        goal = chain["goal"]
        context = chain["accumulated_context"]
        last_step = chain["steps"][-1] if chain["steps"] else {}
        
        # Build prompt for AI to suggest next tool
        prompt = f"""You are executing a multi-step tool chain to accomplish: "{goal}"

CONTEXT SO FAR:
{context}

LAST TOOL EXECUTED: {last_step.get('tool', 'None')}
LAST RESULT: {last_step.get('result', 'None')[:300]}
CHAIN SUGGESTION: {last_step.get('chain_suggestion', 'None')}

Based on the goal and results so far, what should be the NEXT tool to use?

Respond in this EXACT format:
NEXT_TOOL: [tool_name or "GOAL_ACHIEVED"]
REASONING: [why this tool / why goal is achieved]
PARAMETERS: {{"param1": "value1", "param2": "value2"}}

If the goal is achieved, respond with NEXT_TOOL: GOAL_ACHIEVED"""

        try:
            # Call AI through master queue
            import requests
            response = self.brain.master_ai_queue.submit_request(
                lambda: requests.post(
                    "http://localhost:8080/v1/chat/completions",
                    json={
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 300,
                        "temperature": 0.6
                    },
                    timeout=20
                ),
                timeout=20,
                priority=1
            )
            
            if response and response.status_code == 200:
                ai_response = response.json()['choices'][0]['message']['content']
                
                # Parse AI response
                next_action = self._parse_next_action(ai_response)
                return next_action
            else:
                return {"action": "error", "reasoning": "AI unavailable"}
                
        except Exception as e:
            logger.error(f"Failed to generate next action: {e}")
            return {"action": "error", "reasoning": str(e)}
    
    def _parse_next_action(self, ai_response: str) -> Dict[str, Any]:
        """Parse AI response to extract next tool, reasoning, and parameters"""
        lines = ai_response.strip().split('\n')
        
        next_tool = None
        reasoning = ""
        parameters = {}
        
        for line in lines:
            if line.startswith("NEXT_TOOL:"):
                next_tool = line.split(":", 1)[1].strip()
            elif line.startswith("REASONING:"):
                reasoning = line.split(":", 1)[1].strip()
            elif line.startswith("PARAMETERS:"):
                param_str = line.split(":", 1)[1].strip()
                try:
                    parameters = json.loads(param_str)
                except:
                    parameters = {}
        
        return {
            "action": next_tool if next_tool else "unknown",
            "reasoning": reasoning,
            "parameters": parameters
        }
    
    def advance_chain(self, chain_id: str) -> Dict[str, Any]:
        """
        Advance the tool chain by executing the next suggested action.
        
        Returns:
            Result of the step execution
        """
        chain = self.active_chains.get(chain_id)
        if not chain:
            return {"error": "Chain not found"}
        
        if chain["status"] != "active":
            return {"error": f"Chain is {chain['status']}, not active"}
        
        # Get last step's next action
        if not chain["steps"]:
            return {"error": "No steps in chain"}
        
        last_step = chain["steps"][-1]
        next_action = last_step.get("next_action", {})
        
        # Check if goal achieved
        if next_action.get("action") == "GOAL_ACHIEVED":
            chain["status"] = "completed"
            chain["goal_achieved"] = True
            chain["completed_at"] = time.time()
            self._save_chain(chain_id)
            
            # Send completion signal to consciousness
            if hasattr(self.brain, 'consciousness') and self.brain.consciousness:
                self.brain.consciousness.send_signal("tool_chain_completed", {
                    "chain_id": chain_id,
                    "goal": chain["goal"]
                })
            
            logger.info(f"✅ Tool chain {chain_id} completed successfully")
            return {"status": "completed", "message": "Goal achieved"}
        
        # Execute next tool
        next_tool = next_action.get("action")
        next_params = next_action.get("parameters", {})
        reasoning = next_action.get("reasoning", "Continuing chain")
        
        if not next_tool or next_tool == "unknown":
            return {"error": "No valid next action determined"}
        
        result = self._execute_step(chain_id, next_tool, next_params, reasoning)
        return result
    
    def get_chain_status(self, chain_id: str) -> Dict[str, Any]:
        """Get current status of a tool chain"""
        chain = self.active_chains.get(chain_id)
        if not chain:
            # Try loading from disk
            chain = self._load_chain(chain_id)
            if not chain:
                return {"error": "Chain not found"}
        
        return {
            "chain_id": chain_id,
            "goal": chain["goal"],
            "status": chain["status"],
            "steps_completed": len(chain["steps"]),
            "goal_achieved": chain.get("goal_achieved", False),
            "last_tool": chain["steps"][-1]["tool"] if chain["steps"] else None,
            "next_action": chain["steps"][-1].get("next_action", {}) if chain["steps"] else {}
        }
    
    def _save_chain(self, chain_id: str):
        """Save chain to disk"""
        chain = self.active_chains.get(chain_id)
        if not chain:
            return
        
        chain_file = self.chains_dir / f"{chain_id}.json"
        with open(chain_file, 'w') as f:
            json.dump(chain, f, indent=2, default=str)
    
    def _load_chain(self, chain_id: str) -> Optional[Dict]:
        """Load chain from disk"""
        chain_file = self.chains_dir / f"{chain_id}.json"
        if not chain_file.exists():
            return None
        
        with open(chain_file, 'r') as f:
            return json.load(f)


# ---------------------------------------------------------------------------
# Chain-of-Thought helpers — extracted from SAIGE monolith (Phase 4)
# ---------------------------------------------------------------------------

class ChainSynthesisEngine:
    """Combines insights into meaningful conclusions — replaces text concatenation."""

    def __init__(self, brain_system=None):
        self.brain_system = brain_system

    def synthesize_step(self, step_output: str, chain_context: Dict) -> Dict:
        """Extract and synthesize insights from step output."""
        insights = self._extract_insights(step_output)
        connections = self._find_connections(insights, chain_context)
        synthesis = self._create_synthesis_statement(insights, connections)
        return {
            "insights": insights,
            "connections": connections,
            "synthesis": synthesis,
            "contribution_to_conclusion": self._assess_conclusion_contribution(synthesis, chain_context),
        }

    # -- internal helpers --------------------------------------------------

    def _extract_insights(self, output: str) -> list:
        insights = []
        _KEYWORDS = {
            "insight:", "conclusion:", "key finding:", "realization:",
            "therefore", "thus", "in summary", "ultimately", "final insight",
            "deep understanding", "comprehensive", "complete picture",
        }
        for line in output.split("\n"):
            low = line.lower()
            if any(kw in low for kw in _KEYWORDS):
                insights.append(line.strip())
            elif line.strip()[:2] in ("- ", "• ", "* ") or (
                len(line.strip()) > 2 and line.strip()[:3] in ("1. ", "2. ", "3. ")
            ):
                if len(line.strip()) > 20:
                    insights.append(line.strip())
        return insights[:5]

    def _find_connections(self, new_insights: list, chain_context: Dict) -> list:
        connections = []
        previous = chain_context.get("insights", [])
        for new in new_insights:
            for prev in previous[-3:]:
                if self._insights_related(new, prev):
                    connections.append(f"Connects to previous insight: {prev[:50]}...")
        return connections

    @staticmethod
    def _insights_related(a: str, b: str) -> bool:
        _STOP = {
            "the", "a", "an", "and", "or", "but", "in", "on", "at",
            "to", "for", "of", "with", "by", "is", "are", "was", "were",
        }
        w1 = set(a.lower().split()) - _STOP
        w2 = set(b.lower().split()) - _STOP
        return len(w1 & w2) > 1

    def _create_synthesis_statement(self, insights: list, connections: list) -> str:
        if not insights:
            return "Step contributed background context."
        synthesis = f"This step revealed: {'; '.join(insights[:3])}"
        if connections:
            synthesis += f" Building on: {'; '.join(connections[:2])}"
        return synthesis

    @staticmethod
    def _assess_conclusion_contribution(synthesis: str, chain_context: Dict) -> int:
        low = synthesis.lower()
        _CONC = ["conclusion", "therefore", "thus", "in summary", "ultimately",
                 "final insight", "summary", "overall", "key findings"]
        _DEPTH = ["deep understanding", "comprehensive", "complete picture",
                  "synthesis", "unified", "integrated", "holistic", "systematic", "thorough"]
        score = sum(1 for w in _CONC if w in low) + sum(1 for w in _DEPTH if w in low)
        goal = chain_context.get("goal", "")
        if any(w in low for w in goal.lower().split()):
            score += 1
        return min(score, 5)


class AutonomousConclusionEvaluator:
    """AI-driven conclusion detection — no forced completion."""

    def __init__(self, brain_system=None):
        self.brain_system = brain_system

    def should_conclude(self, chain_context: Dict, synthesis_result: Dict) -> bool:
        """Determine if exploration should conclude based on AI assessment."""
        contribution = synthesis_result.get("contribution_to_conclusion", 0)
        total_insights = len(chain_context.get("insights", []))
        progress = chain_context.get("progress_level", 0)

        sufficient_depth = contribution >= 2
        sufficient_insights = total_insights >= 5
        good_progress = progress > 0.7

        if sufficient_depth and (sufficient_insights or good_progress):
            return self._ai_final_assessment(chain_context, synthesis_result)
        if progress > 0.9:
            return True
        return False

    def _ai_final_assessment(self, chain_context: Dict, synthesis_result: Dict) -> bool:
        """Let AI decide if conclusion is warranted."""
        prompt = (
            "Based on this exploration state, should we conclude or continue?\n\n"
            f"GOAL: {chain_context.get('goal', '')}\n"
            f"PROGRESS: {chain_context.get('progress_level', 0)*100:.1f}%\n"
            f"TOTAL INSIGHTS: {len(chain_context.get('insights', []))}\n"
            f"LATEST SYNTHESIS: {synthesis_result.get('synthesis', '')}\n\n"
            "Respond with only 'CONCLUDE' or 'CONTINUE' followed by brief reasoning."
        )
        if self.brain_system:
            response = self.brain_system._call_ai_service(prompt)
            return "CONCLUDE" in response.upper()
        return False  # default: continue
