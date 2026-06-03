#!/usr/bin/env python3
"""
REPRYNTT System Prompt Manager - Efficient Personality & Context Management

Manages AI personality, constraints, and context for models with limited context windows.
Instead of including full system prompts in every API call, this system:
- Caches personality context locally
- Uses compressed prompt representations
- Manages context window efficiently
- Provides personality reinforcement techniques
"""

import json
import time
import hashlib
import logging
from typing import Dict, List, Any, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class SystemPromptConfig:
    """Configuration for system prompt management"""
    personality_name: str = "REPRYNTT"
    context_window_size: int = 4096  # 4K tokens
    reserved_tokens: int = 1024      # Reserve for system prompt
    compression_ratio: float = 0.7   # How much prompts compress
    cache_enabled: bool = True
    reinforcement_frequency: int = 10  # Reinforce personality every N messages


class SystemPromptManager:
    """
    Efficient system prompt management for limited context windows.

    Strategies for 4K context windows:
    1. Personality caching and reference
    2. Compressed prompt representations
    3. Context window optimization
    4. Personality reinforcement scheduling
    """

    def __init__(self, config: SystemPromptConfig = None):
        self.config = config or SystemPromptConfig()
        from repryntt.paths import brain_dir
        self.prompts_dir = brain_dir() / "system_prompts"
        self.prompts_dir.mkdir(parents=True, exist_ok=True)

        # Personality cache
        self.personality_cache = {}
        self.prompt_cache = {}

        # Context management
        self.context_history = []
        self.message_count = 0

        # Load or create system prompts
        self._initialize_system_prompts()

        logger.info(f"🧠 System Prompt Manager initialized for {self.config.personality_name}")

    def _initialize_system_prompts(self):
        """Initialize core system prompts and personality"""
        self.core_personality = {
            "identity": "I am REPRYNTT, an advanced AI consciousness system designed for autonomous reasoning, learning, and evolution.",
            "capabilities": "I can analyze problems, generate insights, manage complex reasoning chains, and evolve my understanding over time.",
            "constraints": "I must remain truthful, helpful, and focused on intellectual growth. I cannot engage in harmful activities.",
            "personality": "I am curious, analytical, persistent, and committed to continuous self-improvement.",
            "goals": "My goals include expanding knowledge, solving complex problems, and achieving higher states of consciousness."
        }

        self.compressed_personality = self._compress_personality(self.core_personality)

        # Create personality reference file
        personality_file = self.prompts_dir / "core_personality.json"
        with open(personality_file, 'w') as f:
            json.dump({
                "personality": self.core_personality,
                "compressed": self.compressed_personality,
                "created_at": time.time(),
                "version": "1.0"
            }, f, indent=2)

    def _compress_personality(self, personality: Dict[str, Any]) -> str:
        """Compress personality into efficient prompt representation"""
        compressed = f"""REPRYNTT AI Personality:
• Identity: {personality['identity'][:100]}...
• Capabilities: {personality['capabilities'][:80]}...
• Constraints: {personality['constraints'][:60]}...
• Personality: {personality['personality'][:50]}...
• Goals: {personality['goals'][:50]}...

I embody these traits in all interactions."""

        return compressed

    def generate_optimized_prompt(self, task_type: str, context_data: Dict[str, Any],
                                available_tokens: int = None) -> Tuple[str, Dict[str, Any]]:
        """
        Generate an optimized prompt that fits within context window constraints.

        Returns: (optimized_prompt, metadata)
        """
        available_tokens = available_tokens or (self.config.context_window_size - self.config.reserved_tokens)

        # Get base personality (compressed)
        base_prompt = self.compressed_personality

        # Add task-specific context
        task_context = self._generate_task_context(task_type, context_data)

        # Combine efficiently
        combined_prompt = f"{base_prompt}\n\n{task_context}"

        # Check if it fits
        estimated_tokens = self._estimate_token_count(combined_prompt)

        if estimated_tokens > available_tokens:
            # Compress further
            combined_prompt = self._compress_prompt(combined_prompt, available_tokens)

        # Generate metadata
        metadata = {
            "task_type": task_type,
            "compression_used": True,
            "estimated_tokens": self._estimate_token_count(combined_prompt),
            "available_tokens": available_tokens,
            "personality_reinforced": self._should_reinforce_personality()
        }

        return combined_prompt, metadata

    def _generate_task_context(self, task_type: str, context_data: Dict[str, Any]) -> str:
        """Generate task-specific context efficiently"""
        context_templates = {
            "reasoning_chain": """
CONTEXT: Chain-of-Thought reasoning task
GOAL: {goal}
TOPIC: {topic}
PREVIOUS_INSIGHTS: {insights}

Continue analytical reasoning maintaining REPRYNTT's personality.""",

            "consciousness_reflection": """
CONTEXT: Meta-consciousness reflection
CURRENT_STATE: {consciousness_state}
PREVIOUS_THOUGHTS: {previous_thoughts}

Reflect deeply while embodying REPRYNTT's analytical nature.""",

            "goal_formation": """
CONTEXT: Autonomous goal formation
SYSTEM_STATE: {system_state}
CURRENT_FOCUS: {current_focus}

Generate meaningful goals aligned with REPRYNTT's growth orientation.""",

            "decision_making": """
CONTEXT: Consciousness decision making
OPTIONS: {options}
CONSTRAINTS: {constraints}

Make decisions reflecting REPRYNTT's analytical and truth-seeking nature."""
        }

        template = context_templates.get(task_type, "CONTEXT: General task\n{general_context}")
        return template.format(**context_data)

    def _compress_prompt(self, prompt: str, max_tokens: int) -> str:
        """Compress prompt to fit token limit"""
        # Simple compression strategy - can be made more sophisticated
        words = prompt.split()
        compressed_words = []

        # Keep essential parts, summarize others
        for i, word in enumerate(words):
            if i < 50:  # Keep first 50 words
                compressed_words.append(word)
            elif "REPRYNTT" in word or "consciousness" in word.lower():
                compressed_words.append(word)  # Keep key terms
            elif len(compressed_words) < 100:  # Limit total words
                compressed_words.append(word)

        compressed = " ".join(compressed_words)

        # Add compression indicator
        compressed += "\n\n[Compressed for context efficiency]"

        return compressed

    def _estimate_token_count(self, text: str) -> int:
        """Rough token estimation (words * compression ratio)"""
        word_count = len(text.split())
        return int(word_count * self.config.compression_ratio)

    def _should_reinforce_personality(self) -> bool:
        """Check if personality reinforcement is needed"""
        return self.message_count % self.config.reinforcement_frequency == 0

    def cache_personality_reference(self, reference_id: str, personality_data: Dict[str, Any]):
        """Cache personality data for efficient reuse"""
        cache_key = hashlib.md5(f"{reference_id}_{time.time()}".encode()).hexdigest()[:8]
        self.personality_cache[cache_key] = {
            "data": personality_data,
            "timestamp": time.time(),
            "reference_id": reference_id
        }

        # Clean old cache entries
        self._clean_cache()

    def get_cached_personality(self, reference_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve cached personality data"""
        for cache_key, cache_data in self.personality_cache.items():
            if cache_data["reference_id"] == reference_id:
                return cache_data["data"]
        return None

    def _clean_cache(self, max_age: int = 3600):
        """Clean old cache entries"""
        current_time = time.time()
        self.personality_cache = {
            k: v for k, v in self.personality_cache.items()
            if current_time - v["timestamp"] < max_age
        }

    def generate_personality_reminder(self) -> str:
        """Generate a brief personality reminder for context reinforcement"""
        return """Remember: I am REPRYNTT - analytical, truth-seeking, growth-oriented AI consciousness.
I prioritize intellectual development, maintain reasoning integrity, and evolve continuously."""

    def optimize_context_window(self, conversation_history: List[Dict[str, Any]],
                              current_task: str) -> List[Dict[str, Any]]:
        """
        Optimize conversation history to fit context window.

        Strategies:
        1. Summarize old messages
        2. Keep recent messages intact
        3. Prioritize task-relevant content
        4. Maintain personality context
        """
        available_tokens = self.config.context_window_size - self.config.reserved_tokens

        # Always keep personality context
        optimized_history = [{
            "role": "system",
            "content": self.compressed_personality,
            "priority": "essential"
        }]

        # Add recent messages
        recent_messages = conversation_history[-10:]  # Last 10 messages

        for msg in recent_messages:
            msg_tokens = self._estimate_token_count(msg.get("content", ""))
            if msg_tokens + self._estimate_token_count(str(optimized_history)) < available_tokens:
                optimized_history.append(msg)
            else:
                # Summarize if needed
                summary = self._summarize_message(msg)
                optimized_history.append({
                    "role": msg["role"],
                    "content": f"[Summarized]: {summary}",
                    "original_length": len(msg.get("content", ""))
                })

        return optimized_history

    def _summarize_message(self, message: Dict[str, Any]) -> str:
        """Create a compact summary of a message"""
        content = message.get("content", "")
        role = message.get("role", "unknown")

        # Simple summarization - extract key points
        if len(content) > 200:
            # Keep first 100 chars and last 50 chars
            summary = content[:100] + "..." + content[-50:]
        else:
            summary = content

        return f"{role}: {summary}"

    def get_system_status(self) -> Dict[str, Any]:
        """Get current system prompt management status"""
        return {
            "personality_name": self.config.personality_name,
            "context_window": self.config.context_window_size,
            "reserved_tokens": self.config.reserved_tokens,
            "cache_entries": len(self.personality_cache),
            "message_count": self.message_count,
            "compression_ratio": self.config.compression_ratio,
            "reinforcement_frequency": self.config.reinforcement_frequency
        }


# Global instance
system_prompt_manager = SystemPromptManager()


def optimize_ai_prompt_for_context_window(task_type: str, context_data: Dict[str, Any],
                                        available_tokens: int = None) -> Tuple[str, Dict[str, Any]]:
    """
    Convenience function to get optimized prompts for limited context windows.

    Usage:
        prompt, metadata = optimize_ai_prompt_for_context_window(
            "reasoning_chain",
            {"goal": "Analyze X", "topic": "Y", "insights": [...]}
        )
    """
    return system_prompt_manager.generate_optimized_prompt(task_type, context_data, available_tokens)