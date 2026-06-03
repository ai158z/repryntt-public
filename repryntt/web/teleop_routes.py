"""repryntt.web.teleop_routes — Manual driving UI for collecting demos.

Adds a /teleop route to the existing nexus Flask app. Lets a human drive
Andrew with WASD from any browser on the LAN. Records every command +
camera frame to ~/.repryntt/data/teleop_demos/{date}.jsonl in the same
schema as nav_experience, so the curator (curate_nav_data.py) ingests them.

Safety:
  - Server-side dead-man: a background watcher calls tank.stop() if no
    /cmd arrives within DEADMAN_TIMEOUT_S seconds. Critical — a closed
    browser tab otherwise leaves the last command running.
  - Per-command duration is hard-capped to MAX_CMD_DURATION_S regardless
    of what the client sends.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request, Response

from repryntt.hardware.motor_client import (
    MotorClientError,
    MotorSession,
    Preempted,
    Priority,
    session as motor_session,
)

logger = logging.getLogger(__name__)

teleop_bp = Blueprint("teleop", __name__, url_prefix="/teleop")

DEMO_DIR = Path.home() / ".repryntt" / "data" / "teleop_demos"
DEMO_FRAMES_DIR = DEMO_DIR / "frames"
DEADMAN_TIMEOUT_S = 0.30
MAX_CMD_DURATION_S = 0.40
SESSION_IDLE_RELEASE_S = 5.0   # release operator lease after 5s no input
ACTIONS = {"forward", "backward", "turn_left", "turn_right", "stop"}

# Module-level state. Teleop holds at most one motor session at a time.
# When the operator goes idle, the session is released so the autonomous
# explorer can take the lease back.
_state_lock = threading.Lock()
_last_cmd_ts: float = 0.0
_recording: bool = False
_deadman_thread: Optional[threading.Thread] = None
_deadman_stop = threading.Event()
_active_session: Optional[MotorSession] = None
_session_cm = None  # the contextlib._GeneratorContextManager keeping it open


def _broker():
    from repryntt.hardware.camera_broker import broker
    return broker


def _ensure_deadman_running() -> None:
    """Start the dead-man + session-idle watcher.

    Two roles:
      1. If no /cmd arrives for DEADMAN_TIMEOUT_S, brake the motors once
         (covers the "browser tab froze mid-keypress" case).
      2. If no /cmd arrives for SESSION_IDLE_RELEASE_S, release the
         operator lease so the autonomous explorer can resume.
    """
    global _deadman_thread
    if _deadman_thread is not None and _deadman_thread.is_alive():
        return
    _deadman_stop.clear()

    def _watch():
        logger.info("teleop deadman: watcher started")
        last_stop_for: float = 0.0
        while not _deadman_stop.is_set():
            with _state_lock:
                cmd_ts = _last_cmd_ts
                sess = _active_session
            idle = (time.time() - cmd_ts) if cmd_ts else 0
            if cmd_ts and idle > DEADMAN_TIMEOUT_S and cmd_ts != last_stop_for:
                if sess is not None and not sess.preempted:
                    try:
                        sess.stop()
                    except Exception as e:
                        logger.debug("teleop deadman: stop failed: %s", e)
                last_stop_for = cmd_ts
            if cmd_ts and idle > SESSION_IDLE_RELEASE_S and sess is not None:
                logger.info(
                    "teleop deadman: idle %.1fs — releasing operator lease",
                    idle,
                )
                _close_session_locked_safe()
            time.sleep(DEADMAN_TIMEOUT_S / 2)

    _deadman_thread = threading.Thread(
        target=_watch, name="teleop-deadman", daemon=True,
    )
    _deadman_thread.start()


def _ensure_session() -> Optional[MotorSession]:
    """Acquire the operator motor lease if we don't already hold one.

    Returns the active session, or None if the daemon is unreachable
    AND fallback is disabled (REPRYNTT_NO_FALLBACK=1).
    """
    global _active_session, _session_cm
    with _state_lock:
        if _active_session is not None and not _active_session.preempted:
            return _active_session
        if _active_session is not None and _active_session.preempted:
            _close_session_locked_safe()
        try:
            cm = motor_session(
                priority=Priority.OPERATOR,
                holder_label="teleop",
                wait_timeout_s=2.0,
            )
            sess = cm.__enter__()
        except Exception as e:
            logger.warning("teleop: failed to acquire motor lease: %s", e)
            return None
        _session_cm = cm
        _active_session = sess
        return sess


def _close_session_locked_safe() -> None:
    """Close the active session. Caller must hold _state_lock."""
    global _active_session, _session_cm
    if _session_cm is not None:
        try:
            _session_cm.__exit__(None, None, None)
        except Exception as e:
            logger.debug("teleop: session close failed: %s", e)
    _session_cm = None
    _active_session = None


def _save_demo_frame(action: str, speed: float, duration: float,
                     frame_bytes: Optional[bytes]) -> None:
    """Append a teleop demo row to today's JSONL. Best-effort, never raises."""
    try:
        DEMO_DIR.mkdir(parents=True, exist_ok=True)
        DEMO_FRAMES_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.time()
        log_path = DEMO_DIR / f"{time.strftime('%Y-%m-%d')}.jsonl"
        image_path = ""
        if frame_bytes:
            image_path = str(DEMO_FRAMES_DIR / f"{int(ts * 1000)}.jpg")
            with open(image_path, "wb") as f:
                f.write(frame_bytes)
        # Schema mirrors nav_experience so curate_nav_data ingests both.
        row = {
            "ts": ts,
            "image": image_path,
            "decision": action,
            "method": "teleop",
            "confidence": 1.0,           # human is ground truth
            "executed": True,
            "motor_success": True,
            "motor_method": "teleop",
            "perception_failed": False,
            "speed": speed,
            "duration": duration,
            "scene": "teleop demo",
        }
        with log_path.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as e:
        logger.debug("teleop demo save failed: %s", e)


