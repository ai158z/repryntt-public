"""
Micro-Chain Execution Engine — Generalized Sequential Reasoning for Small LLMs
================================================================================

Extends the proven micro_chain_trader.py pattern to arbitrary tasks.
Each task is decomposed into 3-8 self-contained micro-steps, each fitting
in ~600-800 tokens. The SYSTEM carries state between steps, not the model's
context window.

Architecture:
  Task → Decompose into MicroSteps → Execute sequentially → Aggregate result

Each MicroStep:
  - Self-contained prompt (system 1-line + user <600 tokens)
  - Structured output (JSON or constrained text)
  - Quality gate (reject gibberish, retry once)
  - State bus passes results forward

Why this works on 1-8B models with 4K context:
  - No bootstrap/identity context (saves ~2000 tokens)
  - Each step is isolated — model doesn't need to remember previous steps
  - The engine carries state externally via the state_bus dict
  - Constrained outputs (max 150-300 tokens) prevent rambling
  - Stop tokens prevent runaway generation

Integration:
  - evolution_loop.py calls execute_task() for local LLM work
  - TaskSystem provides tasks; engine decomposes and executes
  - Results feed back into task_system as completed deliverables
  - Decision logs feed into QLoRA training pipeline

Based on: micro_chain_trader.py (proven working on Qwen2.5-3B)
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

logger = logging.getLogger("micro_chain_engine")

# ── Paths ──
DATA_DIR = Path.home() / ".repryntt" / "data"
CHAIN_LOG_FILE = DATA_DIR / "micro_chain_decisions.jsonl"

# ── LLM Config ──
from repryntt.paths import local_llm_endpoint as _llm_ep
LLM_ENDPOINT = _llm_ep()
LLM_TIMEOUT = 30
DEFAULT_MAX_TOKENS = 200
DEFAULT_TEMPERATURE = 0.4
STOP_TOKENS = ["\n\n\n", "---"]

# ── Quality Gate ──
MIN_RESPONSE_LENGTH = 5  # chars — reject empty/gibberish
MAX_RETRIES = 1


# ════════════════════════════════════════════════════════════════════
# LLM INTERFACE — Minimal, self-contained calls
# ════════════════════════════════════════════════════════════════════

def _llm_call(system_msg: str, prompt: str,
              max_tokens: int = DEFAULT_MAX_TOKENS,
              temperature: float = DEFAULT_TEMPERATURE) -> Optional[str]:
    """Single LLM call with minimal context. No identity, no bootstrap."""
    try:
        resp = requests.post(
            LLM_ENDPOINT,
            json={
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
                "top_p": 0.9,
                "stop": STOP_TOKENS,
            },
            timeout=LLM_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return content.strip() if content else None
    except requests.exceptions.ConnectionError:
        logger.warning("LLM not reachable at localhost:8080")
        return None
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        return None


def _llm_healthy() -> bool:
    """Quick health check."""
    try:
        r = requests.get("http://localhost:8080/health", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _quality_gate(response: Optional[str], step_name: str) -> bool:
    """Check response quality. Returns True if acceptable."""
    if not response:
        return False
    if len(response.strip()) < MIN_RESPONSE_LENGTH:
        logger.warning(f"[{step_name}] Response too short: {response!r}")
        return False
    # Reject if the model just echoed the prompt back
    if response.strip().startswith("You are") or response.strip().startswith("System:"):
        logger.warning(f"[{step_name}] Model echoed system message")
        return False
    return True


# ════════════════════════════════════════════════════════════════════
# MICRO-STEP DEFINITION
# ════════════════════════════════════════════════════════════════════

class MicroStep:
    """A single self-contained reasoning step.

    Each step:
    - Has a 1-line system message (persona for this step)
    - Builds its user prompt from the state_bus
    - Parses structured output into state_bus updates
    - Is fully independent — no memory of other steps
    """

    def __init__(
        self,
        name: str,
        system_msg: str,
        prompt_builder: Callable[[Dict[str, Any]], str],
        output_parser: Callable[[str, Dict[str, Any]], Dict[str, Any]],
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        skip_if: Optional[Callable[[Dict[str, Any]], bool]] = None,
        fallback: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ):
        self.name = name
        self.system_msg = system_msg
        self.prompt_builder = prompt_builder
        self.output_parser = output_parser
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.skip_if = skip_if  # Skip this step if condition met
        self.fallback = fallback  # Fallback if LLM fails


# ════════════════════════════════════════════════════════════════════
# MICRO-CHAIN — Sequence of MicroSteps
# ════════════════════════════════════════════════════════════════════

class MicroChain:
    """A sequence of MicroSteps that execute on a shared state_bus.

    The state_bus is a dict that carries data between steps.
    Each step reads from it, does one LLM call, and writes results back.
    The model never sees previous steps — only the current step's data.
    """

    def __init__(self, name: str, steps: List[MicroStep]):
        self.name = name
        self.steps = steps

    def execute(self, initial_state: Dict[str, Any] = None) -> Dict[str, Any]:
        """Run all steps sequentially, carrying state between them.

        Returns the final state_bus with all accumulated results.
        """
        state_bus = dict(initial_state or {})
        state_bus["_chain_name"] = self.name
        state_bus["_started_at"] = time.time()
        state_bus["_steps_completed"] = []
        state_bus["_steps_skipped"] = []
        state_bus["_steps_failed"] = []

        for step in self.steps:
            step_start = time.time()

            # Check skip condition
            if step.skip_if and step.skip_if(state_bus):
                state_bus["_steps_skipped"].append(step.name)
                logger.info(f"[{self.name}] Skipping step: {step.name}")
                continue

            # Build prompt from current state
            try:
                prompt = step.prompt_builder(state_bus)
            except Exception as e:
                logger.error(f"[{self.name}:{step.name}] Prompt builder failed: {e}")
                state_bus["_steps_failed"].append(step.name)
                if step.fallback:
                    state_bus.update(step.fallback(state_bus))
                continue

            # Call LLM with retry
            response = None
            for attempt in range(1 + MAX_RETRIES):
                raw = _llm_call(step.system_msg, prompt, step.max_tokens, step.temperature)
                if _quality_gate(raw, step.name):
                    response = raw
                    break
                if attempt < MAX_RETRIES:
                    logger.info(f"[{self.name}:{step.name}] Retrying ({attempt + 1})")

            if response is None:
                logger.warning(f"[{self.name}:{step.name}] LLM failed after retries")
                state_bus["_steps_failed"].append(step.name)
                if step.fallback:
                    state_bus.update(step.fallback(state_bus))
                continue

            # Parse output into state_bus updates
            try:
                updates = step.output_parser(response, state_bus)
                state_bus.update(updates)
                state_bus["_steps_completed"].append(step.name)
                elapsed = time.time() - step_start
                logger.info(f"[{self.name}:{step.name}] Done ({elapsed:.1f}s)")
            except Exception as e:
                logger.error(f"[{self.name}:{step.name}] Parser failed: {e}")
                state_bus["_steps_failed"].append(step.name)
                if step.fallback:
                    state_bus.update(step.fallback(state_bus))

        state_bus["_elapsed"] = time.time() - state_bus["_started_at"]
        state_bus["_success"] = len(state_bus["_steps_failed"]) == 0

        # Log decision for training data
        _log_chain_execution(state_bus)

        return state_bus


# ════════════════════════════════════════════════════════════════════
# LOGGING — For QLoRA training pipeline
# ════════════════════════════════════════════════════════════════════

def _log_chain_execution(state_bus: Dict[str, Any]):
    """Log chain execution for training data collection."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "chain": state_bus.get("_chain_name", "unknown"),
            "success": state_bus.get("_success", False),
            "steps_completed": state_bus.get("_steps_completed", []),
            "steps_failed": state_bus.get("_steps_failed", []),
            "steps_skipped": state_bus.get("_steps_skipped", []),
            "elapsed": state_bus.get("_elapsed", 0),
            "task_type": state_bus.get("task_type", "unknown"),
        }
        with open(CHAIN_LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════
# BUILT-IN CHAIN TEMPLATES — Ready-to-use micro-chains
# ════════════════════════════════════════════════════════════════════

# ──────────────────────────────────────────────────────────────────
# 1. RESEARCH CHAIN — Web search → Extract facts → Synthesize
# ──────────────────────────────────────────────────────────────────

def build_research_chain() -> MicroChain:
    """3-step research chain: Plan → Extract → Synthesize."""

    plan_step = MicroStep(
        name="plan_queries",
        system_msg="You are a research assistant. Generate search queries.",
        prompt_builder=lambda s: (
            f"Topic: {s.get('topic', 'unknown')}\n"
            f"Goal: {s.get('goal', 'research this topic')}\n\n"
            f"Generate exactly 3 focused search queries to research this topic.\n"
            f"One line per query. No numbering, no explanation."
        ),
        output_parser=lambda resp, s: {
            "queries": [q.strip() for q in resp.strip().split("\n") if q.strip()][:3]
        },
        max_tokens=100,
        temperature=0.5,
        fallback=lambda s: {"queries": [s.get("topic", "general research")]},
    )

    extract_step = MicroStep(
        name="extract_facts",
        system_msg="You are a fact extractor. Pull key facts from search results.",
        prompt_builder=lambda s: (
            f"Topic: {s.get('topic', 'unknown')}\n"
            f"Search results:\n{s.get('search_results', 'No results available.')[:1500]}\n\n"
            f"Extract 5-8 key facts as bullet points. One fact per line.\n"
            f"Start each with '- '. Be specific with numbers and names."
        ),
        output_parser=lambda resp, s: {
            "facts": [line.strip() for line in resp.split("\n")
                      if line.strip().startswith("- ")][:8]
        },
        max_tokens=300,
        temperature=0.3,
        fallback=lambda s: {"facts": ["- No facts could be extracted"]},
    )

    synthesize_step = MicroStep(
        name="synthesize",
        system_msg="You are a report writer. Summarize findings concisely.",
        prompt_builder=lambda s: (
            f"Topic: {s.get('topic', 'unknown')}\n"
            f"Goal: {s.get('goal', 'summarize research')}\n"
            f"Key facts:\n" + "\n".join(s.get("facts", ["No facts"])) + "\n\n"
            f"Write a 2-3 paragraph summary of these findings.\n"
            f"Focus on actionable insights. Be specific."
        ),
        output_parser=lambda resp, s: {"report": resp.strip()},
        max_tokens=400,
        temperature=0.5,
    )

    return MicroChain("research", [plan_step, extract_step, synthesize_step])


# ──────────────────────────────────────────────────────────────────
# 2. ANALYSIS CHAIN — Observe → Measure → Conclude
# ──────────────────────────────────────────────────────────────────

def build_analysis_chain() -> MicroChain:
    """3-step analysis: Observe → Measure → Conclude."""

    observe_step = MicroStep(
        name="observe",
        system_msg="You are a data analyst. Identify key observations.",
        prompt_builder=lambda s: (
            f"Subject: {s.get('subject', 'unknown')}\n"
            f"Data:\n{s.get('data', 'No data provided.')[:1500]}\n\n"
            f"List 3-5 key observations. One per line, start with '- '.\n"
            f"Focus on patterns, anomalies, and notable values."
        ),
        output_parser=lambda resp, s: {
            "observations": [l.strip() for l in resp.split("\n")
                             if l.strip().startswith("- ")][:5]
        },
        max_tokens=200,
        temperature=0.3,
    )

    measure_step = MicroStep(
        name="measure",
        system_msg="You are a quantitative analyst. Score and rank findings.",
        prompt_builder=lambda s: (
            f"Subject: {s.get('subject', 'unknown')}\n"
            f"Observations:\n" + "\n".join(s.get("observations", ["None"])) + "\n\n"
            f"For each observation, rate severity/importance 1-10.\n"
            f"Format: SCORE: N | observation text\n"
            f"Then on the last line: OVERALL: N/10"
        ),
        output_parser=lambda resp, s: _parse_scored_observations(resp),
        max_tokens=200,
        temperature=0.2,
    )

    conclude_step = MicroStep(
        name="conclude",
        system_msg="You are a decision advisor. Give clear recommendations.",
        prompt_builder=lambda s: (
            f"Subject: {s.get('subject', 'unknown')}\n"
            f"Goal: {s.get('goal', 'analyze and recommend')}\n"
            f"Scored observations:\n" +
            "\n".join(f"[{o.get('score', '?')}/10] {o.get('text', '?')}"
                      for o in s.get("scored_observations", [])) +
            f"\nOverall score: {s.get('overall_score', '?')}/10\n\n"
            f"Based on this analysis:\n"
            f"1. What is the main conclusion? (1 sentence)\n"
            f"2. What action should be taken? (1 sentence)\n"
            f"3. What risk exists? (1 sentence)"
        ),
        output_parser=lambda resp, s: {"conclusion": resp.strip()},
        max_tokens=200,
        temperature=0.4,
    )

    return MicroChain("analysis", [observe_step, measure_step, conclude_step])


def _parse_scored_observations(resp: str) -> Dict[str, Any]:
    """Parse SCORE: N | text format."""
    scored = []
    overall = 5
    for line in resp.split("\n"):
        line = line.strip()
        score_match = re.match(r'SCORE:\s*(\d+)\s*\|\s*(.*)', line, re.IGNORECASE)
        overall_match = re.match(r'OVERALL:\s*(\d+)', line, re.IGNORECASE)
        if score_match:
            scored.append({
                "score": int(score_match.group(1)),
                "text": score_match.group(2).strip(),
            })
        elif overall_match:
            overall = int(overall_match.group(1))
    return {"scored_observations": scored, "overall_score": overall}


# ──────────────────────────────────────────────────────────────────
# 3. PLANNING CHAIN — Assess → Prioritize → Schedule
# ──────────────────────────────────────────────────────────────────

def build_planning_chain() -> MicroChain:
    """3-step planning: Assess situation → Prioritize → Schedule actions."""

    assess_step = MicroStep(
        name="assess",
        system_msg="You are a project manager. Assess the current situation.",
        prompt_builder=lambda s: (
            f"Context: {s.get('context', 'No context')[:1000]}\n"
            f"Goal: {s.get('goal', 'plan next actions')}\n"
            f"Resources: {s.get('resources', 'standard tools')}\n\n"
            f"What is the current status? List 3-5 key points.\n"
            f"Format: STATUS: good/needs_work/blocked | description"
        ),
        output_parser=lambda resp, s: {"assessment": resp.strip()},
        max_tokens=200,
        temperature=0.3,
    )

    prioritize_step = MicroStep(
        name="prioritize",
        system_msg="You are a task prioritizer. Rank actions by impact.",
        prompt_builder=lambda s: (
            f"Goal: {s.get('goal', 'determine priorities')}\n"
            f"Assessment:\n{s.get('assessment', 'No assessment')[:800]}\n\n"
            f"List 3-5 concrete actions, ranked by priority.\n"
            f"Format: P1: action (reason) / P2: action (reason) / etc.\n"
            f"Each action must have a clear deliverable."
        ),
        output_parser=lambda resp, s: {
            "priorities": [l.strip() for l in resp.split("\n") if l.strip()][:5]
        },
        max_tokens=200,
        temperature=0.3,
    )

    schedule_step = MicroStep(
        name="schedule",
        system_msg="You are a scheduler. Create an execution plan.",
        prompt_builder=lambda s: (
            f"Priorities:\n" + "\n".join(s.get("priorities", ["No priorities"])) + "\n\n"
            f"Create an execution plan. For each priority:\n"
            f"TASK: description | TOOLS: tool1, tool2 | TIME: estimate\n"
            f"Be specific about which tools to use."
        ),
        output_parser=lambda resp, s: {"plan": resp.strip()},
        max_tokens=300,
        temperature=0.3,
    )

    return MicroChain("planning", [assess_step, prioritize_step, schedule_step])


# ──────────────────────────────────────────────────────────────────
# 4. SYSTEM CHECK CHAIN — Scan → Diagnose → Recommend
# ──────────────────────────────────────────────────────────────────

def build_system_check_chain() -> MicroChain:
    """3-step system health: Scan → Diagnose → Recommend."""

    scan_step = MicroStep(
        name="scan",
        system_msg="You are a system administrator. Analyze system data.",
        prompt_builder=lambda s: (
            f"System data:\n{s.get('system_data', 'No data')[:1500]}\n\n"
            f"Identify any issues. List each as:\n"
            f"- OK: component — status\n"
            f"- WARN: component — issue\n"
            f"- FAIL: component — critical issue"
        ),
        output_parser=lambda resp, s: {"scan_results": resp.strip()},
        max_tokens=200,
        temperature=0.2,
    )

    diagnose_step = MicroStep(
        name="diagnose",
        system_msg="You are a diagnostician. Determine root causes.",
        prompt_builder=lambda s: (
            f"Scan results:\n{s.get('scan_results', 'No results')[:800]}\n\n"
            f"For each WARN or FAIL, determine the likely root cause.\n"
            f"Format: ISSUE: description | CAUSE: likely cause | FIX: suggested fix"
        ),
        output_parser=lambda resp, s: {"diagnosis": resp.strip()},
        max_tokens=200,
        temperature=0.3,
        skip_if=lambda s: "WARN" not in s.get("scan_results", "") and
                          "FAIL" not in s.get("scan_results", ""),
    )

    recommend_step = MicroStep(
        name="recommend",
        system_msg="You are a sysadmin advisor. Give clear action items.",
        prompt_builder=lambda s: (
            f"Scan:\n{s.get('scan_results', 'clean')[:500]}\n"
            f"Diagnosis:\n{s.get('diagnosis', 'no issues found')[:500]}\n\n"
            f"Summarize system health in 1 sentence.\n"
            f"Then list action items (if any) as:\n"
            f"ACTION: description | PRIORITY: high/medium/low"
        ),
        output_parser=lambda resp, s: {"recommendation": resp.strip()},
        max_tokens=200,
        temperature=0.3,
    )

    return MicroChain("system_check", [scan_step, diagnose_step, recommend_step])


# ──────────────────────────────────────────────────────────────────
# 5. COMMERCE CHECK CHAIN — Inventory → Sales → Actions
# ──────────────────────────────────────────────────────────────────

def build_commerce_chain() -> MicroChain:
    """3-step commerce: Check inventory → Analyze sales → Recommend actions."""

    inventory_step = MicroStep(
        name="inventory_check",
        system_msg="You are a commerce manager. Review inventory status.",
        prompt_builder=lambda s: (
            f"Store data:\n{s.get('store_data', 'No store data')[:1500]}\n\n"
            f"Review inventory status. For each product:\n"
            f"PRODUCT: name | STOCK: N | STATUS: ok/low/out"
        ),
        output_parser=lambda resp, s: {"inventory_status": resp.strip()},
        max_tokens=200,
        temperature=0.2,
    )

    sales_step = MicroStep(
        name="sales_analysis",
        system_msg="You are a sales analyst. Identify trends.",
        prompt_builder=lambda s: (
            f"Inventory:\n{s.get('inventory_status', 'unknown')[:500]}\n"
            f"Sales data:\n{s.get('sales_data', 'No sales data')[:1000]}\n\n"
            f"Analyze sales performance:\n"
            f"- Top seller and why\n"
            f"- Underperformers and likely cause\n"
            f"- Trend direction (up/flat/down)"
        ),
        output_parser=lambda resp, s: {"sales_analysis": resp.strip()},
        max_tokens=200,
        temperature=0.3,
    )

    action_step = MicroStep(
        name="commerce_actions",
        system_msg="You are a commerce strategist. Recommend specific actions.",
        prompt_builder=lambda s: (
            f"Inventory:\n{s.get('inventory_status', 'unknown')[:400]}\n"
            f"Sales analysis:\n{s.get('sales_analysis', 'unknown')[:400]}\n\n"
            f"Recommend 2-3 specific actions to improve sales.\n"
            f"Format: ACTION: description | EXPECTED: outcome | EFFORT: low/medium/high"
        ),
        output_parser=lambda resp, s: {"commerce_actions": resp.strip()},
        max_tokens=200,
        temperature=0.4,
    )

    return MicroChain("commerce", [inventory_step, sales_step, action_step])


# ──────────────────────────────────────────────────────────────────
# 6. GENERAL TASK CHAIN — Understand → Plan → Execute → Verify
# ──────────────────────────────────────────────────────────────────

def build_general_task_chain() -> MicroChain:
    """4-step general task: Understand → Plan → Execute → Verify.

    This is the catch-all chain for any task type. The 'execute' step
    produces a structured action plan rather than actually executing
    (the caller handles execution).
    """

    understand_step = MicroStep(
        name="understand",
        system_msg="You are a task analyst. Break down what needs to be done.",
        prompt_builder=lambda s: (
            f"Task: {s.get('task_title', 'unknown')}\n"
            f"Description: {s.get('task_description', 'none')[:500]}\n"
            f"Deliverable: {s.get('deliverable', 'not specified')}\n\n"
            f"Answer these 3 questions:\n"
            f"1. WHAT: What exactly needs to be produced? (1 sentence)\n"
            f"2. HOW: What tools/steps are needed? (list 2-4 steps)\n"
            f"3. DONE: How do we know it's complete? (1 sentence)"
        ),
        output_parser=lambda resp, s: {"task_understanding": resp.strip()},
        max_tokens=200,
        temperature=0.3,
    )

    plan_step = MicroStep(
        name="plan",
        system_msg="You are a task planner. Create minimal actionable plans.",
        prompt_builder=lambda s: (
            f"Task: {s.get('task_title', 'unknown')}\n"
            f"Understanding:\n{s.get('task_understanding', 'none')[:600]}\n"
            f"Available tools: {', '.join(s.get('available_tools', ['read_file', 'write_file', 'web_search']))}\n\n"
            f"Create a step-by-step execution plan (3-5 steps max).\n"
            f"Format: STEP N: tool_name | action description | expected output"
        ),
        output_parser=lambda resp, s: {"execution_plan": resp.strip()},
        max_tokens=250,
        temperature=0.3,
    )

    execute_step = MicroStep(
        name="execute",
        system_msg="You are a task executor. Carry out the plan step by step.",
        prompt_builder=lambda s: (
            f"Task: {s.get('task_title', 'unknown')}\n"
            f"Plan:\n{s.get('execution_plan', 'no plan')[:600]}\n"
            f"Context data:\n{s.get('execution_context', 'no additional context')[:600]}\n\n"
            f"Execute the plan. For each step, state:\n"
            f"DONE: step description | RESULT: what was produced\n"
            f"or BLOCKED: step description | REASON: why it can't be done"
        ),
        output_parser=lambda resp, s: {"execution_result": resp.strip()},
        max_tokens=300,
        temperature=0.4,
    )

    verify_step = MicroStep(
        name="verify",
        system_msg="You are a quality checker. Verify task completion.",
        prompt_builder=lambda s: (
            f"Task: {s.get('task_title', 'unknown')}\n"
            f"Expected deliverable: {s.get('deliverable', 'not specified')}\n"
            f"Execution result:\n{s.get('execution_result', 'no result')[:600]}\n\n"
            f"Verify: Is the task complete?\n"
            f"Reply EXACTLY: COMPLETE | summary of what was produced\n"
            f"or: INCOMPLETE | what's still missing"
        ),
        output_parser=lambda resp, s: _parse_verification(resp),
        max_tokens=100,
        temperature=0.2,
    )

    return MicroChain("general_task", [understand_step, plan_step, execute_step, verify_step])


def _parse_verification(resp: str) -> Dict[str, Any]:
    """Parse COMPLETE/INCOMPLETE verification."""
    resp = resp.strip()
    if resp.upper().startswith("COMPLETE"):
        summary = re.sub(r'^COMPLETE\s*\|?\s*', '', resp, flags=re.IGNORECASE).strip()
        return {"task_complete": True, "verification": summary or "Task completed"}
    else:
        missing = re.sub(r'^INCOMPLETE\s*\|?\s*', '', resp, flags=re.IGNORECASE).strip()
        return {"task_complete": False, "verification": missing or "Task incomplete"}


# ════════════════════════════════════════════════════════════════════
# CHAIN REGISTRY — Map task types to chains
# ════════════════════════════════════════════════════════════════════

_CHAIN_BUILDERS: Dict[str, Callable[[], MicroChain]] = {
    "research": build_research_chain,
    "analysis": build_analysis_chain,
    "planning": build_planning_chain,
    "system": build_system_check_chain,
    "commerce": build_commerce_chain,
    "general": build_general_task_chain,
}


def get_chain_for_task_type(task_type: str) -> MicroChain:
    """Get the appropriate micro-chain for a task type."""
    builder = _CHAIN_BUILDERS.get(task_type, _CHAIN_BUILDERS["general"])
    return builder()


def register_chain(task_type: str, builder: Callable[[], MicroChain]):
    """Register a custom chain builder for a task type."""
    _CHAIN_BUILDERS[task_type] = builder


# ════════════════════════════════════════════════════════════════════
# HIGH-LEVEL API — Execute a task via micro-chains
# ════════════════════════════════════════════════════════════════════

def execute_task(
    task_title: str,
    task_description: str = "",
    task_type: str = "general",
    deliverable: str = "",
    context_data: Dict[str, Any] = None,
    available_tools: List[str] = None,
) -> Dict[str, Any]:
    """Execute a task using the appropriate micro-chain.

    This is the main entry point. Call this from the evolution loop
    or any other system that needs local LLM reasoning.

    Args:
        task_title: Short description of what to do
        task_description: Longer description with details
        task_type: One of: research, analysis, planning, system, commerce, general
        deliverable: What the completed task should produce
        context_data: Extra data to inject into the state bus
        available_tools: List of tool names the system has access to

    Returns:
        State bus dict with all results, including:
        - _success: bool
        - _steps_completed: list
        - _elapsed: float
        - task-specific outputs (report, conclusion, plan, etc.)
    """
    if not _llm_healthy():
        return {
            "_success": False,
            "_error": "LLM not available at localhost:8080",
            "_steps_completed": [],
            "_steps_failed": ["llm_health_check"],
        }

    chain = get_chain_for_task_type(task_type)

    # Build initial state bus
    initial_state = {
        "task_title": task_title,
        "task_description": task_description,
        "task_type": task_type,
        "deliverable": deliverable,
        "available_tools": available_tools or ["read_file", "write_file", "web_search"],
    }
    if context_data:
        initial_state.update(context_data)

    logger.info(f"Executing micro-chain '{chain.name}' for task: {task_title}")
    result = chain.execute(initial_state)
    logger.info(
        f"Chain '{chain.name}' complete: "
        f"success={result.get('_success')} "
        f"steps={len(result.get('_steps_completed', []))} "
        f"elapsed={result.get('_elapsed', 0):.1f}s"
    )

    return result


def classify_task_type(title: str, description: str = "") -> str:
    """Classify a task into a chain type using keyword matching.

    Falls back to 'general' if no strong match. This is a fast
    heuristic — no LLM call needed.
    """
    text = f"{title} {description}".lower()

    if any(w in text for w in ["research", "search", "find out", "look up", "investigate"]):
        return "research"
    if any(w in text for w in ["analyze", "analysis", "evaluate", "score", "compare", "measure"]):
        return "analysis"
    if any(w in text for w in ["plan", "schedule", "prioritize", "organize", "roadmap"]):
        return "planning"
    if any(w in text for w in ["system", "health", "uptime", "service", "error", "log", "disk", "memory"]):
        return "system"
    if any(w in text for w in ["commerce", "shopify", "etsy", "product", "listing",
                                "inventory", "sales", "order", "revenue"]):
        return "commerce"
    if any(w in text for w in ["trade", "token", "portfolio", "signal", "scalp"]):
        return "analysis"  # Trading analysis uses analysis chain

    return "general"
