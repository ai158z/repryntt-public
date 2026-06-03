"""
Orchestrator Dashboard — UI + REST API for Andrew's supervisor.

Surfaces the Orchestrator's snapshot (heartbeat scores, CodeForge bypass,
idle loops, council activity), lets the operator post a Director Brief
on demand, and exposes a kill-switch for the council debate engine.
"""

import logging

from flask import Blueprint, jsonify, render_template, request

from repryntt.agents.orchestrator import Orchestrator

log = logging.getLogger("repryntt.web.orchestrator_dashboard")

orchestrator_bp = Blueprint(
    "orchestrator", __name__, url_prefix="/orchestrator"
)


@orchestrator_bp.route("", strict_slashes=False)
def dashboard():
    return render_template("orchestrator.html")


@orchestrator_bp.route("/api/snapshot")
def api_snapshot():
    hours = int(request.args.get("hours", 6))
    return jsonify(Orchestrator().snapshot(hours=hours))


@orchestrator_bp.route("/api/brief", methods=["POST"])
def api_write_brief():
    orch = Orchestrator()
    snap = orch.snapshot()
    post_id = orch.write_director_brief(snap)
    if not post_id:
        return jsonify({"success": False, "error": "Brief was rejected (likely duplicate) or post failed"}), 200
    return jsonify({"success": True, "post_id": post_id, "verdict": snap["verdict"]})


@orchestrator_bp.route("/api/latest_brief")
def api_latest_brief():
    brief = Orchestrator().latest_brief(max_age_minutes=24 * 60)
    return jsonify(brief or {})


@orchestrator_bp.route("/api/council/toggle", methods=["POST"])
def api_council_toggle():
    enabled = bool((request.get_json(silent=True) or {}).get("enabled", True))
    try:
        from repryntt.agents.persistent_agents import get_agent_daemon
        daemon = get_agent_daemon(auto_start=False)
        result = daemon.enable_council(enabled)
        return jsonify(result)
    except Exception as e:
        log.warning(f"Council toggle failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@orchestrator_bp.route("/api/council/status")
def api_council_status():
    try:
        from repryntt.agents.persistent_agents import get_agent_daemon
        daemon = get_agent_daemon(auto_start=False)
        return jsonify({"enabled": bool(getattr(daemon, "_council_enabled", False))})
    except Exception as e:
        return jsonify({"enabled": False, "error": str(e)})
