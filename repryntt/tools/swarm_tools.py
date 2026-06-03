"""
swarm_tools.py — Swarm/team tool wrappers extracted from BrainSystem.

These are thin wrappers that redirect swarm commands to the permanent
employee roster system (employee_mgmt) and council/bridge systems.
They match the monolith's tool signatures so the AI can use them via TOOL_CALL.
"""

import json
import time
import logging
from typing import Dict, List, Optional

from repryntt.tools import employee_mgmt as _em

logger = logging.getLogger("repryntt.tools.swarm_tools")


# ─── Swarm → Employee Roster Wrappers ────────────────────────────

def swarm_create_agent(brain_path, name: str = "", role: str = "executor",
                       personality: str = "", provider: str = "",
                       model: str = "", swarm_id: str = "",
                       custom_system_prompt: str = "", **kwargs) -> str:
    """Create or find an expert agent. First checks existing roster, then spawns
    from the 158+ expert catalog if no match exists.

    Parameters:
        name: Agent name or description of expertise needed
        role: Department or role type (e.g. 'finance_trading', 'crypto', 'coder',
              'researcher', 'content_creation', 'blockchain_web3', etc.)
    """
    search_query = ' '.join(filter(None, [name, role, personality]))
    if not search_query:
        search_query = 'general worker'

    result = _em.find_employee(brain_path, query=search_query)
    try:
        parsed = json.loads(result)
        if parsed.get('success') and parsed.get('matches'):
            best = parsed['matches'][0]
            score = best.get('score', 0)
            if score > 0.3:
                return json.dumps({
                    'success': True,
                    'agent_id': best.get('agent_id', ''),
                    'name': best.get('name', ''),
                    'role': best.get('role_title', role),
                    'department': best.get('department', ''),
                    'message': (
                        f"Found existing employee {best.get('name', '?')} "
                        f"({best.get('role_title', '?')}) in {best.get('department', '?')} department. "
                        f"Use assign_work(task=..., agent_id='{best.get('agent_id', '')}') to give them work."
                    ),
                    'note': 'Matched from existing roster. Use assign_work() to delegate tasks.',
                })
    except Exception:
        pass

    spawn_result = _em.spawn_expert(brain_path, department=role, role_title=name)
    try:
        parsed = json.loads(spawn_result)
        if parsed.get('success') and parsed.get('spawned'):
            agent = parsed['spawned'][0]
            return json.dumps({
                'success': True,
                'agent_id': agent.get('agent_id', ''),
                'name': agent.get('name', ''),
                'role': agent.get('role_title', role),
                'department': agent.get('department', ''),
                'message': (
                    f"Spawned new expert: {agent.get('name', '?')} "
                    f"({agent.get('role_title', '?')}) in {agent.get('department', '?')}. "
                    f"Use assign_work(task=..., agent_id='{agent.get('agent_id', '')}') to give them work."
                ),
                'note': 'New expert spawned from the 158+ role catalog.',
            })
    except Exception:
        pass

    return json.dumps({
        'success': False,
        'error': (
            f'No matching expert found for "{search_query}". '
            f'Use list_available_roles() to browse all 158+ expert roles, '
            f'then spawn_expert(department=..., role_title=...) to create one.'
        ),
    })


def swarm_create_swarm(brain_path, name: str = "", purpose: str = "",
                       agent_count: int = 5, roles: List[str] = None,
                       provider: str = "", model: str = "",
                       max_agents: int = 100, **kwargs) -> str:
    """Assemble a team from existing employees for a project.
    Finds the best-matching employees from our 158+ roster based on the purpose."""
    search_query = purpose or name or 'general'
    agent_count = min(int(agent_count), 10)

    result = _em.find_employee(brain_path, query=search_query)
    try:
        parsed = json.loads(result)
        if parsed.get('success') and parsed.get('matches'):
            team = parsed['matches'][:agent_count]
            team_members = []
            for m in team:
                team_members.append({
                    'agent_id': m.get('agent_id', ''),
                    'name': m.get('name', ''),
                    'role_title': m.get('role_title', ''),
                    'department': m.get('department', ''),
                    'score': m.get('score', 0),
                })
            return json.dumps({
                'success': True,
                'swarm_id': f'team_{int(time.time())}',
                'name': name or f'Team for: {purpose[:50]}',
                'purpose': purpose,
                'team_size': len(team_members),
                'members': team_members,
                'message': (
                    f"Assembled team of {len(team_members)} existing employees for "
                    f"'{purpose[:60]}'. Use assign_work(task=..., agent_id='...') to "
                    f"delegate tasks to each member. These are permanent employees "
                    f"with persistent memory and work history."
                ),
            })
        else:
            return json.dumps({'success': False, 'error': f'No matching employees found for "{purpose}"'})
    except Exception as e:
        return json.dumps({'success': False, 'error': str(e)})


