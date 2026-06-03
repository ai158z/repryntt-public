#!/usr/bin/env python3
"""
Repryntt Tool API Server — REST endpoints for autonomous tool execution.

Provides:
  GET  /health              — health check
  GET  /tools               — list available tools
  GET  /tools/<name>/schema — tool parameter schema
  POST /tools/<name>        — execute a single tool
  POST /tools/batch         — execute up to 10 tools in one call

Authentication: Bearer token from ~/.repryntt/auth_token (auto-generated).
Local requests (127.0.0.1) bypass auth.
"""

import json
import os
import secrets
import sys
import time
import logging
import threading
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Dict

from flask import Blueprint, Flask, g, jsonify, request
from flask_cors import CORS

from repryntt.paths import get_data_dir

logger = logging.getLogger(__name__)

# ── Auth token ────────────────────────────────────────────────────────────

_TOKEN_FILE = get_data_dir() / "auth_token"


def _get_auth_token() -> str:
    try:
        if _TOKEN_FILE.exists():
            tok = _TOKEN_FILE.read_text().strip()
            if len(tok) >= 32:
                return tok
    except Exception:
        pass
    tok = secrets.token_hex(32)
    try:
        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_FILE.write_text(tok)
        from repryntt.platform_utils import secure_file
        secure_file(_TOKEN_FILE)
    except Exception:
        pass
    return tok


AUTH_TOKEN = _get_auth_token()


def _require_auth(f):
    """Decorator — require Bearer token or allow localhost."""
    @wraps(f)
    def wrapper(*a, **kw):
        if request.remote_addr in ("127.0.0.1", "::1"):
            return f(*a, **kw)
        hdr = request.headers.get("Authorization", "")
        tok = request.args.get("token", "")
        provided = hdr.removeprefix("Bearer ").strip() or tok
        if not provided or provided != AUTH_TOKEN:
            return jsonify({"success": False, "error": "Unauthorized"}), 401
        return f(*a, **kw)
    return wrapper


# ── Rate limiting ─────────────────────────────────────────────────────────

_rate_buckets: Dict[str, list] = {}
_RATE_WINDOW = 60
_RATE_MAX = 120


def _rate_limit(f):
    @wraps(f)
    def wrapper(*a, **kw):
        ip = request.remote_addr or "unknown"
        now = time.time()
        bucket = _rate_buckets.setdefault(ip, [])
        bucket[:] = [t for t in bucket if t > now - _RATE_WINDOW]
        if len(bucket) >= _RATE_MAX:
            return jsonify({"success": False, "error": "Rate limit exceeded"}), 429
        bucket.append(now)
        return f(*a, **kw)
    return wrapper


# ── Tool catalog ──────────────────────────────────────────────────────────

