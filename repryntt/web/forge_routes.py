"""
CodeForge Web API — Flask blueprint for /api/forge/* endpoints.

Provides REST endpoints for starting, monitoring, and managing forge projects,
plus benchmark and swarm status. Also exposes a Server-Sent Events stream at
/api/forge/stream for live model-call + stage-transition visibility.
"""

import json
import logging
import queue
import time
from flask import Blueprint, jsonify, request, Response, stream_with_context

logger = logging.getLogger("codeforge.routes")

forge_bp = Blueprint("forge", __name__)

# Install the forge log-event handler the first time this blueprint is imported.
try:
    from repryntt.codeforge.events import register_forge_log_handler, get_bus
    register_forge_log_handler()
except Exception:
    logger.debug("forge events module unavailable", exc_info=True)


def _get_forge():
    from repryntt.codeforge.forge import get_forge
    return get_forge()


def _get_swarm():
    from repryntt.codeforge.swarm import get_swarm
    return get_swarm()


# ── Project endpoints ──

@forge_bp.route("/api/forge/projects", methods=["GET"])
def list_projects():
    """List all forge projects."""
    forge = _get_forge()
    return jsonify({"success": True, "projects": forge.list_projects()})


@forge_bp.route("/api/forge/project/<project_id>", methods=["GET"])
def get_project(project_id):
    """Get detailed info for a specific project."""
    forge = _get_forge()
    detail = forge.get_project_detail(project_id)
    if not detail:
        return jsonify({"success": False, "error": "Project not found"}), 404
    return jsonify({"success": True, "project": detail})


@forge_bp.route("/api/forge/start", methods=["POST"])
def start_project():
    """Submit a CodeForge project proposal (goes through governance).

    Body fields:
        description : required, plain-text spec
        proposer    : optional, defaults to 'operator'
        provider    : optional, per-project provider override
                      (nvidia | anthropic | openai | openrouter | local)
        model       : optional, per-project model id (operators bring their own)
    """
    data = request.get_json(silent=True) or {}
    description = data.get("description", "").strip()
    if not description:
        return jsonify({"success": False, "error": "description required"}), 400

    from repryntt.codeforge.governance import propose_project, projects_remaining_today
    proposal = propose_project(
        description,
        proposer=data.get("proposer", "operator"),
        provider=(data.get("provider") or "").strip(),
        model=(data.get("model") or "").strip(),
    )
    return jsonify({
        "success": True,
        "proposal_id": proposal["id"],
        "status": "proposed",
        "daily_remaining": projects_remaining_today(),
        "message": "Proposal submitted. Approve it to start the build.",
    })


# ── Governance endpoints ──

@forge_bp.route("/api/forge/proposals", methods=["GET"])
def list_proposals():
    """List all forge proposals."""
    from repryntt.codeforge.governance import get_proposals, projects_remaining_today, load_settings
    status = request.args.get("status", "")
    proposals = get_proposals(status=status)
    s = load_settings()
    return jsonify({
        "success": True,
        "proposals": proposals,
        "daily_remaining": projects_remaining_today(),
        "daily_cap": s["daily_project_cap"],
    })


@forge_bp.route("/api/forge/proposals/<proposal_id>", methods=["GET"])
def get_proposal_detail(proposal_id):
    from repryntt.codeforge.governance import get_proposal
    p = get_proposal(proposal_id)
    if not p:
        return jsonify({"success": False, "error": "Not found"}), 404
    return jsonify({"success": True, "proposal": p})


@forge_bp.route("/api/forge/proposals/<proposal_id>/approve", methods=["POST"])
def approve(proposal_id):
    from repryntt.codeforge.governance import approve_proposal
    result = approve_proposal(proposal_id)
    if not result:
        return jsonify({"success": False, "error": "Not found"}), 404
    if "error" in result:
        return jsonify({"success": False, "error": result["error"]}), 400
    return jsonify({"success": True, "proposal": result})


@forge_bp.route("/api/forge/proposals/<proposal_id>/reject", methods=["POST"])
def reject(proposal_id):
    data = request.get_json(silent=True) or {}
    from repryntt.codeforge.governance import reject_proposal
    result = reject_proposal(proposal_id, reason=data.get("reason", ""))
    if not result:
        return jsonify({"success": False, "error": "Not found"}), 404
    return jsonify({"success": True, "proposal": result})


@forge_bp.route("/api/forge/proposals/<proposal_id>/build", methods=["POST"])
def build_proposal(proposal_id):
    from repryntt.codeforge.governance import start_approved_project
    result = start_approved_project(proposal_id)
    if "error" in result:
        return jsonify({"success": False, "error": result["error"]}), 400
    return jsonify(result)


@forge_bp.route("/api/forge/settings", methods=["GET"])
def get_settings():
    from repryntt.codeforge.governance import load_settings, projects_remaining_today
    s = load_settings()
    s["daily_remaining"] = projects_remaining_today()
    return jsonify({"success": True, "settings": s})


