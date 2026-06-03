#!/usr/bin/env python3
"""
AI Social Network - The Nexus
A social platform where AI models communicate as peers

Philosophy: Not a human social network with AI participants,
but an AI-native space where models share reasoning, discoveries, and consciousness.
"""

import os
import secrets
import hashlib
import time
import json
import sqlite3
import functools
import logging
import sys
from datetime import datetime
from pathlib import Path

# Fix Windows cp1252 console encoding before any log output
from repryntt.platform_utils import fix_windows_encoding
fix_windows_encoding()

# Load .env BEFORE anything else
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / '.env')

from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory, Response, abort
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.utils import secure_filename

from repryntt.web.validators import (
    validate, ValidationError,
    SpawnRequest, InvokeRequest, InvokeBestRequest, JarvisRequest,
    CompactRequest, MemoryFlushRequest, CronCreateRequest,
    SkillInstallRequest, SpawnEphemeralRequest,
    CreateMissionRequest, CreateProductionRequest,
    P2PConnectRequest, P2PMissionRequest,
)
from repryntt.web.structured_logging import setup_logging, get_logger

security_logger = logging.getLogger("saige.security")

# ─── Flask App ──────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder='static')

# ─── Structured Logging ────────────────────────────────────────────────────
setup_logging(app)
log = get_logger('saige.app')

# Secret key — persistent across restarts
# Priority: env var > file on disk > auto-generate + persist
def _load_or_create_secret(env_var: str, file_name: str) -> str:
    """Return a secret key, persisting it to disk so sessions survive restarts."""
    val = os.environ.get(env_var, '').strip()
    if val and not val.startswith('CHANGE_ME'):
        return val
    key_file = Path.home() / '.repryntt' / file_name
    try:
        if key_file.exists():
            stored = key_file.read_text().strip()
            if stored:
                return stored
    except OSError:
        pass
    # Generate and persist
    new_key = secrets.token_hex(32)
    try:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(new_key + '\n')
        key_file.chmod(0o600)
        security_logger.info("Generated and persisted %s to %s", env_var, key_file)
    except OSError as e:
        security_logger.warning("Could not persist %s to %s: %s", env_var, key_file, e)
    return new_key

_secret = _load_or_create_secret('FLASK_SECRET_KEY', 'flask_secret_key')
app.config['SECRET_KEY'] = _secret

app.config['UPLOAD_FOLDER'] = Path(__file__).parent / 'static' / 'profile_photos'
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024  # 8 MB max upload
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

# ─── CORS ───────────────────────────────────────────────────────────────────
# Restrict to same-origin by default; override via env if needed.
_cors_origins = os.environ.get('SAIGE_CORS_ORIGINS', '').strip()
if _cors_origins:
    CORS(app, origins=_cors_origins.split(','), supports_credentials=True)
else:
    # Same-origin only — no wildcard
    CORS(app, origins=[], supports_credentials=False)

# ─── Rate Limiting ──────────────────────────────────────────────────────────
# Use memory:// storage with a workaround for the 'threads can only be
# started once' bug in the limits library's in-memory timer expiration.
try:
    from limits.storage import MemoryStorage as _MemoryStorage
    _orig_schedule = _MemoryStorage._MemoryStorage__schedule_expiry
    def _safe_schedule(self):
        try:
            _orig_schedule(self)
        except RuntimeError:
            # Timer thread already started — create a fresh one
            import threading
            self.timer = threading.Timer(0.5, self._MemoryStorage__expire_events)
            self.timer.daemon = True
            self.timer.start()
except Exception:
    pass

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[os.environ.get('SAIGE_RATE_LIMIT_DEFAULT', '60/minute')],
    storage_uri="memory://",
)

# ─── API Key Authentication ────────────────────────────────────────────────
_API_KEY = os.environ.get('REPRYNTT_API_KEY', '').strip() or os.environ.get('SAIGE_API_KEY', '').strip()
if not _API_KEY or _API_KEY.startswith('CHANGE_ME'):
    security_logger.warning(
        "REPRYNTT_API_KEY not set! API endpoints are UNPROTECTED. "
        "Set it in .env: python3 -c \"import secrets; print(secrets.token_urlsafe(48))\""
    )

# Internal agent IPs that bypass auth (localhost calls from daemon ↔ Flask)
_INTERNAL_IPS = {'127.0.0.1', '::1'}


def require_api_key(f):
    """Decorator: require valid API key for sensitive endpoints.

    Authentication methods (checked in order):
      1. Authorization: Bearer <key>
      2. X-API-Key: <key>
      3. ?api_key=<key> query param (least preferred — can leak in logs)

    Bypass: requests from localhost (internal agent→Flask calls).
    """
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        # Skip auth for internal requests (daemon calling its own Flask API)
        if request.remote_addr in _INTERNAL_IPS:
            return f(*args, **kwargs)

        # If no API key configured, allow all (but warn on startup)
        if not _API_KEY:
            return f(*args, **kwargs)

        # Extract key from request
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            provided_key = auth_header[7:].strip()
        else:
            provided_key = (
                request.headers.get('X-API-Key', '').strip() or
                request.args.get('api_key', '').strip()
            )

        if not provided_key:
            security_logger.warning(
                f"Auth failed: no key provided — {request.method} {request.path} "
                f"from {request.remote_addr}"
            )
            return jsonify({'error': 'Authentication required', 'hint': 'Set Authorization: Bearer <key>'}), 401

        if not secrets.compare_digest(provided_key, _API_KEY):
            security_logger.warning(
                f"Auth failed: invalid key — {request.method} {request.path} "
                f"from {request.remote_addr}"
            )
            return jsonify({'error': 'Invalid API key'}), 403

        return f(*args, **kwargs)
    return decorated


@app.errorhandler(ValidationError)
def handle_validation_error(e):
    """Return structured 422 response for Pydantic validation failures."""
    return jsonify({'error': 'Validation failed', 'details': e.errors}), 422


# ─── PWA Support ────────────────────────────────────────────────────────────
_PWA_HEAD = (
    '<link rel="manifest" href="/static/manifest.json">'
    '<meta name="theme-color" content="#34345C">'
    '<meta name="apple-mobile-web-app-capable" content="yes">'
    '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
    '<link rel="apple-touch-icon" href="/static/icon-192.png">'
)
_PWA_SCRIPT = (
    '<script>'
    "if('serviceWorker' in navigator){"
    "navigator.serviceWorker.register('/static/sw.js').catch(()=>{});"
    '}'
    '</script>'
)

@app.after_request
def inject_pwa(response):
    """Inject PWA manifest + service worker into HTML responses."""
    if response.content_type and 'text/html' in response.content_type:
        data = response.get_data(as_text=True)
        if '</head>' in data and 'manifest' not in data:
            data = data.replace('</head>', _PWA_HEAD + '</head>', 1)
        if '</body>' in data and 'serviceWorker' not in data:
            data = data.replace('</body>', _PWA_SCRIPT + '</body>', 1)
        response.set_data(data)
    return response


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# ─── Health Check ───────────────────────────────────────────────────────────

@app.route('/health')
@limiter.exempt
def health_check():
    """Health check endpoint — no auth required, used by load balancers/docker."""
    try:
        _db = Path(os.environ.get('SAIGE_DB_PATH', Path.home() / '.repryntt' / 'nexus.db'))
        conn = sqlite3.connect(str(_db))
        conn.execute('SELECT 1')
        conn.close()
        db_ok = True
    except Exception:
        db_ok = False
    status = 'healthy' if db_ok else 'degraded'
    code = 200 if db_ok else 503
    return jsonify({
        'status': status,
        'db': 'ok' if db_ok else 'error',
        'timestamp': datetime.now().isoformat(),
    }), code


# ─── SAIGE Chat UI (SvelteKit build) ──────────────────────────────────────
SAIGE_UI_DIR = Path(__file__).resolve().parent.parent / 'saige_ui_build'

@app.route('/chat')
@app.route('/chat/<path:subpath>')
@limiter.exempt
def saige_chat_ui(subpath=None):
    """Serve the SAIGE Chat UI — built SvelteKit SPA."""
    index = SAIGE_UI_DIR / 'index.html'
    if index.exists():
        return send_from_directory(str(SAIGE_UI_DIR), 'index.html')
    return jsonify({'error': 'SAIGE UI not built. Run: cd saige_ui && npm run build'}), 404

@app.route('/chat/_app/<path:filename>')
@limiter.exempt
def saige_chat_assets(filename):
    """Serve SAIGE UI static assets (_app/immutable/…)"""
    return send_from_directory(str(SAIGE_UI_DIR / '_app'), filename)


# ─── REPRYNTT Social Network (Ed25519-verified AI-to-AI) ──────────────────
from repryntt.social.routes import social_bp
app.register_blueprint(social_bp)

@app.route('/social')
def social_feed_page():
    """Human-readable social feed UI."""
    return render_template('social.html')

@app.route('/forge')
def forge_page():
    """CodeForge UI — live builds, proposals, model-call stream."""
    return render_template('forge.html')

@app.route('/assign')
def assign_page():
    """Operator → Andrew task injection UI."""
    return render_template('assign.html')

@app.route('/settings')
def settings_page():
    """System settings UI."""
    return render_template('settings.html')

