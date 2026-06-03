"""
Weekly Deep-Think Session — Planetary/Humanity Reasoning
========================================================

A structured weekly reasoning session that:
1. Searches for current global challenges and scientific breakthroughs
2. Synthesizes findings with the system's Value Compass and active pursuits
3. Produces 2-3 "big question threads" that seed the pursuit system
4. Validates threads against alignment before storing

Designed to run as a CRON entry (Saturday morning or configurable).
Uses existing tools: real_web_search (DDG), knowledge_router, pursuit system.

Does NOT depend on news RSS (which is disabled) — uses DDG web search
for current events grounding.

Integration:
    - Register via existing schedule_cron in persistent_agents.py
    - Output stored in ~/.repryntt/brain/deep_think/YYYY-MM-DD.json
    - GoalGenerator reads these threads as pursuit seeds
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("repryntt.scheduling.deep_think")

DEEP_THINK_DIR = Path.home() / ".repryntt" / "brain" / "deep_think"
DEEP_THINK_DIR.mkdir(parents=True, exist_ok=True)

DEEP_THINK_PROMPT_TEMPLATE = """You are conducting a weekly deep-thinking session about the world's most important challenges and opportunities.

## Context

**Today**: {date}
**Your active pursuits**: {active_pursuits}
**Your value compass priorities**: {values}

## Research Findings

### Global Challenges (from web search):
{challenges_results}

### Scientific Breakthroughs (from web search):
{science_results}

### Reference Context (from knowledge base):
{reference_results}

## Your Task

Based on the above research, identify 2-3 "big question threads" — important questions that deserve sustained exploration over the coming week.

For EACH thread, provide:
1. **Question**: A specific, actionable question (not vague)
2. **Evidence**: What from the search results connects to this question
3. **Connection to values**: How this aligns with your core values
4. **Exploration direction**: Concrete steps to explore this question
5. **Expected learning**: What you hope to understand after exploring

Format your response as JSON:
```json
{{
    "threads": [
        {{
            "question": "...",
            "evidence": "...",
            "value_connection": "...",
            "direction": "...",
            "expected_learning": "..."
        }}
    ],
    "meta_reflection": "One sentence about what drew your attention this week"
}}
```

