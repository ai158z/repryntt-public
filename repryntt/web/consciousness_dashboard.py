"""Consciousness Dashboard — real-time operator visibility into Andrew's reasoning.

Demonstrates the layered agency architecture:
  Layer 3 (Andrew/LLM) sets goals and conscious intent overrides
  Layer 2 (Explorer)   runs the autonomous see→think→act loop
  Layer 1 (Tank)       executes motor commands

Routes
------
  GET  /consciousness          HTML dashboard
  GET  /consciousness/stream   SSE event stream (live state updates)
  POST /consciousness/intent   Set conscious intent override
  POST /consciousness/intent/clear   Clear active intent
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Generator

from flask import Blueprint, Response, jsonify, render_template, request

logger = logging.getLogger(__name__)

consciousness_bp = Blueprint("consciousness", __name__, url_prefix="/consciousness")


# ── Helpers ─────────────────────────────────────────────────────────────────


def _get_explorer():
    from repryntt.hardware.explorer import get_explorer
    return get_explorer()


def _image_url(abs_path: str) -> str | None:
    """Convert an absolute frame path to a /vision/frame/<date>/<name> URL."""
    if not abs_path:
        return None
    p = Path(abs_path)
    if not p.exists():
        return None
    # Expect .../vision/YYYY-MM-DD/filename.jpg
    try:
        date_str = p.parent.name
        return f"/vision/frame/{date_str}/{p.name}"
    except Exception:
        return None


def _sse_event(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


# ── Routes ───────────────────────────────────────────────────────────────────


@consciousness_bp.route("", strict_slashes=False)
def dashboard():
    return render_template("consciousness_dashboard.html")


@consciousness_bp.route("/stream")
def stream():
    """Server-Sent Events stream pushing live explorer state once per second."""

    def generate() -> Generator[str, None, None]:
        # Send an immediate heartbeat so the browser knows the connection is live
        yield _sse_event({"type": "connected", "ts": time.time()})

        while True:
            try:
                explorer = _get_explorer()
                state = explorer.get_live_state()

                # Convert absolute image path to a URL the browser can fetch
                frame_url = _image_url(state.get("last_image_path", ""))
                state["frame_url"] = frame_url
                state.pop("last_image_path", None)
                state["type"] = "state"
                state["ts"] = time.time()

                yield _sse_event(state)
            except Exception as exc:
                logger.warning("consciousness stream error: %s", exc)
                yield _sse_event({"type": "error", "message": str(exc), "ts": time.time()})

            time.sleep(1.0)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering if behind proxy
        },
    )


@consciousness_bp.route("/intent", methods=["POST"])
def set_intent():
    """Set Andrew's conscious navigation intent.

    Body (JSON): {"direction": "left"|"right"|"forward"|"backward"|"stop",
                  "reason": "optional explanation", "duration_steps": 20}
    """
    data = request.get_json(silent=True) or {}
    direction = str(data.get("direction", "")).strip()
    reason = str(data.get("reason", "operator override")).strip()
    duration = int(data.get("duration_steps", 20))

    if not direction:
        return jsonify({"error": "direction is required"}), 400

    if direction == "stop":
        result = _get_explorer().stop(reason=f"operator stop: {reason}")
    else:
        result = _get_explorer().set_intent(direction, reason, duration)

    return jsonify(result)


@consciousness_bp.route("/intent/clear", methods=["POST"])
def clear_intent():
    """Clear the active conscious intent — VLM resumes directional control."""
    result = _get_explorer().clear_intent()
    return jsonify(result)
