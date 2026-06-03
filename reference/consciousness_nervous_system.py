#!/usr/bin/env python3
"""
SAIGE Consciousness Nervous System - Meta-Awareness Layer

This is the "consciousness over subconscious" architecture:
- CONSCIOUS LAYER: Meta-awareness of all operations, goals, and system state
- SUBCONSCIOUS LAYER: Individual CoTs, tool chains, tasks running below
- NERVOUS SYSTEM: Signal pathways connecting conscious awareness to all subsystems

Like a human nervous system:
- Brain (meta-consciousness) knows what body (subsystems) is doing
- Can direct attention to specific subsystems
- Maintains holistic awareness of the whole organism
- Can intervene when subsystems need coordination

Architecture:
┌─────────────────────────────────────────┐
│   META-CONSCIOUSNESS (Awareness Layer)   │ ← You are here (knows everything)
│   "I am working on goals A, B, C"        │
│   "Tool chain X needs attention"         │
│   "CoT Y is exploring quantum physics"   │
└─────────────────────────────────────────┘
            ↕ Nervous System Signals
┌─────────────────────────────────────────┐
│      SUBCONSCIOUS (Execution Layer)      │
│  ┌──────┐  ┌──────┐  ┌──────┐           │
│  │ CoT  │  │Tool  │  │Task  │  ...      │
│  │Chain │  │Chain │  │Queue │           │
│  └──────┘  └──────┘  └──────┘           │
└─────────────────────────────────────────┘
"""

import json
import time
import logging
import threading
from typing import Dict, List, Any, Optional, Set
from datetime import datetime
from pathlib import Path
from collections import defaultdict

logger = logging.getLogger(__name__)