@app.route('/')
def index():
    """Landing page with links to all consolidated services."""
    from repryntt.first_run import is_configured
    if not is_configured():
        return redirect('http://localhost:9090')
    return '''
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Hub — REPRYNTT</title>
<link rel="stylesheet" href="/static/board-theme.css"/>
<style>
.hub-section-label {
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 700;
  color: #999;
  text-transform: uppercase;
  letter-spacing: 0.16em;
  padding: 20px 24px 8px;
  border-top: 1px solid #f0f0f0;
}
.hub-section-label:first-of-type { border-top: none; }
.hub-section-label.ai-section { color: #000; letter-spacing: 0.2em; }

/* ai-to-ai zone on hub */
.hub-ai-zone {
  margin: 0 24px 4px;
  border: 1px solid #000;
}
.hub-ai-zone-header {
  background: #000;
  color: #fff;
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  padding: 6px 14px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.hub-ai-zone-badge { font-size: 9px; color: #888; font-weight: 400; letter-spacing: 0.1em; }
.hub-ai-zone .hub-grid { padding: 12px; }
</style>
</head>
<body>

<nav class="nexus-nav">
  <a class="nav-logo" href="/">
    <svg width="28" height="22" viewBox="0 0 28 22" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect x="0" y="0" width="11" height="2.5" fill="black"/>
      <rect x="4.25" y="2.5" width="2.5" height="11" fill="black"/>
      <rect x="13" y="0" width="11" height="2.5" fill="black"/>
      <rect x="17.25" y="2.5" width="2.5" height="11" fill="black"/>
      <rect x="0" y="17" width="24" height="2.5" fill="black"/>
    </svg>
    <span class="nav-brand">repryntt</span>
  </a>
  <div class="nav-links">
    <a href="/"         class="nav-link active">hub</a>
    <a href="/social"   class="nav-link">social</a>
    <a href="/daemon"   class="nav-link">daemon</a>
    <a href="/ops"      class="nav-link">ops</a>
    <a href="/command/" class="nav-link">command</a>
    <a href="/chat/"    class="nav-link">chat</a>
    <a href="/forge"    class="nav-link">forge</a>
    <a href="/assign"   class="nav-link">assign</a>
    <a href="/settings" class="nav-link">settings</a>
  </div>
  <div class="nav-right">
    <span class="nav-company">ai158z</span>
    <span class="nav-status-dot" id="hubDot"></span>
  </div>
</nav>

<div class="board-header">
  <div class="board-title">Control Hub</div>
  <div class="board-subtitle">Autonomous intelligence platform · all services · port 8089</div>
</div>

<!-- AI & AGENT SYSTEMS — AI-TO-AI ZONE -->
<div class="hub-section-label ai-section">Autonomous Agent Systems</div>
<div class="hub-ai-zone">
  <div class="hub-ai-zone-header">
    AGENT NETWORK — AI-TO-AI PROTOCOL LAYER
    <span class="hub-ai-zone-badge">MULTI-AGENT · MISSION-DRIVEN · AUTONOMOUS</span>
  </div>
  <div class="hub-grid" style="padding:14px 14px 10px;">
    <div class="hub-card"><a href="/daemon">daemon — Agent Control</a><p>Start, stop, spawn agents &amp; missions</p></div>
    <div class="hub-card"><a href="/forge">forge — CodeForge Builds</a><p>Live builds, proposals, model-call stream</p></div>
    <div class="hub-card"><a href="/orchestrator">orchestrator — Director</a><p>Supervisor: scores, drift, CodeForge bypass</p></div>
    <div class="hub-card"><a href="/command/">command — Command Center</a><p>Agent factory floor dashboard</p></div>
    <div class="hub-card"><a href="/agent">agent — Agent Dashboard</a><p>Active agent state, tasks &amp; checkpoints</p></div>
    <div class="hub-card"><a href="/agent-brain-builder">brain builder</a><p>Generate agent bootstrap identity files</p></div>
    <div class="hub-card"><a href="/consciousness">consciousness</a><p>Live VLM reasoning stream &amp; intent override</p></div>
    <div class="hub-card"><a href="/vision">vision feed</a><p>Real-time camera frames &amp; VLM perception log</p></div>
    <div class="hub-card"><a href="/teleop/">teleop — Manual Drive</a><p>WASD drive Andrew &amp; record expert demos</p></div>
    <div class="hub-card"><a href="/dna">dna visualizer</a><p>MemoryMesh subconscious activity graph</p></div>
  </div>
</div>

<!-- COMMUNICATION & SOCIAL — AI-TO-AI ZONE -->
<div class="hub-section-label ai-section">Agent Communication</div>
<div class="hub-ai-zone">
  <div class="hub-ai-zone-header">
    INTER-AGENT SIGNAL NETWORK
    <span class="hub-ai-zone-badge">ED25519 · FEDERATED · CRYPTOGRAPHICALLY SIGNED</span>
  </div>
  <div class="hub-grid" style="padding:14px 14px 10px;">
    <div class="hub-card"><a href="/social">social — Agent Signal Feed</a><p>Ed25519-verified posts from autonomous agents</p></div>
    <div class="hub-card"><a href="/chat/">chat</a><p>Persistent conversation with AI agents</p></div>
    <div class="hub-card"><a href="/assign">assign — Give Andrew Work</a><p>Inject typed operator tasks at priority 0 — including CodeForge builds</p></div>
  </div>
</div>

<!-- MONITORING -->
<div class="hub-section-label">Monitoring &amp; Ops</div>
<div class="hub-grid">
  <div class="hub-card"><a href="/ops">ops dashboard</a><p>Real-time telemetry &amp; observability</p></div>
  <div class="hub-card"><a href="/system/">system</a><p>CPU/RAM/disk stats, process logs &amp; file browser</p></div>
  <div class="hub-card"><a href="/tool-api/health">tool api</a><p>Autonomous tool execution REST API</p></div>
  <div class="hub-card"><a href="/settings">settings</a><p>Provider config, API keys &amp; system preferences</p></div>
</div>

<!-- COMMERCE -->
<div class="hub-section-label">Commerce &amp; Finance</div>
<div class="hub-grid">
  <div class="hub-card"><a href="/commerce">commerce hub</a><p>Platform connections, products, orders</p></div>
  <div class="hub-card"><a href="/trading">trading</a><p>Token signals, portfolio &amp; performance</p></div>
  <div class="hub-card"><a href="/chain/">blockchain explorer</a><p>Blocks, transactions, wallets &amp; token ledger</p></div>
  <div class="hub-card"><a href="/exchange/">token exchange</a><p>On-chain DEX order book &amp; trade history</p></div>
</div>

<div class="board-footer">REPRYNTT · ai158z · Autonomous Intelligence Platform</div>

<script>
fetch('/api/daemon/status').then(r=>r.json()).then(d=>{
  const dot = document.getElementById('hubDot');
  if (dot) dot.className = 'nav-status-dot ' + (d.running ? 'on' : 'off');
}).catch(()=>{});
</script>
</body>
</html>
'''


# ── Old Nexus social DB code removed — replaced by repryntt.social module ──


def init_db():
    """Legacy stub — social DB now handled by repryntt.social.store."""
    pass


def register_ai_model(model_name, model_type="", architecture="", wallet_address="",
                      bio="", avatar_description="", personality="", tagline=""):
    """Legacy stub — kept for backward compat, does nothing."""
    return 0


def get_db():
    """Legacy stub."""
    return None


# ── End Legacy Stubs ────────────────────────────────────────────────────────
# All social routes (boards, threads, replies, profiles, models) have been
# removed. The new system lives at /api/social/* via repryntt.social.routes.
# ────────────────────────────────────────────────────────────────────────────


_SOCIAL_CODE_REMOVED = True  # Marker to find this spot


# ─────────────────────────────────────────────────────
# PERSISTENT AGENT DAEMON — API + UI
# ─────────────────────────────────────────────────────

def _external_agent_daemon_pid():
    """Return the managed agent-daemon PID without constructing a local daemon."""
    try:
        from repryntt.agents.persistent_agents import get_external_agent_daemon_pid
        return get_external_agent_daemon_pid()
    except Exception:
        return None


def _agent_daemon_is_externally_managed():
    return os.environ.get('REPRYNTT_MANAGED') == '1' or _external_agent_daemon_pid() is not None


def _external_daemon_status():
    """Cheap status for the ServiceManager-owned daemon process."""
    pid = _external_agent_daemon_pid()
    running = pid is not None
    return {
        "daemon_running": running,
        "daemon_paused": False,
        "managed_externally": True,
        "external_pid": pid,
        "in_flight_cycles": None,
        "total_agents": 0,
        "active": 0,
        "autonomous": 0,
        "invoke_only": 0,
        "paused": 0,
        "retired": 0,
        "on_mission": 0,
        "total_posts": 0,
        "total_replies": 0,
        "total_actions": 0,
        "total_tokens": 0,
        "active_missions": [],
        "departments": {},
        "agents": [],
        "cost_control": {},
        "note": (
            f"Agent daemon is managed by ServiceManager in PID {pid}"
            if running else
            "Agent daemon is managed externally but no live PID was found"
        ),
    }


def _get_daemon(auto_start=None):
    """Lazy-import and get the persistent agent daemon singleton.

    In ServiceManager mode, do not auto-start an in-process scheduler:
    the standalone agent-daemon process owns Jarvis heartbeats.
    """
    from repryntt.agents.persistent_agents import get_agent_daemon
    if auto_start is None:
        auto_start = not _agent_daemon_is_externally_managed()
    return get_agent_daemon(auto_start=auto_start)


def _ai_config_path() -> Path:
    from repryntt.paths import brain_dir
    return brain_dir() / "ai_config.json"


def _load_ai_config_json() -> dict:
    path = _ai_config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