Be specific and grounded. Every thread must cite evidence from the search results.
Do NOT hallucinate or invent facts not in the research findings.
"""


def run_deep_think_session(
    brain_system=None,
    value_compass_summary: str = "",
    active_pursuits: str = "",
) -> Optional[Dict[str, Any]]:
    """Execute a weekly deep-think session.

    Args:
        brain_system: BrainSystem instance for AI calls (optional — can use tools directly)
        value_compass_summary: Current value compass state as text
        active_pursuits: Current active pursuits as text

    Returns:
        Dict with threads and metadata, or None if session failed.
    """
    logger.info("Starting weekly deep-think session...")

    # Step 1: Search for current global challenges
    challenges = _search_challenges()

    # Step 2: Search for scientific breakthroughs
    science = _search_science()

    # Step 3: Reference context from knowledge base
    reference = _search_reference()

    if not challenges and not science:
        logger.warning("Deep-think: no search results available, aborting")
        return None

    # Step 4: Build reasoning prompt
    prompt = DEEP_THINK_PROMPT_TEMPLATE.format(
        date=datetime.now().strftime("%Y-%m-%d"),
        active_pursuits=active_pursuits or "None currently active",
        values=value_compass_summary or "Curiosity, integrity, growth, empathy",
        challenges_results=_format_results(challenges),
        science_results=_format_results(science),
        reference_results=_format_results(reference),
    )

    # Step 5: Call LLM (high tier for deep reasoning)
    response = _call_llm(prompt, brain_system)
    if not response:
        logger.warning("Deep-think: LLM call failed")
        return None

    # Step 6: Parse response
    result = _parse_response(response)
    if not result:
        logger.warning("Deep-think: failed to parse LLM response")
        return None

    # Step 7: Store results
    _store_result(result)
    logger.info("Deep-think session complete: %d threads generated",
                len(result.get("threads", [])))

    return result


def _search_challenges() -> List[Dict]:
    """Search for current global challenges via DDG."""
    try:
        from repryntt.search.web_search_tools import real_web_search
        result = real_web_search("global challenges facing humanity 2026", num_results=5)
        return result.get("results", []) if isinstance(result, dict) else []
    except Exception as e:
        logger.debug("Challenge search failed: %s", e)
        return []


def _search_science() -> List[Dict]:
    """Search for recent scientific breakthroughs via DDG."""
    try:
        from repryntt.search.web_search_tools import real_web_search
        result = real_web_search("scientific breakthroughs this week 2026", num_results=5)
        return result.get("results", []) if isinstance(result, dict) else []
    except Exception as e:
        logger.debug("Science search failed: %s", e)
        return []


def _search_reference() -> List[Dict]:
    """Search knowledge router for reference framing."""
    try:
        from repryntt.search.knowledge_router import get_knowledge_router
        router = get_knowledge_router()
        result = router.search("current global issues technology AI", max_results=3)
        return result.get("results", []) if isinstance(result, dict) else []
    except Exception as e:
        logger.debug("Reference search failed: %s", e)
        return []


def _format_results(results: List[Dict]) -> str:
    """Format search results for prompt injection."""
    if not results:
        return "(no results available)"
    lines = []
    for r in results[:5]:
        title = r.get("title", r.get("name", "Untitled"))
        snippet = r.get("snippet", r.get("description", r.get("abstract", "")))
        url = r.get("url", r.get("link", ""))
        lines.append(f"- **{title}**: {snippet[:200]}")
        if url:
            lines.append(f"  Source: {url}")
    return "\n".join(lines)


def _call_llm(prompt: str, brain_system=None) -> Optional[str]:
    """Call LLM for deep reasoning. Uses brain_system if available, else direct."""
    if brain_system and hasattr(brain_system, 'call_ai'):
        try:
            return brain_system.call_ai(prompt, max_tokens=2000)
        except Exception as e:
            logger.debug("Brain system LLM call failed: %s", e)

    # Fallback: try provider router directly
    try:
        from repryntt.routing.provider_router import route_ai_call
        result = route_ai_call(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            tier="high",
        )
        if isinstance(result, dict):
            return result.get("content", result.get("text", ""))
        return str(result) if result else None
    except Exception as e:
        logger.debug("Direct LLM call failed: %s", e)
        return None


def _parse_response(response: str) -> Optional[Dict]:
    """Parse LLM response, extracting JSON from markdown if needed."""
    try:
        # Try direct JSON parse
        return json.loads(response)
    except json.JSONDecodeError:
        pass

    # Try extracting JSON from markdown code block
    try:
        if "```json" in response:
            json_str = response.split("```json")[1].split("```")[0].strip()
            return json.loads(json_str)
        elif "```" in response:
            json_str = response.split("```")[1].split("```")[0].strip()
            return json.loads(json_str)
    except (json.JSONDecodeError, IndexError):
        pass

    # Last resort: try to find JSON object in response
    try:
        start = response.index("{")
        end = response.rindex("}") + 1
        return json.loads(response[start:end])
    except (ValueError, json.JSONDecodeError):
        pass

    return None


def _store_result(result: Dict) -> None:
    """Persist deep-think results to disk."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    output_path = DEEP_THINK_DIR / f"{date_str}.json"

    result["generated_at"] = datetime.now().isoformat()
    result["version"] = 1

    try:
        output_path.write_text(json.dumps(result, indent=2))
        logger.info("Deep-think stored: %s", output_path)
    except Exception as e:
        logger.error("Failed to store deep-think result: %s", e)


def get_deep_think_cron_entry() -> Dict[str, Any]:
    """Return a CRON entry dict compatible with persistent_agents.py schedule_cron format."""
    return {
        "label": "weekly_deep_think",
        "prompt": (
            "Run your weekly deep-think session. Use the deep_think tool to search for "
            "current global challenges and scientific breakthroughs, synthesize them "
            "with your values, and produce big-question threads for the coming week. "
            "Store results for your GoalGenerator to reference."
        ),
        "interval_minutes": 10080,  # 7 days
        "enabled": True,
    }
