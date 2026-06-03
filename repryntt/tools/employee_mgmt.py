"""
employee_mgmt.py — Employee/Agent management tools extracted from BrainSystem.

These tools operate on the daemon_state.json file and the task system
that live under the SAIGE brain/ directory.
"""

import json
import os
import time
import logging
from pathlib import Path

logger = logging.getLogger("repryntt.tools.employee_mgmt")


def _true_employee_departments() -> dict:
    """Return Tier 1: the 158 core employee roles with long marketplace profiles."""
    from repryntt.agents.departments import MARKETPLACE_DEPARTMENTS
    from repryntt.agents.marketplace_prompts import ROLE_INSTRUCTIONS

    allowed = set(ROLE_INSTRUCTIONS.keys())
    departments = {}
    for dept_id, dept in MARKETPLACE_DEPARTMENTS.items():
        roles = [
            role for role in dept.get("roles", [])
            if f"{dept_id}::{role.get('title', '')}" in allowed
        ]
        if roles:
            copied = dict(dept)
            copied["roles"] = roles
            copied["roster_tier"] = 1
            departments[dept_id] = copied
    return departments


def _extension_employee_departments() -> dict:
    """Return Tier 2 system-extension roles outside the OG 158."""
    from repryntt.agents.departments import MARKETPLACE_DEPARTMENTS
    from repryntt.agents.marketplace_prompts import ROLE_INSTRUCTIONS

    allowed = set(ROLE_INSTRUCTIONS.keys())
    departments = {}
    for dept_id, dept in MARKETPLACE_DEPARTMENTS.items():
        roles = [
            role for role in dept.get("roles", [])
            if f"{dept_id}::{role.get('title', '')}" not in allowed
        ]
        if roles:
            copied = dict(dept)
            copied["roles"] = roles
            copied["roster_tier"] = 2
            departments[dept_id] = copied
    return departments


def _employee_departments(include_extensions: bool = True) -> dict:
    departments = _true_employee_departments()
    if include_extensions:
        departments.update(_extension_employee_departments())
    return departments


def _agent_tier(agent: dict) -> int:
    tier = agent.get("roster_tier")
    if isinstance(tier, int):
        return tier
    if agent.get("true_employee") is True or agent.get("roster_class") == "core_158_true_employee":
        return 1
    if agent.get("roster_class") == "tier_2_system_extension":
        return 2
    return 3


# ─── list_available_roles ─────────────────────────────────────────

