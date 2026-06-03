"""
Tool context builders for AI prompts.

Provides intelligent tool context, task hints, and search-result formatting
for chain-of-thought and autonomous operations.

Standalone functions extracted from BrainSystem monolith (Batch 3).
"""

import inspect
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Prompt Context Builders ──────────────────────────────────────────

def get_swarm_tools_for_prompt(swarm_commander: Any = None) -> str:
    """Return compact swarm commander guidance for AI prompts."""
    if not swarm_commander:
        return ""

    return """
🐝 SWARM TOOLS (Agent Teams):
- quick_research(topic, num_agents=3): Multi-agent deep research
- quick_brainstorm(topic, num_agents=3): Creative ideation swarm
- start_discussion(topic, agents): Agent discussion panel
- swarm_dispatch_task(swarm_id, task): Send task to specific swarm
- swarm_create_agent(name, role, capabilities): Create specialist agent
- swarm_create_swarm(name, mission, agent_names): Form new team
"""


def get_council_tools_for_prompt() -> str:
    """Council tools disabled — returns empty string."""
    return ""


def get_mcp_tools_for_prompt(mcp_client: Any = None) -> str:
    """Return compact MCP tools summary for AI prompts."""
    if not mcp_client:
        return ""

    try:
        mcp_tools = getattr(mcp_client, "tools", None) or {}
        if not mcp_tools:
            return ""

        lines = ["\n🔌 MCP TOOLS (External Services):"]
        for name, tool in list(mcp_tools.items())[:15]:
            desc = ""
            if isinstance(tool, dict):
                desc = tool.get("description", "")[:80]
            elif hasattr(tool, "description"):
                desc = (tool.description or "")[:80]
            lines.append(f"- {name}: {desc}")

        return "\n".join(lines)

    except Exception as e:
        logger.debug(f"MCP tools prompt failed: {e}")
        return ""


def build_intelligent_tool_context(
    available_tools: Dict[str, Callable],
    task_context: str = "",
    map_network: Any = None,
    mcp_client: Any = None,
    swarm_commander: Any = None,
    max_tools: int = 20,
) -> str:
    """Build an intelligent, context-aware tool description block for prompts.

    Uses *map_network* (MapSyncNetwork) to rank tools by relevance to the
    current task when available.

    Returns a formatted string suitable for inclusion in an AI system prompt.
    """
    if not available_tools:
        return "No tools available."

    sections: List[str] = ["🛠️ AVAILABLE TOOLS:"]

    # ── Select relevant tools ────────────────────────────────────
    selected_names: List[str] = []

    if map_network and task_context:
        try:
            relevant = map_network.search_tools_hybrid(task_context, limit=max_tools)
            if relevant:
                selected_names = [n for n in relevant if n in available_tools]
        except Exception:
            pass

    if not selected_names:
        # Fallback: prioritize commonly useful tools
        priority = [
            "search_knowledge", "brain_network_search", "grokipedia_search",
            "write_file", "read_file", "list_directory",
            "quick_research", "quick_brainstorm",
            "mcp_fetch_fetch", "get_current_time", "store_thoughts",
        ]
        selected_names = [n for n in priority if n in available_tools]
        # Fill with remaining tools up to limit
        for name in available_tools:
            if name not in selected_names:
                selected_names.append(name)
            if len(selected_names) >= max_tools:
                break

    # ── Build descriptions ───────────────────────────────────────
    for name in selected_names[:max_tools]:
        func = available_tools[name]
        doc = (inspect.getdoc(func) or "No description")[:120]

        try:
            sig = inspect.signature(func)
            params = []
            for pname, param in sig.parameters.items():
                if pname in ("self", "cls", "brain", "brain_system", "kwargs"):
                    continue
                if param.default is inspect.Parameter.empty:
                    params.append(pname)
                else:
                    params.append(f"{pname}={param.default!r}")
            param_str = ", ".join(params)
        except (ValueError, TypeError):
            param_str = "..."

        sections.append(f"- {name}({param_str}): {doc}")

    # ── Append specialized tool sections ─────────────────────────
    swarm_section = get_swarm_tools_for_prompt(swarm_commander)
    if swarm_section:
        sections.append(swarm_section)

    council_section = get_council_tools_for_prompt()
    sections.append(council_section)

    mcp_section = get_mcp_tools_for_prompt(mcp_client)
    if mcp_section:
        sections.append(mcp_section)

    return "\n".join(sections)


# ── Task Hints ───────────────────────────────────────────────────────

def get_task_tool_examples(task_type: str) -> str:
    """Return additional tool guidance relevant to a chain's task type.

    For technical/coding tasks, reminds AI it can create code/files.
    ``grokipedia_search`` is always preferred for knowledge acquisition.
    Agent swarm tools are suggested for all research tasks.
    """
    task_type_lower = (task_type or "").lower()

    if task_type_lower in ("technical_development", "technical_innovation", "problem_solving"):
        return (
            "\nFor this technical task, you should also BUILD things.\n"
            "Use write_file, read_file for code. Use quick_research for multi-agent deep research.\n"
            "Use grokipedia_search for background. Create actual files with working code."
        )
    elif task_type_lower in ("research_analysis", "learning_education"):
        return (
            "\nUse quick_research with 3+ agents for deep multi-source research.\n"
            "Use grokipedia_search for in-depth educational articles.\n"
            "Use mcp_fetch_fetch to fetch web pages for current data.\n"
            "Save key findings with write_file."
        )
    return ""