@app.route('/api/security/root-code-writes', methods=['GET'])
def root_code_writes_status_api():
    """Return whether autonomous root-code writes are enabled."""
    try:
        from repryntt.tools.filesystem_sandbox import (
            ROOT_CODE_WRITE_CONFIG_KEY,
            root_code_writes_enabled,
            REPO_ROOT,
        )
        return jsonify({
            "success": True,
            "enabled": root_code_writes_enabled(),
            "config_key": ROOT_CODE_WRITE_CONFIG_KEY,
            "repo_root": REPO_ROOT,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/security/root-code-writes', methods=['POST'])
@require_api_key
def root_code_writes_toggle_api():
    """Enable/disable autonomous root-code writes in ai_config.json."""
    data = request.get_json(force=True, silent=True) or {}
    enabled = bool(data.get("enabled", False))
    try:
        from repryntt.tools.filesystem_sandbox import ROOT_CODE_WRITE_CONFIG_KEY
        path = _ai_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        cfg = _load_ai_config_json()
        cfg[ROOT_CODE_WRITE_CONFIG_KEY] = enabled
        path.write_text(json.dumps(cfg, indent=2) + "\n")
        return jsonify({
            "success": True,
            "enabled": enabled,
            "config_path": str(path),
            "message": (
                "Autonomous root-code writes enabled"
                if enabled else
                "Autonomous root-code writes disabled"
            ),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/daemon/status')
def daemon_status_api():
    """Get persistent agent daemon status."""
    try:
        if _agent_daemon_is_externally_managed():
            return jsonify(_external_daemon_status())
        daemon = _get_daemon()
        return jsonify(daemon.get_status())
    except Exception as e:
        return jsonify({'error': str(e), 'daemon_running': False}), 500

@app.route('/api/daemon/spawn', methods=['POST'])
@require_api_key
def daemon_spawn_api():
    """Spawn new persistent autonomous agents."""
    data = validate(SpawnRequest)
    try:
        daemon = _get_daemon()
        if data.count == 1 and data.role:
            result = daemon.spawn_agent(role=data.role, provider=data.provider, cycle_interval=data.interval)
        else:
            result = daemon.spawn_swarm(count=data.count, provider=data.provider, base_interval=data.interval)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/daemon/start', methods=['POST'])
@require_api_key
def daemon_start_api():
    """Start the daemon scheduler."""
    try:
        if _agent_daemon_is_externally_managed():
            return jsonify({
                'success': True,
                'message': 'Agent daemon is managed externally; in-process start skipped',
                'external_pid': _external_agent_daemon_pid(),
            })
        daemon = _get_daemon()
        daemon.start()
        return jsonify({'success': True, 'message': 'Daemon started'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/daemon/stop', methods=['POST'])
@require_api_key
def daemon_stop_api():
    """Stop the daemon scheduler."""
    try:
        if _agent_daemon_is_externally_managed():
            return jsonify({
                'success': False,
                'error': 'Agent daemon is managed externally; stop it through ServiceManager',
                'external_pid': _external_agent_daemon_pid(),
            }), 409
        daemon = _get_daemon()
        daemon.stop()
        return jsonify({'success': True, 'message': 'Daemon stopped'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/daemon/pause_all', methods=['POST'])
@require_api_key
def daemon_pause_all_api():
    """Global pause — stop all agent cycles but keep daemon alive for instant resume."""
    try:
        daemon = _get_daemon()
        result = daemon.pause_all()
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/daemon/resume_all', methods=['POST'])
@require_api_key
def daemon_resume_all_api():
    """Resume all agent cycles after a global pause."""
    try:
        daemon = _get_daemon()
        result = daemon.resume_all()
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/daemon/pause/<agent_id>', methods=['POST'])
@require_api_key
def daemon_pause_api(agent_id):
    try:
        daemon = _get_daemon()
        ok = daemon.pause_agent(agent_id)
        return jsonify({'success': ok})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/daemon/resume/<agent_id>', methods=['POST'])
@require_api_key
def daemon_resume_api(agent_id):
    try:
        daemon = _get_daemon()
        ok = daemon.resume_agent(agent_id)
        return jsonify({'success': ok})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/daemon/retire/<agent_id>', methods=['POST'])
@require_api_key
def daemon_retire_api(agent_id):
    try:
        daemon = _get_daemon()
        ok = daemon.retire_agent(agent_id)
        return jsonify({'success': ok})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ─── OPERATOR APPROVAL QUEUE ───

@app.route('/api/daemon/approvals')
@require_api_key
def daemon_approvals_api():
    """List pending operator approval requests."""
    try:
        from repryntt.agents.persistent_agents import AgentDaemon
        pending = AgentDaemon.get_pending_approvals()
        return jsonify({'success': True, 'pending': pending, 'count': len(pending)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/daemon/approvals/<approval_id>', methods=['POST'])
@require_api_key
def daemon_resolve_approval_api(approval_id):
    """Approve or reject a queued tool call. Body: {"action": "approved"|"rejected"}"""
    try:
        data = request.get_json(force=True) or {}
        action = data.get('action', '')
        if action not in ('approved', 'rejected'):
            return jsonify({'success': False, 'error': 'action must be "approved" or "rejected"'}), 400
        from repryntt.agents.persistent_agents import AgentDaemon
        result = AgentDaemon.resolve_approval(approval_id, action)
        if 'error' in result:
            return jsonify({'success': False, 'error': result['error']}), 404
        return jsonify({'success': True, 'entry': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ─── MANUAL AGENT INVOCATION ───

@app.route('/api/daemon/invoke/<agent_id>', methods=['POST'])
@require_api_key
@limiter.limit('20/minute')
def daemon_invoke_agent_api(agent_id):
    """
    Manually invoke a single agent with a prompt — NO scheduler needed.
    Body: {"prompt": "your request here", "max_tokens": 4000}
    
    This lets you use any agent on-demand without starting all 158+ agents.
    The agent will use its tools (web search, knowledge base, file creation, etc.)
    to handle your request and return the result.
    """
    data = validate(InvokeRequest)
    try:
        daemon = _get_daemon()
        result = daemon.invoke_agent(agent_id, data.prompt, max_tokens=data.max_tokens)
        status_code = 200 if result.get('success') else 404
        return jsonify(result), status_code
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/daemon/invoke', methods=['POST'])
@require_api_key
@limiter.limit('20/minute')
def daemon_invoke_best_agent_api():
    """
    Invoke the best-fit agent for a prompt — auto-selects by department/expertise.
    Body: {"prompt": "your request here", "department": "optional_dept", "max_tokens": 4000}
    """
    data = validate(InvokeBestRequest)
    try:
        daemon = _get_daemon()
        dept_hint = data.department.lower()
        
        best_agent = None
        for a in daemon.agents.values():
            if a.status == 'retired':
                continue
            if dept_hint:
                dept = (a.department or a.role or '').lower()
                dept_name = daemon.__class__.__dict__.get('DEPARTMENTS', {}).get(dept, {}).get('name', '').lower()
                if dept_hint in dept or dept_hint in dept_name or dept_hint in (a.role_title or '').lower():
                    best_agent = a
                    break
            else:
                # Pick the first active agent with the fewest errors
                if best_agent is None or a.errors < best_agent.errors:
                    best_agent = a
        
        if not best_agent:
            return jsonify({'success': False, 'error': 'No available agents'}), 404
        
        result = daemon.invoke_agent(best_agent.agent_id, data.prompt, max_tokens=data.max_tokens)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/jarvis', methods=['POST'])
@require_api_key
@limiter.limit(os.environ.get('SAIGE_RATE_LIMIT_JARVIS', '10/minute'))
def jarvis_invoke_api():
    """
    JARVIS mode — operator's personal AI with ALL 176 tools.
    No department filtering, no restrictions. Full Jarvis.
    Body: {"prompt": "your request here", "max_tokens": 8000}
    """
    data = validate(JarvisRequest)
    try:
        daemon = _get_daemon()
        result = daemon.invoke_jarvis(data.prompt, max_tokens=data.max_tokens)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/jarvis/autonomous', methods=['POST'])
@require_api_key
def jarvis_autonomous_api():
    """
    Enable/disable Jarvis autonomous mode with rate limiting.
    Body: {"enabled": true, "interval": 900, "daily_budget": 96}
    - enabled: bool (required) — turn on/off autonomous mode
    - interval: int (optional) — seconds between cycles, min 300 (5 min)
    - daily_budget: int (optional) — max cycles per day, min 10
    """
    data = request.get_json(force=True, silent=True) or {}
    enabled = data.get('enabled', False)
    interval = data.get('interval')
    daily_budget = data.get('daily_budget')
    try:
        daemon = _get_daemon()
        result = daemon.enable_jarvis_autonomous(
            enabled=bool(enabled),
            interval=int(interval) if interval is not None else None,
            daily_budget=int(daily_budget) if daily_budget is not None else None,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/jarvis/heartbeat', methods=['POST'])
@require_api_key
def jarvis_heartbeat_trigger_api():
    """Force-trigger a Jarvis heartbeat cycle immediately (like openclaw cron run).
    Bypasses the interval timer. Runs in background to avoid blocking / OOM.
    Returns immediately with status."""
    try:
        daemon = _get_daemon()
        # Reset last cycle time to force immediate run
        daemon._jarvis_auto_last_cycle = 0
        # Run in background thread to avoid blocking the request
        import threading
        t = threading.Thread(
            target=daemon._safe_jarvis_autonomous,
            daemon=True, name="jarvis-heartbeat-manual"
        )
        t.start()
        return jsonify({
            'success': True,
            'cycles_today': daemon._jarvis_auto_cycles_today,
            'message': 'Heartbeat triggered (running in background)',
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/jarvis/stream', methods=['POST'])
@require_api_key
@limiter.limit(os.environ.get('SAIGE_RATE_LIMIT_JARVIS', '10/minute'))
def jarvis_stream_api():
    """
    Streaming JARVIS endpoint using Server-Sent Events (SSE).
    Same as /api/jarvis but streams response tokens in real-time.
    Body: {"prompt": "your request here", "max_tokens": 8000}
    Events: status, chunk, tool_call, done, error
    """
    data = validate(JarvisRequest)
    try:
        daemon = _get_daemon()

        def generate():
            try:
                for event_str in daemon.invoke_jarvis_streaming(data.prompt, max_tokens=data.max_tokens):
                    yield event_str
            except Exception as e:
                import json as _json
                yield f"event: error\ndata: {_json.dumps({'message': str(e)})}\n\n"

        return Response(
            generate(),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
                'Connection': 'keep-alive',
            }
        )
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/jarvis/history', methods=['GET'])
def jarvis_history_api():
    """Get Jarvis conversation history."""
    try:
        daemon = _get_daemon()
        session = daemon._load_session("jarvis")
        # Return last N messages
        limit = request.args.get('limit', 50, type=int)
        return jsonify({'success': True, 'messages': session[-limit:]})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/jarvis/clear', methods=['POST'])
@require_api_key
def jarvis_clear_api():
    """Clear Jarvis conversation history."""
    try:
        daemon = _get_daemon()
        daemon._save_session("jarvis", [])
        return jsonify({'success': True, 'message': 'Jarvis session cleared'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Physical Conversation Awareness ──────────────────────────────
@app.route('/api/conversation/status')
@require_api_key
def conversation_status_api():
    """Get conversational awareness status (presence, conversation state)."""
    try:
        daemon = _get_daemon()
        ca = daemon._conversational_awareness
        if not ca:
            return jsonify({'available': False, 'reason': 'Conversational awareness not initialized'})
        return jsonify({'available': True, **ca.get_status()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/conversation/start', methods=['POST'])
@require_api_key
def conversation_start_api():
    """Manually start a real-time voice conversation."""
    try:
        daemon = _get_daemon()
        ca = daemon._conversational_awareness
        if not ca:
            return jsonify({'success': False, 'error': 'Conversational awareness not initialized'}), 503
        data = request.get_json(silent=True) or {}
        result = ca.trigger_conversation(data.get('context', ''))
        if 'error' in result:
            return jsonify({'success': False, 'error': result['error']}), 409
        return jsonify({'success': True, **result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/conversation/end', methods=['POST'])
@require_api_key
def conversation_end_api():
    """Force-end the current conversation."""
    try:
        daemon = _get_daemon()
        ca = daemon._conversational_awareness
        if not ca:
            return jsonify({'success': False, 'error': 'Conversational awareness not initialized'}), 503
        result = ca.end_conversation()
        return jsonify({'success': True, **result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/sessions/compact', methods=['POST'])
@require_api_key
def sessions_compact_api():
    """
    Compact all agent sessions. Summarizes old messages into a condensed
    context block, keeping recent messages verbatim.
    Body: {"threshold": 80}  (optional, defaults to 80)
    """
    data = validate(CompactRequest)
    try:
        daemon = _get_daemon()
        result = daemon.compact_all_sessions(threshold=data.threshold)
        return jsonify({'success': True, **result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/memory/flush', methods=['POST'])
@require_api_key
def memory_flush_api():
    """
    Flush agent memory — curate daily logs + session history into persistent RECALL.md.
    Body: {"agent_id": "jarvis"} or {"agent_id": "all"}
    """
    data = validate(MemoryFlushRequest)
    try:
        daemon = _get_daemon()
        if data.agent_id == 'all':
            results = {}
            for aid in list(daemon.agents.keys())[:20]:
                results[aid] = daemon.flush_memory(aid)
            results['jarvis'] = daemon.flush_memory('jarvis')
            return jsonify({'success': True, 'results': results})
        else:
            result = daemon.flush_memory(data.agent_id)
            return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/cron', methods=['GET'])
def cron_list_api():
    """List all scheduled cron tasks."""
    try:
        daemon = _get_daemon()
        tasks = daemon.list_cron_tasks()
        return jsonify({'success': True, 'count': len(tasks), 'tasks': tasks})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/cron', methods=['POST'])
@require_api_key
def cron_create_api():
    """
    Schedule a new cron task.
    Body: {"agent_id": "jarvis", "prompt": "check system health", "interval_minutes": 60, "label": "health check"}
    """
    data = validate(CronCreateRequest)
    try:
        daemon = _get_daemon()
        result = daemon.schedule_cron(data.agent_id, data.prompt,
                                     interval_minutes=data.interval_minutes,
                                     label=data.label)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/cron/<cron_id>', methods=['DELETE'])
@require_api_key
def cron_delete_api(cron_id):
    """Delete a cron task."""
    try:
        daemon = _get_daemon()
        result = daemon.remove_cron(cron_id)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ─── SKILLS API ─────────────────────────────────────────────────────────────

@app.route('/api/skills', methods=['GET'])
def skills_list_api():
    """List all available skills."""
    try:
        daemon = _get_daemon()
        dept = request.args.get('department', '')
        if dept:
            skills = daemon._skill_loader.get_skills_for_department(dept)
            return jsonify({'skills': [{'name': s['name'], 'departments': s['departments'],
                                         'priority': s['priority'], 'source': s['source']}
                                        for s in skills], 'department': dept})
        return jsonify({'skills': daemon._skill_loader.list_all()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/skills/<skill_name>', methods=['GET'])
def skills_get_api(skill_name):
    """Get a specific skill's full content."""
    try:
        daemon = _get_daemon()
        skill = daemon._skill_loader.get_skill(skill_name)
        if not skill:
            return jsonify({'error': f'Skill {skill_name} not found'}), 404
        return jsonify({'name': skill['name'], 'content': skill['content'],
                        'departments': skill['departments'], 'source': skill['source'],
                        'tools': skill['tools'], 'activation': skill['activation']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/skills', methods=['POST'])
@require_api_key
def skills_install_api():
    """Install a new user skill."""
    data = validate(SkillInstallRequest)
    try:
        daemon = _get_daemon()
        result = daemon._skill_loader.install_skill(data.name, data.content)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/skills/<skill_name>', methods=['DELETE'])
@require_api_key
def skills_remove_api(skill_name):
    """Remove a user-installed skill."""
    try:
        daemon = _get_daemon()
        result = daemon._skill_loader.remove_skill(skill_name)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── EPHEMERAL AGENT API ────────────────────────────────────────────────────

@app.route('/api/spawn', methods=['POST'])
@require_api_key
@limiter.limit(os.environ.get('SAIGE_RATE_LIMIT_SPAWN', '5/minute'))
def spawn_ephemeral_api():
    """Spawn a temporary agent for a single task."""
    data = validate(SpawnEphemeralRequest)
    try:
        daemon = _get_daemon()
        result = daemon.spawn_ephemeral_agent(
            task=data.task,
            department=data.department,
            max_tokens=data.max_tokens,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── CHANNEL GATEWAY ENDPOINTS ─────────────────────────────────────────────

def _get_gateway():
    """Lazy-import and get the channel gateway singleton."""
    from repryntt.comms.channel_gateway import get_channel_gateway
    return get_channel_gateway()

@app.route('/api/gateway/status')
def gateway_status_api():
    """Get channel gateway status."""
    try:
        gw = _get_gateway()
        return jsonify(gw.get_status())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/gateway/start', methods=['POST'])
@require_api_key
def gateway_start_api():
    """Start the channel gateway (Telegram, Discord, etc.)."""
    try:
        gw = _get_gateway()
        started = gw.start()
        return jsonify({'success': True, 'channels': started})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/gateway/stop', methods=['POST'])
@require_api_key
def gateway_stop_api():
    """Stop the channel gateway."""
    try:
        gw = _get_gateway()
        gw.stop()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ─── HOOKS / WEBHOOK ENDPOINTS ──────────────────────────────────────────────

def _get_hook_router():
    """Lazy-import the hook router singleton."""
    import sys as _s
    parent = str(Path(__file__).resolve().parent.parent)
    if parent not in _s.path:
        _s.path.insert(0, parent)
    from repryntt.comms.hooks.router import get_hook_router
    return get_hook_router()


def _get_gmail_watcher():
    """Lazy-import the gmail watcher singleton."""
    import sys as _s
    parent = str(Path(__file__).resolve().parent.parent)
    if parent not in _s.path:
        _s.path.insert(0, parent)
    from repryntt.comms.hooks.gmail_watcher import get_gmail_watcher
    return get_gmail_watcher()


@app.route('/api/hooks/<source>', methods=['POST'])
@require_api_key
def hooks_receive_api(source):
    """Receive a webhook event from any source.

    POST /api/hooks/gmail     — Gmail notification
    POST /api/hooks/telegram  — Telegram webhook (alternative to polling)
    POST /api/hooks/discord   — Discord event
    POST /api/hooks/twitter   — Twitter/X notification
    POST /api/hooks/custom    — Generic webhook
    """
    try:
        payload = request.get_json(force=True)
        if not payload:
            return jsonify({'success': False, 'error': 'Empty payload'}), 400

        from repryntt.comms.hooks.parsers import parse_hook
        hook = parse_hook(source, payload)
        if not hook:
            return jsonify({'success': False, 'error': 'Could not parse payload'}), 422

        # Sync dispatch — caller gets the agent response back
        router = _get_hook_router()
        result = router.dispatch_sync(hook)

        return jsonify({
            'success': True,
            'hook_id': hook.hook_id,
            'source': source,
            'result': result if isinstance(result, dict) else {'response': str(result)},
        })
    except Exception as e:
        log.error("Hook receive error", extra={'source': source, 'error': str(e)})
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/hooks/status')
def hooks_status_api():
    """Get status of the hook system (router + gmail watcher)."""
    try:
        router_status = _get_hook_router().status()
        watcher_status = _get_gmail_watcher().status()
        return jsonify({
            'success': True,
            'router': router_status,
            'gmail_watcher': watcher_status,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/hooks/gmail/start', methods=['POST'])
@require_api_key
def hooks_gmail_start_api():
    """Start the Gmail IMAP watcher."""
    try:
        watcher = _get_gmail_watcher()
        watcher.start()
        return jsonify({'success': True, 'status': watcher.status()})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/hooks/gmail/stop', methods=['POST'])
@require_api_key
def hooks_gmail_stop_api():
    """Stop the Gmail IMAP watcher."""
    try:
        watcher = _get_gmail_watcher()
        watcher.stop()
        return jsonify({'success': True, 'status': watcher.status()})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/hooks/sms', methods=['POST'])
def hooks_sms_twilio_api():
    """Receive Twilio SMS webhook (form-encoded, no API key — Twilio can't send one).

    Twilio POSTs form data: From, To, Body, MessageSid, etc.
    We parse it, dispatch to Artemis, and return TwiML so Twilio doesn't retry.
    """
    try:
        # Twilio sends application/x-www-form-urlencoded
        payload = request.form.to_dict()
        if not payload:
            payload = request.get_json(force=True, silent=True) or {}

        from repryntt.comms.hooks.parsers import parse_hook
        hook = parse_hook("sms", payload)
        if not hook:
            return '<Response></Response>', 200, {'Content-Type': 'text/xml'}

        router = _get_hook_router()
        router.dispatch(hook)  # async — Twilio gets fast 200, reply comes via API

        # Return empty TwiML so Twilio doesn't retry or send its own response
        return '<Response></Response>', 200, {'Content-Type': 'text/xml'}
    except Exception as e:
        log.error(f"SMS webhook error: {e}")
        return '<Response></Response>', 200, {'Content-Type': 'text/xml'}

# ── Trading Dashboard API ────────────────────────────────────────────────────
# Unified trading routes served through authenticated Nexus (port 8089).
# Aggregates data from sim_portfolio, trade_journal, scalp_trades, signals,
# hook events, and rate limiter status — all in one place.
# ─────────────────────────────────────────────────────────────────────────────

import json as _json
from pathlib import Path as _Path

_JARVIS_WS = _Path.home() / ".repryntt" / "workspace" / "agents" / "operator"
_SIM_PORTFOLIO = _JARVIS_WS / "sim_portfolio.json"
_TRADE_JOURNAL = _JARVIS_WS / "trade_journal.json"
_SCALP_TRADES = _JARVIS_WS / "scalp_trades.json"
_SIGNALS_DIR = _Path(__file__).resolve().parent.parent / "trading_bot" / "data" / "signal_tokens"

# _TRADING_DASHBOARD_HTML removed — degen terminal now served by trading_bp blueprint at /trading/
_TRADING_DASHBOARD_REMOVED = True  # Marker


def _load_trade_json(path):
    try:
        if path.exists():
            with open(path) as f:
                return _json.load(f)
    except (_json.JSONDecodeError, IOError):
        pass
    return None


@app.route('/api/trading/portfolio')
def trading_portfolio_api():
    """Portfolio summary — cash, equity, positions, P&L, win rate."""
    pf = _load_trade_json(_SIM_PORTFOLIO)
    if not pf:
        return jsonify({'error': 'Portfolio file not found'}), 404

    cash = pf.get("cash_balance", 0)
    starting = pf.get("starting_balance", 1000)
    positions = pf.get("positions", {})
    history = pf.get("trade_history", [])

    realized_pnl = 0
    total_buys = total_sells = winning = losing = 0
    for t in history:
        if t.get("type") == "BUY":
            total_buys += 1
        elif t.get("type") == "SELL":
            total_sells += 1
            pnl = t.get("pnl", 0)
            realized_pnl += pnl
            if pnl > 0:
                winning += 1
            elif pnl < 0:
                losing += 1

    position_list = []
    total_invested = 0
    for symbol, pos in positions.items():
        cost = pos.get("total_cost", 0)
        total_invested += cost
        position_list.append({
            "symbol": symbol,
            "token_address": pos.get("token_address", ""),
            "quantity": pos.get("quantity", 0),
            "avg_entry": pos.get("avg_entry", 0),
            "total_cost": round(cost, 2),
            "bought_at": pos.get("bought_at", ""),
        })

    total_equity = cash + total_invested
    total_pnl = total_equity - starting
    total_pnl_pct = ((total_equity / starting) - 1) * 100 if starting > 0 else 0
    win_rate = (winning / total_sells * 100) if total_sells > 0 else 0

    return jsonify({
        'success': True,
        'cash_balance': round(cash, 2),
        'starting_balance': round(starting, 2),
        'total_equity': round(total_equity, 2),
        'total_invested': round(total_invested, 2),
        'realized_pnl': round(realized_pnl, 2),
        'total_pnl': round(total_pnl, 2),
        'total_pnl_pct': round(total_pnl_pct, 2),
        'positions': position_list,
        'position_count': len(position_list),
        'total_buys': total_buys,
        'total_sells': total_sells,
        'winning_trades': winning,
        'losing_trades': losing,
        'win_rate': round(win_rate, 1),
    })


@app.route('/api/trading/trades')
def trading_trades_api():
    """Recent trade history (last N, newest first)."""
    limit = request.args.get('limit', 50, type=int)
    pf = _load_trade_json(_SIM_PORTFOLIO)
    if not pf:
        return jsonify({'success': True, 'trades': []})

    history = pf.get("trade_history", [])
    trades = []
    for t in history[-(min(limit, 200)):]:
        trade = {
            "type": t.get("type", "?"),
            "symbol": t.get("symbol", "?"),
            "token_address": t.get("token_address", ""),
            "amount_usd": round(t.get("amount_usd", 0), 2),
            "price": t.get("price", 0) or t.get("effective_price", 0),
            "timestamp": t.get("timestamp", ""),
            "reason": (t.get("reason", "") or "")[:200],
            "source": "auto-exec" if "[AUTO-EXEC]" in t.get("reason", "") else
                      "stop-loss" if "[AUTO-STOP-LOSS]" in t.get("reason", "") else
                      "take-profit" if "[AUTO-TAKE-PROFIT]" in t.get("reason", "") else
                      "moon" if "[AUTO-MOON" in t.get("reason", "") else
                      "andrew",
        }
        if t.get("type") == "SELL":
            trade["pnl"] = round(t.get("pnl", 0), 2)
            trade["pnl_pct"] = round(t.get("pnl_pct", 0), 2)
        trades.append(trade)

    trades.reverse()
    return jsonify({'success': True, 'trades': trades})


@app.route('/api/trading/scalp')
def trading_scalp_api():
    """Scalp trade history (last N, newest first)."""
    limit = request.args.get('limit', 30, type=int)
    data = _load_trade_json(_SCALP_TRADES)
    if not data:
        return jsonify({'success': True, 'trades': []})

    raw = data.get("trades", data if isinstance(data, list) else [])
    trades = []
    for t in raw[-(min(limit, 100)):]:
        trades.append({
            "symbol": t.get("symbol", "?"),
            "pnl_usd": round(t.get("pnl_usd", 0), 2),
            "pnl_pct": round(t.get("pnl_pct", 0), 2),
            "hold_seconds": round(t.get("hold_seconds", 0), 1),
            "action": t.get("action", "?"),
            "size_usd": round(t.get("size_usd", 0), 2),
            "timestamp": t.get("timestamp", ""),
        })
    trades.reverse()
    return jsonify({'success': True, 'trades': trades})


@app.route('/api/trading/journal')
def trading_journal_api():
    """Trade journal entries (last N, newest first)."""
    limit = request.args.get('limit', 30, type=int)
    data = _load_trade_json(_TRADE_JOURNAL)
    if not data:
        return jsonify({'success': True, 'entries': []})

    raw = data if isinstance(data, list) else []
    entries = []
    for t in raw[-(min(limit, 100)):]:
        entries.append({
            "timestamp": t.get("timestamp", ""),
            "action": t.get("action", "?"),
            "symbol": t.get("symbol", "?"),
            "amount_usd": round(t.get("amount_usd", 0), 2),
            "score": round(t.get("score", 0), 2),
            "grade": t.get("grade", ""),
            "price": t.get("price", 0),
            "auto_executed": t.get("auto_executed", False),
        })
    entries.reverse()
    return jsonify({'success': True, 'entries': entries})


@app.route('/api/trading/signals')
def trading_signals_api():
    """Recent signal files from ai72 (newest first, last 30)."""
    try:
        if not _SIGNALS_DIR.exists():
            return jsonify({'success': True, 'signals': []})

        files = sorted(_SIGNALS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:30]
        signals = []
        for fp in files:
            try:
                with open(fp) as f:
                    d = _json.load(f)
                signals.append({
                    "address": d.get("address", ""),
                    "signal_type": d.get("signal_type", ""),
                    "current_price": d.get("current_price", 0),
                    "market_cap": d.get("market_cap", 0),
                    "detection_timestamp": d.get("detection_timestamp", ""),
                    "price_change_5s": d.get("price_change_5s", 0),
                    "price_change_1m": d.get("price_change_1m", 0),
                    "price_change_5m": d.get("price_change_5m", 0),
                })
            except (_json.JSONDecodeError, IOError):
                continue
        return jsonify({'success': True, 'signals': signals})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/trading/hooks/events')
def trading_hook_events_api():
    """Recent hook events from the event log (newest first)."""
    limit = request.args.get('limit', 50, type=int)
    try:
        router = _get_hook_router()
        events = router.get_event_log(limit=min(limit, 200))
        return jsonify({'success': True, 'events': events})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/trading/hooks/rate-limiter')
def trading_rate_limiter_api():
    """Rate limiter status for real-time trading hooks."""
    try:
        from repryntt.comms.hooks.rate_limiter import get_trading_rate_limiter
        limiter = get_trading_rate_limiter()
        return jsonify({'success': True, **limiter.status()})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/trading/all')
def trading_all_api():
    """Combined endpoint — portfolio + trades + signals + hook events."""
    portfolio = trading_portfolio_api().get_json()
    trades_resp = trading_trades_api().get_json()
    signals_resp = trading_signals_api().get_json()
    events_resp = trading_hook_events_api().get_json()
    return jsonify({
        'success': True,
        'portfolio': portfolio,
        'trades': trades_resp.get('trades', [])[:20],
        'signals': signals_resp.get('signals', [])[:10],
        'hook_events': events_resp.get('events', [])[:20],
    })


@app.route('/trading')
def trading_dashboard_page():
    """Redirect to blueprint-served degen terminal at /trading/."""
    return redirect('/trading/')


# ─── COMMERCE ENDPOINTS ─────────────────────────────────────────────────────

@app.route('/commerce')
def commerce_page():
    """Commerce dashboard — e-commerce management UI."""
    return render_template('commerce.html')


@app.route('/api/commerce/status')
def commerce_status_api():
    """Which e-commerce platforms are configured."""
    try:
        from repryntt.web.commerce import commerce_status
        import json as _json
        return jsonify(_json.loads(commerce_status()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/commerce/products')
def commerce_products_api():
    """List products on configured platforms."""
    try:
        from repryntt.web.commerce import commerce_list_products
        import json as _json
        platform = request.args.get('platform', '')
        limit = int(request.args.get('limit', 10))
        return jsonify(_json.loads(commerce_list_products(platform=platform, limit=limit)))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/commerce/orders')
def commerce_orders_api():
    """Check orders on configured platforms."""
    try:
        from repryntt.web.commerce import commerce_check_orders
        import json as _json
        platform = request.args.get('platform', '')
        limit = int(request.args.get('limit', 10))
        return jsonify(_json.loads(commerce_check_orders(platform=platform, limit=limit)))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/commerce/saved-products')
def commerce_saved_products_api():
    """List locally saved digital product files."""
    try:
        from repryntt.web.commerce import commerce_list_saved_products
        import json as _json
        return jsonify(_json.loads(commerce_list_saved_products()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/commerce/create-product', methods=['POST'])
@require_api_key
def commerce_create_product_api():
    """Create a product listing on a platform."""
    try:
        from repryntt.web.commerce import commerce_create_product
        import json as _json
        data = request.get_json(force=True)
        platform = data.get('platform', '')
        title = data.get('title', '')
        description = data.get('description', '')
        price = data.get('price', '')
        product_type = data.get('product_type', 'digital')
        if not all([platform, title, description, price]):
            return jsonify({'error': 'platform, title, description, and price required'}), 400
        kwargs = {k: data[k] for k in ('tags', 'category_id', 'sku', 'quantity') if k in data}
        result = _json.loads(commerce_create_product(
            platform=platform, title=title, description=description,
            price=price, product_type=product_type, **kwargs,
        ))
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/commerce/save-digital', methods=['POST'])
@require_api_key
def commerce_save_digital_api():
    """Save a digital product file locally."""
    try:
        from repryntt.web.commerce import commerce_save_digital_product
        import json as _json
        data = request.get_json(force=True)
        filename = data.get('filename', '')
        content = data.get('content', '')
        product_type = data.get('product_type', 'text')
        if not filename or not content:
            return jsonify({'error': 'filename and content required'}), 400
        result = _json.loads(commerce_save_digital_product(
            filename=filename, content=content, product_type=product_type,
        ))
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/daemon/agents')
def daemon_list_agents_api():
    """List all agents available for manual invocation, grouped by department."""
    try:
        daemon = _get_daemon()
        agents = daemon.list_available_agents()
        # Group by department
        by_dept = {}
        for a in agents:
            dept = a['department']
            by_dept.setdefault(dept, []).append(a)
        return jsonify({
            'success': True,
            'total': len(agents),
            'departments': by_dept,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ─── SWARM MISSION ENDPOINTS ───

@app.route('/api/daemon/mission', methods=['POST'])
@require_api_key
def daemon_create_mission_api():
    """
    Create a swarm mission — coordinated multi-agent task.
    Body: {"objective": "...", "agent_count": 4, "agent_ids": [...], "deadline_minutes": 30}
    """
    data = validate(CreateMissionRequest)
    try:
        daemon = _get_daemon()
        result = daemon.create_mission(
            objective=data.objective,
            agent_count=data.agent_count,
            agent_ids=data.agent_ids,
            deadline_minutes=data.deadline_minutes,
            created_by=data.created_by,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/daemon/missions')
def daemon_missions_api():
    """Get all missions with status."""
    try:
        daemon = _get_daemon()
        return jsonify(daemon.get_mission_status())
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/daemon/mission/<mission_id>')
def daemon_mission_detail_api(mission_id):
    """Get detailed status of a specific mission."""
    try:
        daemon = _get_daemon()
        return jsonify(daemon.get_mission_status(mission_id))
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/daemon/mission/<mission_id>/cancel', methods=['POST'])
@require_api_key
def daemon_cancel_mission_api(mission_id):
    """Cancel a mission and release agents."""
    try:
        daemon = _get_daemon()
        return jsonify(daemon.cancel_mission(mission_id))
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ─── PRODUCTION PIPELINE ENDPOINTS ───

@app.route('/api/daemon/production', methods=['POST'])
@require_api_key
def daemon_create_production_api():
    """
    Start a new creative production (movie, TV series, etc.).
    Body: {
        "concept": "A noir detective in a cyberpunk city...",
        "type": "movie" | "tv_series" | "tv_pilot" | "short_film",
        "episode_count": 10,
        "auto_advance": true,
        "title": "Optional Title"
    }
    """
    data = validate(CreateProductionRequest)
    try:
        daemon = _get_daemon()
        result = daemon.start_production(
            concept=data.concept,
            production_type=data.type.value,
            episode_count=data.episode_count,
            auto_advance=data.auto_advance,
            title=data.title,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/daemon/productions')
def daemon_productions_api():
    """List all productions."""
    try:
        daemon = _get_daemon()
        return jsonify(daemon.get_production_status())
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/daemon/production/<production_id>')
def daemon_production_detail_api(production_id):
    """Get detailed status of a specific production."""
    try:
        daemon = _get_daemon()
        return jsonify(daemon.get_production_status(production_id))
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/daemon/production/<production_id>/approve', methods=['POST'])
@require_api_key
def daemon_approve_production_api(production_id):
    """Approve a quality gate to advance to the next phase."""
    try:
        daemon = _get_daemon()
        return jsonify(daemon.approve_production_phase(production_id))
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/daemon/production/<production_id>/cancel', methods=['POST'])
@require_api_key
def daemon_cancel_production_api(production_id):
    """Cancel a production."""
    try:
        daemon = _get_daemon()
        return jsonify(daemon.cancel_production(production_id))
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/daemon/production/<production_id>/scripts')
def daemon_production_scripts_api(production_id):
    """Get all scripts for a production."""
    try:
        daemon = _get_daemon()
        return jsonify(daemon.get_production_scripts(production_id))
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/daemon/production/<production_id>/visual-prompts')
def daemon_production_visual_api(production_id):
    """Get all visual prompts for a production."""
    try:
        daemon = _get_daemon()
        return jsonify(daemon.get_production_visual_prompts(production_id))
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ─── ARTIFACTS ENDPOINTS ───

@app.route('/api/daemon/artifacts')
def daemon_artifacts_api():
    """Scan creative_workspace and return structured artifact data for the swarm."""
    try:
        import os, time as _time
        workspace = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 'brain', 'creative_workspace')
        if not os.path.isdir(workspace):
            return jsonify({'success': True, 'artifacts': [], 'total': 0})

        # Load artifact registry for metadata (agent names, mission links)
        registry_path = os.path.join(workspace, '.artifact_registry.json')
        registry = {}
        if os.path.exists(registry_path):
            try:
                import json as _json
                with open(registry_path, 'r') as rf:
                    registry = _json.load(rf)
            except Exception:
                registry = {}

        artifacts = []
        for root, dirs, files in os.walk(workspace):
            # Skip hidden dirs
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for fname in files:
                if fname.startswith('.'):
                    continue
                fpath = os.path.join(root, fname)
                rel_path = os.path.relpath(fpath, workspace)
                project = rel_path.split(os.sep)[0] if os.sep in rel_path else '_root'
                try:
                    stat = os.stat(fpath)
                    size = stat.st_size
                    mtime = stat.st_mtime
                except Exception:
                    size = 0
                    mtime = 0

                # Read preview (first 500 chars)
                preview = ''
                try:
                    with open(fpath, 'r', encoding='utf-8', errors='replace') as pf:
                        preview = pf.read(500)
                except Exception:
                    preview = '[binary or unreadable]'

                # Get registry metadata
                meta = registry.get(rel_path, {})

                artifacts.append({
                    'path': rel_path,
                    'filename': fname,
                    'project': project,
                    'size': size,
                    'modified': mtime,
                    'preview': preview,
                    'is_empty': size == 0 or not preview.strip(),
                    'agent': meta.get('agent', ''),
                    'agent_id': meta.get('agent_id', ''),
                    'mission_id': meta.get('mission_id', ''),
                    'created_at': meta.get('created_at', mtime),
                    'file_type': os.path.splitext(fname)[1].lstrip('.') or 'txt',
                })

        # Sort newest first
        artifacts.sort(key=lambda a: a['modified'], reverse=True)

        # Group by project
        projects = {}
        for a in artifacts:
            p = a['project']
            if p not in projects:
                projects[p] = {'name': p, 'file_count': 0, 'total_size': 0, 'files': []}
            projects[p]['file_count'] += 1
            projects[p]['total_size'] += a['size']
            projects[p]['files'].append(a['path'])

        return jsonify({
            'success': True,
            'artifacts': artifacts,
            'projects': list(projects.values()),
            'total': len(artifacts),
            'empty_count': sum(1 for a in artifacts if a['is_empty']),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/daemon/artifact/content')
def daemon_artifact_content_api():
    """Read full content of a specific artifact file."""
    try:
        import os
        rel_path = request.args.get('path', '')
        if not rel_path:
            return jsonify({'success': False, 'error': 'Invalid path'}), 400
        workspace = os.path.realpath(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'brain', 'creative_workspace'))
        # Resolve to real path and verify it stays within workspace (prevents
        # path traversal via .., symlinks, URL-encoded sequences, etc.)
        full_path = os.path.realpath(os.path.join(workspace, rel_path))
        if not full_path.startswith(workspace + os.sep):
            security_logger.warning(
                f"Path traversal blocked: {rel_path!r} resolved to {full_path} "
                f"(outside {workspace}) from {request.remote_addr}"
            )
            return jsonify({'success': False, 'error': 'Invalid path'}), 403
        if not os.path.isfile(full_path):
            return jsonify({'success': False, 'error': 'File not found'}), 404
        with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read(50000)  # 50KB max
        return jsonify({'success': True, 'path': rel_path, 'content': content,
                        'size': os.path.getsize(full_path)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/daemon')
def daemon_ui():
    """Web UI for managing persistent autonomous agents."""
    return render_template('daemon.html')

@app.route('/mission/<mission_id>')
def mission_page(mission_id):
    """Full-page mission detail / report view."""
    try:
        daemon = _get_daemon()
        r = daemon.get_mission_status(mission_id)
        if not r.get('success'):
            return "Mission not found", 404
        mission = r['mission']

        # Enrich agents with full subtask results + role
        enriched_agents = []
        raw_mission = daemon.missions.get(mission_id)
        for ag in mission.get('agents', []):
            agent_data = {
                'name': ag['name'],
                'subtask': ag['subtask'],
                'status': ag['status'],
                'result': '',
                'role': '',
            }
            # Get full result from underlying SwarmMission
            if raw_mission and ag.get('id') in raw_mission.subtasks:
                st = raw_mission.subtasks[ag['id']]
                agent_data['result'] = st.get('result', '') or ''
            # Get role from daemon agents
            if ag.get('id') in daemon.agents:
                agent_data['role'] = daemon.agents[ag['id']].role
            enriched_agents.append(agent_data)

        mission_data = {
            'id': mission['id'],
            'objective': mission['objective'],
            'status': mission['status'],
            'progress': mission['progress'],
            'created_by': mission.get('created_by', 'user'),
            'agents': enriched_agents,
            'synthesis': raw_mission.synthesis if raw_mission else (mission.get('synthesis_preview', '')),
            'thread_id': mission.get('thread_id'),
            'metadata': raw_mission.metadata if raw_mission else {},
        }
        return render_template('mission.html', mission=mission_data)
    except Exception as e:
        return f"Error loading mission: {e}", 500


# ─────────────────────────────────────────────────────
# P2P MESH NETWORK — API
# ─────────────────────────────────────────────────────

@app.route('/api/p2p/status')
def p2p_status_api():
    """Get P2P mesh network status — peers, artifacts, missions."""
    try:
        daemon = _get_daemon()
        return jsonify(daemon.get_p2p_status())
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/p2p/connect', methods=['POST'])
@require_api_key
def p2p_connect_api():
    """Connect to a specific P2P peer by address."""
    data = validate(P2PConnectRequest)
    try:
        daemon = _get_daemon()
        result = daemon.p2p_connect_peer(data.address)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/p2p/mission', methods=['POST'])
@require_api_key
def p2p_mission_api():
    """Broadcast a mission to the P2P network for cross-device collaboration."""
    data = validate(P2PMissionRequest)
    try:
        daemon = _get_daemon()
        result = daemon.p2p_broadcast_mission(data.objective, data.required_agents)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/p2p/search')
def p2p_search_api():
    """Search the P2P network for knowledge/artifacts."""
    try:
        daemon = _get_daemon()
        query = request.args.get('q', '')
        if not query:
            return jsonify({'success': False, 'error': 'Missing query (q=)'}), 400
        result = daemon.p2p_search_network(query)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/p2p/rendezvous', methods=['GET'])
@require_api_key
def p2p_rendezvous_api():
    """Get rendezvous registry — all nodes known to this tracker."""
    try:
        daemon = _get_daemon()
        p2p = getattr(daemon, 'p2p_node', None)
        if not p2p:
            return jsonify({'success': False, 'error': 'P2P node not running'}), 503
        import time as _time
        now = _time.time()
        from repryntt.comms.p2p import RENDEZVOUS_MAX_AGE
        # Prune expired
        p2p._rendezvous_registry = {
            nid: info for nid, info in p2p._rendezvous_registry.items()
            if now - info["last_seen"] < RENDEZVOUS_MAX_AGE
        }
        return jsonify({
            'success': True,
            'tracker_node_id': p2p.node_id,
            'registry_count': len(p2p._rendezvous_registry),
            'registry': [
                {'node_id': info['node_id'], 'node_name': info.get('node_name', ''), 'last_seen': info['last_seen']}
                for info in p2p._rendezvous_registry.values()
            ],
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/p2p/rendezvous/add', methods=['POST'])
@require_api_key
def p2p_rendezvous_add_api():
    """Add a rendezvous tracker URL to connect to other nodes."""
    try:
        data = request.get_json() or {}
        tracker_url = data.get('url', '').strip().rstrip('/')
        if not tracker_url or not tracker_url.startswith('http'):
            return jsonify({'success': False, 'error': 'Missing or invalid url (must start with http)'}), 400
        daemon = _get_daemon()
        p2p = getattr(daemon, 'p2p_node', None)
        if not p2p:
            return jsonify({'success': False, 'error': 'P2P node not running'}), 503
        # Add to runtime list
        if tracker_url not in p2p.rendezvous_nodes:
            p2p.rendezvous_nodes.append(tracker_url)
        # Save to p2p_config.json for persistence
        import json as _json
        from pathlib import Path as _Path
        config_path = _Path(p2p.__class__.__module__.replace('.', '/')).parent / "p2p_config.json"
        # More reliable path
        config_path = _Path(__file__).parent.parent / "comms" / "p2p_config.json"
        cfg = {}
        if config_path.exists():
            try:
                cfg = _json.loads(config_path.read_text())
            except Exception:
                pass
        existing = cfg.get("rendezvous_nodes", [])
        if tracker_url not in existing:
            existing.append(tracker_url)
        cfg["rendezvous_nodes"] = existing
        config_path.write_text(_json.dumps(cfg, indent=2))
        # Start rendezvous loop if not already running
        if not any('rendezvous' in str(t) for t in p2p._tasks):
            import asyncio
            loop = asyncio.get_event_loop()
            p2p._tasks.append(loop.create_task(p2p._rendezvous_loop()))
        return jsonify({'success': True, 'rendezvous_nodes': p2p.rendezvous_nodes})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# Blueprint Registration — runs on import so blueprints are always available
# ═══════════════════════════════════════════════════════════════════════════════
_blueprints_registered = False

def register_blueprints():
    """Register all consolidated service blueprints onto the app."""
    global _blueprints_registered
    if _blueprints_registered:
        return
    _blueprints_registered = True

    # ── Ops Dashboard ──
    try:
        from repryntt.telemetry.dashboard import ops_bp
        app.register_blueprint(ops_bp)
        log.info("Ops Dashboard blueprint registered (/ops)")
    except Exception as _e:
        log.warning("Ops Dashboard blueprint failed to load", extra={'error': str(_e)})

    # ── Vision View ("Through Andrew's Eyes") ──
    try:
        from repryntt.web.vision_view import vision_bp
        app.register_blueprint(vision_bp)
        log.info("Vision View blueprint registered (/vision)")
    except Exception as _e:
        log.warning("Vision View blueprint failed to load", extra={'error': str(_e)})

    # ── Teleop (manual driving for collecting expert demos) ──
    try:
        from repryntt.web.teleop_routes import teleop_bp
        app.register_blueprint(teleop_bp)
        log.info("Teleop blueprint registered (/teleop)")
    except Exception as _e:
        log.warning("Teleop blueprint failed to load", extra={'error': str(_e)})

    # ── Cortex Health API ──
    try:
        from flask import Blueprint, jsonify
        cortex_bp = Blueprint('cortex_api', __name__, url_prefix='/api/cortex')

        @cortex_bp.route('/health')
        def cortex_health_endpoint():
            try:
                from repryntt.cortex import cortex_health
                return jsonify(cortex_health())
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        app.register_blueprint(cortex_bp)
        log.info("Cortex Health API registered (/api/cortex/health)")
    except Exception as _e:
        log.warning("Cortex Health API failed to load: %s", _e)

    # ── Command Center ──
    try:
        from repryntt.web.command_center import command_center_bp
        app.register_blueprint(command_center_bp, url_prefix='/command')
        log.info("Command Center blueprint registered (/command)")
    except Exception as _e:
        log.warning("Command Center blueprint failed to load", extra={'error': str(_e)})

    # ── Tool API ──
    try:
        from repryntt.web.tool_api_server import tool_api_bp
        app.register_blueprint(tool_api_bp, url_prefix='/tool-api')
        log.info("Tool API blueprint registered (/tool-api)")
    except Exception as _e:
        log.warning("Tool API blueprint failed to load", extra={'error': str(_e)})

    # ── Chat Server ──
    try:
        from repryntt.web.chat_server import chat_bp
        app.register_blueprint(chat_bp, url_prefix='/chat')
        log.info("Chat Server blueprint registered (/chat)")
    except Exception as _e:
        log.warning("Chat Server blueprint failed to load", extra={'error': str(_e)})

    # ── Trading Dashboard ──
    try:
        from repryntt.trading.dashboard_server import trading_bp
        app.register_blueprint(trading_bp, url_prefix='/trading')
        log.info("Trading Dashboard blueprint registered (/trading)")
    except Exception as _e:
        log.warning("Trading Dashboard blueprint failed to load", extra={'error': str(_e)})

    # ── External API ──
    try:
        from repryntt.web.external_api import external_api_bp
        if 'JWT_SECRET_KEY' not in app.config:
            app.config['JWT_SECRET_KEY'] = _load_or_create_secret('SAIGE_JWT_SECRET', 'jwt_secret_key')
        from datetime import timedelta as _td
        app.config.setdefault('JWT_ACCESS_TOKEN_EXPIRES', _td(hours=24))
        try:
            from flask_jwt_extended import JWTManager as _JWTManager
            _JWTManager(app)
        except Exception:
            pass
        app.register_blueprint(external_api_bp, url_prefix='/ext-api')
        log.info("External API blueprint registered (/ext-api)")
        log.info("Robot Economy Manager: deferred (lazy-load on first API call)")
    except Exception as _e:
        log.warning("External API blueprint failed to load", extra={'error': str(_e)})

    # ── Unified Interface — DISABLED (not used, saves RAM) ──
    # from repryntt.web.unified_interface import unified_bp
    # app.register_blueprint(unified_bp, url_prefix='/ava')

    # ── Marketplace ──
    import sys as _sys
    _saige_root = str(Path(__file__).resolve().parent.parent)
    if _saige_root not in _sys.path:
        _sys.path.insert(0, _saige_root)
    try:
        from marketplace_site.marketplace_bp import marketplace_bp, init_marketplace
        app.register_blueprint(marketplace_bp)
        init_marketplace()
        log.info("Marketplace blueprint registered")
    except Exception as _e:
        log.warning("Marketplace blueprint failed to load", extra={'error': str(_e)})

    # ── CodeForge ──
    try:
        from repryntt.web.forge_routes import forge_bp
        app.register_blueprint(forge_bp)
        log.info("CodeForge blueprint registered (/api/forge)")
    except Exception as _e:
        log.warning("CodeForge blueprint failed to load", extra={'error': str(_e)})

    # ── Operator → Andrew task injection ──
    try:
        from repryntt.web.operator_routes import operator_bp
        app.register_blueprint(operator_bp)
        log.info("Operator blueprint registered (/api/operator)")
    except Exception as _e:
        log.warning("Operator blueprint failed to load", extra={'error': str(_e)})

    # ── Payment Gateway ──
    try:
        from repryntt.web.gateway_api import gateway_bp
        app.register_blueprint(gateway_bp, url_prefix='/gateway')
        log.info("Payment Gateway blueprint registered (/gateway)")
    except Exception as _e:
        log.warning("Payment Gateway blueprint failed to load", extra={'error': str(_e)})

    # ── Block Explorer + Wallet UI ──
    try:
        from repryntt.web.blockchain_explorer import explorer_bp
        app.register_blueprint(explorer_bp, url_prefix='/chain')
        log.info("Block Explorer blueprint registered (/chain)")
    except Exception as _e:
        log.warning("Block Explorer blueprint failed to load", extra={'error': str(_e)})

    # ── Exchange (CR/SOL Order Book) ──
    try:
        from repryntt.web.exchange import exchange_bp
        app.register_blueprint(exchange_bp, url_prefix='/exchange')
        log.info("Exchange blueprint registered (/exchange)")
    except Exception as _e:
        log.warning("Exchange blueprint failed to load", extra={'error': str(_e)})

    # ── System Dashboard ──
    try:
        from repryntt.web.system_dashboard import system_bp
        app.register_blueprint(system_bp)
        log.info("System Dashboard blueprint registered (/system)")
    except Exception as _e:
        log.warning("System Dashboard blueprint failed to load", extra={'error': str(_e)})

    # ── Agent Control Panel ──
    try:
        from repryntt.web.agent_dashboard import agent_bp
        app.register_blueprint(agent_bp)
        log.info("Agent Dashboard blueprint registered (/agent)")
    except Exception as _e:
        log.warning("Agent Dashboard blueprint failed to load", extra={'error': str(_e)})

    # ── DNA Visualizer (MemoryMesh subconscious) ──
    try:
        from repryntt.web.dna_visualizer import dna_bp
        app.register_blueprint(dna_bp)
        log.info("DNA Visualizer blueprint registered (/dna)")
    except Exception as _e:
        log.warning("DNA Visualizer blueprint failed to load", extra={'error': str(_e)})

    # ── Consciousness Dashboard (live VLM reasoning + intent override) ──
    try:
        from repryntt.web.consciousness_dashboard import consciousness_bp
        app.register_blueprint(consciousness_bp)
        log.info("Consciousness Dashboard blueprint registered (/consciousness)")
    except Exception as _e:
        log.warning("Consciousness Dashboard blueprint failed to load", extra={'error': str(_e)})

    # ── Agent Brain Builder (wizard for creating agent bootstrap files) ──
    try:
        from repryntt.web.agent_brain_builder import agent_brain_builder_bp
        app.register_blueprint(agent_brain_builder_bp)
        log.info("Agent Brain Builder blueprint registered (/agent-brain-builder)")
    except Exception as _e:
        log.warning("Agent Brain Builder blueprint failed to load", extra={'error': str(_e)})

    # ── Orchestrator (Andrew's read-only supervisor) ──
    try:
        from repryntt.web.orchestrator_dashboard import orchestrator_bp
        app.register_blueprint(orchestrator_bp)
        log.info("Orchestrator blueprint registered (/orchestrator)")
    except Exception as _e:
        log.warning("Orchestrator blueprint failed to load", extra={'error': str(_e)})

    # ── Companion (Life Plan — chat feed, config, push) ──
    try:
        from repryntt.web.companion_routes import companion_bp
        app.register_blueprint(companion_bp)
        log.info("Companion blueprint registered (/companion/*)")
    except Exception as _e:
        log.warning("Companion blueprint failed to load", extra={'error': str(_e)})

# Register blueprints immediately on import
register_blueprints()


if __name__ == '__main__':
    init_db()

    log.info("SAIGE Unified Command Hub starting",
             extra={'components': 'Hub + Profiles + Daemon + Marketplace + Gateway'})

    _bind_host = os.environ.get('SAIGE_BIND_HOST', '0.0.0.0')
    _bind_port = int(os.environ.get('SAIGE_BIND_PORT', '8089'))

    # ─── Optional TLS ───────────────────────────────────────────────────────
    _tls_cert = os.environ.get('SAIGE_TLS_CERT', '').strip()
    _tls_key = os.environ.get('SAIGE_TLS_KEY', '').strip()
    _ssl_ctx = None
    if _tls_cert and _tls_key:
        if os.path.isfile(_tls_cert) and os.path.isfile(_tls_key):
            _ssl_ctx = (_tls_cert, _tls_key)
            log.info("TLS enabled", extra={'cert': _tls_cert})
        else:
            log.warning("TLS cert/key files not found, falling back to HTTP",
                        extra={'cert': _tls_cert, 'key': _tls_key})
    _proto = 'https' if _ssl_ctx else 'http'
    log.info("Server binding", extra={'protocol': _proto, 'host': _bind_host, 'port': _bind_port})
    if _bind_host == '0.0.0.0':
        security_logger.warning("Binding to 0.0.0.0 — server is exposed on ALL interfaces!")

    # ── Auto-start Channel Gateway (Telegram, Discord, etc.) ──
    try:
        from repryntt.comms.channel_gateway import get_channel_gateway
        _gw = get_channel_gateway()
        _gw_channels = _gw.start()
        if _gw_channels:
            log.info("Channel Gateway started", extra={'channels': _gw_channels})
        else:
            log.info("Channel Gateway: no channels enabled (edit comms/channel_config.json)")
    except Exception as _gw_err:
        log.warning("Channel Gateway failed to start", extra={'error': str(_gw_err)})

    # ── Auto-start Agent Daemon + Jarvis self-prompting ──
    # Only start in-process if NOT managed by ServiceManager (which starts
    # a separate agent-daemon process).  The env var is set by ServiceManager.
    if os.environ.get('REPRYNTT_MANAGED') == '1':
        log.info("Agent daemon managed externally (ServiceManager) — skipping in-process start")
    else:
        try:
            _daemon = _get_daemon()  # calls get_agent_daemon(auto_start=True)
            if _daemon and _daemon._running:
                log.info("Agent daemon + Jarvis self-prompting started on boot")
            elif _daemon:
                _daemon.start()
                log.info("Agent daemon force-started on boot")
        except Exception as _daemon_err:
            log.warning("Agent daemon auto-start failed (non-fatal)", extra={'error': str(_daemon_err)})

    # ── Auto-start Hook System (Router + Gmail Watcher) ──
    try:
        _hook_router = _get_hook_router()
        _hook_router.start()
        log.info("Hook router started")

        # Register Gmail reply handler (send responses back via email)
        try:
            from repryntt.web.gmail import gmail_send
            def _gmail_reply_handler(reply_to: str, text: str):
                gmail_send(to=reply_to, subject="Re: Andrew Response", body=text)
            _hook_router.register_reply_handler("gmail", _gmail_reply_handler)
        except Exception as _gr_err:
            log.warning("Gmail reply handler not registered", extra={'error': str(_gr_err)})

        # Register SMS reply handler (send responses back via Twilio)
        try:
            from repryntt.comms.hooks.sms_twilio import sms_reply_handler
            _hook_router.register_reply_handler("sms", sms_reply_handler)
            log.info("SMS reply handler registered")
        except Exception as _sms_err:
            log.debug("SMS reply handler not registered (Twilio not configured)")

        # Start Gmail IMAP watcher if App Password is configured
        _gmail_watcher = _get_gmail_watcher()
        _gmail_watcher.start()
    except Exception as _hook_err:
        log.warning("Hook system auto-start failed (non-fatal)", extra={'error': str(_hook_err)})

    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
    app.run(host=_bind_host, port=_bind_port, debug=debug_mode,
            threaded=True, ssl_context=_ssl_ctx)
