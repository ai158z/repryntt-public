"""
Memory dataclass models — extracted from SAIGE/brain/brain_system.py lines 1269-1327.

Classes:
    MemoryEntry       — Base for all memory entries
    EpisodicMemory    — Conversation history and context
    SemanticMemory    — Factual knowledge from APIs and learning
    ProceduralMemory  — How-to knowledge and tool usage patterns
    WorkingMemory     — Current conversation context (temporary)
    ToolCall          — Represents a tool/API call made by the AI
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class MemoryEntry:
    """Base class for all memory entries"""
    id: str
    content: str
    timestamp: float
    confidence: float = 1.0
    source: str = "unknown"
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EpisodicMemory(MemoryEntry):
    """Conversation history and context"""
    conversation_id: str = ""
    user_input: str = ""
    ai_response: str = ""
    tool_calls: List[Dict] = field(default_factory=list)
    outcome: str = "neutral"  # success, failure, neutral


@dataclass
class SemanticMemory(MemoryEntry):
    """Factual knowledge from APIs and learning"""
    topic: str = ""
    domain: str = ""
    key_facts: List[str] = field(default_factory=list)
    related_topics: List[str] = field(default_factory=list)
    verification_sources: List[str] = field(default_factory=list)


@dataclass
class ProceduralMemory(MemoryEntry):
    """How-to knowledge and tool usage patterns"""
    task_type: str = ""
    steps: List[str] = field(default_factory=list)
    tools_used: List[str] = field(default_factory=list)
    success_rate: float = 0.0
    execution_time: float = 0.0


@dataclass
class WorkingMemory:
    """Current conversation context (temporary)"""
    conversation_id: str
    current_topic: str = ""
    relevant_memories: List[Dict] = field(default_factory=list)
    active_tools: List[str] = field(default_factory=list)
    context_window: str = ""  # For 3200 word limit management
    last_updated: float = field(default_factory=time.time)


@dataclass
class ToolCall:
    """Represents a tool/API call made by the AI"""
    tool_name: str
    parameters: Dict[str, Any]
    timestamp: float
    result: Optional[Dict] = None
    success: bool = False
    execution_time: float = 0.0
    error_message: Optional[str] = None
