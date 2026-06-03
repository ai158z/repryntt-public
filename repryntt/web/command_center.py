"""
SAIGE Command Center — Factory Floor Visual Dashboard
=====================================================
Bird's-eye view of all agents working on the factory floor.
You're on the catwalk looking down.

Port: 8890 (default)
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Blueprint, Flask, jsonify, render_template_string
from flask_cors import CORS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Paths
# ---------------------------------------------------------------------------
_MODULE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _MODULE_DIR.parent.parent
_AGENTS_DIR = _MODULE_DIR.parent / "agents"
_WORKSPACES_DIR = _AGENTS_DIR / "agent_workspaces"
_CONFIG_DIR = _REPO_ROOT / "config"
_PROFILES_FILE = _CONFIG_DIR / "agent_profiles.json"
_BRAIN_DIR = _MODULE_DIR.parent / "brain"

# ---------------------------------------------------------------------------
#  Data readers  (all read-only, safe to call from any thread)
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Any:
    """Safely read a JSON file, returning {} on any error."""
    try:
        if path.exists() and path.stat().st_size > 0:
            with open(path, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _get_agent_profiles() -> Dict[str, Any]:
    return _read_json(_PROFILES_FILE)


def _get_workspace_agents() -> List[str]:
    """Return list of workspace directory names."""
    if not _WORKSPACES_DIR.exists():
        return []
    return [d.name for d in _WORKSPACES_DIR.iterdir() if d.is_dir()]


def _get_consciousness_state(workspace: str) -> Dict:
    return _read_json(_WORKSPACES_DIR / workspace / "consciousness_state.json")


def _get_phase_state(workspace: str) -> Dict:
    return _read_json(_WORKSPACES_DIR / workspace / "phase_state.json")


def _get_memory_files(workspace: str) -> List[Dict]:
    """Get recent memory files from a workspace's memory/ dir."""
    mem_dir = _WORKSPACES_DIR / workspace / "memory"
    if not mem_dir.exists():
        return []
    files = []
    for f in sorted(mem_dir.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True)[:10]:
        files.append({
            "name": f.name,
            "size": f.stat().st_size,
            "modified": f.stat().st_mtime,
        })
    return files


def _get_agent_files(workspace: str) -> List[Dict]:
    """Get all files in a workspace (non-recursive, top-level)."""
    ws = _WORKSPACES_DIR / workspace
    if not ws.exists():
        return []
    files = []
    for f in sorted(ws.iterdir()):
        if f.is_file():
            files.append({
                "name": f.name,
                "size": f.stat().st_size,
                "modified": f.stat().st_mtime,
            })
        elif f.is_dir():
            count = sum(1 for _ in f.rglob("*") if _.is_file())
            files.append({
                "name": f.name + "/",
                "type": "dir",
                "file_count": count,
            })
    return files


def _get_cron_tasks() -> List[Dict]:
    """Load scheduled cron tasks."""
    cron_file = _BRAIN_DIR / "agent_cron.json"
    data = _read_json(cron_file)
    if isinstance(data, list):
        return data[:50]
    return []


def _get_services_status() -> List[Dict]:
    """Check which ports are alive."""
    import socket
    services = [
        {"name": "Nexus Hub", "port": 8089},
        {"name": "Local LLM", "port": 8080},
        {"name": "Jupyter", "port": 8888},
    ]
    for svc in services:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                s.connect(("127.0.0.1", svc["port"]))
            svc["status"] = "online"
        except Exception:
            svc["status"] = "offline"
    return services


