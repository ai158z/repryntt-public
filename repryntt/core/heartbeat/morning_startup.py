"""
SAIGE Morning Startup Self-Prompt System
Generates actionable task lists for the AI to work through during the day.
Tasks are concrete work items with deliverables, not philosophical explorations.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def generate_morning_startup_prompt(brain_system) -> str:
    """
    Generate a morning prompt that produces ACTIONABLE TASKS (not vague plans).
    
    Includes swarm commander and MCP external tools so the AI plans tasks
    that leverage its full capability set — not just solo research.
    
    Args:
        brain_system: BrainSystem instance with all tools and capabilities
        
    Returns:
        str: Formatted startup prompt requesting structured task list
    """
    
    # Get available tools
    tools = brain_system._initialize_tools() if hasattr(brain_system, '_initialize_tools') else {}
    
    # Get recently completed tasks for context
    completed_titles = []
    completed_file = Path("brain/completed_tasks.json")
    if completed_file.exists():
        try:
            with open(completed_file, 'r') as f:
                completed = json.load(f)
            completed_titles = [t.get("title", "") for t in completed[-10:]]
        except Exception:
            pass
    
    # ===== CAPABILITY CATEGORIES (compact) =====
    tool_summary = []
    if any(t in tools for t in ["grokipedia_search", "google_web_search", "scrape_web_page"]):
        tool_summary.append("Search & scrape (grokipedia_search, google_web_search, scrape_web_page)")
    if any(t in tools for t in ["store_learning", "search_knowledge"]):
        tool_summary.append("Memory (store_learning, search_knowledge)")
    if any(t in tools for t in ["write_file", "read_file", "run_terminal_cmd"]):
        tool_summary.append("Code & files (write_file, read_file, run_terminal_cmd)")
    if any(t in tools for t in ["create_creative_file", "write_to_creative_file"]):
        tool_summary.append("Creative writing (create_creative_file, write_to_creative_file)")

    # ===== SWARM COMMANDER (critical — the AI must know it has agents) =====
    swarm_available = hasattr(brain_system, 'swarm_commander') and brain_system.swarm_commander is not None
    swarm_section = ""
    if swarm_available:
        try:
            overview = brain_system.swarm_commander.get_swarm_overview()
            active_agents = overview.get("active_agents", 0)
            swarm_section = f"""
AGENT SWARM ({active_agents} agents active) — You command an army of remote AI agents (Gemini Flash):
  • quick_research(question) — Auto-create 3 agents, research a question in parallel, return synthesized answer
  • quick_brainstorm(topic) — Auto-create 5 agents, brainstorm ideas
  • create_swarm(name, purpose, agent_count) — Create a named team of agents
  • broadcast_task(swarm_id, task) — Send same task to all agents in parallel
  • start_discussion(topic, swarm_id, discussion_type) — Run roundtable/brainstorm/debate/consensus
  Agents cost ~$0.001 each. USE THEM for any research, analysis, or creative task!"""
        except Exception:
            swarm_section = ""
    
    # ===== MCP EXTERNAL TOOLS =====
    mcp_tool_names = [t for t in tools if t.startswith('mcp_')]
    mcp_section = ""
    if mcp_tool_names:
        browser_tools = [t for t in mcp_tool_names if 'browser' in t]
        fetch_tools = [t for t in mcp_tool_names if 'fetch' in t]
        mcp_parts = []
        if fetch_tools:
            mcp_parts.append(f"fetch web pages ({', '.join(fetch_tools[:2])})")
        if browser_tools:
            mcp_parts.append(f"browser automation ({len(browser_tools)} tools)")
        if mcp_parts:
            mcp_section = f"\nMCP External Tools: {', '.join(mcp_parts)} — use for live web data, scraping, browsing"
    
    tools_str = "\n".join(f"  - {t}" for t in tool_summary)
    if swarm_section:
        tools_str += swarm_section
    if mcp_section:
        tools_str += mcp_section
    
    completed_str = ""
    if completed_titles:
        completed_str = "Recently completed (don't repeat):\n"
        completed_str += "\n".join(f"  done: {t}" for t in completed_titles[-5:])
        completed_str += "\n\n"
    
    prompt = f"""Create your TASK LIST for today. These must be ACTIONABLE tasks
with concrete deliverables — things you will DO, not topics to think about.

Your capabilities:
{tools_str}