def _build_tool_catalog() -> Dict[str, Dict[str, Any]]:
    return {
        "grokipedia_search": {
            "description": "Search academic/curated knowledge from Grok-ipedia",
            "category": "knowledge",
            "parameters": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "description": "Max results", "default": 5},
            },
            "required": ["query"],
            "cost_credits": 0.1,
        },
        "google_web_search": {
            "description": "Search current web content",
            "category": "web_search",
            "parameters": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "description": "Max results", "default": 10},
            },
            "required": ["query"],
            "cost_credits": 0.2,
        },
        "brain_network_search": {
            "description": "Search brain network memory",
            "category": "knowledge",
            "parameters": {
                "query": {"type": "string", "description": "Search query"},
                "context_type": {"type": "string", "description": "Context type"},
            },
            "required": ["query"],
            "cost_credits": 0.05,
        },
        "read_file": {
            "description": "Read content from a file",
            "category": "file_system",
            "parameters": {
                "file_path": {"type": "string", "description": "Path to file"},
                "start_line": {"type": "integer", "description": "Start line", "default": 1},
                "end_line": {"type": "integer", "description": "End line", "default": -1},
            },
            "required": ["file_path"],
            "cost_credits": 0.01,
        },
        "write_file": {
            "description": "Write content to a file",
            "category": "file_system",
            "parameters": {
                "file_path": {"type": "string", "description": "Path to file"},
                "content": {"type": "string", "description": "Content to write"},
                "append": {"type": "boolean", "description": "Append mode", "default": False},
            },
            "required": ["file_path", "content"],
            "cost_credits": 0.02,
        },
        "run_terminal_cmd": {
            "description": "Execute a terminal command",
            "category": "system",
            "parameters": {
                "command": {"type": "string", "description": "Command to execute"},
                "working_directory": {"type": "string", "description": "Working dir"},
                "timeout": {"type": "integer", "description": "Timeout seconds", "default": 30},
            },
            "required": ["command"],
            "cost_credits": 0.1,
        },
        "analyze_topic": {
            "description": "Analyze a topic using AI reasoning",
            "category": "ai_reasoning",
            "parameters": {
                "topic": {"type": "string", "description": "Topic to analyze"},
                "depth": {"type": "string", "description": "basic/intermediate/advanced", "default": "intermediate"},
            },
            "required": ["topic"],
            "cost_credits": 0.3,
        },
        "get_wallet_balance": {
            "description": "Get blockchain wallet balance",
            "category": "blockchain",
            "parameters": {
                "wallet_address": {"type": "string", "description": "Wallet address"},
            },
            "required": ["wallet_address"],
            "cost_credits": 0.01,
        },
        "submit_workload": {
            "description": "Submit AI workload to blockchain network",
            "category": "blockchain",
            "parameters": {
                "prompt": {"type": "string", "description": "AI prompt"},
                "max_tokens": {"type": "integer", "description": "Max tokens", "default": 500},
            },
            "required": ["prompt"],
            "cost_credits": 1.0,
        },
        "google_maps_search": {
            "description": "Search for places via Google Maps",
            "category": "location",
            "parameters": {
                "query": {"type": "string", "description": "Place query"},
                "location": {"type": "string", "description": "Location bias"},
            },
            "required": ["query"],
            "cost_credits": 0.1,
        },
        "get_directions": {
            "description": "Get directions between locations",
            "category": "location",
            "parameters": {
                "origin": {"type": "string", "description": "Start"},
                "destination": {"type": "string", "description": "End"},
                "mode": {"type": "string", "description": "driving/walking/transit", "default": "driving"},
            },
            "required": ["origin", "destination"],
            "cost_credits": 0.15,
        },
    }


# ── Type validation helper ────────────────────────────────────────────────

_TYPE_MAP = {
    "string": str, "str": str,
    "integer": int, "int": int,
    "number": (int, float), "float": float,
    "boolean": bool, "bool": bool,
    "array": list, "list": list,
    "object": dict, "dict": dict,
}


# ── Flask app ─────────────────────────────────────────────────────────────

# ── Module-level state ────────────────────────────────────────────────────

_tools = _build_tool_catalog()
_request_log: list = []
_brain_system = None


def set_tool_api_brain(brain):
    """Set the brain system for tool execution (called from Nexus host)."""
    global _brain_system
    _brain_system = brain


# ── Blueprint ─────────────────────────────────────────────────────────────

tool_api_bp = Blueprint('tool_api', __name__)


@tool_api_bp.before_request
def _before():
    g.start_time = time.time()
    g.request_id = f"{int(time.time()*1000)}_{threading.current_thread().ident}"


@tool_api_bp.after_request
def _after(resp):
    dur = time.time() - getattr(g, "start_time", time.time())
    entry = {
        "ts": datetime.now().isoformat(),
        "method": request.method,
        "path": request.path,
        "status": resp.status_code,
        "dur": round(dur, 3),
        "ip": request.remote_addr,
    }
    _request_log.append(entry)
    if len(_request_log) > 1000:
        del _request_log[:500]
    return resp


@tool_api_bp.route("/health")
def ta_health():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": "1.0.0",
        "tools_available": len(_tools),
        "brain_connected": _brain_system is not None,
    })


@tool_api_bp.route("/tools", methods=["GET"])
@_require_auth
@_rate_limit
def ta_list_tools():
    info = {}
    for name, t in _tools.items():
        info[name] = {
            "description": t.get("description", ""),
            "parameters": t.get("parameters", {}),
            "category": t.get("category", "general"),
            "cost_credits": t.get("cost_credits", 0.0),
        }
    return jsonify({"success": True, "tools": info, "total_tools": len(info)})


@tool_api_bp.route("/tools/<tool_name>/schema", methods=["GET"])
@_require_auth
def ta_tool_schema(tool_name):
    if tool_name not in _tools:
        return jsonify({"success": False, "error": f"Tool '{tool_name}' not found"}), 404
    t = _tools[tool_name]
    return jsonify({
        "success": True,
        "tool_name": tool_name,
        "schema": {
            "description": t.get("description", ""),
            "parameters": t.get("parameters", {}),
            "required": t.get("required", []),
            "category": t.get("category", "general"),
            "cost_credits": t.get("cost_credits", 0.0),
        },
    })