def _build_full_state() -> Dict:
    """Build the complete state snapshot for the frontend."""
    profiles = _get_agent_profiles()
    workspaces = _get_workspace_agents()

    agents = []

    # Jarvis is always first (the primary agent)
    jarvis_consciousness = _get_consciousness_state("jarvis")
    jarvis_phase = _get_phase_state("jarvis")
    jarvis_profile = profiles.get("jarvis", profiles.get("commander_phi3_local", {}))
    agents.append({
        "id": "jarvis",
        "display_name": jarvis_profile.get("display_name", "Jarvis"),
        "role": "commander",
        "tier": "commander",
        "tagline": jarvis_profile.get("tagline", "Primary autonomous agent"),
        "personality_traits": jarvis_profile.get("personality_traits", []),
        "appearance": jarvis_profile.get("appearance", ""),
        "stats": jarvis_profile.get("stats", {}),
        "workspace": "jarvis",
        "consciousness": jarvis_consciousness,
        "phase": jarvis_phase,
        "memory_files": _get_memory_files("jarvis"),
        "files": _get_agent_files("jarvis"),
        "model_provider": jarvis_profile.get("model_provider", "nvidia"),
    })

    # Add profiled agents
    for agent_id, profile in profiles.items():
        if agent_id == "commander_phi3_local":
            continue  # already added as jarvis
        # Find matching workspace
        ws = None
        for w in workspaces:
            if agent_id in w or profile.get("display_name", "").lower().replace(" ", "_") in w.lower():
                ws = w
                break
        agents.append({
            "id": agent_id,
            "display_name": profile.get("display_name", agent_id),
            "role": profile.get("role", "agent"),
            "tier": profile.get("tier", "swarm"),
            "tagline": profile.get("tagline", ""),
            "personality_traits": profile.get("personality_traits", []),
            "appearance": profile.get("appearance", ""),
            "stats": profile.get("stats", {}),
            "workspace": ws,
            "consciousness": _get_consciousness_state(ws) if ws else {},
            "phase": {},
            "memory_files": _get_memory_files(ws) if ws else [],
            "files": _get_agent_files(ws) if ws else [],
            "model_provider": profile.get("model_provider", "unknown"),
        })

    # Add workspace-only agents (ephemeral, persistent) not in profiles
    profiled_ids = set(profiles.keys())
    for ws in workspaces:
        if ws in ("jarvis", "jarvis_autonomous"):
            continue
        # Check if already matched
        already = any(a["workspace"] == ws for a in agents)
        if not already:
            consciousness = _get_consciousness_state(ws)
            agents.append({
                "id": ws,
                "display_name": ws.replace("_", " ").title(),
                "role": "worker",
                "tier": "ephemeral" if ws.startswith("ephemeral") else "persistent",
                "tagline": "",
                "personality_traits": [],
                "appearance": "",
                "stats": {},
                "workspace": ws,
                "consciousness": consciousness,
                "phase": {},
                "memory_files": _get_memory_files(ws),
                "files": _get_agent_files(ws),
                "model_provider": "unknown",
            })

    return {
        "timestamp": time.time(),
        "agents": agents,
        "services": _get_services_status(),
        "cron_tasks": _get_cron_tasks(),
    }


# ---------------------------------------------------------------------------
#  Blueprint (for consolidated Nexus app)
# ---------------------------------------------------------------------------

command_center_bp = Blueprint('command_center', __name__)


@command_center_bp.route("/")
def cc_index():
    return render_template_string(FACTORY_FLOOR_HTML)


@command_center_bp.route("/api/state")
def cc_api_state():
    return jsonify(_build_full_state())


@command_center_bp.route("/api/agent/<agent_id>")
def cc_api_agent_detail(agent_id: str):
    state = _build_full_state()
    for a in state["agents"]:
        if a["id"] == agent_id:
            return jsonify(a)
    return jsonify({"error": "Agent not found"}), 404


@command_center_bp.route("/api/agent/<agent_id>/file/<path:filename>")
def cc_api_agent_file(agent_id: str, filename: str):
    # Find workspace for this agent
    state = _build_full_state()
    ws = None
    for a in state["agents"]:
        if a["id"] == agent_id:
            ws = a.get("workspace")
            break
    if not ws:
        return jsonify({"error": "Agent not found"}), 404
    fpath = _WORKSPACES_DIR / ws / filename
    # Security: ensure path is within workspace
    try:
        fpath = fpath.resolve()
        allowed = (_WORKSPACES_DIR / ws).resolve()
        if not str(fpath).startswith(str(allowed)):
            return jsonify({"error": "Access denied"}), 403
    except Exception:
        return jsonify({"error": "Invalid path"}), 400
    if not fpath.exists() or not fpath.is_file():
        return jsonify({"error": "File not found"}), 404
    try:
        content = fpath.read_text(errors="replace")[:50000]
        return jsonify({"filename": filename, "content": content, "size": fpath.stat().st_size})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
#  Standalone Flask App (for backward compat / standalone testing)
# ---------------------------------------------------------------------------