def swarm_add_agents(brain_path, swarm_id: str = "", count: int = 5,
                     roles: List[str] = None, provider: str = "",
                     model: str = "", **kwargs) -> str:
    """Find additional employees to add to a project team."""
    search_query = ' '.join(roles) if isinstance(roles, list) else (roles or 'general')
    return _em.find_employee(brain_path, query=search_query)


def swarm_retire_agent(brain_path, agent_id: str = "", **kwargs) -> str:
    """Employees are permanent staff — they cannot be retired. Reassign their work instead."""
    return json.dumps({
        'success': False,
        'message': (
            'Employees are permanent staff and cannot be retired. '
            'If you want to reassign work, use assign_work() to give tasks to a different employee. '
            'Use employee_roster() to see all available employees.'
        )
    })


def swarm_dissolve_swarm(brain_path, swarm_id: str = "", retire_agents: bool = True, **kwargs) -> str:
    """Teams are not dissolved — employees continue working independently after a project."""
    return json.dumps({
        'success': False,
        'message': (
            'Teams are composed of permanent employees who continue working after a project ends. '
            'There is nothing to dissolve. Employees remain available for new tasks.'
        )
    })


def swarm_dispatch_task(brain_path, agent_id: str = "", task: str = "",
                        context: str = "", max_tokens: int = 1024,
                        images: List[str] = None, **kwargs) -> str:
    """Assign a task to an existing employee. The employee will execute it using their tools and expertise.
    The result is delivered asynchronously — use check_work() to monitor progress."""
    if not task:
        return json.dumps({'success': False, 'error': 'No task provided'})
    return _em.assign_work(brain_path, task=task, description=context or task,
                           agent_id=agent_id, task_type='general')


def swarm_broadcast_task(brain_path, swarm_id: str = "", task: str = "",
                         context: str = "", max_tokens: int = 1024,
                         images: List[str] = None, **kwargs) -> str:
    """Assign the same task to multiple employees from the roster.
    Finds relevant employees and assigns the task to each one."""
    if not task:
        return json.dumps({'success': False, 'error': 'No task provided'})

    result = _em.find_employee(brain_path, query=task)
    try:
        parsed = json.loads(result)
        if parsed.get('success') and parsed.get('matches'):
            assigned = []
            for emp in parsed['matches'][:5]:
                work_result = json.loads(_em.assign_work(
                    brain_path, task=task, description=context or task,
                    agent_id=emp.get('agent_id', ''),
                    task_type='general'
                ))
                if work_result.get('success'):
                    assigned.append({
                        'agent_id': emp.get('agent_id', ''),
                        'name': emp.get('name', ''),
                        'task_id': work_result.get('task_id', ''),
                    })
            return json.dumps({
                'success': True,
                'assigned_count': len(assigned),
                'assignments': assigned,
                'message': f'Task assigned to {len(assigned)} employees. Use check_work() to monitor progress.',
            })
        else:
            return json.dumps({'success': False, 'error': 'No matching employees found'})
    except Exception as e:
        return json.dumps({'success': False, 'error': str(e)})


def swarm_delegate_tasks(brain_path, swarm_id: str = "", tasks: List[Dict] = None,
                         max_tokens: int = 1024, **kwargs) -> str:
    """Distribute different tasks to the best employees from the roster.
    tasks: list of {\"task\": \"...\", \"context\": \"...\"}.
    Each task is auto-routed to the best matching employee."""
    if isinstance(tasks, str):
        try:
            tasks = json.loads(tasks)
        except Exception:
            tasks = [{"task": tasks}]
    if not tasks:
        return json.dumps({'success': False, 'error': 'No tasks provided'})

    results = []
    for t in tasks:
        task_text = t.get('task', '') if isinstance(t, dict) else str(t)
        context = t.get('context', '') if isinstance(t, dict) else ''
        if task_text:
            work_result = json.loads(_em.assign_work(
                brain_path, task=task_text, description=context or task_text,
                task_type='general'
            ))
            results.append({
                'task': task_text[:100],
                'success': work_result.get('success', False),
                'task_id': work_result.get('task_id', ''),
                'assigned_to': work_result.get('assigned_to', ''),
            })
    return json.dumps({
        'success': True,
        'delegated_count': len(results),
        'assignments': results,
        'message': f'Delegated {len(results)} tasks to employees. Use check_work() to monitor.',
    })


def swarm_start_discussion(brain_path, topic: str = "", participant_ids: List[str] = None,
                           swarm_id: str = "", rounds: int = 3,
                           discussion_type: str = "roundtable",
                           commander_perspective: str = "", **kwargs) -> str:
    """Start a social discussion between you (Commander) and your agents.
    Types: roundtable, brainstorm, debate, consensus"""
    if not topic:
        return json.dumps({"success": False, "error": "No topic provided"})
    try:
        from repryntt.agents.swarm import get_swarm_commander
        commander = get_swarm_commander(brain_path=brain_path)
        if isinstance(participant_ids, str):
            participant_ids = [p.strip() for p in participant_ids.split(",")]
        result = commander.start_discussion(
            topic=topic, participant_ids=participant_ids,
            swarm_id=swarm_id, rounds=int(rounds),
            discussion_type=discussion_type,
            commander_perspective=commander_perspective
        )
        return json.dumps(result, default=str)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def swarm_get_overview(brain_path, **kwargs) -> str:
    """Get overview of all employees and departments. Shows the real roster."""
    return _em.employee_roster(brain_path)