def get_step_tool_hint(current_action: str, task_type: str = "") -> str:
    """Return a compact tool suggestion based on the action verb in the current step.

    Checks the FIRST WORD of the action for the primary verb to avoid
    false matches from secondary verbs in the description text.
    """
    first_word = current_action.split(":")[0].strip().split()[0].upper() if current_action else ""
    action_upper = (current_action or "").upper()

    if first_word in ("VERIFY", "READ", "REVIEW", "CHECK"):
        return "\nUse read_file to examine the relevant files."
    elif first_word in ("SEARCH", "EXPLORE", "RESEARCH"):
        return "\nUse quick_research or grokipedia_search to investigate."
    elif first_word in ("BUILD", "WRITE", "CREATE", "SAVE", "PRODUCE", "DOCUMENT"):
        return "\nUse write_file to create the output."
    elif first_word in ("ANALYZE", "DESIGN", "DEVELOP", "SYNTHESIZE", "CONCLUDE"):
        return "\nAnalyze thoroughly. Use quick_research, grokipedia_search, or brain_network_search if you need more data."
    elif first_word in ("DISCUSS", "BRAINSTORM", "DEBATE"):
        return "\nUse quick_brainstorm or start_discussion."
    elif first_word in ("CONSULT", "ADVISE", "DELIBERATE"):
        return "\nUse quick_research or quick_brainstorm for multi-perspective analysis."
    elif first_word in ("FETCH", "DOWNLOAD", "SCRAPE", "BROWSE"):
        return "\nUse mcp_fetch_fetch to retrieve the web content."
    elif "SEARCH" in action_upper:
        return "\nUse quick_research or grokipedia_search to investigate."
    elif any(verb in action_upper for verb in ("BUILD", "WRITE", "CREATE", "SAVE")):
        return "\nUse write_file to create the output."
    return "\nUse tools: quick_research, grokipedia_search, mcp_fetch_fetch, write_file, quick_brainstorm."


# ── Search Result Formatting ─────────────────────────────────────────

def format_google_search_insights(result: Dict[str, Any], query: str) -> str:
    """Format knowledge search results into readable insights for AI."""
    insights: List[str] = []

    insights.append(f"🔍 Knowledge Search Results for '{query}':\n")

    if "search_results" in result:
        results_list = result["search_results"]
        insights.append(f"Found {len(results_list)} results:\n")

        for i, res in enumerate(results_list[:5], 1):
            title = res.get("title", "Unknown")
            snippet = res.get("snippet", "")
            url = res.get("url", "")

            insights.append(f"{i}. **{title}**")
            if snippet:
                insights.append(f"   {snippet[:200]}...")
            insights.append(f"   URL: {url}\n")

    if "scraped_pages" in result:
        pages = result["scraped_pages"]
        if pages:
            insights.append(f"\n📖 Full Content Scraped from {len(pages)} pages:\n")

            for i, page in enumerate(pages, 1):
                title = page.get("title", "Unknown")
                content = page.get("content", "")
                headings = page.get("headings", [])

                insights.append(f"\n{i}. **{title}**")
                insights.append(f"   Content length: {len(content)} characters")

                if headings:
                    insights.append("   Key sections:")
                    for heading in headings[:5]:
                        level = heading.get("level", 1)
                        text = heading.get("text", "")
                        insights.append(f"   {'  ' * (level - 1)}• {text}")

                content_preview = content[:500] if len(content) > 500 else content
                insights.append(f"\n   {content_preview}...\n")

    if result.get("stored_in_brain", 0) > 0:
        insights.append(f"\n💾 Stored {result['stored_in_brain']} pages in brain knowledge base for future reference")

    return "\n".join(insights)


# ── Fallback Web Search ──────────────────────────────────────────────

def fallback_limited_web_search(
    query: str,
    knowledge_api_fn: Optional[Callable] = None,
) -> Optional[Dict[str, Any]]:
    """LIMITED fallback web search when Grokipedia has no information.

    Only used for current events or very specific queries.

    Parameters
    ----------
    query : str
        The search query.
    knowledge_api_fn : callable | None
        A ``call_knowledge_api_feeder(query)`` function to call.

    Returns
    -------
    dict | None
        Minimal search result or None.
    """
    if not knowledge_api_fn:
        return None

    try:
        logger.info(f"Performing limited web search for: '{query}'")
        web_result = knowledge_api_fn(query)

        if web_result and "results" in web_result and web_result["results"]:
            best = web_result["results"][0]
            return {
                "insights": f"📚 External Knowledge (Grokipedia unavailable): {best.get('title', 'Unknown')}\n   {best.get('content', '')[:300]}...",
                "source": "limited_web_fallback",
                "limited": True,
                "recommendation": "Consider expanding Grokipedia knowledge base for this topic",
            }

        return None

    except Exception as e:
        logger.warning(f"Fallback web search failed: {e}")
        return None


# ── Topic Generation via Tools ───────────────────────────────────────

def generate_topics_via_tools(
    brain_inspiration_fn: Optional[Callable] = None,
) -> List[str]:
    """Use AI reasoning and tools to generate novel research topics.

    AI should rely purely on self-prompting and memory recall tools
    — no predetermined topics or template combinations.
    """
    tool_topics: List[str] = []

    try:
        if brain_inspiration_fn:
            brain_inspiration_fn()
    except Exception as e:
        logger.warning(f"Error generating tool-based topics: {e}")

    return tool_topics