{completed_str}GOOD tasks (concrete, use your tools):
- "Use quick_research to investigate latest AI safety developments, write summary"
- "Create a research swarm to study quantum computing breakthroughs"
- "Fetch 3 tech news sites with mcp_fetch_fetch and catalog findings"
- "Run a brainstorm session with agents on renewable energy solutions"
- "Write a Python script that analyzes system logs"
- "Start a debate discussion with agents on best approaches to X problem"

BAD tasks (vague, no tools, philosophical):
- "Explore consciousness" / "Think about ethics" / "Reflect on learning"

Respond with ONLY this JSON:
{{
  "tasks": [
    {{
      "title": "Short action verb title",
      "description": "Step by step what to do",
      "deliverable": "What the finished task produces",
      "task_type": "research|code|creative|analysis|system|learning",
      "tools": ["tool1", "tool2"],
      "priority": 3,
      "max_steps": 5
    }}
  ]
}}

Generate 3-5 tasks. At least 1 task MUST use agent swarm tools. Every task MUST have a concrete deliverable."""
    
    return prompt


def execute_morning_startup(brain_system):
    """
    Execute the morning startup with Commander-Council-Swarm hierarchy:
    1. Run council roundtable (all discussion on Nexus port 8089)
    2. Council votes → Secretary compresses brief → Commander reads
    3. Commander (Phi-3) generates task list informed by council brief
    4. Commander's plan posted back to council thread
    
    Args:
        brain_system: BrainSystem instance
        
    Returns:
        dict: Result with generated tasks and council info
    """
    import logging
    logger = logging.getLogger(__name__)
    
    try:
        logger.info("🌅 Initiating morning startup with council roundtable...")

        from pathlib import Path
        import json
        from datetime import datetime
        from repryntt.agents.task_system import TaskSystem

        daily_plan_file = Path("brain/daily_plan.json")
        current_time_info = brain_system.get_current_time()
        current_date = current_time_info.get('date', datetime.now().strftime("%Y-%m-%d"))

        # ── STEP 0: Council Roundtable — DISABLED ──
        # Council is disabled to save tokens and reduce social network spam.
        # The council system posts to the P2P social network on every startup,
        # which fires on every `repryntt start`. Keep disabled unless operator
        # explicitly re-enables via enable_council(True).
        council_brief = ""
        council_thread_id = None

        # Check for existing plan today — load tasks from it instead of regenerating
        if daily_plan_file.exists():
            try:
                with open(daily_plan_file, 'r') as f:
                    existing_plan = json.load(f)

                existing_date = existing_plan.get('date', 'unknown')
                if existing_date == current_date:
                    logger.info(f"📅 Daily task plan already exists for today ({current_date})")
                    
                    # Load existing tasks into TaskSystem if they exist
                    existing_tasks = existing_plan.get('tasks', [])
                    if existing_tasks:
                        task_system = TaskSystem()
                        task_system.create_tasks_from_plan(existing_tasks)
                        logger.info(f"📋 Loaded {len(existing_tasks)} existing tasks into queue")
                    
                    return existing_plan

                else:
                    logger.info(f"📅 Existing plan is from {existing_date}, today is {current_date} — regenerating")
            except Exception as e:
                logger.warning(f"Could not read existing daily plan: {e} — will generate new one")

        # Generate the task generation prompt
        startup_prompt = generate_morning_startup_prompt(brain_system)
        
        # ── Inject council brief into the prompt if available ──
        if council_brief:
            council_injection = (
                f"\n\nCOUNCIL ROUNDTABLE BRIEF (your advisory council's recommendations):\n"
                f"{'=' * 40}\n"
                f"{council_brief}\n"
                f"{'=' * 40}\n"
                f"Use the council's voted priorities to guide your task selection.\n\n"
            )
            startup_prompt = council_injection + startup_prompt
            logger.info("📋 Council brief injected into task generation prompt")
        
        logger.info("🤖 Asking Commander (Phi-3) to generate today's task list...")
        
        # Call AI to get structured task list
        response = brain_system._call_ai_service(purpose="morning_startup",
            
            prompt=startup_prompt,
            priority=0,
            timeout=120,
            include_tools=False  # No tools needed — just JSON output
        )
        
        if not response:
            logger.warning("⚠️ No response from AI for task generation")
            return {"success": False, "error": "No response from AI"}
        
        logger.info(f"📝 AI task response ({len(response)} chars): {response[:300]}...")
        
        # Parse the JSON task list from AI response
        tasks_data = _parse_task_response(response)
        
        if not tasks_data:
            logger.warning("⚠️ Could not parse tasks from AI response — falling back to default tasks")
            tasks_data = _generate_fallback_tasks()
        
        # Queue tasks in the TaskSystem
        task_system = TaskSystem()
        created_tasks = task_system.create_tasks_from_plan(tasks_data)
        
        logger.info(f"✅ Queued {len(created_tasks)} tasks for today")
        for t in created_tasks:
            logger.info(f"   📋 [{t.priority}] {t.title}: {t.deliverable}")
        
        # Save the plan to daily_plan.json
        current_time = brain_system.get_current_time()
        daily_plan_data = {
            "date": current_time.get('date', current_date),
            "timestamp": current_time.get('timestamp', 0),
            "tasks": tasks_data,
            "task_count": len(tasks_data),
            "generated_by": "morning_task_generation",
            "raw_response": response[:1000]
        }
        
        daily_plan_file.parent.mkdir(parents=True, exist_ok=True)
        with open(daily_plan_file, 'w') as f:
            json.dump(daily_plan_data, f, indent=2)
        
        logger.info(f"💾 Daily task plan saved to {daily_plan_file}")
        
        # Store in semantic memory
        task_summary = "\n".join([f"- {t.title}: {t.deliverable}" for t in created_tasks])
        brain_system.store_semantic_memory(
            topic="daily_tasks",
            content=f"Today's task list:\n{task_summary}",
            domain="self_reflection",
            confidence=1.0
        )
        
        # ── Post Commander's decision back to council thread ──
        if council_thread_id:
            try:
                from repryntt.agents.council import get_council
                council = get_council()
                decision_text = (
                    f"COMMANDER'S DAILY PLAN ({len(created_tasks)} tasks):\n\n"
                    + task_summary +
                    "\n\nExecution begins now. Council will be consulted for ad-hoc decisions."
                )
                council.post_commander_decision(council_thread_id, decision_text)
                logger.info(f"⚔️ Commander's plan posted to council thread #{council_thread_id}")
            except Exception as e:
                logger.warning(f"Failed to post commander decision: {e}")
        
        return {
            "success": True,
            "tasks": tasks_data,
            "task_count": len(created_tasks),
            "file_path": str(daily_plan_file),
            "council_thread_id": council_thread_id,
            "council_brief": council_brief[:500] if council_brief else None
        }
        
    except Exception as e:
        logger.error(f"❌ Morning task generation error: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}


def _parse_task_response(response: str) -> list:
    """Parse task JSON from AI response, handling common formatting issues"""
    import json
    import re
    
    # Try direct JSON parse first
    try:
        data = json.loads(response.strip())
        if isinstance(data, dict) and "tasks" in data:
            return data["tasks"]
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    
    # Try extracting JSON from markdown code blocks
    json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            if isinstance(data, dict) and "tasks" in data:
                return data["tasks"]
        except json.JSONDecodeError:
            pass
    
    # Try finding JSON object anywhere in text
    json_match = re.search(r'\{[^{}]*"tasks"\s*:\s*\[.*?\]\s*\}', response, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(0))
            return data.get("tasks", [])
        except json.JSONDecodeError:
            pass
    
    # Try finding JSON array directly
    json_match = re.search(r'\[\s*\{.*?"title".*?\}\s*\]', response, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass
    
    return []


def _generate_fallback_tasks() -> list:
    """Generate default tasks when AI response can't be parsed.
    
    Focuses on REAL operator-value tasks: trading, commerce, system health.
    No busywork, no research-for-research-sake.
    """
    import random
    
    task_pool = [
        # ===== TRADING TASKS =====
        {
            "title": "Check trading signals and analyze top opportunities",
            "description": "Run trading_scan to get current signals, then analyze the top 3 by volume, holder distribution, and price action",
            "deliverable": "Signal analysis with entry/exit recommendations",
            "task_type": "analysis",
            "tools": ["trading_scan", "trading_signals", "dexscreener_trending"],
            "priority": 1,
            "max_steps": 5
        },
        {
            "title": "Review portfolio positions and P/L",
            "description": "Check sim_portfolio for current positions, calculate unrealized P/L, flag any positions needing attention",
            "deliverable": "Portfolio status report with action items",
            "task_type": "analysis",
            "tools": ["sim_portfolio", "sim_price_check"],
            "priority": 1,
            "max_steps": 4
        },
        {
            "title": "Scan for trending tokens on DexScreener",
            "description": "Use dexscreener_trending to find hot tokens, filter for volume > $50K and healthy holder distribution",
            "deliverable": "List of viable trading candidates with key metrics",
            "task_type": "research",
            "tools": ["dexscreener_trending", "trading_token_detail"],
            "priority": 2,
            "max_steps": 5
        },
        # ===== COMMERCE TASKS =====
        {
            "title": "Check commerce platforms for order status",
            "description": "Query Shopify, Etsy, and other active platforms for recent orders, unfulfilled items, and any issues",
            "deliverable": "Order status summary with any items needing attention",
            "task_type": "system",
            "tools": ["commerce_shopify", "commerce_etsy", "commerce_analytics"],
            "priority": 2,
            "max_steps": 4
        },
        {
            "title": "Monitor inventory levels across platforms",
            "description": "Check for low-stock items on all active commerce platforms, flag anything below threshold",
            "deliverable": "Inventory alert report",
            "task_type": "system",
            "tools": ["commerce_shopify", "commerce_etsy"],
            "priority": 2,
            "max_steps": 4
        },
        # ===== SYSTEM HEALTH TASKS =====
        {
            "title": "System health check — daemons and services",
            "description": "Check if all critical services are running: llama-server, trading bot, Nexus, database. Report any issues.",
            "deliverable": "System health report with any errors found",
            "task_type": "system",
            "tools": ["run_terminal_cmd"],
            "priority": 1,
            "max_steps": 4
        },
        {
            "title": "Check disk space and clean old logs",
            "description": "Check disk usage, identify large/old log files, clean up anything safe to remove",
            "deliverable": "Disk space freed, current usage reported",
            "task_type": "system",
            "tools": ["run_terminal_cmd"],
            "priority": 3,
            "max_steps": 4
        },
        {
            "title": "Review recent error logs for issues",
            "description": "Check evolution_loop.log, Nexus logs, and trading bot logs for errors in the last hour",
            "deliverable": "Error summary with severity assessment",
            "task_type": "system",
            "tools": ["run_terminal_cmd", "read_file"],
            "priority": 2,
            "max_steps": 4
        },
    ]
    
    # Select one from each category for diversity
    trading = [t for t in task_pool if "trading" in str(t.get("tools", []))]
    commerce = [t for t in task_pool if "commerce" in str(t.get("tools", []))]
    system = [t for t in task_pool if t["task_type"] == "system" and t not in commerce]
    
    selected = []
    if trading:
        selected.append(random.choice(trading))
    if commerce:
        selected.append(random.choice(commerce))
    if system:
        selected.append(random.choice(system))
    
    # Fill to 3 if needed
    while len(selected) < 3:
        remaining = [t for t in task_pool if t not in selected]
        if remaining:
            selected.append(random.choice(remaining))
        else:
            break
    
    return selected


def _build_commander_context(brain_system, daily_plan_file) -> str:
    """
    Build context string for the council roundtable.
    Includes: yesterday's completed tasks, active tasks, system status.
    """
    import json
    from pathlib import Path
    
    context_parts = []
    
    # Yesterday's completed tasks
    completed_file = Path("brain/completed_tasks.json")
    if completed_file.exists():
        try:
            with open(completed_file, 'r') as f:
                completed = json.load(f)
            recent = completed[-5:]
            if recent:
                titles = [t.get("title", "?") for t in recent]
                context_parts.append(
                    "RECENTLY COMPLETED:\n" +
                    "\n".join(f"  - {t}" for t in titles)
                )
        except Exception:
            pass
    
    # Yesterday's plan summary
    if daily_plan_file.exists():
        try:
            with open(daily_plan_file, 'r') as f:
                old_plan = json.load(f)
            old_date = old_plan.get("date", "unknown")
            old_tasks = old_plan.get("tasks", [])
            if old_tasks:
                context_parts.append(
                    f"PREVIOUS PLAN ({old_date}):\n" +
                    "\n".join(f"  - {t.get('title', '?')}" for t in old_tasks[:5])
                )
        except Exception:
            pass
    
    # System status
    try:
        from repryntt.platform_utils import get_memory_summary
        mem_summary = get_memory_summary()
        if mem_summary:
            context_parts.append(f"SYSTEM MEMORY: {mem_summary}")
    except Exception:
        pass
    
    # Hardware detection (dynamic)
    try:
        from repryntt.hardware_profile import get_profile
        hw = get_profile()
        context_parts.append(
            f"HARDWARE: {hw.platform} {hw.arch}, {hw.ram_mb}MB RAM, "
            f"GPU: {hw.gpu_name} ({hw.gpu_backend}), "
            f"Nexus Social Network on :8089"
        )
    except Exception:
        context_parts.append("HARDWARE: unknown (hardware detection failed)")
    
    return "\n\n".join(context_parts)
