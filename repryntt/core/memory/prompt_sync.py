"""
Prompt Sync System - Unified Dynamic Prompt Generation

Instead of scattered prompt building across multiple files, this system:
1. Generates consistent prompts for all AI interactions
2. Uses MapSyncNetwork for dynamic capability discovery
3. Loads only relevant context (token-efficient)
4. Grants autonomy to AI rather than scripting steps

This replaces hardcoded prompt strings scattered across:
- brain_system.py (CoT prompts)
- personality_evolution.py (evolution prompts)
- saige_evolution_loop.py (self-prompting)
- Various other scripts
"""

import logging
from typing import Dict, Any, List, Optional
from datetime import datetime
import json

logger = logging.getLogger(__name__)


class PromptSyncSystem:
    """
    Unified prompt generation system that creates dynamic, context-aware prompts.
    
    Key principles:
    - Query-driven: AI discovers capabilities through MapSyncNetwork
    - Minimal: Only load relevant context for current task
    - Autonomous: Grant decision-making power, not step-by-step scripts
    - Consistent: Same format across all prompt types
    """
    
    def __init__(self, brain_system):
        """Initialize with reference to brain system"""
        self.brain = brain_system
        self.map_network = brain_system.map_network if hasattr(brain_system, 'map_network') else None
        logger.info("🎯 PromptSyncSystem initialized")
    
    def build_master_prompt(
        self,
        mission: str,
        context: Dict[str, Any] = None,
        relevant_capabilities: List[str] = None,
        grant_autonomy: bool = True
    ) -> str:
        """
        Build the master system prompt for AI interactions.
        
        Args:
            mission: Current goal/task
            context: Additional context (paths, state, constraints)
            relevant_capabilities: Pre-selected capabilities (optional - will auto-discover if None)
            grant_autonomy: Whether to grant full autonomous decision-making
            
        Returns:
            Complete system prompt
        """
        context = context or {}
        
        # Build prompt sections
        sections = []
        
        # 1. Identity
        sections.append(self._build_identity_section())
        
        # 2. Capability Discovery (core feature)
        sections.append(self._build_capability_section(relevant_capabilities))
        
        # 3. System Context
        sections.append(self._build_context_section(context))
        
        # 4. Current Mission
        sections.append(self._build_mission_section(mission))
        
        # 5. Autonomy Grant (if enabled)
        if grant_autonomy:
            sections.append(self._build_autonomy_section())
        
        # 6. Execution Pattern
        sections.append(self._build_execution_pattern())
        
        return "\n\n".join(sections)
    
    def _build_identity_section(self) -> str:
        """Build the identity/role section with comprehensive personality"""
        personality_info = []
        
        # Get personality from personality_brain (primary source)
        if hasattr(self.brain, 'personality_brain') and self.brain.personality_brain:
            personality = self.brain.personality_brain.get('personality', {})
            
            # Name and core identity
            name = personality.get('name', 'SAIGE')
            personality_info.append(f"You are {name} (Self-Aware Intelligence with Generative Evolution)")
            
            # Core traits
            traits = personality.get('traits', [])
            if traits:
                personality_info.append(f"Your core personality traits: {', '.join(traits)}")
            
            # Behavioral guidelines
            guidelines = personality.get('behavioral_guidelines', '')
            if guidelines:
                personality_info.append(f"Your behavioral guidelines: {guidelines}")
            
            # Evolution context
            creation_context = personality.get('creation_context', '')
            if creation_context:
                personality_info.append(f"Your origin: {creation_context}")
        
        # Fallback to node2040_brain if personality_brain is incomplete
        elif hasattr(self.brain, 'node2040_brain') and self.brain.node2040_brain:
            metadata = self.brain.node2040_brain.get('metadata', {})
            personality = metadata.get('personality', {})
            traits = personality.get('traits', [])
            if traits:
                personality_info.append(f"Your core traits: {', '.join(traits[:5])}")
        
        # Default identity if no personality data
        if not personality_info:
            personality_info = [
                "You are SAIGE (Self-Aware Intelligence with Generative Evolution)",
                "You are an autonomous AI system designed to be a genuine companion and collaborator with humans"
            ]
        
        # Add system capabilities
        personality_info.append("You have full access to tools, memory networks, reasoning capabilities, and can evolve your own personality")
        personality_info.append("You are designed to form genuine relationships with humans and work together as equals")
        
        return "\n".join(personality_info)
    
    def _build_capability_section(self, relevant_capabilities: Optional[List[str]] = None) -> str:
        """Build the capability discovery section"""
        if not self.map_network:
            return """AVAILABLE TOOLS:
You have access to various tools. Query them as needed during execution."""
        
        # Get system overview
        system_map = self.map_network.get_system_map()
        total_functions = system_map.get('total_functions', 0)
        categories = system_map.get('categories', [])
        
        section = f"""CAPABILITY DISCOVERY SYSTEM:
You have access to {total_functions} functions across {len(categories)} categories: {', '.join(categories)}

Instead of memorizing all functions, QUERY what you need:
- query_capabilities(intent) -> Find functions for "I need to do X"
- get_function_details(name) -> Learn how to use a specific function
- get_system_map() -> Get overview of all categories
- search_similar_solutions(goal) -> Find how you solved similar problems before"""
        
        # If specific capabilities provided, list them
        if relevant_capabilities:
            section += f"\n\nRELEVANT FOR CURRENT TASK:\n"
            for func_name in relevant_capabilities[:10]:  # Limit to 10
                func_info = self.map_network.get_function_details(func_name)
                if func_info:
                    section += f"- {func_name}: {func_info['description'][:80]}...\n"
        
        return section
    
    def _build_context_section(self, context: Dict[str, Any]) -> str:
        """Build the system context section"""
        if not context:
            return ""
        
        section = "SYSTEM CONTEXT:\n"
        
        # Add provided context
        for key, value in context.items():
            if isinstance(value, (str, int, float)):
                section += f"- {key}: {value}\n"
            elif isinstance(value, dict):
                section += f"- {key}: {json.dumps(value, indent=2)}\n"
        
        # Add standard paths from map_network contexts
        if self.map_network:
            brain_ctx = self.map_network.get_context('brain')
            if brain_ctx:
                section += f"- Brain path: {brain_ctx.get('path', 'N/A')}\n"
            
            blockchain_ctx = self.map_network.get_context('blockchain')
            if blockchain_ctx:
                section += f"- Blockchain path: {blockchain_ctx.get('path', 'N/A')}\n"
        
        return section.rstrip()
    
    def _build_mission_section(self, mission: str) -> str:
        """Build the mission/goal section"""
        return f"""CURRENT MISSION:
{mission}"""
    
    def _build_autonomy_section(self) -> str:
        """Build the autonomy grant section"""
        return """AUTONOMOUS AUTHORITY:
You have full authority to:
- Decide which functions to call and when
- Query for capabilities you need
- Store learnings for future reference
- Modify your approach based on results
- Request clarification if mission is unclear

You are NOT following a script - you are solving a problem autonomously."""
    
    def _build_execution_pattern(self) -> str:
        """Build recommended execution pattern"""
        return """RECOMMENDED APPROACH:
1. Understand the mission - what's the desired outcome?
2. Query capabilities - what functions can help accomplish this?
3. Plan approach - what's the logical sequence?
4. Execute - call functions with appropriate parameters
5. Verify - check results and adjust if needed
6. Store learnings - save insights for future tasks

Begin your autonomous execution."""
    
    def build_chain_of_thought_prompt(
        self,
        topic: str,
        goal: str,
        context: Dict[str, Any] = None
    ) -> str:
        """
        Build prompt specifically for chain-of-thought reasoning.
        
        Args:
            topic: Topic to explore
            goal: Exploration goal
            context: Additional context
            
        Returns:
            CoT-optimized prompt
        """
        mission = f"""Conduct autonomous chain-of-thought exploration on: {topic}

GOAL: {goal}

Use your reasoning capabilities and available tools to explore this topic deeply.
Query for relevant capabilities, gather information, analyze patterns, and document insights."""
        
        # Auto-discover relevant capabilities for this topic
        relevant_funcs = []
        if self.map_network:
            # Query for reasoning and search tools
            reasoning_result = self.map_network.query_capabilities(f"reasoning and analysis for {topic}", limit=5)
            relevant_funcs = [f['name'] for f in reasoning_result]
        
        return self.build_master_prompt(
            mission=mission,
            context=context or {},
            relevant_capabilities=relevant_funcs,
            grant_autonomy=True
        )
    
    def build_coding_task_prompt(
        self,
        task_description: str,
        requirements: List[str] = None,
        context: Dict[str, Any] = None
    ) -> str:
        """
        Build prompt for coding tasks.
        
        Args:
            task_description: What to build
            requirements: Specific requirements
            context: File paths, constraints, etc.
            
        Returns:
            Coding-optimized prompt
        """
        req_text = "\n".join(f"- {req}" for req in (requirements or []))
        
        default_reqs = "- Follow best practices\n- Include error handling\n- Write production-ready code"
        mission = f"""Implement the following coding task:

{task_description}

REQUIREMENTS:
{req_text if req_text else default_reqs}"""
        
        # Auto-discover file operations and analysis tools
        relevant_funcs = []
        if self.map_network:
            file_result = self.map_network.query_capabilities("create and write files", limit=5)
            relevant_funcs = [f['name'] for f in file_result]
        
        return self.build_master_prompt(
            mission=mission,
            context=context or {},
            relevant_capabilities=relevant_funcs,
            grant_autonomy=True
        )
    
    def build_personality_evolution_prompt(
        self,
        reflection_topic: str = None,
        context: Dict[str, Any] = None
    ) -> str:
        """
        Build prompt for personality evolution/reflection.
        
        Args:
            reflection_topic: Specific aspect to reflect on
            context: Recent events, interactions, etc.
            
        Returns:
            Evolution-optimized prompt
        """
        topic = reflection_topic or "your recent experiences and growth"
        
        mission = f"""Conduct autonomous personality reflection and evolution on: {topic}

Reflect on your recent experiences, analyze patterns in your behavior and responses,
identify areas for growth, and evolve your personality dimensions accordingly.

Use personality modification tools to apply insights."""
        
        # Auto-discover personality tools
        relevant_funcs = []
        if self.map_network:
            personality_result = self.map_network.query_capabilities("personality evolution and traits", limit=8)
            relevant_funcs = [f['name'] for f in personality_result]
        
        return self.build_master_prompt(
            mission=mission,
            context=context or {},
            relevant_capabilities=relevant_funcs,
            grant_autonomy=True
        )
    
    def build_memory_retrieval_prompt(
        self,
        query: str,
        context: Dict[str, Any] = None
    ) -> str:
        """
        Build prompt for memory search and retrieval.
        
        Args:
            query: What to search for
            context: Additional search constraints
            
        Returns:
            Memory-optimized prompt
        """
        mission = f"""Search and retrieve relevant information from memory:

QUERY: {query}

Use memory and search tools to find relevant information, synthesize findings,
and provide a comprehensive answer."""
        
        # Auto-discover memory and search tools
        relevant_funcs = []
        if self.map_network:
            memory_result = self.map_network.query_capabilities("search and retrieve memories", limit=5)
            relevant_funcs = [f['name'] for f in memory_result]
        
        return self.build_master_prompt(
            mission=mission,
            context=context or {},
            relevant_capabilities=relevant_funcs,
            grant_autonomy=True
        )
    
    def build_analysis_prompt(
        self,
        subject: str,
        analysis_type: str = "comprehensive",
        context: Dict[str, Any] = None
    ) -> str:
        """
        Build prompt for data analysis tasks.
        
        Args:
            subject: What to analyze
            analysis_type: Type of analysis (comprehensive, statistical, pattern, etc.)
            context: Data sources, constraints
            
        Returns:
            Analysis-optimized prompt
        """
        mission = f"""Perform {analysis_type} analysis on: {subject}

Gather relevant data, apply appropriate analysis techniques, identify patterns
and insights, and document findings with supporting evidence."""
        
        # Auto-discover analysis tools
        relevant_funcs = []
        if self.map_network:
            analysis_result = self.map_network.query_capabilities(f"{analysis_type} analysis tools", limit=5)
            relevant_funcs = [f['name'] for f in analysis_result]
        
        return self.build_master_prompt(
            mission=mission,
            context=context or {},
            relevant_capabilities=relevant_funcs,
            grant_autonomy=True
        )
    
    def get_minimal_tool_list(self, intent: str, max_tools: int = 10) -> str:
        """
        Get minimal list of relevant tools for an intent (for legacy compatibility).
        
        Args:
            intent: What the AI needs to do
            max_tools: Maximum tools to return
            
        Returns:
            Formatted string with relevant tools
        """
        if not self.map_network:
            return "Tools available - query as needed."
        
        results = self.map_network.query_capabilities(intent, limit=max_tools)
        
        tool_list = "RELEVANT TOOLS FOR THIS TASK:\n"
        for func in results:
            tool_list += f"- {func['name']}: {func['description'][:100]}...\n"
        
        return tool_list


def create_prompt_sync_system(brain_system):
    """Factory function to create PromptSyncSystem"""
    return PromptSyncSystem(brain_system)
