"""
repryntt.routing — Three-tier AI model routing.

Tiers:
    Edge    — Small local LLM (Qwen2.5-3B, Kappa-Phi) via llama.cpp on Jetson GPU
    Cloud   — API-based (Gemini 2.0 Flash, OpenAI, Anthropic, OpenRouter)
    Heavy   — Large GPU models (70B+, user-provided remote GPU)

Modules:
    ai_queue        — MasterAIQueue singleton (thread-safe priority queue)
    provider_router — Multi-provider HTTP routing + native tool loop
    task_hierarchy  — Task-type classification (research, creative, technical, …)
    mcp_client      — Model Context Protocol for dynamic tool discovery

Extracted from SAIGE/brain/brain_system.py (18K-line monolith).
"""

from .ai_queue import MasterAIQueue, master_ai_queue
from .provider_router import (
    call_ai_service,
    load_ai_provider_config,
    route_ai_call,
    build_native_tool_schemas,
    execute_native_tool_calls,
)
from .task_hierarchy import TaskType, TaskConfiguration, TaskHierarchySystem

__all__ = [
    "MasterAIQueue",
    "master_ai_queue",
    "call_ai_service",
    "load_ai_provider_config",
    "route_ai_call",
    "build_native_tool_schemas",
    "execute_native_tool_calls",
    "TaskType",
    "TaskConfiguration",
    "TaskHierarchySystem",
]