def _grab_jpeg(sensor_id: int = 0, quality: int = 70) -> Optional[bytes]:
    """Grab one frame as JPEG. Tries our own broker first; falls back to the
    shared shm snapshot when another process owns the /dev/video flock
    (e.g. the agent daemon)."""
    try:
        import cv2
    except ImportError:
        return None
    # Short timeout — if we don't own the camera lock we'll never get one,
    # so fall through to the shm snapshot fast instead of hanging the UI.
    frame, _ts = _broker().get_latest(sensor_id, max_age_ms=1500, timeout_s=0.2)
    if frame is not None:
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if ok:
            return buf.tobytes()

    # Fallback: read the cross-process snapshot the OTHER process is writing.
    try:
        from repryntt.hardware.camera_broker import shm_snapshot_path
        path = shm_snapshot_path(sensor_id)
        if not os.path.exists(path):
            return None
        # Reject snapshots older than 2 s — a stale picture is worse than
        # a black screen because the operator can't tell motion has stopped.
        if (time.time() - os.path.getmtime(path)) > 2.0:
            return None
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None


# ── Routes ──────────────────────────────────────────────────────────


@teleop_bp.route("/")
def index():
    """Single-page driving UI. WASD on keydown sends /cmd; on keyup sends stop."""
    return Response(_HTML, mimetype="text/html")


