"""
repryntt.desktop.dashboard — Unified Navigation Shell (Desktop + Mobile PWA)
=============================================================================
Flask app serving a responsive single-page dashboard that wraps all repryntt
web services into one navigable interface.  Works as:

  - Desktop: embedded inside pywebview native window
  - Mobile PWA: installable on Android & iOS home screens
  - Browser: any device on the local network

Port: 8891 (default)

Routes:
    /                  — Main dashboard UI (responsive SPA)
    /manifest.json     — PWA manifest for Add-to-Home-Screen
    /sw.js             — Service worker for offline shell caching
    /api/status        — Service health + system metrics (JSON)
    /api/logs/<name>   — Tail last 200 lines of a service log (JSON)
    /api/auth          — Token validation endpoint
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import time
from functools import wraps
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string, request
from flask_cors import CORS

logger = logging.getLogger(__name__)

DASHBOARD_PORT = 8891

# ---------------------------------------------------------------------------
#  Service registry
# ---------------------------------------------------------------------------

SERVICES = [
    {"id": "command-center", "name": "Command Center", "port": 8890,
     "icon": "\U0001f3ed", "desc": "Factory floor agent overview"},
    {"id": "nexus", "name": "Nexus Dashboard", "port": 8089,
     "icon": "\U0001f310", "desc": "Agent workspace & consciousness"},
    {"id": "trading", "name": "Trading Dashboard", "port": 8888,
     "icon": "\U0001f4c8", "desc": "Token monitoring & signals"},
    {"id": "chat", "name": "Chat Interface", "port": 4000,
     "icon": "\U0001f4ac", "desc": "Direct agent messaging"},
    {"id": "unified", "name": "Unified Interface", "port": 3000,
     "icon": "\U0001f9e0", "desc": "Brain API & health"},
    {"id": "web", "name": "Web Server", "port": 5000,
     "icon": "\U0001f5a5\ufe0f", "desc": "TTS, media, and web tools"},
    {"id": "tool-api", "name": "Tool API", "port": 8083,
     "icon": "\U0001f527", "desc": "212+ tool endpoints"},
    {"id": "external-api", "name": "External API", "port": 8081,
     "icon": "\U0001f511", "desc": "JWT-protected AI services"},
    {"id": "llm", "name": "Local LLM", "port": 8080,
     "icon": "\U0001f916", "desc": "llama.cpp inference server"},
]

# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _port_open(port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False


def _get_logs_dir() -> Path:
    try:
        from repryntt.paths import logs_dir
        return logs_dir()
    except Exception:
        return Path.home() / ".repryntt" / "logs"


# ---------------------------------------------------------------------------
#  Token auth for remote access
# ---------------------------------------------------------------------------

def _generate_token() -> str:
    """Generate a cryptographically random 32-char hex token."""
    return secrets.token_hex(16)


def _require_token(f):
    """Decorator: if app.config['AUTH_TOKEN'] is set, enforce Bearer auth."""
    @wraps(f)
    def decorated(*args, **kwargs):
        from flask import current_app
        token = current_app.config.get("AUTH_TOKEN")
        if not token:
            return f(*args, **kwargs)
        auth = request.headers.get("Authorization", "")
        # Also accept ?token= query param (for mobile convenience)
        request_token = ""
        if auth.startswith("Bearer "):
            request_token = auth[7:]
        else:
            request_token = request.args.get("token", "")
        if not request_token or not hmac.compare_digest(request_token, token):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
#  Flask App
# ---------------------------------------------------------------------------

def create_dashboard_app(auth_token: str | None = None) -> Flask:
    app = Flask(__name__)
    CORS(app)
    app._start_time = time.time()
    if auth_token:
        app.config["AUTH_TOKEN"] = auth_token

    @app.route("/")
    def index():
        token = app.config.get("AUTH_TOKEN", "")
        html = DASHBOARD_HTML.replace("__AUTH_TOKEN__", token)
        return render_template_string(html)

    @app.route("/manifest.json")
    def manifest():
        return Response(PWA_MANIFEST, mimetype="application/manifest+json")

    @app.route("/sw.js")
    def service_worker():
        return Response(SERVICE_WORKER, mimetype="application/javascript")

    @app.route("/api/status")
    @_require_token
    def api_status():
        results = []
        for svc in SERVICES:
            results.append({**svc, "alive": _port_open(svc["port"])})

        system = {"uptime": int(time.time() - app._start_time)}
        try:
            import psutil
            system["cpu_percent"] = psutil.cpu_percent(interval=0)
            mem = psutil.virtual_memory()
            system["ram_used_mb"] = mem.used // (1024 * 1024)
            system["ram_total_mb"] = mem.total // (1024 * 1024)
            system["ram_percent"] = mem.percent
        except ImportError:
            from repryntt.platform_utils import get_ram_mb
            total = get_ram_mb()
            if total:
                system["ram_total_mb"] = total

        alive_count = sum(1 for r in results if r["alive"])
        return jsonify({
            "services": results,
            "system": system,
            "summary": f"{alive_count}/{len(results)} services running",
        })

    @app.route("/api/logs/<service_name>")
    @_require_token
    def api_logs(service_name):
        safe = re.sub(r"[^a-zA-Z0-9_\-]", "", service_name)
        if safe != service_name:
            return jsonify({"lines": [], "error": "Invalid service name"}), 400

        log_dir = _get_logs_dir()
        log_file = (log_dir / f"{safe}.log").resolve()

        if not str(log_file).startswith(str(log_dir.resolve())):
            return jsonify({"lines": [], "error": "Invalid path"}), 400

        if not log_file.exists():
            return jsonify({"lines": [], "error": "Log file not found"})

        try:
            lines = log_file.read_text(errors="replace").splitlines()[-200:]
            return jsonify({"lines": lines})
        except Exception as e:
            return jsonify({"lines": [], "error": str(e)})

    @app.route("/api/auth")
    @_require_token
    def api_auth():
        return jsonify({"ok": True})

    return app


# ---------------------------------------------------------------------------
#  Standalone runner (python -m repryntt.desktop.dashboard)
# ---------------------------------------------------------------------------

def main():
    app = create_dashboard_app()
    print(f"Dashboard running on http://127.0.0.1:{DASHBOARD_PORT}")
    app.run(host="127.0.0.1", port=DASHBOARD_PORT, debug=False)


# ---------------------------------------------------------------------------
#  PWA Manifest
# ---------------------------------------------------------------------------

PWA_MANIFEST = """{
  "name": "Repryntt",
  "short_name": "Repryntt",
  "description": "Autonomous AI Framework — Agent Dashboard & Control Center",
  "start_url": "/",
  "display": "standalone",
  "orientation": "any",
  "background_color": "#0d1117",
  "theme_color": "#161b22",
  "icons": [
    {
      "src": "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><circle cx='50' cy='50' r='45' fill='%23161b22' stroke='%2358a6ff' stroke-width='4'/><circle cx='50' cy='50' r='20' fill='%233fb950'/></svg>",
      "sizes": "any",
      "type": "image/svg+xml",
      "purpose": "any"
    }
  ],
  "categories": ["utilities", "developer"],
  "lang": "en"
}"""


# ---------------------------------------------------------------------------
#  Service Worker — offline shell caching
# ---------------------------------------------------------------------------

SERVICE_WORKER = r"""
const CACHE_NAME = 'repryntt-v1';
const SHELL_URLS = ['/', '/manifest.json'];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(SHELL_URLS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  // API calls: network-only (always live data)
  if (url.pathname.startsWith('/api/')) return;
  // Shell: cache-first, fallback to network
  event.respondWith(
    caches.match(event.request).then(cached => cached || fetch(event.request))
  );
});
"""


# ---------------------------------------------------------------------------
#  Dashboard HTML — responsive SPA with PWA support (desktop + mobile)
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#161b22">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Repryntt">
<link rel="manifest" href="/manifest.json">
<title>Repryntt</title>
<style>
/* ── Reset ─────────────────────────────────────────────────────────── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{height:100%;-webkit-text-size-adjust:100%}
body{
  height:100%;overflow:hidden;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  background:#0d1117;color:#c9d1d9;display:flex;flex-direction:column;
  /* iOS notch safe areas */
  padding-top:env(safe-area-inset-top);
  padding-left:env(safe-area-inset-left);
  padding-right:env(safe-area-inset-right);
}
::-webkit-scrollbar{width:6px}
::-webkit-scrollbar-track{background:#0d1117}
::-webkit-scrollbar-thumb{background:#30363d;border-radius:3px}

/* ── Top bar ───────────────────────────────────────────────────────── */
.topbar{
  background:#161b22;border-bottom:1px solid #30363d;
  padding:0 16px;height:48px;display:flex;align-items:center;gap:12px;flex-shrink:0;
}
.topbar .brand{font-weight:700;font-size:16px;color:#f0f6fc;letter-spacing:1px;user-select:none}
.topbar .brand span{color:#58a6ff}
.status-pill{
  background:#21262d;border:1px solid #30363d;border-radius:12px;
  padding:4px 12px;font-size:12px;font-weight:500;transition:color .3s;
}
.topbar .metrics{
  margin-left:auto;display:flex;gap:12px;font-size:12px;color:#8b949e;
}
.topbar .metrics span{white-space:nowrap}
.hamburger{display:none;background:none;border:none;color:#c9d1d9;font-size:22px;padding:4px 8px;cursor:pointer}

/* ── Layout ────────────────────────────────────────────────────────── */
.main{display:flex;flex:1;overflow:hidden}

/* ── Sidebar ───────────────────────────────────────────────────────── */
.sidebar{
  width:230px;background:#161b22;border-right:1px solid #30363d;
  display:flex;flex-direction:column;flex-shrink:0;overflow-y:auto;
}
.sidebar-section{padding:14px 0}
.sidebar-section h3{
  font-size:10px;text-transform:uppercase;letter-spacing:.8px;
  color:#8b949e;padding:0 16px 8px;font-weight:600;
}
.sidebar-item{
  display:flex;align-items:center;gap:10px;
  padding:9px 16px;cursor:pointer;font-size:13px;
  transition:background .12s,border-color .12s;
  border-left:3px solid transparent;user-select:none;
}
.sidebar-item:hover{background:#21262d}
.sidebar-item.active{background:#1c2333;border-left-color:#58a6ff;color:#f0f6fc}
.sidebar-item.dead-svc{opacity:.55}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.dot.alive{background:#3fb950;box-shadow:0 0 6px rgba(63,185,80,.5)}
.dot.dead{background:#484f58}
.svc-icon{font-size:15px;line-height:1}
.svc-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.svc-port{font-size:11px;color:#8b949e;font-family:monospace}

.sidebar-footer{
  margin-top:auto;padding:12px 16px;border-top:1px solid #30363d;
}
.sidebar-footer .sidebar-item{padding:8px 0}

/* ── Content ───────────────────────────────────────────────────────── */
.content{flex:1;display:flex;flex-direction:column;overflow:hidden;position:relative}
.content iframe{flex:1;width:100%;border:none;background:#0d1117}
.frame-overlay{
  position:absolute;top:0;left:0;right:0;bottom:0;
  display:flex;align-items:center;justify-content:center;
  background:#0d1117;z-index:10;
}
.frame-overlay.hidden{display:none}
.frame-overlay .spinner{
  width:40px;height:40px;border:3px solid #30363d;
  border-top-color:#58a6ff;border-radius:50%;animation:spin .8s linear infinite;
}
@keyframes spin{to{transform:rotate(360deg)}}

/* ── Home view ─────────────────────────────────────────────────────── */
.home{flex:1;padding:28px;overflow-y:auto;-webkit-overflow-scrolling:touch}
.home h2{font-size:24px;color:#f0f6fc;margin-bottom:6px;font-weight:700}
.home .subtitle{color:#8b949e;margin-bottom:24px;font-size:14px;line-height:1.5}
.service-grid{
  display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px;
}
.svc-card{
  background:#161b22;border:1px solid #30363d;border-radius:10px;
  padding:18px;cursor:pointer;transition:all .2s ease;
}
.svc-card:hover{
  border-color:#58a6ff;transform:translateY(-2px);
  box-shadow:0 8px 24px rgba(0,0,0,.35);
}
.svc-card.dead-card{opacity:.6}
.svc-card.dead-card:hover{border-color:#484f58;transform:none;box-shadow:none}
.card-header{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.card-icon{font-size:24px;line-height:1}
.card-name{font-size:14px;font-weight:600;color:#f0f6fc}
.card-status{
  margin-left:auto;display:flex;align-items:center;gap:6px;font-size:12px;
}
.card-status .label-up{color:#3fb950}
.card-status .label-down{color:#8b949e}
.card-desc{font-size:13px;color:#8b949e;margin-bottom:12px;line-height:1.4}
.card-port{font-size:12px;color:#58a6ff;font-family:monospace}

/* ── Log viewer modal ──────────────────────────────────────────────── */
.log-modal{
  position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.7);
  z-index:100;display:flex;align-items:center;justify-content:center;
  padding:16px;
}
.log-modal.hidden{display:none}
.log-box{
  background:#161b22;border:1px solid #30363d;border-radius:10px;
  width:100%;max-width:900px;max-height:80vh;display:flex;flex-direction:column;
}
.log-box .log-header{
  padding:14px 16px;border-bottom:1px solid #30363d;
  display:flex;align-items:center;justify-content:space-between;
}
.log-box .log-header h3{font-size:15px;color:#f0f6fc}
.log-box .log-close{
  background:none;border:none;color:#8b949e;font-size:22px;cursor:pointer;
  padding:4px 8px;
}
.log-box .log-close:hover{color:#f0f6fc}
.log-box .log-content{
  flex:1;overflow-y:auto;padding:14px 16px;-webkit-overflow-scrolling:touch;
  font-family:'Cascadia Code','Fira Code','JetBrains Mono','SF Mono',monospace;
  font-size:11px;line-height:1.6;color:#c9d1d9;white-space:pre-wrap;
  word-break:break-all;
}

/* ── Status bar (desktop only) ─────────────────────────────────────── */
.statusbar{
  background:#161b22;border-top:1px solid #30363d;
  padding:4px 16px;font-size:11px;color:#8b949e;
  display:flex;align-items:center;gap:16px;flex-shrink:0;
}
.statusbar .pulse{
  width:6px;height:6px;border-radius:50%;background:#3fb950;
  animation:pulse-glow 2s ease-in-out infinite;
}
@keyframes pulse-glow{
  0%,100%{box-shadow:0 0 0 0 rgba(63,185,80,.4)}
  50%{box-shadow:0 0 0 4px rgba(63,185,80,0)}
}

/* ── Mobile-first install banner ───────────────────────────────────── */
.install-banner{
  display:none;background:#1c2333;border-bottom:1px solid #30363d;
  padding:10px 16px;text-align:center;font-size:13px;color:#c9d1d9;
  flex-shrink:0;
}
.install-banner button{
  background:#238636;color:#fff;border:none;border-radius:6px;
  padding:6px 16px;font-size:13px;cursor:pointer;margin-left:8px;
}
.install-banner .dismiss{
  background:none;color:#8b949e;margin-left:4px;font-size:18px;
  padding:2px 6px;vertical-align:middle;
}

/* ── Bottom navigation (mobile only) ──────────────────────────────── */
.bottom-nav{
  display:none;background:#161b22;border-top:1px solid #30363d;
  flex-shrink:0;
  padding:6px 0 calc(6px + env(safe-area-inset-bottom));
}
.bottom-nav-inner{
  display:flex;justify-content:space-around;align-items:center;
}
.bnav-item{
  display:flex;flex-direction:column;align-items:center;gap:2px;
  padding:6px 12px;cursor:pointer;font-size:10px;color:#8b949e;
  user-select:none;-webkit-tap-highlight-color:transparent;
  transition:color .15s;
}
.bnav-item.active{color:#58a6ff}
.bnav-item .bnav-icon{font-size:20px;line-height:1}
.bnav-item .bnav-dot{
  width:6px;height:6px;border-radius:50%;
}
.bnav-dot.alive{background:#3fb950}
.bnav-dot.dead{background:#484f58}

/* ══════════════════════════════════════════════════════════════════════
   Responsive — Mobile (<768px)
   ══════════════════════════════════════════════════════════════════════ */
@media(max-width:767px){
  .hamburger{display:block}
  .topbar .metrics{display:none}
  .sidebar{
    position:fixed;top:48px;left:0;bottom:0;z-index:50;
    width:260px;transform:translateX(-100%);
    transition:transform .25s ease;
  }
  .sidebar.open{transform:translateX(0)}
  .sidebar-backdrop{
    display:none;position:fixed;top:48px;left:0;right:0;bottom:0;
    background:rgba(0,0,0,.5);z-index:49;
  }
  .sidebar-backdrop.visible{display:block}
  .statusbar{display:none}
  .bottom-nav{display:block}
  .home{padding:16px}
  .home h2{font-size:20px}
  .service-grid{grid-template-columns:1fr;gap:10px}
  .svc-card{padding:14px}
  .svc-card:hover{transform:none;box-shadow:none}
  .card-icon{font-size:20px}
  .log-box{
    width:100%;max-width:100%;max-height:90vh;border-radius:10px 10px 0 0;
    position:fixed;bottom:0;left:0;right:0;
  }
}

/* ── Medium screens (tablet landscape) ─────────────────────────────── */
@media(min-width:768px) and (max-width:1023px){
  .sidebar{width:200px}
  .service-grid{grid-template-columns:repeat(auto-fill,minmax(220px,1fr))}
}

/* ── Touch improvements ────────────────────────────────────────────── */
@media(hover:none) and (pointer:coarse){
  .sidebar-item{padding:12px 16px}
  .svc-card{padding:16px}
  .svc-card:active{background:#1c2333;border-color:#58a6ff}
  .svc-card:hover{transform:none;box-shadow:none}
}
</style>
</head>

<body>
<!-- ── Install Banner (PWA prompt) ─────────────────────────────────── -->
<div class="install-banner" id="install-banner">
  Install Repryntt for quick access
  <button onclick="installPWA()">Install</button>
  <button class="dismiss" onclick="dismissInstall()">&times;</button>
</div>

<!-- ── Top Bar ──────────────────────────────────────────────────────── -->
<div class="topbar">
  <button class="hamburger" onclick="toggleSidebar()" aria-label="Menu">&#9776;</button>
  <div class="brand"><span>R</span>EPRYNTT</div>
  <div class="status-pill" id="status-pill">Connecting...</div>
  <div class="metrics" id="metrics">
    <span id="cpu-metric">CPU: —</span>
    <span id="ram-metric">RAM: —</span>
    <span id="uptime-metric">Uptime: —</span>
  </div>
</div>

<!-- ── Sidebar Backdrop (mobile) ───────────────────────────────────── -->
<div class="sidebar-backdrop" id="sidebar-backdrop" onclick="closeSidebar()"></div>

<!-- ── Main Layout ─────────────────────────────────────────────────── -->
<div class="main">
  <!-- Sidebar -->
  <div class="sidebar" id="sidebar">
    <div class="sidebar-section">
      <h3>Services</h3>
      <div id="service-list"></div>
    </div>
    <div class="sidebar-footer">
      <div class="sidebar-item" onclick="goHome()">
        <span class="svc-icon">&#x1F3E0;</span>
        <span class="svc-name">Home</span>
      </div>
    </div>
  </div>

  <!-- Content Area -->
  <div class="content">
    <div class="home" id="home-view">
      <h2>Repryntt</h2>
      <p class="subtitle">
        Autonomous AI Framework — 22 agents, 212+ tools, three-tier model routing.<br>
        Select a service to get started.
      </p>
      <div class="service-grid" id="service-grid"></div>
    </div>
    <iframe id="service-frame" style="display:none" sandbox="allow-same-origin allow-scripts allow-forms allow-popups"></iframe>
    <div class="frame-overlay hidden" id="frame-loading">
      <div class="spinner"></div>
    </div>
  </div>
</div>

<!-- ── Log Viewer Modal ────────────────────────────────────────────── -->
<div class="log-modal hidden" id="log-modal" onclick="closeLogModal(event)">
  <div class="log-box">
    <div class="log-header">
      <h3 id="log-title">Logs</h3>
      <button class="log-close" onclick="closeLogModal()">&times;</button>
    </div>
    <div class="log-content" id="log-content">Loading...</div>
  </div>
</div>

<!-- ── Bottom Navigation (mobile) ──────────────────────────────────── -->
<div class="bottom-nav" id="bottom-nav">
  <div class="bottom-nav-inner" id="bottom-nav-inner"></div>
</div>

<!-- ── Status Bar (desktop) ────────────────────────────────────────── -->
<div class="statusbar" id="statusbar">
  <div class="pulse"></div>
  <span>repryntt v0.1.0</span>
  <span id="sb-status">Starting...</span>
</div>

<!-- ── JavaScript ──────────────────────────────────────────────────── -->
<script>
"use strict";

var AUTH_TOKEN = "__AUTH_TOKEN__";
var currentService = null;
var services = [];
var isMobile = window.innerWidth < 768;
var deferredInstallPrompt = null;
var serverHost = location.hostname;

/* ── Auth header helper ────────────────────────────────────────────── */

function authFetch(url, opts) {
  opts = opts || {};
  if (AUTH_TOKEN) {
    opts.headers = opts.headers || {};
    opts.headers["Authorization"] = "Bearer " + AUTH_TOKEN;
  }
  return fetch(url, opts);
}

/* ── Get base URL for services (uses the server host, not 127.0.0.1) */

function serviceUrl(port) {
  return location.protocol + "//" + serverHost + ":" + port;
}

/* ── PWA Install ───────────────────────────────────────────────────── */

window.addEventListener("beforeinstallprompt", function(e) {
  e.preventDefault();
  deferredInstallPrompt = e;
  document.getElementById("install-banner").style.display = "block";
});

function installPWA() {
  if (deferredInstallPrompt) {
    deferredInstallPrompt.prompt();
    deferredInstallPrompt.userChoice.then(function() {
      document.getElementById("install-banner").style.display = "none";
      deferredInstallPrompt = null;
    });
  }
}

function dismissInstall() {
  document.getElementById("install-banner").style.display = "none";
}

/* Register service worker */
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(function() {});
}

/* ── Sidebar (mobile) ─────────────────────────────────────────────── */

function toggleSidebar() {
  document.getElementById("sidebar").classList.toggle("open");
  document.getElementById("sidebar-backdrop").classList.toggle("visible");
}

function closeSidebar() {
  document.getElementById("sidebar").classList.remove("open");
  document.getElementById("sidebar-backdrop").classList.remove("visible");
}

/* ── Navigation ────────────────────────────────────────────────────── */

function goHome() {
  currentService = null;
  document.getElementById("home-view").style.display = "";
  document.getElementById("service-frame").style.display = "none";
  document.getElementById("frame-loading").classList.add("hidden");
  updateSidebarActive();
  updateBottomNav();
  closeSidebar();
}

function selectService(id) {
  var svc = services.find(function(s) { return s.id === id; });
  if (!svc) return;
  closeSidebar();

  if (!svc.alive) {
    showLogs(svc.id, svc.name);
    return;
  }

  /* On mobile — open in new tab (iframes are poor UX on small screens) */
  if (isMobile) {
    window.open(serviceUrl(svc.port), "_blank");
    return;
  }

  currentService = id;
  document.getElementById("home-view").style.display = "none";
  var frame = document.getElementById("service-frame");
  var overlay = document.getElementById("frame-loading");

  overlay.classList.remove("hidden");
  frame.style.display = "";
  frame.src = serviceUrl(svc.port);
  frame.onload = function() { overlay.classList.add("hidden"); };

  updateSidebarActive();
  updateBottomNav();
}

function updateSidebarActive() {
  document.querySelectorAll(".sidebar-item[data-id]").forEach(function(el) {
    el.classList.toggle("active", el.dataset.id === currentService);
  });
}

/* ── Log viewer ────────────────────────────────────────────────────── */

function showLogs(serviceId, serviceName) {
  var modal = document.getElementById("log-modal");
  var title = document.getElementById("log-title");
  var content = document.getElementById("log-content");
  title.textContent = serviceName + " — Logs";
  content.textContent = "Loading...";
  modal.classList.remove("hidden");

  authFetch("/api/logs/" + encodeURIComponent(serviceId))
    .then(function(resp) { return resp.json(); })
    .then(function(data) {
      if (data.lines && data.lines.length > 0) {
        content.textContent = data.lines.join("\n");
        content.scrollTop = content.scrollHeight;
      } else {
        content.textContent = data.error || "No log data available.";
      }
    })
    .catch(function(e) {
      content.textContent = "Failed to fetch logs: " + e.message;
    });
}

function closeLogModal(ev) {
  if (ev && ev.target !== ev.currentTarget) return;
  document.getElementById("log-modal").classList.add("hidden");
}

/* ── Rendering — Sidebar ───────────────────────────────────────────── */

function renderSidebar() {
  var list = document.getElementById("service-list");
  list.innerHTML = services.map(function(svc) {
    return '<div class="sidebar-item ' +
      (currentService === svc.id ? 'active ' : '') +
      (!svc.alive ? 'dead-svc ' : '') + '" ' +
      'data-id="' + svc.id + '" onclick="selectService(\'' + svc.id + '\')">' +
      '<div class="dot ' + (svc.alive ? 'alive' : 'dead') + '"></div>' +
      '<span class="svc-icon">' + svc.icon + '</span>' +
      '<span class="svc-name">' + svc.name + '</span>' +
      '<span class="svc-port">:' + svc.port + '</span>' +
    '</div>';
  }).join("");
}

/* ── Rendering — Service Grid (home) ──────────────────────────────── */

function renderGrid() {
  var grid = document.getElementById("service-grid");
  grid.innerHTML = services.map(function(svc) {
    return '<div class="svc-card ' + (!svc.alive ? 'dead-card' : '') + '" ' +
      'onclick="selectService(\'' + svc.id + '\')">' +
      '<div class="card-header">' +
        '<span class="card-icon">' + svc.icon + '</span>' +
        '<span class="card-name">' + svc.name + '</span>' +
        '<div class="card-status">' +
          '<div class="dot ' + (svc.alive ? 'alive' : 'dead') + '"></div>' +
          '<span class="' + (svc.alive ? 'label-up' : 'label-down') + '">' +
            (svc.alive ? 'Running' : 'Stopped') +
          '</span>' +
        '</div>' +
      '</div>' +
      '<div class="card-desc">' + svc.desc + '</div>' +
      '<div class="card-port">' + serverHost + ':' + svc.port + '</div>' +
    '</div>';
  }).join("");
}

/* ── Rendering — Bottom Nav (mobile) ──────────────────────────────── */

function renderBottomNav() {
  /* Show top 5 most important services */
  var top5 = ["command-center","nexus","trading","chat","unified"];
  var inner = document.getElementById("bottom-nav-inner");

  var html = '<div class="bnav-item' + (!currentService ? ' active' : '') +
    '" onclick="goHome()"><span class="bnav-icon">&#x1F3E0;</span>Home</div>';

  top5.forEach(function(id) {
    var svc = services.find(function(s) { return s.id === id; });
    if (!svc) return;
    html += '<div class="bnav-item' + (currentService === svc.id ? ' active' : '') +
      '" onclick="selectService(\'' + svc.id + '\')">' +
      '<div class="bnav-dot ' + (svc.alive ? 'alive' : 'dead') + '"></div>' +
      '<span style="font-size:10px">' + svc.name.split(' ')[0] + '</span></div>';
  });

  inner.innerHTML = html;
}

function updateBottomNav() {
  if (isMobile) renderBottomNav();
}

/* ── Metrics ───────────────────────────────────────────────────────── */

function formatUptime(sec) {
  var h = Math.floor(sec / 3600);
  var m = Math.floor((sec % 3600) / 60);
  if (h > 0) return h + "h " + m + "m";
  if (m > 0) return m + "m";
  return sec + "s";
}

function updateMetrics(sys) {
  if (sys.cpu_percent !== undefined)
    document.getElementById("cpu-metric").textContent = "CPU: " + sys.cpu_percent + "%";
  if (sys.ram_used_mb !== undefined) {
    var used = (sys.ram_used_mb / 1024).toFixed(1);
    var total = (sys.ram_total_mb / 1024).toFixed(1);
    document.getElementById("ram-metric").textContent = "RAM: " + used + "/" + total + "G";
  }
  if (sys.uptime !== undefined)
    document.getElementById("uptime-metric").textContent = "Uptime: " + formatUptime(sys.uptime);
}

/* ── Polling ───────────────────────────────────────────────────────── */

function poll() {
  authFetch("/api/status")
    .then(function(resp) { return resp.json(); })
    .then(function(data) {
      services = data.services;

      var pill = document.getElementById("status-pill");
      pill.textContent = data.summary;
      var alive = services.filter(function(s) { return s.alive; }).length;
      pill.style.color = alive === services.length ? "#3fb950"
                       : alive > 0 ? "#d29922" : "#f85149";

      var sb = document.getElementById("sb-status");
      if (sb) sb.textContent = data.summary;

      updateMetrics(data.system);
      renderSidebar();
      if (!currentService) renderGrid();
      renderBottomNav();
    })
    .catch(function() {
      document.getElementById("status-pill").textContent = "Disconnected";
      document.getElementById("status-pill").style.color = "#f85149";
    });
}

poll();
setInterval(poll, 5000);

/* ── Keyboard shortcuts ────────────────────────────────────────────── */
document.addEventListener("keydown", function(e) {
  if (e.key === "Escape") {
    if (!document.getElementById("log-modal").classList.contains("hidden")) {
      closeLogModal();
    } else if (currentService) {
      goHome();
    }
  }
});

/* ── Responsive resize ─────────────────────────────────────────────── */
window.addEventListener("resize", function() {
  isMobile = window.innerWidth < 768;
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