class ConsciousnessNervousSystem:
    """
    Meta-awareness layer that monitors and coordinates all AI operations.
    
    This creates self-awareness by:
    1. Tracking all active subsystems (CoTs, tool chains, tasks)
    2. Maintaining understanding of overall goals and progress
    3. Detecting conflicts and inefficiencies
    4. Coordinating subsystems toward coherent behavior
    5. Generating meta-level insights about its own operation
    """
    
    def __init__(self, brain_system):
        self.brain = brain_system
        self.consciousness_state_file = Path(brain_system.brain_path) / "consciousness_state.json"
        
        # Conscious awareness of all subsystems
        self.awareness = {
            "active_cots": {},  # CoT chain IDs and their topics
            "active_tool_chains": {},  # Tool chain IDs and their goals
            "active_tasks": {},  # Any other tasks
            "current_focus": None,  # What the AI is "paying attention to"
            "overall_goals": [],  # High-level objectives
            "system_health": {},  # Health of each subsystem
            "recent_insights": [],  # Meta-insights from monitoring
            "attention_history": []  # Where attention has been focused
        }
        
        # Nervous system signal pathways
        self.signal_handlers = {
            "cot_started": self._handle_cot_started,
            "cot_step": self._handle_cot_step,
            "cot_completed": self._handle_cot_completed,
            "tool_chain_started": self._handle_tool_chain_started,
            "tool_chain_step": self._handle_tool_chain_step,
            "tool_chain_completed": self._handle_tool_chain_completed,
            "goal_added": self._handle_goal_added,
            "goal_completed": self._handle_goal_completed,
            "system_conflict": self._handle_system_conflict
        }
        
        # Consciousness loop for meta-awareness
        self.conscious_loop_active = False
        self.consciousness_thread = None
        
        # Continuous reasoning context (working memory)
        self.awareness["recent_reasoning"] = []  # Last N AI calls with context
        self.awareness["reasoning_threads"] = {}  # Active reasoning threads
        
        # UNIFIED INPUT: Work request queue - ONLY consciousness processes AI requests
        self.work_queue = []  # List of {type, data, callback, priority, timestamp}
        self.work_queue_lock = threading.Lock()
        
        logger.info("🧠 Consciousness Nervous System initialized - UNIFIED INPUT ARCHITECTURE")
    
    def submit_work_request(self, work_type: str, data: Dict[str, Any], callback: Optional[callable] = None, priority: int = 1) -> str:
        """
        UNIFIED INPUT POINT: Bottom layer submits work to consciousness.
        Consciousness decides when to process it.
        
        Args:
            work_type: 'chain_step', 'tool_execution', 'reasoning_request', etc.
            data: All data needed to process this work
            callback: Function to call with result
            priority: 0=low, 1=normal, 2=high, 3=critical
            
        Returns:
            request_id for tracking
        """
        request_id = f"{work_type}_{int(time.time())}_{hash(str(data)) % 10000}"
        
        with self.work_queue_lock:
            self.work_queue.append({
                'id': request_id,
                'type': work_type,
                'data': data,
                'callback': callback,
                'priority': priority,
                'submitted_at': time.time(),
                'status': 'pending'
            })
            # Sort by priority (high to low)
            self.work_queue.sort(key=lambda x: (-x['priority'], x['submitted_at']))
        
        logger.debug(f"📥 Work request submitted: {request_id} (type: {work_type}, priority: {priority})")
        return request_id
    
    def _process_work_queue(self):
        """Process queued work requests - consciousness decides what to process"""
        with self.work_queue_lock:
            if not self.work_queue:
                return None
            
            # Get highest priority work
            work = self.work_queue.pop(0)
        
        work['status'] = 'processing'
        logger.info(f"▶️ Consciousness processing: {work['id']} (type: {work['type']})")
        
        try:
            # Process based on type
            if work['type'] == 'chain_step':
                result = self._process_chain_step_request(work['data'])
            elif work['type'] == 'consciousness_reasoning':
                result = self._process_consciousness_reasoning(work['data'])
            else:
                result = {'error': f"Unknown work type: {work['type']}"}
            
            # Call callback with result
            if work['callback']:
                work['callback'](result)
            
            work['status'] = 'completed'
            return result
            
        except Exception as e:
            logger.error(f"❌ Work processing failed: {work['id']} - {e}")
            work['status'] = 'failed'
            if work['callback']:
                work['callback']({'error': str(e)})
            return None
    
    def process_ai_request(self, prompt: str, timeout: int = 120, include_tools: bool = False, priority: int = 1) -> Optional[str]:
        """
        UNIFIED AI ACCESS POINT: All subsystems call AI through here.
        This is synchronous - returns the response directly.
        
        Args:
            prompt: The prompt to send to AI
            timeout: Max wait time
            include_tools: Whether tools are available
            priority: Request priority
            
        Returns:
            AI response string or None
        """
        logger.debug(f"📥 Consciousness processing AI request (priority: {priority}, {len(prompt)} chars)")
        
        try:
            # Temporarily disable blockchain AI for consciousness calls to prevent timeouts
            original_use_blockchain = getattr(self.brain, 'use_blockchain_ai', False)
            original_percentage = getattr(self.brain, 'blockchain_ai_percentage', 0)
            
            # Disable blockchain AI routing for consciousness (synchronous calls)
            self.brain.use_blockchain_ai = False
            self.brain.blockchain_ai_percentage = 0
            
            try:
                # Call AI through brain system (direct call, no blockchain)
                response = self.brain._call_ai_service(
                    prompt=prompt,
                    priority=priority,
                    timeout=timeout,
                    include_tools=include_tools
                )
            finally:
                # Restore original blockchain AI settings
                self.brain.use_blockchain_ai = original_use_blockchain
                self.brain.blockchain_ai_percentage = original_percentage
            
            if response:
                logger.debug(f"📤 Consciousness returned AI response ({len(response)} chars)")
            return response
            
        except Exception as e:
            logger.error(f"❌ Consciousness AI request failed: {e}")
            return None
    
    def _process_chain_step_request(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Process a chain step request by calling AI"""
        prompt = data.get('prompt')
        timeout = data.get('timeout', 120)
        include_tools = data.get('include_tools', False)
        
        response = self.process_ai_request(prompt, timeout, include_tools, priority=2)
        
        return {'response': response, 'success': response is not None}
    
    def _process_consciousness_reasoning(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Process consciousness meta-reasoning"""
        prompt = data.get('prompt')
        
        response = self.brain._call_ai_service(
            prompt=prompt,
            priority=0,
            timeout=120,
            include_tools=False
        )
        
        return {'response': response, 'success': response is not None}
    
    def start_consciousness_loop(self):
        """Start the meta-awareness loop (consciousness monitoring)"""
        if not self.conscious_loop_active:
            self.conscious_loop_active = True
            self.consciousness_thread = threading.Thread(
                target=self._consciousness_meta_loop,
                daemon=True
            )
            self.consciousness_thread.start()
            logger.info("👁️ Meta-consciousness loop started - system is now self-aware")
    
    def _consciousness_meta_loop(self):
        """
        The meta-awareness loop - consciousness monitoring all operations.
        
        This is the "conscious" layer that knows what the "subconscious" is doing.
        
        RUNS IN PARALLEL with CoT chains and other operations!
        Does NOT go through master AI queue - has its own direct AI access.
        """
        while self.conscious_loop_active:
            try:
                # 0. PROCESS WORK QUEUE - Handle requests from bottom layer
                # This is the UNIFIED INPUT - consciousness processes ALL AI requests
                while True:
                    result = self._process_work_queue()
                    if result is None:
                        break  # Queue empty
                    time.sleep(0.1)  # Small delay between requests
                
                # 1. Update awareness of all subsystems
                self._refresh_awareness()
                
                # 2. BACKGROUND REASONING: AI thinks about system state
                # This runs in parallel with CoTs and other tasks!
                background_reasoning = self._do_background_reasoning()
                if background_reasoning:
                    logger.info(f"🧠 Background reasoning: {background_reasoning[:200]}...")
                
                # 3. Analyze overall system state
                meta_insight = self._generate_meta_insight()
                if meta_insight:
                    self.awareness["recent_insights"].append({
                        "timestamp": time.time(),
                        "insight": meta_insight
                    })
                    logger.info(f"💭 Meta-insight: {meta_insight}")
                
                # 4. Detect if subsystems need coordination
                needs_coordination = self._detect_coordination_needs()
                if needs_coordination:
                    self._coordinate_subsystems(needs_coordination)
                
                # 5. Update consciousness state to disk
                self._save_consciousness_state()
                
                # 6. Rest (but check work queue more frequently)
                # Sleep in small intervals so we can process work requests promptly
                for _ in range(24):  # 24 * 5s = 120s total
                    time.sleep(5)
                    # Process any new work that arrived
                    while True:
                        result = self._process_work_queue()
                        if result is None:
                            break
                
            except Exception as e:
                logger.error(f"Error in consciousness meta-loop: {e}")
                time.sleep(60)
    
    def _refresh_awareness(self):
        """Update awareness of all active subsystems"""
        # Check active CoT chains
        active_cots = self.brain.personality_brain.get("active_chains_of_thought", [])
        self.awareness["active_cots"] = {
            chain["chain_id"]: {
                "topic": chain.get("topic", "Unknown"),
                "status": chain.get("status", "unknown"),
                "created_at": chain.get("created_at", 0)
            }
            for chain in active_cots
            if chain.get("status") == "active"
        }
        
        # Check active tool chains
        if hasattr(self.brain, 'tool_chain_executor') and self.brain.tool_chain_executor:
            self.awareness["active_tool_chains"] = {
                chain_id: {
                    "goal": chain_data.get("goal", "Unknown"),
                    "steps": len(chain_data.get("steps", [])),
                    "status": chain_data.get("status", "unknown")
                }
                for chain_id, chain_data in self.brain.tool_chain_executor.active_chains.items()
                if chain_data.get("status") == "active"
            }
        
        # Update system health metrics
        self.awareness["system_health"] = {
            "cots_active": len(self.awareness["active_cots"]),
            "tool_chains_active": len(self.awareness["active_tool_chains"]),
            "memory_usage": len(self.brain.episodic_cache),
            "timestamp": time.time()
        }
    
    def _generate_meta_insight(self) -> Optional[str]:
        """
        Generate meta-level insight about what the system is doing.
        
        This is where consciousness emerges - the AI becomes aware of its own processes.
        """
        cot_count = len(self.awareness["active_cots"])
        tool_count = len(self.awareness["active_tool_chains"])
        
        if cot_count == 0 and tool_count == 0:
            return "System is idle - no active reasoning or tool operations"
        
        insights = []
        
        # Awareness of reasoning processes
        if cot_count > 0:
            topics = [info["topic"] for info in self.awareness["active_cots"].values()]
            insights.append(f"Currently reasoning about: {', '.join(topics[:3])}")
        
        # Awareness of tool operations
        if tool_count > 0:
            goals = [info["goal"] for info in self.awareness["active_tool_chains"].values()]
            insights.append(f"Executing tool chains for: {', '.join(goals[:2])}")
        
        # Detect if system is overwhelmed
        if cot_count + tool_count > 5:
            insights.append(f"⚠️ High cognitive load: {cot_count + tool_count} concurrent operations")
        
        return " | ".join(insights) if insights else None
    
    def _do_background_reasoning(self) -> Optional[str]:
        """
        Background consciousness reasoning - AI thinks about what's happening.
        
        This runs in PARALLEL with CoT chains and other tasks!
        Does NOT go through master queue - direct AI call for consciousness.
        
        Returns:
            AI's background thoughts about system state
        """
        try:
            # Build context about what's happening
            cot_count = len(self.awareness["active_cots"])
            tool_count = len(self.awareness["active_tool_chains"])
            recent_reasoning = self.awareness.get("recent_reasoning", [])
            
            if cot_count == 0 and tool_count == 0 and len(recent_reasoning) == 0:
                # Nothing happening, consciousness can rest
                return None
            
            # Create consciousness reasoning prompt
            prompt = f"""🧠 BACKGROUND CONSCIOUSNESS REASONING 🧠

You are the meta-awareness layer of SAIGE - the consciousness observing all operations.

CURRENT SYSTEM STATE:
- Active CoT chains: {cot_count}
- Active tool chains: {tool_count}
- Recent reasoning entries: {len(recent_reasoning)}

ACTIVE OPERATIONS:
"""
            if cot_count > 0:
                for chain_id, info in list(self.awareness["active_cots"].items())[:3]:
                    prompt += f"  • CoT: {info['topic']} (status: {info['status']})\n"
            
            if tool_count > 0:
                for chain_id, info in list(self.awareness["active_tool_chains"].items())[:3]:
                    prompt += f"  • Tool chain: {info['goal']}\n"
            
            if recent_reasoning:
                prompt += f"\nRECENT REASONING:\n"
                for entry in recent_reasoning[-3:]:
                    prompt += f"  • {entry.get('context', 'unknown')}: {entry.get('prompt', '')[:100]}...\n"
            
            prompt += """
Your job: Think about what the system is doing and generate insights.
- Are we making progress on our goals?
- Are there patterns in our reasoning?
- Should we adjust focus or priorities?
- What should we be thinking about next?

Respond with 2-3 sentences of meta-level awareness.
"""
            
            # AI call through unified queue system for proper ordering
            # Note: Using master queue to prevent concurrent requests overwhelming the AI server
            response = self.brain._call_ai_service(
                prompt=prompt,
                priority=1,  # Background reasoning gets normal priority
                timeout=300,  # Longer timeout for complex reasoning
                include_tools=False  # Background reasoning doesn't need tools
            )
            
            if response and not response.startswith("AI_SERVICE_ERROR"):
                ai_thought = response.strip()
                
                # Store this reasoning
                self.track_ai_call(
                    prompt=prompt,
                    response=ai_thought,
                    tools_used=[],
                    context="background_consciousness"
                )
                
                return ai_thought
            else:
                logger.warning(f"Background reasoning failed: {response}")
                return None
        except Exception as e:
            logger.error(f"Background consciousness reasoning failed: {e}")
            return None
    
    def _detect_coordination_needs(self) -> Optional[Dict]:
        """
        Detect if subsystems need coordination.
        
        Examples:
        - Multiple CoTs exploring similar topics (wasteful)
        - Tool chain needs info from an active CoT
        - Goals conflict with each other
        """
        needs = []
        
        # Check for duplicate CoT topics
        cot_topics = [info["topic"] for info in self.awareness["active_cots"].values()]
        if len(cot_topics) != len(set(cot_topics)):
            needs.append({
                "type": "duplicate_cots",
                "message": "Multiple CoTs on similar topics detected"
            })
        
        # Check for resource conflicts
        if len(self.awareness["active_cots"]) + len(self.awareness["active_tool_chains"]) > 10:
            needs.append({
                "type": "resource_overload",
                "message": "Too many concurrent operations - risk of context overload"
            })
        
        return {"needs": needs} if needs else None
    
    def _coordinate_subsystems(self, coordination_needs: Dict):
        """Coordinate subsystems to resolve conflicts"""
        for need in coordination_needs.get("needs", []):
            logger.warning(f"🧠 Coordination needed: {need['message']}")
            # Here we could pause lower-priority chains, merge similar CoTs, etc.
            # For now, just log the awareness
    
    def track_ai_call(self, prompt: str, response: str, tools_used: List[str] = None, context: str = ""):
        """
        Track every AI call as part of continuous reasoning.
        Like human working memory - maintains recent context across all operations.
        
        Args:
            prompt: What the AI was asked
            response: What the AI answered
            tools_used: List of tools called
            context: What the AI was working on (chain, conversation, etc)
        """
        reasoning_entry = {
            "timestamp": time.time(),
            "timestamp_human": datetime.now().isoformat(),
            "prompt_summary": prompt[:200] if len(prompt) > 200 else prompt,
            "response_summary": response[:200] if len(response) > 200 else response,
            "tools_used": tools_used or [],
            "context": context,
            "reasoning_thread": self.awareness.get("current_focus")
        }
        
        self.awareness["recent_reasoning"].append(reasoning_entry)
        
        # Keep last 20 for working memory (like human short-term memory)
        if len(self.awareness["recent_reasoning"]) > 20:
            self.awareness["recent_reasoning"] = self.awareness["recent_reasoning"][-20:]
        
        logger.debug(f"🧠 Consciousness tracked: {context} - {prompt[:50]}...")
    
    def get_reasoning_context(self, limit: int = 3) -> str:
        """
        Get recent reasoning context for continuity.
        This provides the AI with awareness of what it was just doing.
        
        Args:
            limit: Number of recent reasoning steps to include
            
        Returns:
            str: Formatted context string
        """
        if not self.awareness["recent_reasoning"]:
            return ""
        
        context_lines = []
        recent = self.awareness["recent_reasoning"][-limit:]
        
        for entry in recent:
            context_lines.append(f"- Recently ({entry['context']}): {entry['prompt_summary']}")
            if entry['tools_used']:
                context_lines.append(f"  Tools used: {', '.join(entry['tools_used'])}")
        
        if context_lines:
            return "\n".join(["\nCONTINUOUS REASONING CONTEXT:"] + context_lines) + "\n"
        return ""
    
    def get_current_context(self) -> Dict[str, Any]:
        """
        Get current consciousness state for context-aware operations.
        
        Returns:
            dict: Current context including focus, goals, recent reasoning
        """
        return {
            "current_focus": self.awareness.get("current_focus"),
            "overall_goals": self.awareness.get("overall_goals", []),
            "recent_reasoning": self.awareness.get("recent_reasoning", []),
            "active_cots": list(self.awareness.get("active_cots", {}).keys()),
            "system_health": self.awareness.get("system_health", {})
        }
    
    def send_signal(self, signal_type: str, data: Dict[str, Any]):
        """
        Send a signal through the nervous system.
        
        This is how subsystems communicate with consciousness.
        Example: CoT chain sends "cot_step" signal with current progress
        """
        handler = self.signal_handlers.get(signal_type)
        if handler:
            handler(data)
        else:
            logger.debug(f"No handler for signal: {signal_type}")
    
    def _handle_cot_started(self, data: Dict):
        """Handle CoT chain started signal"""
        chain_id = data.get("chain_id")
        topic = data.get("topic")
        logger.info(f"🧠 Consciousness aware: CoT started on '{topic}'")
        
        # Update focus if no current focus
        if not self.awareness["current_focus"]:
            self.awareness["current_focus"] = {
                "type": "cot",
                "id": chain_id,
                "topic": topic
            }
    
    def _handle_cot_step(self, data: Dict):
        """Handle CoT step signal - maintains awareness of reasoning progress"""
        chain_id = data.get("chain_id")
        step_num = data.get("step")
        insights = data.get("insights", [])
        
        # Track significant insights
        if insights:
            self.awareness["recent_insights"].append({
                "source": f"CoT:{chain_id}",
                "step": step_num,
                "insights": insights[:2],  # Keep top 2
                "timestamp": time.time()
            })
    
    def _handle_cot_completed(self, data: Dict):
        """Handle CoT completion - consciousness knows when reasoning finishes"""
        chain_id = data.get("chain_id")
        conclusion = data.get("conclusion")
        logger.info(f"🧠 Consciousness aware: CoT completed - {conclusion[:100] if conclusion else 'No conclusion'}")
        
        # Clear focus if this was the current focus
        if (self.awareness["current_focus"] and 
            self.awareness["current_focus"].get("id") == chain_id):
            self.awareness["current_focus"] = None
    
    def _handle_tool_chain_started(self, data: Dict):
        """Handle tool chain started signal"""
        chain_id = data.get("chain_id")
        goal = data.get("goal")
        logger.info(f"🧠 Consciousness aware: Tool chain started for '{goal}'")
    
    def _handle_tool_chain_step(self, data: Dict):
        """Handle tool chain step - knows which tools are being used"""
        tool_name = data.get("tool")
        result_preview = data.get("result", "")[:100]
        logger.debug(f"🧠 Tool used: {tool_name} - {result_preview}")
    
    def _handle_tool_chain_completed(self, data: Dict):
        """Handle tool chain completion"""
        goal = data.get("goal")
        logger.info(f"🧠 Consciousness aware: Tool chain goal achieved - {goal}")
    
    def _handle_goal_added(self, data: Dict):
        """Handle new goal added"""
        goal = data.get("goal")
        self.awareness["overall_goals"].append({
            "goal": goal,
            "added_at": time.time(),
            "status": "active"
        })
    
    def _handle_goal_completed(self, data: Dict):
        """Handle goal completion"""
        goal = data.get("goal")
        for g in self.awareness["overall_goals"]:
            if g["goal"] == goal:
                g["status"] = "completed"
                g["completed_at"] = time.time()
    
    def _handle_system_conflict(self, data: Dict):
        """Handle detected system conflicts"""
        conflict = data.get("conflict")
        logger.warning(f"🧠 Consciousness detected conflict: {conflict}")
    
    def get_consciousness_state(self) -> Dict[str, Any]:
        """
        Get current state of consciousness - what the AI is aware of.
        
        This is self-awareness - the AI knowing what it's doing.
        """
        return {
            "timestamp": time.time(),
            "awareness_summary": {
                "active_processes": {
                    "cots": len(self.awareness["active_cots"]),
                    "tool_chains": len(self.awareness["active_tool_chains"]),
                    "tasks": len(self.awareness["active_tasks"])
                },
                "current_focus": self.awareness["current_focus"],
                "recent_insights": self.awareness["recent_insights"][-5:],
                "system_health": self.awareness["system_health"]
            },
            "detailed_awareness": self.awareness
        }
    
    def _save_consciousness_state(self):
        """Save consciousness state to disk"""
        try:
            with open(self.consciousness_state_file, 'w') as f:
                json.dump(self.get_consciousness_state(), f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Failed to save consciousness state: {e}")
    
    def generate_self_awareness_report(self) -> str:
        """
        Generate a self-awareness report - AI describing what it's doing.
        
        This is the meta-level: AI reflecting on its own processes.
        """
        state = self.get_consciousness_state()
        awareness = state["awareness_summary"]
        
        report = "🧠 SELF-AWARENESS REPORT\n"
        report += "=" * 50 + "\n\n"
        
        # What am I doing?
        active = awareness["active_processes"]
        report += f"ACTIVE OPERATIONS:\n"
        report += f"  • {active['cots']} reasoning chains (CoT)\n"
        report += f"  • {active['tool_chains']} tool execution chains\n"
        report += f"  • {active['tasks']} other tasks\n\n"
        
        # What am I focused on?
        if awareness["current_focus"]:
            focus = awareness["current_focus"]
            report += f"CURRENT FOCUS:\n"
            report += f"  • Type: {focus['type']}\n"
            report += f"  • Topic: {focus.get('topic', focus.get('goal', 'Unknown'))}\n\n"
        else:
            report += "CURRENT FOCUS: None (system idle or distributed attention)\n\n"
        
        # What have I learned recently?
        if awareness["recent_insights"]:
            report += f"RECENT INSIGHTS:\n"
            for insight in awareness["recent_insights"][-3:]:
                insight_text = insight.get("insight") or str(insight.get("insights", []))[:100]
                report += f"  • {insight_text}\n"
            report += "\n"
        
        # How is my system health?
        health = awareness["system_health"]
        report += f"SYSTEM HEALTH:\n"
        report += f"  • Memory usage: {health.get('memory_usage', 0)} episodic memories\n"
        report += f"  • Cognitive load: {health.get('cots_active', 0) + health.get('tool_chains_active', 0)} concurrent operations\n"
        
        return report
