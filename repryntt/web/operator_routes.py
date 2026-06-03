"""
Operator → Andrew task injection.

Exposes /api/operator/* endpoints to:
  - inject typed tasks at priority 0 (operator priority) into Andrew's queue
  - retype an existing task with the 4 typed deliverable fields
  - list / fetch tasks for the dashboard

Andrew's TaskQueue is file-backed and the daemon reloads it every heartbeat,
so a write here is picked up by Andrew on his next ~60-120s tick. No IPC
required between the Flask app and the daemon — they share state via
`<operator_workspace>/task_queue.json`.

Note: the intake_gate runs on add_task, so operator-blocklisted titles still
get rejected here just like they would for an autonomous task. The operator
sees the rejection reasons synchronously and can rewrite. The blocklist is
per-installation; it ships empty.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from flask import Blueprint, jsonify, request

from repryntt.agents.task_queue import TaskQueue
from repryntt.paths import workspace_dir as _workspace_dir


logger = logging.getLogger("operator.routes")

operator_bp = Blueprint("operator", __name__)


def _operator_workspace() -> str:
    return str(_workspace_dir() / "agents" / "operator")


def _get_queue() -> TaskQueue:
    """Fresh TaskQueue each call — file-backed, daemon-shared."""
    return TaskQueue(_operator_workspace())


# ── Inject a typed operator task ────────────────────────────────────────

@operator_bp.route("/api/operator/inject_task", methods=["POST"])
def inject_task():
    """Inject a priority-0 task into Andrew's queue with typed deliverable fields.

    JSON body (all four typed fields are recommended — the critic gate only
    fires on tasks that declare expected_location):
        title                 (required)
        description           (optional)
        expected_artifact_type
        expected_location
        downstream_consumer
        success_criterion
        use_codeforge         (bool) — if true, the description is rewritten
                              to include an explicit "use the forge_project()
                              tool to build this" suffix so Andrew routes it
                              through CodeForge.
    Returns:
        {success, task: {...}}  or  {success=false, status='rejected', reasons:[]}
    """
    data: Dict[str, Any] = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"success": False, "error": "title is required"}), 400

    description = (data.get("description") or "").strip()
    if data.get("use_codeforge"):
        suffix = (
            "\n\nUse the `forge_project()` tool to build this as a CodeForge "
            "package. After approval the build pipeline produces a complete "
            "Python package (setup.py, README, tests, requirements, source modules)."
        )
        if suffix.strip() not in description:
            description = (description + suffix).strip() if description else (title + suffix).strip()

    q = _get_queue()
    t = q.add_task(
        title=title,
        description=description or "",
        priority=0,                     # operator priority always runs first
        source="operator",
        expected_artifact_type=(data.get("expected_artifact_type") or "").strip(),
        expected_location=(data.get("expected_location") or "").strip(),
        downstream_consumer=(data.get("downstream_consumer") or "").strip(),
        success_criterion=(data.get("success_criterion") or "").strip(),
    )

    if isinstance(t, dict) and t.get("status") == "rejected":
        return jsonify({
            "success": False,
            "status": "rejected",
            "reasons": t.get("reasons") or [],
            "task": t,
        }), 400

    return jsonify({"success": True, "task": t})


# ── Retype an existing task ─────────────────────────────────────────────

@operator_bp.route("/api/operator/retype/<task_id>", methods=["POST"])
def retype(task_id: str):
    """Add typed deliverable fields to an existing task that was queued without them.

    JSON body:
        expected_artifact_type
        expected_location
        downstream_consumer
        success_criterion
    """
    data = request.get_json(silent=True) or {}
    q = _get_queue()
    target = None
    for t in q._data.get("tasks", []):
        if t.get("id") == task_id:
            target = t
            break
    if not target:
        return jsonify({"success": False, "error": f"task {task_id!r} not found"}), 404

    from repryntt.agents.intake_gate import check_admissibility
    verdict = check_admissibility({
        "title": target.get("title", ""),
        "description": target.get("description", ""),
        "expected_artifact_type": (data.get("expected_artifact_type") or "").strip(),
        "expected_location": (data.get("expected_location") or "").strip(),
        "downstream_consumer": (data.get("downstream_consumer") or "").strip(),
        "success_criterion": (data.get("success_criterion") or "").strip(),
    }, strict=True)
    if not verdict["accepted"]:
        return jsonify({"success": False, "status": "rejected",
                        "reasons": verdict["reasons"]}), 400

    target["expected_artifact_type"] = (data.get("expected_artifact_type") or "").strip()
    target["expected_location"] = (data.get("expected_location") or "").strip()
    target["downstream_consumer"] = (data.get("downstream_consumer") or "").strip()
    target["success_criterion"] = (data.get("success_criterion") or "").strip()
    q._save()
    return jsonify({"success": True, "task": target})


# ── Read endpoints ──────────────────────────────────────────────────────

@operator_bp.route("/api/operator/tasks", methods=["GET"])
def list_tasks():
    """List all tasks in Andrew's queue. Query params:
        source=operator (filter to operator-injected only)
        status=queued|in_progress|completed|failed|skipped|rejected (filter)
        limit=50 (default)
    """
    src = request.args.get("source", "").strip()
    status = request.args.get("status", "").strip()
    try:
        limit = int(request.args.get("limit", "200"))
    except ValueError:
        limit = 200

    q = _get_queue()
    tasks = q._data.get("tasks", [])
    if src:
        tasks = [t for t in tasks if t.get("source") == src]
    if status:
        tasks = [t for t in tasks if t.get("status") == status]

    tasks = list(reversed(tasks))[:limit]  # newest first
    return jsonify({
        "success": True,
        "tasks": tasks,
        "queue_day": q._data.get("day"),
    })


@operator_bp.route("/api/operator/task/<task_id>", methods=["GET"])
def task_detail(task_id: str):
    q = _get_queue()
    for t in q._data.get("tasks", []):
        if t.get("id") == task_id:
            return jsonify({"success": True, "task": t})
    return jsonify({"success": False, "error": f"task {task_id!r} not found"}), 404


# ── Forge convenience: list eligible deliverable types ──────────────────

@operator_bp.route("/api/operator/types", methods=["GET"])
def list_types():
    """Return the typed-deliverable taxonomy + suggested operator-visible locations.
    The forge dashboard / assign page uses this to populate dropdowns + path stubs.
    """
    try:
        from repryntt.agents.intake_gate import ALLOWED_ARTIFACT_TYPES, OPERATOR_VISIBLE_PREFIXES
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    # path stub per type — must stay under an operator-visible prefix
    base = "workspace/agents/operator"
    stubs = {
        "code":                  f"{base}/code/<NAME>.py",
        "smart_contract":        f"{base}/code/<NAME>.sol",
        "research_md":           f"{base}/research/<NAME>.md",
        "analysis_md":           f"{base}/analysis/<NAME>.md",
        "plan_md":               f"{base}/plans/<NAME>.md",
        "design_md":             f"{base}/plans/<NAME>.md",
        "legal_md":              f"{base}/legal/<NAME>.md",
        "financial_model":       f"{base}/data/<NAME>.md",
        "tokenomics":            f"{base}/data/<NAME>.md",
        "patent_claim":          f"{base}/legal/<NAME>.md",
        "curriculum_md":         f"{base}/research/<NAME>.md",
        "marketing_copy":        f"{base}/deliverables/<NAME>.md",
        "report":                f"{base}/reports/<NAME>.md",
        "data_extract":          f"{base}/data/<NAME>.json",
        "robotics_doc":          f"{base}/plans/<NAME>.md",
        "hr_doc":                f"{base}/deliverables/<NAME>.md",
        "real_estate_analysis":  f"{base}/analysis/<NAME>.md",
    }
    return jsonify({
        "success": True,
        "types": list(ALLOWED_ARTIFACT_TYPES),
        "location_stubs": stubs,
        "operator_visible_prefixes": list(OPERATOR_VISIBLE_PREFIXES),
    })