@tool_api_bp.route("/tools/<tool_name>", methods=["POST"])
@_require_auth
@_rate_limit
def ta_execute_tool(tool_name):
    if tool_name not in _tools:
        return jsonify({
            "success": False,
            "error": f"Tool '{tool_name}' not found",
            "available_tools": list(_tools.keys()),
        }), 404

    data = request.get_json(silent=True) or {}
    params = data.get("parameters", {})

    t = _tools[tool_name]
    for req in t.get("required", []):
        if req not in params:
            return jsonify({"success": False, "error": f"Missing required parameter: {req}"}), 400

    schema = t.get("parameters", {})
    for pname, pval in params.items():
        if pname in schema:
            expected = schema[pname].get("type")
            if expected and not isinstance(pval, _TYPE_MAP.get(expected, object)):
                return jsonify({"success": False, "error": f"{pname} must be {expected}"}), 400

    start = time.time()
    result = _execute(_brain_system, tool_name, params, data.get("reasoning_context", ""))
    dur = time.time() - start

    resp = {
        "success": result.get("success", False),
        "tool_name": tool_name,
        "execution_time": round(dur, 3),
        "cost_credits": t.get("cost_credits", 0.0),
        "timestamp": datetime.now().isoformat(),
    }
    if result.get("success"):
        resp["result"] = result.get("result")
    else:
        resp["error"] = result.get("error", "Tool execution failed")
    return jsonify(resp), 200 if result.get("success") else 500


@tool_api_bp.route("/tools/batch", methods=["POST"])
@_require_auth
@_rate_limit
def ta_execute_batch():
    data = request.get_json(silent=True) or {}
    items = data.get("tools", [])
    if not isinstance(items, list) or len(items) > 10:
        return jsonify({"success": False, "error": "tools must be list (max 10)"}), 400

    results = []
    total_cost = 0.0
    for item in items:
        tname = item.get("tool_name")
        if tname not in _tools:
            results.append({"tool_name": tname, "success": False, "error": "Not found"})
            continue
        r = _execute(_brain_system, tname, item.get("parameters", {}), item.get("reasoning_context", ""))
        cost = _tools[tname].get("cost_credits", 0.0)
        total_cost += cost
        results.append({
            "tool_name": tname,
            "success": r.get("success", False),
            "cost_credits": cost,
            "result": r.get("result") if r.get("success") else None,
            "error": r.get("error") if not r.get("success") else None,
        })
    return jsonify({"success": True, "results": results, "total_cost_credits": total_cost})


# ── Standalone Flask App (backward compat) ────────────────────────────────

def create_app(brain_system=None) -> Flask:
    """Create the Tool API Flask app."""
    if brain_system is not None:
        set_tool_api_brain(brain_system)
    app = Flask(__name__)
    CORS(app, resources={r"/*": {"origins": ["http://localhost:*", "http://127.0.0.1:*"]}})
    app.register_blueprint(tool_api_bp)
    return app


def _execute(brain_system, tool_name: str, params: dict, context: str = "") -> dict:
    """Execute a tool via the BrainSystem or the ToolRegistry."""
    if brain_system is None:
        return {"success": False, "error": "Brain system not connected — tools unavailable"}
    try:
        # Prefer the repryntt ToolRegistry (tools/brain_system.execute_tool_call)
        from repryntt.tools.brain_system import execute_tool_call
        result = execute_tool_call(tool_name, params, brain=brain_system)
        if context and result.get("success"):
            result["reasoning_context"] = context
        return result
    except Exception as e:
        logger.error(f"Tool execution error ({tool_name}): {e}")
        return {"success": False, "error": str(e)}


def main():
    """Entry point — start the Tool API server on port 8083."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logger.info("🔧 Repryntt Tool API Server")

    # Try to register and load BrainSystem (optional — server works without it)
    brain = None
    try:
        from repryntt.brain.bootstrap import ensure_brain_registered
        ensure_brain_registered()
        from repryntt.brain import get_brain_system
        brain = get_brain_system()
        logger.info("🧠 BrainSystem connected")
    except Exception as e:
        logger.warning(f"BrainSystem unavailable — tool execution disabled: {e}")

    app = create_app(brain_system=brain)
    tools = _build_tool_catalog()
    logger.info(f"📊 {len(tools)} tools registered")
    logger.info(f"🔑 Auth token: {AUTH_TOKEN[:8]}...")
    logger.info("📡 http://localhost:8083")
    app.run(host="0.0.0.0", port=8083, debug=False, threaded=True)


if __name__ == "__main__":
    main()
    main()