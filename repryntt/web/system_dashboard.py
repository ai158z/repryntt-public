"""
repryntt.web.system_dashboard — Unified system overview dashboard.

Shows live logs, file browser, system stats, daemon control,
and links to all sub-dashboards in one place.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

from flask import Blueprint, Response, jsonify, render_template, request

system_bp = Blueprint("system", __name__)

DATA_DIR = Path.home() / ".repryntt"
BRAIN_DIR = DATA_DIR / "brain"
BOOTSTRAP_DIR = BRAIN_DIR / "bootstrap"
LOGS_DIR = DATA_DIR / "logs"
REPO_DIR = Path(__file__).resolve().parent.parent.parent


@system_bp.route("/system/")
def system_page():
    """Render the system dashboard."""
    return render_template("system.html")


@system_bp.route("/api/system/stats")
def system_stats():
    """Return system stats (CPU, RAM, GPU, disk, daemon status)."""
    import shutil

    stats = {
        "platform": platform.system(),
        "arch": platform.machine(),
        "hostname": platform.node(),
        "python": sys.version.split()[0],
    }

    # RAM
    try:
        import psutil
        mem = psutil.virtual_memory()
        stats["ram_total_gb"] = round(mem.total / (1024**3), 1)
        stats["ram_used_gb"] = round(mem.used / (1024**3), 1)
        stats["ram_percent"] = mem.percent
        stats["cpu_percent"] = psutil.cpu_percent(interval=0.5)
    except ImportError:
        stats["ram_total_gb"] = 0
        stats["ram_percent"] = 0
        stats["cpu_percent"] = 0

    # Disk
    disk = shutil.disk_usage(str(Path.home()))
    stats["disk_total_gb"] = round(disk.total / (1024**3), 1)
    stats["disk_free_gb"] = round(disk.free / (1024**3), 1)
    stats["disk_percent"] = round((disk.used / disk.total) * 100, 1)

    # GPU
    try:
        from repryntt.hardware_profile import get_profile
        hw = get_profile()
        stats["gpu_name"] = hw.gpu_name or "None"
        stats["gpu_vram_mb"] = hw.gpu_vram_mb
    except Exception:
        stats["gpu_name"] = "N/A"
        stats["gpu_vram_mb"] = 0

    # Daemon status
    pid_file = DATA_DIR / "pids" / "daemon.pid"
    daemon_running = False
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # Check if process exists
            daemon_running = True
        except (ValueError, OSError):
            pass
    stats["daemon_running"] = daemon_running

    # Agent config
    ai_cfg = BRAIN_DIR / "ai_config.json"
    if ai_cfg.exists():
        try:
            cfg = json.loads(ai_cfg.read_text())
            stats["agent_name"] = cfg.get("agent_name", "Andrew")
            stats["provider"] = cfg.get("ai_provider", {}).get("provider", "unknown")
            stats["heartbeat"] = cfg.get("heartbeat_interval", 69)
        except Exception:
            stats["agent_name"] = "Unknown"
            stats["provider"] = "unknown"
            stats["heartbeat"] = 0
    else:
        stats["agent_name"] = "Not configured"
        stats["provider"] = "none"
        stats["heartbeat"] = 0

    return jsonify(stats)


@system_bp.route("/api/system/logs")
def stream_logs():
    """SSE stream of log lines from the most recent log file."""
    def generate():
        # Find latest log file
        log_files = sorted(LOGS_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not log_files:
            yield f"data: No log files found in {LOGS_DIR}\n\n"
            return

        log_file = log_files[0]
        yield f"data: === Tailing {log_file.name} ===\n\n"

        with open(log_file, "r") as f:
            # Start from last 100 lines
            lines = f.readlines()
            for line in lines[-100:]:
                yield f"data: {line.rstrip()}\n\n"

            # Then tail new lines
            while True:
                line = f.readline()
                if line:
                    yield f"data: {line.rstrip()}\n\n"
                else:
                    time.sleep(1)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@system_bp.route("/api/system/files")
def browse_files():
    """List files in a directory (bootstrap, config, brain)."""
    rel_path = request.args.get("path", "bootstrap")

    # Whitelist allowed directories
    allowed = {
        "bootstrap": BOOTSTRAP_DIR,
        "brain": BRAIN_DIR,
        "logs": LOGS_DIR,
        "config": REPO_DIR / "config",
    }

    base = allowed.get(rel_path)
    if not base or not base.exists():
        return jsonify({"error": "Invalid path", "files": []})

    files = []
    for item in sorted(base.iterdir()):
        if item.name.startswith("__"):
            continue
        files.append({
            "name": item.name,
            "is_dir": item.is_dir(),
            "size": item.stat().st_size if item.is_file() else 0,
            "modified": int(item.stat().st_mtime),
        })

    return jsonify({"path": rel_path, "files": files})


@system_bp.route("/api/system/file")
def read_system_file():
    """Read a single file's content (text only, max 100KB)."""
    rel_path = request.args.get("path", "")
    filename = request.args.get("name", "")

    allowed = {
        "bootstrap": BOOTSTRAP_DIR,
        "brain": BRAIN_DIR,
        "logs": LOGS_DIR,
        "config": REPO_DIR / "config",
    }

    base = allowed.get(rel_path)
    if not base:
        return jsonify({"error": "Invalid path"}), 400

    # Prevent path traversal
    target = (base / filename).resolve()
    if not str(target).startswith(str(base.resolve())):
        return jsonify({"error": "Access denied"}), 403

    if not target.is_file():
        return jsonify({"error": "File not found"}), 404

    if target.stat().st_size > 102400:
        return jsonify({"error": "File too large (>100KB)", "size": target.stat().st_size}), 413

    try:
        content = target.read_text(errors="replace")
        return jsonify({"name": filename, "content": content, "size": len(content)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@system_bp.route("/api/system/start", methods=["POST"])
def start_system():
    """Start the repryntt daemon."""
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "repryntt.cli", "start", "--no-llm"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(REPO_DIR),
        )
        time.sleep(3)
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            return jsonify({"started": False, "error": stderr[-500:]}), 500
        return jsonify({"started": True, "pid": proc.pid})
    except Exception as e:
        return jsonify({"started": False, "error": str(e)}), 500


@system_bp.route("/api/system/stop", methods=["POST"])
def stop_system():
    """Stop the repryntt daemon."""
    pid_file = DATA_DIR / "pids" / "daemon.pid"
    if not pid_file.exists():
        return jsonify({"stopped": False, "error": "No PID file"})
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 15)  # SIGTERM
        return jsonify({"stopped": True, "pid": pid})
    except Exception as e:
        return jsonify({"stopped": False, "error": str(e)})
