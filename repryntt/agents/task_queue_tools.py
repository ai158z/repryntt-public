"""
Agent-facing tools for the unified task queue.
These are registered in registry.py and available to Andrew during heartbeats.
"""

import json
import logging

logger = logging.getLogger("repryntt.task_queue_tools")

# Singleton reference — set by persistent_agents.py when queue is loaded
_active_queue = None


def set_active_queue(queue):
    """Called by persistent_agents.py to set the queue singleton."""
    global _active_queue
    _active_queue = queue


def get_active_queue():
    return _active_queue


def task_queue_status(**kw) -> str:
    """View your current task queue: what you're working on, what's next, and what's done today.
    Call this to see your work schedule and plan accordingly.
    """
    q = _active_queue
    if not q:
        return "Task queue not loaded."
    stats = q.get_stats()
    lines = [
        f"📋 Task Queue — {stats['day']}",
        f"  Completed: {stats['completed']} | In Progress: {stats['in_progress']} | "
        f"Queued: {stats['queued']} | Failed: {stats['failed']} | Skipped: {stats['skipped']}",
        "",
    ]
    current = stats.get("current_task")
    if current:
        lines.append(f"🔵 CURRENT: [{current['id']}] {current['title']}")
        if current.get("description"):
            lines.append(f"   {current['description'][:300]}")
        lines.append(f"   Priority: {current['priority']} | Source: {current.get('source', '?')}")
        lines.append(f"   Started: {current.get('started_at', '?')}")
    else:
        lines.append("No task in progress.")

    lines.append("")
    all_tasks = q.get_all_tasks()
    queued = [t for t in all_tasks if t["status"] == "queued"]
    if queued:
        lines.append(f"📝 UPCOMING ({len(queued)}):")
        for t in queued[:8]:
            lines.append(f"  [{t['id']}] {t['title'][:80]} (p{t['priority']}, {t.get('source', '?')})")
    completed = [t for t in all_tasks if t["status"] == "completed"]
    if completed:
        lines.append(f"\n✅ DONE TODAY ({len(completed)}):")
        for t in completed[-5:]:
            lines.append(f"  [{t['id']}] {t['title'][:60]} → {(t.get('summary') or '')[:60]}")
    return "\n".join(lines)


def add_task(title: str = "", description: str = "", priority: int = 3,
             expected_artifact_type: str = "",
             expected_location: str = "",
             downstream_consumer: str = "",
             success_criterion: str = "",
             **kw) -> str:
    """Add a new task to your work queue. A task is a contract: you declare what
    you will produce, where it lands, who it's for, and what success looks like —
    before you start working. The completion-time critic gate uses these fields
    to route adversarial review.

    Parameters:
        title: Short task title (required). E.g. "Write Q2 competitor pricing brief"
        description: Detailed description of what to do (optional but encouraged).
        priority: 0=operator, 1=urgent, 2=daily_plan, 3=autonomous, 5=low (default: 3)
        expected_artifact_type: One of: code, smart_contract, research_md, analysis_md,
            plan_md, design_md, legal_md, financial_model, tokenomics, patent_claim,
            curriculum_md, marketing_copy, report, data_extract, robotics_doc, hr_doc,
            real_estate_analysis.
        expected_location: Operator-visible path where the deliverable lands. Use
            workspace/agents/operator/{code,analysis,research,plans,reports,data}/.
            Never use skills/, RECALL_archive/, or agent_workspaces/jarvis/research/.
        downstream_consumer: An external role: operator, customer, developer,
            auditor, regulator, etc. Never 'self' or 'andrew' — those get rejected.
        success_criterion: One measurable sentence. "high quality" is not measurable.
            "code runs and prints expected output", "cites >=3 distinct primary
            sources", "comparison covers 5 competitors with sourced prices" are.

    Tasks that omit the typed fields are still admitted (legacy compatibility) but
    they bypass the completion-time critic gate, so the deliverable is never
    independently audited. Provide the fields whenever you can.
    """
    q = _active_queue
    if not q:
        return "Task queue not loaded."
    if not title:
        return "Error: title is required."
    task = q.add_task(
        title=title,
        description=description or "",
        priority=int(priority),
        source="autonomous",
        expected_artifact_type=expected_artifact_type,
        expected_location=expected_location,
        downstream_consumer=downstream_consumer,
        success_criterion=success_criterion,
    )
    if task.get("status") == "rejected":
        reasons = task.get("reasons") or []
        return (
            f"❌ Task rejected at intake: {task['title']}\n"
            "Reasons:\n  - " + "\n  - ".join(reasons[:5]) +
            "\nRewrite the title/fields and try again. The operator may have "
            "configured a vocabulary blocklist in `~/.repryntt/brain/intake_blocklist.json` "
            "that matches your title or description; "
            "self-referential consumers and Andrew-internal locations."
        )
    typed = bool(expected_artifact_type and expected_location and downstream_consumer and success_criterion)
    tag = "typed (gated at completion)" if typed else "untyped (no completion gate)"
    return f"✅ Task added: [{task['id']}] {task['title']} (priority {task['priority']}, {tag})"