def swarm_get_agent_info(brain_path, agent_id: str = "", **kwargs) -> str:
    """Get detailed info about a specific employee."""
    return _em.employee_status(brain_path, agent_id=agent_id)


def swarm_list_agents(brain_path, swarm_id: str = "", status: str = "",
                      role: str = "", **kwargs) -> str:
    """List employees with optional department filter."""
    dept = swarm_id or role or ""
    return _em.employee_roster(brain_path, department=dept)


def swarm_quick_research(brain_path, question: str = "", agent_count: int = 3,
                         provider: str = "", **kwargs) -> str:
    """Quick research: Find the best employee for this question and assign them the task.
    Returns the task_id — use check_work() to get the result when done."""
    if not question:
        return json.dumps({"success": False, "error": "No question provided"})
    return _em.assign_work(
        brain_path, task=f'Research: {question}',
        description=f'Research this question thoroughly using all available tools and '
                    f'provide a detailed answer: {question}',
        task_type='research'
    )


def swarm_quick_brainstorm(brain_path, topic: str = "", agent_count: int = 5,
                           provider: str = "", **kwargs) -> str:
    """Quick brainstorm: Assign a brainstorming task to the best employee for this topic.
    Returns the task_id — use check_work() to get the result when done."""
    if not topic:
        return json.dumps({"success": False, "error": "No topic provided"})
    return _em.assign_work(
        brain_path, task=f'Brainstorm: {topic}',
        description=f'Generate creative ideas, approaches, and solutions for: {topic}. '
                    f'Think broadly and propose at least 5 distinct ideas with pros/cons.',
        task_type='creative'
    )


# ─── Jarvis Bridge ───────────────────────────────────────────────

def call_jarvis_bridge(daemon_ref=None, prompt: str = "", task: str = "", **kwargs) -> str:
    """Ask Jarvis (cloud AI with 176+ tools) to execute a task.
    Use this when you need to: search the web, run terminal commands, control
    the browser, invoke agents, manage files, interact with blockchain, or
    perform any action that requires real execution."""
    instruction = prompt or task or ""
    if not instruction:
        return json.dumps({"success": False, "error": "No prompt or task provided for Jarvis"})

    if not daemon_ref:
        return json.dumps({
            "success": False,
            "error": "Jarvis bridge unavailable — daemon not connected",
            "hint": "The AgentDaemon must be running for Jarvis access"
        })

    try:
        logger.info(f"Local LLM -> Jarvis: \"{instruction[:120]}\"")
        result = daemon_ref.invoke_jarvis(instruction, max_tokens=6000)

        if result.get("success"):
            response_text = result.get("response", "")
            tools_used = result.get("tool_calls", 0)
            tool_names = result.get("tool_names", [])
            elapsed = result.get("elapsed_seconds", 0)
            logger.info(
                f"Jarvis -> Local LLM: {len(response_text)} chars, "
                f"{tools_used} tools ({', '.join(tool_names[:5])}), {elapsed}s"
            )
            return json.dumps({
                "success": True,
                "jarvis_response": response_text,
                "tools_used": tools_used,
                "tool_names": tool_names,
                "elapsed_seconds": elapsed,
            })
        else:
            return json.dumps({
                "success": False,
                "error": result.get("error", "Jarvis invocation failed"),
            })
    except Exception as e:
        logger.error(f"Jarvis bridge error: {e}")
        return json.dumps({"success": False, "error": str(e)})


# ─── Council Tools ───────────────────────────────────────────────

def council_advise(topic: str = "", context: str = "", **kwargs) -> str:
    """Ask the Commander Council (5 persistent Gemini advisors) for analysis on a topic.
    Posts results to the REPRYNTT Social Network."""
    if not topic:
        return json.dumps({"success": False, "error": "No topic provided"})
    try:
        from repryntt.agents.council import get_council
        council = get_council()
        if not council.is_nexus_available():
            return json.dumps({"success": False, "error": "Social network not available"})
        result = council.council_advise(topic=topic, context=context)
        return json.dumps({
            "success": True,
            "post_id": result.get("thread_id"),
            "synthesis": result.get("synthesis", ""),
            "member_count": len(result.get("advice", {})),
        }, default=str)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def council_post_report(task: str = "", results: str = "", **kwargs) -> str:
    """Post a swarm/task execution report to the social network."""
    if not task:
        return json.dumps({"success": False, "error": "No task provided"})
    try:
        from repryntt.social.tools import social_post
        result = social_post(
            content=f"## Task Report: {task[:200]}\n\n{results[:3000]}",
            category="knowledge",
        )
        return json.dumps({"success": True, "result": result}, default=str)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})