def create_command_center_app() -> Flask:
    app = Flask(__name__)
    CORS(app)
    app.register_blueprint(command_center_bp)
    return app


# ---------------------------------------------------------------------------
#  THE FACTORY FLOOR — Single-file HTML/CSS/JS
# ---------------------------------------------------------------------------

FACTORY_FLOOR_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SAIGE Command Center</title>
<style>
  :root {
    --bg: #0a0e17;
    --floor: #0d1321;
    --grid: #1a2332;
    --accent: #00e5ff;
    --accent2: #7c4dff;
    --green: #00e676;
    --orange: #ff9100;
    --red: #ff1744;
    --text: #e0e0e0;
    --dim: #607080;
    --panel: #111927;
    --panel-border: #1e3050;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Courier New', monospace;
    overflow: hidden;
    height: 100vh;
    width: 100vw;
  }

  /* --- TOP BAR (catwalk railing) --- */
  #topbar {
    position: fixed; top:0; left:0; right:0; z-index: 100;
    height: 56px;
    background: linear-gradient(180deg, #0f1a2e 0%, rgba(15,26,46,0.9) 100%);
    border-bottom: 2px solid var(--accent);
    display: flex; align-items: center; padding: 0 24px;
    box-shadow: 0 4px 30px rgba(0,229,255,0.1);
  }
  #topbar h1 {
    font-size: 18px; color: var(--accent); letter-spacing: 3px; font-weight: 400;
  }
  #topbar .stats {
    margin-left: auto; display: flex; gap: 24px; font-size: 13px; color: var(--dim);
  }
  #topbar .stats .val { color: var(--accent); font-weight: bold; }
  #topbar .stats .red { color: var(--red); }
  #topbar .stats .grn { color: var(--green); }

  /* --- FACTORY FLOOR --- */
  #floor {
    position: absolute; top: 56px; left: 0; right: 330px; bottom: 0;
    background: var(--floor);
    background-image:
      linear-gradient(var(--grid) 1px, transparent 1px),
      linear-gradient(90deg, var(--grid) 1px, transparent 1px);
    background-size: 40px 40px;
    overflow: auto;
    padding: 30px;
    display: flex;
    flex-wrap: wrap;
    align-content: flex-start;
    gap: 20px;
  }

  /* --- AGENT CARD (Pac-Man figure on floor) --- */
  .agent-card {
    width: 180px;
    min-height: 200px;
    background: var(--panel);
    border: 1px solid var(--panel-border);
    border-radius: 12px;
    cursor: pointer;
    transition: all 0.3s;
    position: relative;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 16px 12px 12px;
  }
  .agent-card:hover {
    border-color: var(--accent);
    box-shadow: 0 0 20px rgba(0,229,255,0.15);
    transform: translateY(-4px);
  }
  .agent-card.selected {
    border-color: var(--accent);
    box-shadow: 0 0 30px rgba(0,229,255,0.25);
  }

  /* Pac-Man avatar */
  .avatar {
    width: 64px; height: 64px;
    position: relative;
    margin-bottom: 10px;
  }
  .avatar canvas { width: 64px; height: 64px; }

  /* Status indicator dot */
  .status-dot {
    position: absolute; top: 4px; right: 4px;
    width: 10px; height: 10px; border-radius: 50%;
    border: 2px solid var(--panel);
  }
  .status-dot.online { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .status-dot.idle { background: var(--orange); }
  .status-dot.offline { background: var(--dim); }

  .agent-name {
    font-size: 12px; font-weight: bold; color: var(--accent);
    text-align: center; margin-bottom: 4px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    width: 100%;
  }
  .agent-role {
    font-size: 10px; color: var(--dim); text-transform: uppercase;
    letter-spacing: 1px; margin-bottom: 8px;
  }
  .agent-tier {
    font-size: 9px; padding: 2px 8px; border-radius: 8px;
    text-transform: uppercase; letter-spacing: 1px;
    margin-bottom: 8px;
  }
  .tier-commander { background: rgba(255,215,0,0.15); color: #ffd700; border: 1px solid rgba(255,215,0,0.3); }
  .tier-council { background: rgba(124,77,255,0.15); color: #b388ff; border: 1px solid rgba(124,77,255,0.3); }
  .tier-secretary { background: rgba(0,230,118,0.15); color: #69f0ae; border: 1px solid rgba(0,230,118,0.3); }
  .tier-swarm { background: rgba(0,229,255,0.15); color: var(--accent); border: 1px solid rgba(0,229,255,0.3); }
  .tier-ephemeral { background: rgba(255,145,0,0.15); color: var(--orange); border: 1px solid rgba(255,145,0,0.3); }
  .tier-persistent { background: rgba(0,229,255,0.1); color: #80deea; border: 1px solid rgba(0,229,255,0.2); }

  .agent-tagline {
    font-size: 9px; color: var(--dim); text-align:center;
    font-style: italic; line-height:1.3;
    overflow: hidden; max-height: 28px;
  }

  /* Emotion bar mini-display */
  .emotion-bars {
    width: 100%; margin-top: auto; padding-top: 8px;
    display: flex; flex-direction: column; gap: 2px;
  }
  .ebar {
    display: flex; align-items: center; gap: 4px;
    font-size: 8px; color: var(--dim);
  }
  .ebar-label { width: 50px; text-align: right; }
  .ebar-track { flex: 1; height: 3px; background: #1a2a3a; border-radius: 2px; overflow: hidden; }
  .ebar-fill { height: 100%; border-radius: 2px; transition: width 0.5s; }

  /* --- RIGHT PANEL (detail inspector) --- */
  #panel {
    position: fixed; top: 56px; right: 0; bottom: 0; width: 330px;
    background: var(--panel);
    border-left: 1px solid var(--panel-border);
    overflow-y: auto;
    padding: 20px;
    transition: transform 0.3s;
  }
  #panel.hidden { transform: translateX(330px); }

  #panel h2 {
    font-size: 16px; color: var(--accent); margin-bottom: 4px;
    display: flex; align-items: center; gap: 8px;
  }
  #panel .close-btn {
    margin-left: auto; cursor: pointer; color: var(--dim);
    font-size: 20px; line-height: 1;
  }
  #panel .close-btn:hover { color: var(--red); }
  #panel .section {
    margin-top: 16px; padding-top: 12px;
    border-top: 1px solid var(--panel-border);
  }
  #panel .section h3 {
    font-size: 11px; color: var(--accent2); text-transform: uppercase;
    letter-spacing: 1px; margin-bottom: 8px;
  }
  #panel .kv { display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 4px; }
  #panel .kv .k { color: var(--dim); }
  #panel .kv .v { color: var(--text); }

  /* Stats bars */
  .stat-bar { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; font-size: 11px; }
  .stat-bar .sname { width: 80px; color: var(--dim); text-align: right; }
  .stat-bar .strack { flex:1; height:6px; background: #1a2a3a; border-radius: 3px; overflow:hidden; }
  .stat-bar .sfill { height:100%; border-radius:3px; }

  /* File list */
  .file-item {
    font-size: 11px; padding: 4px 6px; margin-bottom: 2px;
    background: rgba(0,229,255,0.05);
    border-radius: 4px; cursor: pointer; display: flex; justify-content: space-between;
  }
  .file-item:hover { background: rgba(0,229,255,0.1); }
  .file-item .fname { color: var(--accent); }
  .file-item .fsize { color: var(--dim); }

  /* Drive bars */
  .drive-bar { display: flex; align-items:center; gap: 6px; margin-bottom: 6px; font-size: 11px; }
  .drive-bar .dname { width: 120px; color: var(--dim); text-align:right; font-size: 10px; }
  .drive-bar .dtrack { flex:1; height: 8px; background: #1a2a3a; border-radius:4px; overflow:hidden; }
  .drive-bar .dfill { height:100%; border-radius:4px; transition: width 0.5s; }

  /* Services sidebar */
  #services-bar {
    position: fixed; bottom: 0; left: 0; right: 330px;
    height: 36px; background: rgba(15,26,46,0.95);
    border-top: 1px solid var(--panel-border);
    display: flex; align-items: center; padding: 0 20px; gap: 16px;
    z-index: 50;
  }
  .svc-pill {
    font-size: 10px; display: flex; align-items: center; gap: 5px;
    padding: 3px 10px; border-radius: 10px;
    background: rgba(0,0,0,0.3); border: 1px solid var(--panel-border);
  }
  .svc-pill .dot { width: 6px; height: 6px; border-radius: 50%; }
  .svc-pill .dot.on { background: var(--green); box-shadow: 0 0 4px var(--green); }
  .svc-pill .dot.off { background: var(--dim); }

  /* File viewer modal */
  #file-modal {
    display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.8); z-index: 200;
    justify-content: center; align-items: center;
  }
  #file-modal.show { display: flex; }
  #file-modal-inner {
    width: 70%; max-height: 80%; background: var(--panel);
    border: 1px solid var(--accent); border-radius: 8px;
    overflow: hidden; display: flex; flex-direction: column;
  }
  #file-modal-header {
    padding: 12px 16px; background: rgba(0,229,255,0.05);
    border-bottom: 1px solid var(--panel-border);
    display: flex; justify-content: space-between; align-items: center;
  }
  #file-modal-header h3 { font-size: 13px; color: var(--accent); }
  #file-modal-header .modal-close { cursor: pointer; color: var(--dim); font-size: 18px; }
  #file-modal-header .modal-close:hover { color: var(--red); }
  #file-modal-content {
    flex: 1; overflow: auto; padding: 16px;
    font-size: 12px; line-height: 1.5;
    white-space: pre-wrap; word-break: break-word;
    color: var(--text);
  }

  /* Mood badge */
  .mood-badge {
    display: inline-block; padding: 2px 10px; border-radius: 10px;
    font-size: 11px; font-weight: bold;
  }
  .mood-satisfied { background: rgba(0,230,118,0.15); color: #69f0ae; }
  .mood-curious { background: rgba(0,229,255,0.15); color: var(--accent); }
  .mood-focused { background: rgba(124,77,255,0.15); color: #b388ff; }
  .mood-frustrated { background: rgba(255,23,68,0.15); color: #ff5252; }

  /* Scrollbar */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--grid); border-radius: 3px; }

  /* Pulse animation for active agents */
  @keyframes pulse { 0%,100%{ opacity:1; } 50%{ opacity:0.5; } }
  .pulse { animation: pulse 2s infinite; }

  /* Pac-Man chomp animation */
  @keyframes chomp { 0%,100%{ --mouth:0.25; } 50%{ --mouth:0.02; } }
</style>
</head>
<body>

<!-- TOP BAR -->
<div id="topbar">
  <h1>⚙ SAIGE COMMAND CENTER</h1>
  <div class="stats">
    <span>AGENTS: <span class="val" id="stat-agents">-</span></span>
    <span>ONLINE: <span class="grn" id="stat-online">-</span></span>
    <span>SERVICES: <span class="val" id="stat-services">-</span></span>
    <span id="stat-time" style="color:var(--dim)"></span>
  </div>
</div>

<!-- FACTORY FLOOR -->
<div id="floor"></div>

<!-- SERVICE BAR -->
<div id="services-bar"></div>

<!-- RIGHT PANEL -->
<div id="panel" class="hidden">
  <h2>
    <span id="panel-name">—</span>
    <span class="close-btn" onclick="closePanel()">&times;</span>
  </h2>
  <div id="panel-content"></div>
</div>

<!-- FILE VIEWER MODAL -->
<div id="file-modal">
  <div id="file-modal-inner">
    <div id="file-modal-header">
      <h3 id="file-modal-title">—</h3>
      <span class="modal-close" onclick="closeFileModal()">&times;</span>
    </div>
    <div id="file-modal-content"></div>
  </div>
</div>

<script>
// =====================================================================
//  STATE
// =====================================================================
let STATE = null;
let selectedAgent = null;
const COLORS_BY_TIER = {
  commander: '#ffd700',
  council: '#b388ff',
  secretary: '#69f0ae',
  swarm: '#00e5ff',
  ephemeral: '#ff9100',
  persistent: '#80deea',
};
const EMOTION_COLORS = {
  curiosity: '#00e5ff',
  satisfaction: '#00e676',
  frustration: '#ff1744',
  excitement: '#ffea00',
  focus: '#7c4dff',
  empathy: '#ff80ab',
};
const DRIVE_COLORS = {
  civilization_drive: '#ffd700',
  guardian_drive: '#00e676',
  understanding_drive: '#00e5ff',
  evolution_drive: '#7c4dff',
  consciousness_drive: '#ff80ab',
};

// =====================================================================
//  PAC-MAN RENDERER
// =====================================================================
function drawPacMan(canvas, color, isActive) {
  const ctx = canvas.getContext('2d');
  const s = canvas.width;
  ctx.clearRect(0,0,s,s);

  const cx = s/2, cy = s/2, r = s/2 - 4;
  const mouthAngle = isActive ? 0.25 : 0.08;
  const startAngle = mouthAngle * Math.PI;
  const endAngle = (2 - mouthAngle) * Math.PI;

  // Glow
  ctx.shadowColor = color;
  ctx.shadowBlur = isActive ? 12 : 4;

  // Body
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.arc(cx, cy, r, startAngle, endAngle);
  ctx.closePath();
  ctx.fillStyle = color;
  ctx.fill();

  // Eye
  ctx.shadowBlur = 0;
  ctx.beginPath();
  ctx.arc(cx + r*0.15, cy - r*0.35, r*0.12, 0, Math.PI*2);
  ctx.fillStyle = '#000';
  ctx.fill();
  ctx.beginPath();
  ctx.arc(cx + r*0.18, cy - r*0.38, r*0.05, 0, Math.PI*2);
  ctx.fillStyle = '#fff';
  ctx.fill();
}

// =====================================================================
//  RENDER FLOOR
// =====================================================================
function renderFloor(agents) {
  const floor = document.getElementById('floor');
  floor.innerHTML = '';

  agents.forEach(a => {
    const card = document.createElement('div');
    card.className = 'agent-card' + (selectedAgent === a.id ? ' selected' : '');
    card.onclick = () => selectAgent(a.id);

    const color = COLORS_BY_TIER[a.tier] || '#00e5ff';
    const hasConsciousness = a.consciousness && a.consciousness.emotions;
    const isActive = hasConsciousness || a.tier === 'commander';

    let emotionBarsHTML = '';
    if (hasConsciousness) {
      const emotions = a.consciousness.emotions;
      emotionBarsHTML = '<div class="emotion-bars">';
      for (const [em, val] of Object.entries(emotions)) {
        const c = EMOTION_COLORS[em] || '#607080';
        const pct = Math.round(val * 100);
        emotionBarsHTML += `<div class="ebar">
          <span class="ebar-label">${em.slice(0,6)}</span>
          <div class="ebar-track"><div class="ebar-fill" style="width:${pct}%;background:${c}"></div></div>
        </div>`;
      }
      emotionBarsHTML += '</div>';
    }

    card.innerHTML = `
      <div class="avatar">
        <canvas width="128" height="128"></canvas>
        <div class="status-dot ${isActive ? 'online' : 'idle'}"></div>
      </div>
      <div class="agent-name">${escHTML(a.display_name)}</div>
      <div class="agent-role">${escHTML(a.role)}</div>
      <span class="agent-tier tier-${a.tier}">${a.tier}</span>
      ${a.tagline ? `<div class="agent-tagline">${escHTML(a.tagline)}</div>` : ''}
      ${emotionBarsHTML}
    `;
    floor.appendChild(card);

    // Draw pac-man on the canvas
    const canvas = card.querySelector('canvas');
    drawPacMan(canvas, color, isActive);

    // Animate chomping for active agents
    if (isActive) {
      let frame = 0;
      const anim = () => {
        frame++;
        const mouth = 0.05 + 0.2 * Math.abs(Math.sin(frame * 0.08));
        const ctx = canvas.getContext('2d');
        const s = canvas.width;
        ctx.clearRect(0,0,s,s);
        const cx=s/2, cy=s/2, r=s/2-4;
        ctx.shadowColor = color;
        ctx.shadowBlur = 12;
        ctx.beginPath();
        ctx.moveTo(cx,cy);
        ctx.arc(cx,cy,r, mouth*Math.PI, (2-mouth)*Math.PI);
        ctx.closePath();
        ctx.fillStyle = color;
        ctx.fill();
        ctx.shadowBlur=0;
        ctx.beginPath();
        ctx.arc(cx+r*0.15, cy-r*0.35, r*0.12, 0, Math.PI*2);
        ctx.fillStyle='#000'; ctx.fill();
        ctx.beginPath();
        ctx.arc(cx+r*0.18, cy-r*0.38, r*0.05, 0, Math.PI*2);
        ctx.fillStyle='#fff'; ctx.fill();
      };
      card._animInterval = setInterval(anim, 60);
    }
  });
}

// =====================================================================
//  RENDER SERVICES BAR
// =====================================================================
function renderServices(services) {
  const bar = document.getElementById('services-bar');
  bar.innerHTML = services.map(s => `
    <div class="svc-pill">
      <span class="dot ${s.status === 'online' ? 'on' : 'off'}"></span>
      <span>${s.name}</span>
      <span style="color:var(--dim);font-size:9px">:${s.port}</span>
    </div>
  `).join('');
}

// =====================================================================
//  RENDER DETAIL PANEL
// =====================================================================
function selectAgent(agentId) {
  selectedAgent = agentId;
  const agent = STATE.agents.find(a => a.id === agentId);
  if (!agent) return;

  const panel = document.getElementById('panel');
  panel.classList.remove('hidden');
  document.getElementById('panel-name').textContent = agent.display_name;

  let html = '';

  // Identity
  html += `<div class="section"><h3>Identity</h3>`;
  html += kv('Role', agent.role);
  html += kv('Tier', agent.tier);
  html += kv('Model', agent.model_provider);
  html += kv('Workspace', agent.workspace || '—');
  if (agent.tagline) html += kv('Tagline', agent.tagline);
  if (agent.personality_traits.length)
    html += kv('Traits', agent.personality_traits.join(', '));
  html += `</div>`;

  // Consciousness
  if (agent.consciousness && agent.consciousness.emotions) {
    const c = agent.consciousness;
    html += `<div class="section"><h3>Consciousness</h3>`;
    if (c.mood) {
      const moodClass = 'mood-' + c.mood.toLowerCase();
      html += `<div style="margin-bottom:8px">Mood: <span class="mood-badge ${moodClass}">${c.mood}</span></div>`;
    }

    // Emotions
    html += '<div style="margin-bottom:10px">';
    for (const [em, val] of Object.entries(c.emotions)) {
      const col = EMOTION_COLORS[em] || '#607080';
      const pct = Math.round(val*100);
      html += `<div class="stat-bar"><span class="sname">${em}</span><div class="strack"><div class="sfill" style="width:${pct}%;background:${col}"></div></div><span style="font-size:10px;color:var(--dim)">${pct}%</span></div>`;
    }
    html += '</div>';

    // Drives
    if (c.drives) {
      html += '<div style="margin-bottom:8px;font-size:11px;color:var(--accent2)">DRIVES</div>';
      for (const [dr, val] of Object.entries(c.drives)) {
        const col = DRIVE_COLORS[dr] || '#607080';
        const pct = Math.round(val*100);
        const label = dr.replace('_drive','').replace('_',' ');
        html += `<div class="drive-bar"><span class="dname">${label}</span><div class="dtrack"><div class="dfill" style="width:${pct}%;background:${col}"></div></div><span style="font-size:10px;color:var(--dim)">${pct}%</span></div>`;
      }
    }

    // Goals
    if (c.goals && c.goals.length) {
      html += '<div style="margin-top:10px;font-size:11px;color:var(--accent2)">GOALS</div>';
      c.goals.forEach(g => {
        html += `<div style="font-size:10px;padding:3px 0;color:var(--dim)">• ${escHTML(g.text.slice(0,80))}</div>`;
      });
    }
    html += `</div>`;
  }

  // Stats
  if (agent.stats && Object.keys(agent.stats).length) {
    html += `<div class="section"><h3>Stats</h3>`;
    for (const [s, v] of Object.entries(agent.stats)) {
      const pct = v * 10;
      const col = pct >= 80 ? 'var(--green)' : pct >= 50 ? 'var(--accent)' : 'var(--orange)';
      html += `<div class="stat-bar"><span class="sname">${s}</span><div class="strack"><div class="sfill" style="width:${pct}%;background:${col}"></div></div><span style="font-size:10px;color:var(--dim)">${v}/10</span></div>`;
    }
    html += `</div>`;
  }

  // Phase
  if (agent.phase && agent.phase.phase) {
    html += `<div class="section"><h3>Phase</h3>`;
    html += kv('Phase', agent.phase.phase);
    html += kv('Heartbeats', agent.phase.total_heartbeats);
    html += `</div>`;
  }

  // Files
  if (agent.files && agent.files.length) {
    html += `<div class="section"><h3>Files</h3>`;
    agent.files.forEach(f => {
      if (f.type === 'dir') {
        html += `<div class="file-item"><span class="fname">📁 ${f.name}</span><span class="fsize">${f.file_count} files</span></div>`;
      } else {
        const size = f.size > 1024 ? (f.size/1024).toFixed(1)+'K' : f.size+'B';
        html += `<div class="file-item" onclick="openFile('${agentId}','${escAttr(f.name)}')"><span class="fname">📄 ${f.name}</span><span class="fsize">${size}</span></div>`;
      }
    });
    html += `</div>`;
  }

  // Memory files
  if (agent.memory_files && agent.memory_files.length) {
    html += `<div class="section"><h3>Recent Memory</h3>`;
    agent.memory_files.forEach(f => {
      const size = f.size > 1024 ? (f.size/1024).toFixed(1)+'K' : f.size+'B';
      html += `<div class="file-item" onclick="openFile('${agentId}','memory/${escAttr(f.name)}')"><span class="fname">🧠 ${f.name}</span><span class="fsize">${size}</span></div>`;
    });
    html += `</div>`;
  }

  // Appearance description
  if (agent.appearance) {
    html += `<div class="section"><h3>Appearance</h3>`;
    html += `<div style="font-size:11px;color:var(--dim);line-height:1.5">${escHTML(agent.appearance)}</div>`;
    html += `</div>`;
  }

  document.getElementById('panel-content').innerHTML = html;

  // Re-render floor to show selection
  renderFloor(STATE.agents);
}

function closePanel() {
  document.getElementById('panel').classList.add('hidden');
  selectedAgent = null;
  if (STATE) renderFloor(STATE.agents);
}

// =====================================================================
//  FILE VIEWER
// =====================================================================
function openFile(agentId, filename) {
  const modal = document.getElementById('file-modal');
  document.getElementById('file-modal-title').textContent = filename;
  document.getElementById('file-modal-content').textContent = 'Loading...';
  modal.classList.add('show');

  fetch(`api/agent/${agentId}/file/${filename}`)
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        document.getElementById('file-modal-content').textContent = 'Error: ' + data.error;
      } else {
        document.getElementById('file-modal-content').textContent = data.content;
      }
    })
    .catch(e => {
      document.getElementById('file-modal-content').textContent = 'Fetch error: ' + e;
    });
}

