"""
repryntt.learning.llm_tools — Jarvis-callable tools for LLM learning system.

9 tools for monitoring and interacting with the LLM orchestration learner.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def _get_learner():
    from repryntt.learning.llm_learner import get_llm_learner
    return get_llm_learner()


# ---------------------------------------------------------------------------
#  Tools
# ---------------------------------------------------------------------------

def llm_learning_stats(params: Dict[str, Any] = None) -> Dict[str, Any]:
    """Get comprehensive LLM learning statistics — call counts, quality averages,
    context priorities, escalation rules, and model profile."""
    try:
        return _get_learner().get_stats()
    except Exception as e:
        return {"error": str(e)}


def llm_learning_brief(params: Dict[str, Any] = None) -> Dict[str, Any]:
    """Get a compact text brief of LLM learning state (for prompt injection)."""
    try:
        brief = _get_learner().get_brief()
        return {"brief": brief or "(not enough data yet)"}
    except Exception as e:
        return {"error": str(e)}


def llm_escalation_report(params: Dict[str, Any] = None) -> Dict[str, Any]:
    """Get escalation recommendations for all task types — shows which tasks
    should be sent to cloud models instead of local."""
    try:
        return _get_learner().get_escalation_report()
    except Exception as e:
        return {"error": str(e)}


def llm_context_report(params: Dict[str, Any] = None) -> Dict[str, Any]:
    """Get context effectiveness report — shows which context items
    improve output quality and their learned priority weights."""
    try:
        return _get_learner().get_context_report()
    except Exception as e:
        return {"error": str(e)}


def llm_model_profile(params: Dict[str, Any] = None) -> Dict[str, Any]:
    """Get the current model capability profile — tier, context window,
    average quality, latency, and per-task-type performance."""
    try:
        return _get_learner().get_model_profile()
    except Exception as e:
        return {"error": str(e)}


def llm_score_output(params: Dict[str, Any] = None) -> Dict[str, Any]:
    """Score a text response's quality using the learned heuristics.
    Params: text (str), task_type (str, optional)."""
    try:
        params = params or {}
        text = params.get("text", "")
        task_type = params.get("task_type", "general")
        score = _get_learner().score_output(text, task_type)
        reject = _get_learner().should_reject_output(score)
        return {
            "quality_score": score,
            "should_reject": reject,
            "task_type": task_type,
        }
    except Exception as e:
        return {"error": str(e)}


def llm_should_escalate(params: Dict[str, Any] = None) -> Dict[str, Any]:
    """Check if a task type should be escalated to a cloud model.
    Params: task_type (str)."""
    try:
        params = params or {}
        task_type = params.get("task_type", "general")
        return _get_learner().get_escalation_recommendation(task_type)
    except Exception as e:
        return {"error": str(e)}


def llm_context_budget(params: Dict[str, Any] = None) -> Dict[str, Any]:
    """Get optimized context token allocation for a task type.
    Params: task_type (str), max_tokens (int), available_items (dict of item_type: tokens)."""
    try:
        params = params or {}
        task_type = params.get("task_type", "general")
        max_tokens = params.get("max_tokens", 3000)
        available = params.get("available_items", {})
        budget = _get_learner().get_context_budget(task_type, max_tokens, available)
        return {"task_type": task_type, "max_tokens": max_tokens, "budget": budget}
    except Exception as e:
        return {"error": str(e)}


def llm_detect_model(params: Dict[str, Any] = None) -> Dict[str, Any]:
    """Detect and register model capabilities. Call after changing the local model.
    Params: context_window (int), model_name (str)."""
    try:
        params = params or {}
        context_window = params.get("context_window", 4096)
        model_name = params.get("model_name", "")
        _get_learner().detect_model_capabilities(context_window, model_name)
        return _get_learner().get_model_profile()
    except Exception as e:
        return {"error": str(e)}