@teleop_bp.route("/cmd", methods=["POST"])
def cmd():
    global _last_cmd_ts
    _ensure_deadman_running()
    body = request.get_json(silent=True) or {}
    action = str(body.get("action", "")).strip()
    if action not in ACTIONS:
        return jsonify({"ok": False, "error": f"unknown action {action!r}"}), 400
    speed = float(body.get("speed", 0.5))
    duration = max(0.0, min(MAX_CMD_DURATION_S, float(body.get("duration", 0.2))))

    sess = _ensure_session()
    if sess is None:
        return jsonify({
            "ok": False,
            "error": "motor daemon unreachable — start "
                     "`python -m repryntt.hardware.motor_daemon`",
        }), 503

    try:
        if action == "stop":
            res = sess.stop()
        elif action == "forward":
            res = sess.move_forward(speed, duration)
        elif action == "backward":
            res = sess.move_backward(speed, duration)
        elif action == "turn_left":
            res = sess.turn_left(speed, duration)
        elif action == "turn_right":
            res = sess.turn_right(speed, duration)
        else:
            res = {"success": False, "error": f"unhandled action {action}"}
    except Preempted:
        # Higher-priority client (safety / e-stop) took over. Drop our
        # session so the next /cmd re-acquires cleanly.
        with _state_lock:
            _close_session_locked_safe()
        return jsonify({"ok": False, "error": "preempted by safety"}), 409
    except MotorClientError as e:
        logger.warning("teleop /cmd motor error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 502
    except Exception as e:
        logger.exception("teleop /cmd failed")
        return jsonify({"ok": False, "error": str(e)}), 500

    with _state_lock:
        _last_cmd_ts = time.time()
        recording = _recording

    if recording and action != "stop":
        _save_demo_frame(action, speed, duration, _grab_jpeg())

    return jsonify({"ok": bool(res.get("success", True)), "result": res})


@teleop_bp.route("/frame.jpg")
def frame():
    sensor_id = int(request.args.get("cam", 0))
    quality = int(request.args.get("q", 70))
    jpg = _grab_jpeg(sensor_id, quality)
    if jpg is None:
        return Response(status=503)
    return Response(jpg, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store, max-age=0"})


@teleop_bp.route("/demo/start", methods=["POST"])
def demo_start():
    global _recording
    with _state_lock:
        _recording = True
    return jsonify({"ok": True, "recording": True})


@teleop_bp.route("/demo/stop", methods=["POST"])
def demo_stop():
    global _recording
    with _state_lock:
        _recording = False
    return jsonify({"ok": True, "recording": False})


@teleop_bp.route("/status")
def status():
    with _state_lock:
        return jsonify({
            "recording": _recording,
            "last_cmd_age_s": (time.time() - _last_cmd_ts) if _last_cmd_ts else None,
            "deadman_alive": bool(_deadman_thread and _deadman_thread.is_alive()),
            "deadman_timeout_s": DEADMAN_TIMEOUT_S,
            "max_cmd_duration_s": MAX_CMD_DURATION_S,
        })


_HTML = """<!doctype html>
<html><head><meta charset=utf-8><title>Andrew teleop</title>
<style>
  body{font-family:system-ui;background:#111;color:#eee;margin:0;padding:1em;}
  #cam{max-width:90vw;max-height:60vh;background:#000;display:block;margin:0 auto;border:1px solid #333}
  #log{font-family:monospace;font-size:12px;background:#000;padding:.5em;height:7em;overflow:auto;border:1px solid #333;margin-top:1em}
  .row{display:flex;align-items:center;gap:1em;margin:.6em 0;flex-wrap:wrap}
  button{padding:.4em .9em;background:#333;color:#eee;border:1px solid #555;cursor:pointer}
  button.on{background:#a40;border-color:#f60}
  label{font-size:.9em}
  input[type=range]{vertical-align:middle}
  kbd{background:#222;border:1px solid #444;padding:0 .3em;border-radius:3px;font-family:monospace}
</style></head><body>
<h2>Andrew teleop</h2>
<p>Hold <kbd>W</kbd>/<kbd>A</kbd>/<kbd>S</kbd>/<kbd>D</kbd> to drive. Release = stop.
<kbd>Space</kbd> = emergency stop. Buttons work on mobile.</p>
<img id=cam src="/teleop/frame.jpg" alt="cam">
<div class=row>
  <label>Speed: <input type=range id=speed min=0.2 max=1 step=0.1 value=0.6> <span id=speedv>0.6</span></label>
  <button id=rec>Record demo: OFF</button>
  <span id=stat></span>
</div>
<div class=row>
  <button data-act=turn_left>← left</button>
  <button data-act=forward>↑ forward</button>
  <button data-act=turn_right>right →</button>
  <button data-act=backward>↓ backward</button>
  <button data-act=stop>STOP</button>
</div>
<div id=log></div>
<script>
const log=(m)=>{const d=document.getElementById('log');d.innerHTML+=m+'\\n';d.scrollTop=d.scrollHeight};
const speed=document.getElementById('speed'),speedv=document.getElementById('speedv');
speed.oninput=()=>speedv.textContent=speed.value;

let lastSent=0, current=null, recording=false;
const SEND_INTERVAL=120; // ms — re-issue command on hold so deadman stays satisfied

async function send(action){
  const dur = action==='stop'?0:0.25;
  try{
    const r=await fetch('/teleop/cmd',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({action,speed:parseFloat(speed.value),duration:dur})});
    const j=await r.json();
    if(!j.ok) log('cmd '+action+' err: '+(j.error||JSON.stringify(j.result)));
  }catch(e){log('cmd '+action+' net err: '+e)}
}

function setCurrent(action){
  if(current===action) return;
  current=action;
  send(action);
  lastSent=Date.now();
}

setInterval(()=>{
  if(current && current!=='stop' && Date.now()-lastSent>SEND_INTERVAL){
    send(current); lastSent=Date.now();
  }
},SEND_INTERVAL);

const KEYS={'w':'forward','s':'backward','a':'turn_left','d':'turn_right',' ':'stop'};
window.addEventListener('keydown',e=>{
  const k=e.key.toLowerCase();
  if(k in KEYS){e.preventDefault(); setCurrent(KEYS[k]);}
});
window.addEventListener('keyup',e=>{
  const k=e.key.toLowerCase();
  if(k in KEYS && k!==' '){e.preventDefault(); setCurrent('stop');}
});

// Touch buttons
document.querySelectorAll('button[data-act]').forEach(b=>{
  const act=b.dataset.act;
  const start=ev=>{ev.preventDefault(); setCurrent(act);};
  const end=ev=>{ev.preventDefault(); setCurrent('stop');};
  b.addEventListener('mousedown',start);
  b.addEventListener('touchstart',start);
  if(act!=='stop'){b.addEventListener('mouseup',end);b.addEventListener('mouseleave',end);b.addEventListener('touchend',end);}
});

// Recording toggle
const recBtn=document.getElementById('rec');
recBtn.onclick=async()=>{
  const url=recording?'/teleop/demo/stop':'/teleop/demo/start';
  const r=await fetch(url,{method:'POST'});
  const j=await r.json();
  recording=j.recording;
  recBtn.textContent='Record demo: '+(recording?'ON':'OFF');
  recBtn.classList.toggle('on',recording);
};

// Status poll + frame refresh
setInterval(async()=>{
  try{
    const j=await(await fetch('/teleop/status')).json();
    document.getElementById('stat').textContent=
      'deadman:'+(j.deadman_alive?'on':'off')+' rec:'+(j.recording?'on':'off');
  }catch(e){}
},1000);
setInterval(()=>{
  document.getElementById('cam').src='/teleop/frame.jpg?t='+Date.now();
},250);

// Stop on tab hide / before unload
document.addEventListener('visibilitychange',()=>{if(document.hidden) setCurrent('stop');});
window.addEventListener('beforeunload',()=>setCurrent('stop'));
</script>
</body></html>"""