def retype_task(task_id: str = "",
                expected_artifact_type: str = "",
                expected_location: str = "",
                downstream_consumer: str = "",
                success_criterion: str = "",
                **kw) -> str:
    """Add typed deliverable fields to an existing task that was queued without
    them. Use this when:
      - The operator gave you a task via chat/voice and didn't specify the shape
      - You added an exploratory task and now know what you'll actually produce
      - You're about to start a task and want completion-time critic review

    Parameters:
        task_id: The task id (e.g. 't_3'). Required.
        expected_artifact_type: see add_task() for the allowed set.
        expected_location: operator-visible path; never skills/ or RECALL_archive/.
        downstream_consumer: external role (operator, customer, developer, …).
        success_criterion: one measurable sentence.

    Returns the updated task summary, or an error if the task is missing or the
    new values would fail intake (e.g., operator-blocked vocabulary in the
    success criterion).
    """
    q = _active_queue
    if not q:
        return "Task queue not loaded."
    if not task_id:
        return "Error: task_id is required."
    target = None
    for t in q._data.get("tasks", []):
        if t.get("id") == task_id:
            target = t
            break
    if not target:
        return f"Error: task {task_id!r} not found in the queue."

    # Re-check with the proposed fields. The intake gate is the same one the
    # daemon uses at task creation, so behavior is consistent.
    try:
        from repryntt.agents.intake_gate import check_admissibility
    except ImportError:
        return "Error: intake_gate module unavailable."
    verdict = check_admissibility({
        "title": target.get("title", ""),
        "description": target.get("description", ""),
        "expected_artifact_type": expected_artifact_type,
        "expected_location": expected_location,
        "downstream_consumer": downstream_consumer,
        "success_criterion": success_criterion,
    }, strict=True)
    if not verdict["accepted"]:
        return (
            "❌ Retype rejected:\n  - "
            + "\n  - ".join(verdict["reasons"][:5])
            + "\nThe task is unchanged. Adjust the fields and try again."
        )

    target["expected_artifact_type"] = expected_artifact_type
    target["expected_location"] = expected_location
    target["downstream_consumer"] = downstream_consumer
    target["success_criterion"] = success_criterion
    q._save()
    return (
        f"✅ Retyped [{task_id}] {target.get('title','')[:60]}\n"
        f"  type: {expected_artifact_type}\n"
        f"  location: {expected_location}\n"
        f"  consumer: {downstream_consumer}\n"
        f"  success: {success_criterion}\n"
        "Completion-time critic gate is now active on this task."
    )


def _check_nav_guardrail(task: dict) -> str | None:
    """If this is a nav task, verify real executed=true steps exist in today's log."""
    title = (task.get("title") or "").lower()
    if "nav" not in title and "explore" not in title and "navigation" not in title:
        return None  # Not a nav task
    from pathlib import Path
    from datetime import date
    exp_file = Path.home() / ".repryntt" / "data" / "nav_experience" / f"{date.today()}.jsonl"
    if not exp_file.exists():
        return "🚫 Cannot complete nav task — no nav experience log exists for today. Use nav_step(execute=true) to physically move first."
    executed_count = 0
    with open(exp_file) as f:
        for line in f:
            try:
                entry = json.loads(line)
                if entry.get("executed") is True:
                    executed_count += 1
            except Exception:
                pass
    if executed_count < 5:
        return f"🚫 Cannot complete nav task — only {executed_count} executed=true steps logged today (need at least 5). Use nav_step(execute=true) to physically move."
    return None


def complete_current_task(summary: str = "", **kw) -> str:
    """Mark your current task as completed and advance to the next one.
    Only call this when you have genuinely finished and verified the work.

    Parameters:
        summary: Brief summary of what was delivered (1-2 sentences).
    """
    q = _active_queue
    if not q:
        return "Task queue not loaded."
    current = q.get_current()
    if not current:
        return "No task is currently in progress."
    # Nav tasks require real executed movement before completion
    guardrail = _check_nav_guardrail(current)
    if guardrail:
        return guardrail
    completed = q.complete_task(current["id"], summary=summary or "Completed")
    if not completed:
        if current.get("expected_location"):
            return (
                "🚫 Completion blocked: this task declares an expected artifact "
                "and must pass the daemon's critic gate before it can be marked "
                "completed. Leave the task in progress, fix any critic concerns, "
                "and let the heartbeat completion path run the gate."
            )
        return "🚫 Completion blocked: task could not be marked completed."
    next_task = q.get_next_queued()
    if next_task:
        q.start_task(next_task["id"])
        return (f"✅ Completed: {current['title'][:60]}\n"
                f"⏭️ Next task started: [{next_task['id']}] {next_task['title']}")
    return f"✅ Completed: {current['title'][:60]}\n🎉 All tasks done for today!"


def skip_task(reason: str = "", **kw) -> str:
    """Skip the current task if it's blocked or not feasible right now.
    The task won't come back — use this deliberately.

    Parameters:
        reason: Why you're skipping this task (required for accountability).
    """
    q = _active_queue
    if not q:
        return "Task queue not loaded."
    current = q.get_current()
    if not current:
        return "No task is currently in progress."
    q.skip_task(current["id"], reason=reason or "Skipped by agent")
    next_task = q.get_next_queued()
    if next_task:
        q.start_task(next_task["id"])
        return (f"⏭️ Skipped: {current['title'][:60]} ({reason[:60]})\n"
                f"Next task started: [{next_task['id']}] {next_task['title']}")
    return f"⏭️ Skipped: {current['title'][:60]}\nNo more tasks in queue."