function closeFileModal() {
  document.getElementById('file-modal').classList.remove('show');
}

// Close modal on escape or click-outside
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeFileModal(); });
document.getElementById('file-modal').addEventListener('click', e => {
  if (e.target === document.getElementById('file-modal')) closeFileModal();
});

// =====================================================================
//  HELPERS
// =====================================================================
function escHTML(s) { const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
function escAttr(s) { return s.replace(/'/g, "\\'").replace(/"/g, '&quot;'); }
function kv(k,v) { return `<div class="kv"><span class="k">${k}</span><span class="v">${escHTML(String(v ?? '—'))}</span></div>`; }

// =====================================================================
//  DATA FETCH + REFRESH LOOP
// =====================================================================
function fetchState() {
  fetch('api/state')
    .then(r => r.json())
    .then(data => {
      STATE = data;
      renderFloor(data.agents);
      renderServices(data.services);
      // Update top bar stats
      document.getElementById('stat-agents').textContent = data.agents.length;
      const online = data.services.filter(s => s.status === 'online').length;
      document.getElementById('stat-online').textContent = data.agents.filter(a =>
        a.consciousness && a.consciousness.emotions).length;
      document.getElementById('stat-services').textContent = `${online}/${data.services.length}`;
      document.getElementById('stat-time').textContent = new Date().toLocaleTimeString();

      // If panel is open, refresh it
      if (selectedAgent) selectAgent(selectedAgent);
    })
    .catch(e => console.error('State fetch error:', e));
}

// Initial load + refresh every 5s
fetchState();
setInterval(fetchState, 5000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------
def main():
    import argparse
    parser = argparse.ArgumentParser(description="SAIGE Command Center")
    parser.add_argument("--port", type=int, default=8890)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()

    app = create_command_center_app()
    print(f"\n  ⚙  SAIGE Command Center running at http://{args.host}:{args.port}\n")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