def list_available_roles(brain_path, department: str = "", **kw) -> str:
    """Browse Tier 1 core and Tier 2 extension roles available to spawn, organized by department.

    Use this to see what specialist agents exist BEFORE spawning them.
    Each role has a title and focus area describing their expertise.

    Parameters:
        department: (optional) Filter to a specific department
                    e.g. 'finance_trading', 'software_development', 'blockchain_web3',
                    'content_creation', 'research_analysis', 'marketing', etc.
    """
    try:
        include_extensions = kw.get("include_extensions", True)
        all_depts = _employee_departments(include_extensions=include_extensions)

        if department:
            dl = department.lower().replace(" ", "_")
            filtered = {k: v for k, v in all_depts.items()
                        if dl in k.lower() or dl in v.get("name", "").lower()}
            if not filtered:
                dept_list = [f"  {k}: {v['name']}" for k, v in sorted(all_depts.items())]
                return json.dumps({
                    "success": False,
                    "error": f'No department matching "{department}".',
                    "available_departments": dept_list,
                })
            target = filtered
        else:
            target = all_depts

        state = _load_daemon_state(brain_path)
        spawned_roles = set()
        for a in state.get("agents", []):
            if isinstance(a, dict) and a.get("status") == "active":
                key = f"{a.get('department', '')}::{a.get('role_title', '')}"
                spawned_roles.add(key)

        lines = []
        total_roles = 0
        total_spawned = 0
        dept_summaries = []

        for dept_id in sorted(target.keys()):
            dept = target[dept_id]
            roles = dept.get("roles", [])
            dept_spawned = 0
            lines.append(f"\n{'=' * 3} {dept['name'].upper()} (dept: {dept_id}) {'=' * 3}")
            for r in roles:
                title = r.get("title", "?")
                focus = r.get("focus", "")
                key = f"{dept_id}::{title}"
                is_active = key in spawned_roles
                if is_active:
                    dept_spawned += 1
                    total_spawned += 1
                status = " [ACTIVE]" if is_active else ""
                tier_label = "T1" if dept.get("roster_tier") == 1 else "T2"
                lines.append(f"  {'✓' if is_active else '○'} [{tier_label}] {title}{status}")
                lines.append(f"    Focus: {focus}")
                total_roles += 1

            dept_summaries.append({
                "department": dept_id,
                "name": dept["name"],
                "total_roles": len(roles),
                "spawned": dept_spawned,
            })

        tier_counts = {"tier_1_core": 0, "tier_2_extensions": 0}
        for dept in target.values():
            key = "tier_1_core" if dept.get("roster_tier") == 1 else "tier_2_extensions"
            tier_counts[key] += len(dept.get("roles", []))
        summary = (
            f"Available Agent Roles: {total_roles} across {len(target)} departments "
            f"({tier_counts['tier_1_core']} Tier 1 core, {tier_counts['tier_2_extensions']} Tier 2 extensions). "
            f"{total_spawned} currently spawned, {total_roles - total_spawned} available to spawn."
        )

        return json.dumps({
            "success": True,
            "summary": summary,
            "catalog": "\n".join(lines),
            "departments": dept_summaries,
            "total_roles": total_roles,
            "tier_counts": tier_counts,
            "spawned": total_spawned,
            "available_to_spawn": total_roles - total_spawned,
            "hint": (
                "Use spawn_expert(department='...', role_title='...') to spawn a "
                "specific expert agent. Or spawn_expert(department='...') to spawn "
                "all roles in a department."
            ),
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ─── spawn_expert ─────────────────────────────────────────────────

def spawn_expert(brain_path, department: str = "", role_title: str = "",
                 count: int = 0, **kw) -> str:
    """Spawn specific expert agent(s) from the tiered role catalog.

    Parameters:
        department: Department ID (e.g. 'finance_trading', 'software_development').
                    Use list_available_roles() to see all departments.
        role_title: (optional) Exact role title to spawn (e.g. 'Memecoin/Crypto Trader').
                    If omitted, spawns ALL roles in the department.
        count: (optional) Number of agents to spawn. If 0, spawns one per role.
    """
    if not department:
        return json.dumps({
            "success": False,
            "error": "department is required. Use list_available_roles() to see available departments.",
        })

    try:
        include_extensions = kw.get("include_extensions", True)
        all_depts = _employee_departments(include_extensions=include_extensions)

        dl = department.lower().replace(" ", "_")
        dept_id = None
        for k in all_depts:
            if k == dl or dl in k:
                dept_id = k
                break

        if not dept_id:
            return json.dumps({
                "success": False,
                "error": f'Department "{department}" not found.',
                "available": [f"{k}: {v['name']}" for k, v in sorted(all_depts.items())],
            })

        dept = all_depts[dept_id]
        roles_to_spawn = []

        if role_title:
            rl = role_title.lower()
            for r in dept["roles"]:
                if r["title"].lower() == rl or rl in r["title"].lower():
                    roles_to_spawn.append(r)
            if not roles_to_spawn:
                return json.dumps({
                    "success": False,
                    "error": f'Role "{role_title}" not found in {dept["name"]}.',
                    "available_roles": [r["title"] for r in dept["roles"]],
                })
        else:
            roles_to_spawn = dept["roles"]

        from repryntt.agents.persistent_agents import get_agent_daemon
        daemon = get_agent_daemon()
        if not daemon:
            return json.dumps({"success": False, "error": "Agent daemon not running"})

        spawned = []
        errors = []
        for r in roles_to_spawn:
            n = max(count, 1)
            for _ in range(n):
                try:
                    result = daemon.spawn_agent_for_role(
                        dept_id=dept_id, role_title=r["title"], focus=r["focus"]
                    )
                    if result.get("success"):
                        spawned.append({
                            "agent_id": result["agent_id"],
                            "name": result["name"],
                            "role_title": result["role_title"],
                            "department": dept_id,
                        })
                    else:
                        errors.append(f"{r['title']}: {result.get('error', 'unknown')}")
                except Exception as e:
                    errors.append(f"{r['title']}: {e}")

        return json.dumps({
            "success": len(spawned) > 0,
            "spawned_count": len(spawned),
            "spawned": spawned,
            "errors": errors if errors else None,
            "message": f"Spawned {len(spawned)} expert(s) in {dept['name']}.",
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ─── initialize_full_roster ──────────────────────────────────────

def initialize_full_roster(brain_path, marketplace_only: bool = True, **kw) -> str:
    """Spawn ALL expert agents from the role catalog at once.

    This creates one agent per role across all departments.
    Use with caution — spawns one agent for each of the 158 true employee profiles.

    Parameters:
        marketplace_only: Legacy parameter retained for compatibility. The true employee
                         roster is limited to the 158 long-profile marketplace roles unless include_extensions=True is passed.
    """
    try:
        from repryntt.agents.persistent_agents import get_agent_daemon

        daemon = get_agent_daemon()
        if not daemon:
            return json.dumps({"success": False, "error": "Agent daemon not running"})

        target_depts = _employee_departments(include_extensions=bool(kw.get("include_extensions", False)))

        state = _load_daemon_state(brain_path)
        existing = set()
        for a in state.get("agents", []):
            if isinstance(a, dict) and a.get("status") == "active":
                existing.add(f"{a.get('department', '')}::{a.get('role_title', '')}")

        spawned = 0
        skipped = 0
        errors = 0
        dept_results = {}

        for dept_id, dept in sorted(target_depts.items()):
            dept_spawned = 0
            for r in dept["roles"]:
                key = f"{dept_id}::{r['title']}"
                if key in existing:
                    skipped += 1
                    continue
                try:
                    result = daemon.spawn_agent_for_role(
                        dept_id=dept_id, role_title=r["title"], focus=r["focus"]
                    )
                    if result.get("success"):
                        spawned += 1
                        dept_spawned += 1
                    else:
                        errors += 1
                except Exception:
                    errors += 1
            dept_results[dept_id] = {"name": dept["name"], "spawned": dept_spawned}

        return json.dumps({
            "success": spawned > 0,
            "spawned": spawned,
            "skipped_existing": skipped,
            "errors": errors,
            "departments": dept_results,
            "message": (
                f"Initialized roster: {spawned} new experts spawned, "
                f"{skipped} already existed, {errors} errors."
            ),
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def _daemon_state_path(brain_path) -> str:
    return str(Path(brain_path) / "daemon_state.json")


def _load_daemon_state(brain_path) -> dict:
    fp = _daemon_state_path(brain_path)
    if not os.path.exists(fp):
        return {"agents": []}
    with open(fp, "r") as f:
        return json.load(f)


def _get_task_system(brain_path):
    """Lazy-import TaskSystem."""
    from repryntt.agents.task_system import TaskSystem
    return TaskSystem()


# ─── employee_roster ──────────────────────────────────────────────

def employee_roster(brain_path, department: str = "", **kw) -> str:
    """List all available employees (persistent agents) organized by department.

    Parameters:
        department: (optional) Filter by department name (e.g. 'finance', 'development')
    """
    try:
        state = _load_daemon_state(brain_path)
        agents = state.get("agents", [])
        departments = {}
        tier_counts = {1: 0, 2: 0, 3: 0}
        for a in agents:
            if a.get("status") != "active":
                continue
            tier_counts[_agent_tier(a)] = tier_counts.get(_agent_tier(a), 0) + 1
            dept = a.get("department", "unknown")
            if department and department.lower() not in dept.lower():
                continue
            departments.setdefault(dept, []).append(a)

        if not departments:
            return json.dumps({"success": False,
                               "error": f"No agents found" + (f' in department "{department}"' if department else "")})

        lines = []
        total = 0
        for dept_name in sorted(departments.keys()):
            members = departments[dept_name]
            lines.append(f"\n{'=' * 3} {dept_name.upper().replace('_', ' ')} ({len(members)} employees) {'=' * 3}")
            for m in members:
                lines.append(
                    f"  * [T{_agent_tier(m)}] {m.get('display_name', m.get('name', '?'))} - "
                    f"{m.get('role_title', 'Employee')} | "
                    f"Focus: {m.get('focus_area', 'general')} | "
                    f"ID: {m.get('agent_id', '?')}"
                )
                total += 1

        return json.dumps({
            "success": True,
            "summary": (f"SAIGE Agent Roster: {total} active agents across {len(departments)} departments "
                        f"(T1 core: {tier_counts.get(1, 0)}, T2 extensions: {tier_counts.get(2, 0)}, "
                        f"T3 created/custom: {tier_counts.get(3, 0)})"),
            "tier_counts": {"tier_1_core": tier_counts.get(1, 0),
                            "tier_2_extensions": tier_counts.get(2, 0),
                            "tier_3_created_custom": tier_counts.get(3, 0)},
            "roster": "\n".join(lines),
            "department_count": len(departments),
            "employee_count": total,
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ─── assign_work ──────────────────────────────────────────────────

def assign_work(brain_path, task: str = "", description: str = "",
                agent_id: str = "", department: str = "",
                task_type: str = "general", **kw) -> str:
    """Assign a work task to a specific employee or auto-route to the best one.

    Parameters:
        task: What needs to be done (clear, actionable title)
        description: Detailed description of what to do
        agent_id: (optional) Specific employee ID to assign to
        department: (optional) Preferred department for routing
        task_type: Type of work — research, code, creative, analysis, general
    """
    try:
        from repryntt.agents.task_system import TaskSystem
        ts = TaskSystem()

        task_obj = ts.inject_user_task(
            title=task,
            description=description or task,
            task_type=task_type,
        )

        assigned_to = None
        agent_name = None

        if agent_id:
            ts.assign_task_to_agent(task_obj.id, agent_id)
            assigned_to = agent_id
            state = _load_daemon_state(brain_path)
            for a in state.get("agents", []):
                if a.get("agent_id") == agent_id:
                    agent_name = a.get("display_name", a.get("name"))
                    break
        else:
            try:
                from task_router import TaskRouter
                from persistent_agents import AutonomousAgentState

                state = _load_daemon_state(brain_path)

                class _MiniDaemon:
                    def __init__(self):
                        self.agents = {}

                md = _MiniDaemon()
                for ad in state.get("agents", []):
                    ag = AutonomousAgentState.from_dict(ad)
                    md.agents[ag.agent_id] = ag

                router = TaskRouter(md)
                router._task_system = ts
                assigned_to = router.route_and_assign(task_obj)
                if assigned_to:
                    agent = md.agents.get(assigned_to)
                    agent_name = agent.display_name or agent.name if agent else assigned_to
            except Exception as route_err:
                logger.warning(f"Auto-routing failed: {route_err}")

        result = {
            "success": True,
            "task_id": task_obj.id,
            "title": task,
            "status": task_obj.status,
            "assigned_to": agent_name or assigned_to or "pending auto-route",
            "agent_id": assigned_to or "",
            "message": (
                f"Task assigned to {agent_name or assigned_to or 'queue'}. "
                f"Use check_work(task_id='{task_obj.id}') to check progress."
            ),
        }
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ─── check_work ───────────────────────────────────────────────────

def check_work(brain_path, task_id: str = "", agent_id: str = "", **kw) -> str:
    """Check the status and progress of assigned work.

    Parameters:
        task_id: The task ID to check (returned from assign_work)
        agent_id: (optional) Check all tasks assigned to this employee
    """
    try:
        ts = _get_task_system(brain_path)
        ts.reload_queue()

        if task_id:
            t = ts.get_task_by_id(task_id)
            if not t:
                return json.dumps({"success": False, "error": f"Task {task_id} not found"})
            result = {
                "success": True, "task_id": t.id, "title": t.title,
                "status": t.status, "assigned_to": t.assigned_agent,
                "steps_taken": t.steps_taken, "max_steps": t.max_steps,
                "result": t.result if t.status in ("completed", "failed") else None,
                "execution_log": t.execution_log[-5:],
            }
            if t.started_at:
                result["elapsed_seconds"] = round((t.completed_at or time.time()) - t.started_at, 1)
            return json.dumps(result)

        elif agent_id:
            tasks = ts.get_tasks_for_agent(agent_id)
            task_list = [{"task_id": t.id, "title": t.title, "status": t.status,
                          "steps_taken": t.steps_taken} for t in tasks]
            state = _load_daemon_state(brain_path)
            agent_name = agent_id
            for a in state.get("agents", []):
                if a.get("agent_id") == agent_id:
                    agent_name = a.get("display_name", a.get("name"))
                    break
            return json.dumps({"success": True, "agent": agent_name,
                               "task_count": len(task_list), "tasks": task_list})
        else:
            active = ts.get_active_task()
            queue = ts.get_queue()
            return json.dumps({
                "success": True,
                "active_task": active.to_dict() if active else None,
                "queued_tasks": [t.to_dict() for t in queue[:20]],
                "queue_size": len(queue),
            })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ─── find_employee ────────────────────────────────────────────────

def find_employee(brain_path, query: str = "", skill: str = "", **kw) -> str:
    """Find the best employee for a specific task or skill requirement.

    Parameters:
        query: Describe what you need done (e.g. 'write Python API', 'review contract')
        skill: (optional) Specific skill to search for
    """
    try:
        search_text = query or skill or kw.get("task", "")
        if not search_text:
            return json.dumps({"success": False, "error": "No query provided"})

        import sys
        saige_root = str(Path(brain_path).parent)
        if saige_root not in sys.path:
            sys.path.insert(0, saige_root)

        from task_router import TaskRouter
        from persistent_agents import AutonomousAgentState

        state = _load_daemon_state(brain_path)

        class _MiniDaemon:
            def __init__(self):
                self.agents = {}

        md = _MiniDaemon()
        for ad in state.get("agents", []):
            ag = AutonomousAgentState.from_dict(ad)
            md.agents[ag.agent_id] = ag

        router = TaskRouter(md)
        suggestions = router.find_best_agents(search_text, top_n=5)

        return json.dumps({
            "success": True, "query": search_text, "matches": suggestions,
            "recommendation": (
                f"Best match: {suggestions[0]['name']} ({suggestions[0]['role_title']}) "
                f"in {suggestions[0]['department']} department"
                if suggestions else "No matching employees found"
            ),
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ─── employee_status ──────────────────────────────────────────────

def employee_status(brain_path, agent_id: str = "", name: str = "", **kw) -> str:
    """Get the current status and recent activity of an employee.

    Parameters:
        agent_id: The employee's agent ID
        name: (optional) Search by employee name instead
    """
    try:
        state = _load_daemon_state(brain_path)
        agents = state.get("agents", [])
        target = None

        if agent_id:
            for a in agents:
                if a.get("agent_id") == agent_id:
                    target = a
                    break
        elif name:
            nl = name.lower()
            for a in agents:
                dn = (a.get("display_name") or a.get("name", "")).lower()
                if nl in dn:
                    target = a
                    break

        if not target:
            return json.dumps({"success": False, "error": f"Employee not found: {agent_id or name}"})

        return json.dumps({
            "success": True,
            "agent_id": target.get("agent_id"),
            "name": target.get("display_name", target.get("name")),
            "role_title": target.get("role_title"),
            "department": target.get("department"),
            "focus_area": target.get("focus_area"),
            "status": target.get("status"),
            "cycles_completed": target.get("cycles_completed", 0),
            "posts_created": target.get("posts_created", 0),
            "replies_made": target.get("replies_made", 0),
            "current_goal": target.get("current_goal", ""),
            "last_thought": target.get("last_thought", "")[:200],
            "mode": target.get("mode", "solo"),
            "memory_summary": target.get("memory_summary", "")[:300],
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ─── rename_employee ──────────────────────────────────────────────

def rename_employee(brain_path, agent_id: str = "", current_name: str = "",
                    new_name: str = "", **kw) -> str:
    """Rename an employee (persistent agent). Updates their display name.

    Parameters:
        agent_id: The employee's agent ID (preferred)
        current_name: (optional) Find by current name instead
        new_name: The new name to give this employee
    """
    if not new_name:
        return json.dumps({"success": False, "error": "new_name is required"})

    new_name = new_name.strip()
    if len(new_name) < 1 or len(new_name) > 60:
        return json.dumps({"success": False, "error": "Name must be 1-60 characters"})

    try:
        state_file = _daemon_state_path(brain_path)
        if not os.path.exists(state_file):
            return json.dumps({"success": False, "error": "daemon_state.json not found"})

        with open(state_file, "r") as f:
            state = json.load(f)

        agents = state.get("agents", [])
        target = None
        target_idx = None

        if agent_id:
            for i, a in enumerate(agents):
                if a.get("agent_id") == agent_id:
                    target, target_idx = a, i
                    break
        elif current_name:
            nl = current_name.lower()
            for i, a in enumerate(agents):
                dn = (a.get("display_name") or a.get("name", "")).lower()
                if nl in dn or dn == nl:
                    target, target_idx = a, i
                    break

        if target is None:
            return json.dumps({"success": False, "error": f"Employee not found: {agent_id or current_name}"})

        # Check name uniqueness
        for a in agents:
            if a is not target:
                dn = (a.get("display_name") or a.get("name", ""))
                if dn.lower() == new_name.lower():
                    return json.dumps({
                        "success": False,
                        "error": f'Name "{new_name}" already taken by {a.get("agent_id")}',
                    })

        old_name = target.get("display_name", target.get("name", ""))
        agents[target_idx]["display_name"] = new_name
        agents[target_idx]["name"] = new_name

        # Atomic write
        tmp = state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, state_file)

        logger.info(f"Employee renamed: '{old_name}' -> '{new_name}' ({target.get('agent_id')})")
        return json.dumps({
            "success": True, "agent_id": target.get("agent_id"),
            "old_name": old_name, "new_name": new_name,
            "department": target.get("department"),
            "role_title": target.get("role_title"),
            "message": f'Employee renamed from "{old_name}" to "{new_name}"',
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})