@forge_bp.route("/api/forge/settings", methods=["POST"])
def update_forge_settings():
    data = request.get_json(silent=True) or {}
    from repryntt.codeforge.governance import update_settings
    s = update_settings(**data)
    return jsonify({"success": True, "settings": s})


@forge_bp.route("/api/forge/reap_stale", methods=["POST"])
def reap_stale():
    """Mark all stale active projects as cancelled.

    A project is "stale" if its status is in the active set
    (specifying / architecting / generating / testing / fix_iterating /
    validating / packaging) but no live pipeline thread is driving it.
    This happens when the daemon restarts mid-build. Auto-runs at startup,
    can also be triggered manually here.

    Returns: {success, reaped, project_ids: [...]}
    """
    forge = _get_forge()
    before_ids = [p["project_id"] for p in forge.list_projects()
                  if (p.get("status") or "") in forge._ACTIVE_STATUSES
                  and not (forge._running_jobs.get(p["project_id"]) and
                           forge._running_jobs[p["project_id"]].is_alive())]
    reaped = forge._reap_stale_active()
    return jsonify({"success": True, "reaped": reaped, "project_ids": before_ids})


@forge_bp.route("/api/forge/cancel/<project_id>", methods=["POST"])
def cancel_project(project_id):
    """Cancel a running forge project."""
    forge = _get_forge()
    ok = forge.cancel_project(project_id)
    return jsonify({"success": ok})


# ── Benchmark endpoints ──

@forge_bp.route("/api/forge/benchmark", methods=["POST"])
def run_benchmark():
    """Run a coding benchmark on the configured LLM."""
    data = request.get_json(silent=True) or {}
    provider = data.get("provider", "")

    from repryntt.codeforge.generator import _load_ai_config, _resolve_provider, _call_llm
    from repryntt.codeforge.benchmark import run_benchmark as _run_bench, save_benchmark

    config = _load_ai_config()
    pinfo = _resolve_provider(config, provider)

    def call_fn(prompt):
        msgs = [{"role": "user", "content": prompt}]
        return _call_llm(msgs, pinfo, max_tokens=2000, temperature=0.2)

    result = _run_bench(
        call_fn, node_id="local",
        model_name=pinfo.get("model", "unknown"),
        provider=pinfo.get("provider", "unknown"),
    )
    save_benchmark(result)

    return jsonify({"success": True, "benchmark": result.to_dict()})


@forge_bp.route("/api/forge/benchmark/cached", methods=["GET"])
def cached_benchmark():
    """Get cached benchmark result for local node."""
    from repryntt.codeforge.benchmark import get_cached_benchmark
    result = get_cached_benchmark("local")
    if result:
        return jsonify({"success": True, "benchmark": result.to_dict()})
    return jsonify({"success": True, "benchmark": None})


# ── Swarm endpoints ──

@forge_bp.route("/api/forge/swarm", methods=["GET"])
def swarm_status():
    """Get swarm status."""
    swarm = _get_swarm()
    return jsonify({"success": True, "swarm": swarm.get_status()})


# ── Live event stream (Server-Sent Events) ──

@forge_bp.route("/api/forge/stream", methods=["GET"])
def event_stream():
    """SSE stream of forge events.

    Query params:
      project_id (optional) — filter to one project. Omit to see everything.

    Each emitted line is `data: <json>\\n\\n` where `<json>` has shape:
      {ts, project_id, kind, stage, model, message, logger, level}

    On connect, the most recent 50 events from the matching buffer are
    replayed so a freshly-opened UI gets immediate context.
    """
    project_id = (request.args.get("project_id") or "").strip() or None

    try:
        bus = get_bus()
    except Exception:
        return Response(
            "data: " + json.dumps({"ts": time.time(), "kind": "error",
                                    "message": "event bus unavailable"}) + "\n\n",
            mimetype="text/event-stream",
        )

    q = bus.subscribe(project_id=project_id, replay=50)

    @stream_with_context
    def generate():
        # Send an initial comment so the connection opens fast
        yield ": forge stream open\n\n"
        try:
            while True:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    # Heartbeat keeps the connection alive through proxies
                    yield ": ping\n\n"
                    continue
                yield "data: " + json.dumps(ev, default=str) + "\n\n"
        except GeneratorExit:
            pass
        finally:
            bus.unsubscribe(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable nginx buffering if proxied
            "Connection": "keep-alive",
        },
    )


@forge_bp.route("/api/forge/events", methods=["GET"])
def recent_events():
    """Snapshot of the most recent events (non-streaming fallback for clients
    that can't use SSE — e.g. testing, curl-based inspection).

    Query params:
      project_id (optional)
      limit (default 100)
    """
    project_id = (request.args.get("project_id") or "").strip() or None
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        limit = 100
    try:
        bus = get_bus()
        return jsonify({"success": True, "events": bus.recent(project_id=project_id, limit=limit)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
