"""Vision View — "Through Andrew's Eyes" web panel.

Live (and historical) view of what the agent is seeing through its cameras
plus the monocular-depth interpretation. Reads frames captured by
``nav_cortex`` / ``depth_perception`` from
``~/.repryntt/data/sensory/vision/<YYYY-MM-DD>/``.

Routes
------
- ``GET /vision``                       HTML viewer
- ``GET /api/vision/latest``            latest RGB + depth + zone proximity
- ``GET /api/vision/timeline``          paginated frame list (newest first)
- ``GET /vision/frame/<date>/<name>``   path-traversal-safe static frame server

Design choices
~~~~~~~~~~~~~~
- Pure directory-scan (no live coupling to NavCortex) → works whether or
  not the cortex thread is alive.
- Path-traversal hardened: every served file path is validated to live
  inside the canonical sensory/vision directory.
- Per-frame zone proximity is recovered from the depth-map filename when
  available (most monocular frames have only RGB + colorized depth; the
  numeric proximities live in NavCortex memory). The page degrades
  gracefully when zones are unknown.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, Response, jsonify, request, send_file

logger = logging.getLogger(__name__)

vision_bp = Blueprint("vision", __name__)


# ── Paths ───────────────────────────────────────────────────────────────────


def _vision_root() -> Path:
    return (Path.home() / ".repryntt" / "data" / "sensory" / "vision").resolve()


def _safe_frame_path(date_str: str, filename: str) -> Optional[Path]:
    """Resolve a (date, filename) pair to an absolute path strictly under
    the vision root. Rejects path traversal."""
    root = _vision_root()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str or ""):
        return None
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.(jpg|jpeg|png)", filename):
        return None
    candidate = (root / date_str / filename).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.exists() and candidate.is_file() else None


# ── Frame discovery ─────────────────────────────────────────────────────────


_RGB_RE = re.compile(r"^(?P<base>nav_\d+_\d{2}-\d{2}-\d{2}-\d+)\.(?:jpg|jpeg|png)$")
_DEPTH_RE = re.compile(r"^(?P<base>nav_\d+_\d{2}-\d{2}-\d{2}-\d+)_depth\.(?:jpg|jpeg|png)$")
_STEREO_L_RE = re.compile(r"^stereo_L_(?P<ts>\d{2}-\d{2}-\d{2})\.(?:jpg|jpeg|png)$")
_STEREO_DEPTH_RE = re.compile(r"^depth_(?P<ts>\d{2}-\d{2}-\d{2})\.(?:jpg|jpeg|png)$")


def _list_dates() -> List[str]:
    root = _vision_root()
    if not root.exists():
        return []
    dates = [
        p.name for p in root.iterdir()
        if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)
    ]
    return sorted(dates, reverse=True)


def _frame_record(
    date_str: str,
    base: str,
    rgb_name: str,
    depth_name: Optional[str],
    mtime: float,
) -> Dict[str, Any]:
    return {
        "date": date_str,
        "base": base,
        "rgb": rgb_name,
        "rgb_url": f"/vision/frame/{date_str}/{rgb_name}",
        "depth": depth_name,
        "depth_url": f"/vision/frame/{date_str}/{depth_name}" if depth_name else None,
        "mtime": mtime,
        "mtime_iso": datetime.utcfromtimestamp(mtime).isoformat() + "Z",
    }


def _scan_date(date_str: str) -> List[Dict[str, Any]]:
    """Pair RGB frames with their corresponding depth maps for one date."""
    root = _vision_root()
    day = root / date_str
    if not day.exists() or not day.is_dir():
        return []

    rgb_by_base: Dict[str, Tuple[str, float]] = {}
    depth_by_base: Dict[str, str] = {}

    for entry in day.iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue

        # Monocular nav frames
        m = _DEPTH_RE.match(name)
        if m:
            depth_by_base[m.group("base")] = name
            continue
        m = _RGB_RE.match(name)
        if m:
            rgb_by_base[m.group("base")] = (name, mtime)
            continue

        # Stereo pairs (left frame + depth_<ts>.jpg)
        m = _STEREO_L_RE.match(name)
        if m:
            rgb_by_base[f"stereo_{m.group('ts')}"] = (name, mtime)
            continue
        m = _STEREO_DEPTH_RE.match(name)
        if m:
            depth_by_base[f"stereo_{m.group('ts')}"] = name

    records: List[Dict[str, Any]] = []
    for base, (rgb_name, mtime) in rgb_by_base.items():
        records.append(_frame_record(
            date_str=date_str,
            base=base,
            rgb_name=rgb_name,
            depth_name=depth_by_base.get(base),
            mtime=mtime,
        ))
    records.sort(key=lambda r: r["mtime"], reverse=True)
    return records


def _scan_recent(limit: int = 50) -> List[Dict[str, Any]]:
    """Return the ``limit`` most-recent frame records across all dates."""
    out: List[Dict[str, Any]] = []
    for date_str in _list_dates():
        out.extend(_scan_date(date_str))
        if len(out) >= limit * 2:  # over-collect, then trim — handles uneven days
            break
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out[:limit]


# ── Live zone proximity (best-effort) ───────────────────────────────────────


def _live_proximity() -> Dict[str, Any]:
    """Best-effort fetch of current StereoDepth from NavCortex if reachable."""
    try:
        from repryntt.hardware.nav_cortex import NavCortex  # type: ignore
    except Exception:
        return {"available": False, "reason": "nav_cortex import failed"}

    inst = getattr(NavCortex, "_instance", None)
    if inst is None:
        return {"available": False, "reason": "no NavCortex singleton in this process"}

    depth = getattr(inst, "_last_depth", None)
    if depth is None:
        return {"available": False, "reason": "no recent depth capture"}

    return {
        "available": True,
        "left": getattr(depth, "left_proximity", None),
        "center": getattr(depth, "center_proximity", None),
        "right": getattr(depth, "right_proximity", None),
        "min_distance_cm": getattr(depth, "min_distance_cm", None),
        "compute_time_ms": getattr(depth, "compute_time_ms", None),
    }


# ── Recent perception events (best-effort) ──────────────────────────────────


def _recent_perception(limit: int = 10) -> List[Dict[str, Any]]:
    """Pull the most recent perception-flavoured events from the ops audit log."""
    log_dir = Path.home() / ".repryntt" / "workspace" / "telemetry"
    if not log_dir.exists():
        return []

    today = datetime.utcnow().date().isoformat()
    candidates = sorted(
        (p for p in log_dir.iterdir() if p.suffix == ".jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:2]
    if not candidates:
        return []

    import json
    keep_types = {
        "perception", "animal_detected", "presence_detected", "presence_lost",
        "audio_rejected", "audio_received",
    }
    events: List[Dict[str, Any]] = []
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    et = ev.get("event_type") or ev.get("type") or ""
                    if et in keep_types or "perception" in et:
                        events.append(ev)
        except Exception:
            continue

    events.sort(key=lambda e: e.get("ts") or e.get("time") or "", reverse=True)
    _ = today  # silence linter
    return events[:limit]


# ── API ─────────────────────────────────────────────────────────────────────


@vision_bp.route("/api/vision/latest")
def api_latest() -> Response:
    """Latest RGB + depth + per-zone proximity + recent perception events."""
    recent = _scan_recent(limit=1)
    latest = recent[0] if recent else None
    payload = {
        "frame": latest,
        "proximity": _live_proximity(),
        "perception_events": _recent_perception(limit=8),
        "server_time": datetime.utcnow().isoformat() + "Z",
    }
    return jsonify(payload)


@vision_bp.route("/api/vision/timeline")
def api_timeline() -> Response:
    """Paginated newest-first timeline of frames."""
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
    except ValueError:
        limit = 50
    date_str = request.args.get("date")
    if date_str and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
        frames = _scan_date(date_str)[:limit]
    else:
        frames = _scan_recent(limit=limit)
    return jsonify({
        "count": len(frames),
        "frames": frames,
        "available_dates": _list_dates(),
    })


@vision_bp.route("/vision/frame/<date>/<filename>")
def serve_frame(date: str, filename: str) -> Response:
    path = _safe_frame_path(date, filename)
    if path is None:
        return Response("not found", status=404)
    return send_file(str(path), mimetype="image/jpeg", max_age=60)


# ── HTML viewer ─────────────────────────────────────────────────────────────


_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Through Andrew's Eyes</title>
  <style>
    :root {
      --bg: #0b0d10;
      --panel: #14181d;
      --line: #232a33;
      --text: #e6edf3;
      --muted: #8b97a3;
      --accent: #6cf;
      --warn: #f6a;
      --good: #6f6;
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    header { padding: 14px 20px; border-bottom: 1px solid var(--line); display: flex; align-items: baseline; gap: 16px; }
    header h1 { font-size: 18px; margin: 0; font-weight: 600; }
    header .sub { color: var(--muted); font-size: 13px; }
    header .live { margin-left: auto; font-size: 12px; color: var(--good); }
    main { display: grid; grid-template-columns: 1fr 1fr 320px; gap: 16px; padding: 16px; }
    @media (max-width: 1100px) { main { grid-template-columns: 1fr 1fr; } #side { grid-column: 1 / -1; } }
    @media (max-width: 720px)  { main { grid-template-columns: 1fr; } }
    .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px; }
    .panel h2 { margin: 0 0 10px; font-size: 13px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); font-weight: 600; }
    .img-wrap { aspect-ratio: 16/9; background: #000; border-radius: 4px; overflow: hidden; display: flex; align-items: center; justify-content: center; }
    .img-wrap img { width: 100%; height: 100%; object-fit: contain; }
    .img-wrap .placeholder { color: var(--muted); font-size: 13px; }
    .meta { margin-top: 8px; font-size: 12px; color: var(--muted); display: flex; justify-content: space-between; }
    .zones { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; margin-top: 12px; }
    .zone { background: #0b0d10; border: 1px solid var(--line); border-radius: 4px; padding: 8px; text-align: center; }
    .zone .name { font-size: 10px; text-transform: uppercase; color: var(--muted); letter-spacing: 0.08em; }
    .zone .val { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 22px; margin-top: 4px; }
    .zone.warn .val { color: var(--warn); }
    .zone.ok .val { color: var(--good); }
    .bar { height: 4px; background: var(--line); border-radius: 2px; margin-top: 6px; overflow: hidden; }
    .bar > div { height: 100%; background: var(--accent); width: 0; transition: width .25s ease; }
    .zone.warn .bar > div { background: var(--warn); }
    .zone.ok .bar > div { background: var(--good); }
    .stat-row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid var(--line); font-size: 13px; }
    .stat-row:last-child { border-bottom: 0; }
    .stat-row .k { color: var(--muted); }
    .events { max-height: 280px; overflow-y: auto; }
    .event { padding: 8px; margin-bottom: 6px; background: #0b0d10; border-left: 3px solid var(--accent); border-radius: 0 4px 4px 0; font-size: 12px; }
    .event .et { color: var(--accent); text-transform: uppercase; font-size: 10px; letter-spacing: 0.06em; }
    .event .ts { color: var(--muted); float: right; }
    .event .body { margin-top: 4px; line-height: 1.4; word-break: break-word; }
    .event.warn { border-left-color: var(--warn); }
    .event.warn .et { color: var(--warn); }
    .timeline { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 6px; padding: 10px 16px 20px; }
    .thumb { aspect-ratio: 16/9; background: #000; border: 1px solid var(--line); border-radius: 4px; overflow: hidden; cursor: pointer; position: relative; }
    .thumb img { width: 100%; height: 100%; object-fit: cover; }
    .thumb.active { border-color: var(--accent); }
    .thumb .ts { position: absolute; bottom: 2px; right: 4px; font-size: 10px; color: #fff; background: rgba(0,0,0,0.6); padding: 1px 4px; border-radius: 2px; }
  </style>
</head>
<body>
  <header>
    <h1>Through Andrew's Eyes</h1>
    <span class="sub">monocular RGB · Depth Anything v2 · stereo IMX219</span>
    <span class="live" id="live-indicator">● live</span>
  </header>
  <main>
    <section class="panel">
      <h2>RGB (left camera)</h2>
      <div class="img-wrap"><img id="rgb-img" alt="latest RGB frame" /><span id="rgb-placeholder" class="placeholder">no frame yet</span></div>
      <div class="meta"><span id="rgb-name">—</span><span id="rgb-ts">—</span></div>
    </section>
    <section class="panel">
      <h2>Depth heatmap</h2>
      <div class="img-wrap"><img id="depth-img" alt="latest depth heatmap" /><span id="depth-placeholder" class="placeholder">no depth map</span></div>
      <div class="zones">
        <div class="zone" id="zone-left"><div class="name">left</div><div class="val">—</div><div class="bar"><div></div></div></div>
        <div class="zone" id="zone-center"><div class="name">center</div><div class="val">—</div><div class="bar"><div></div></div></div>
        <div class="zone" id="zone-right"><div class="name">right</div><div class="val">—</div><div class="bar"><div></div></div></div>
      </div>
    </section>
    <aside class="panel" id="side">
      <h2>State</h2>
      <div class="stat-row"><span class="k">distance to nearest</span><span id="stat-dist">—</span></div>
      <div class="stat-row"><span class="k">depth compute</span><span id="stat-compute">—</span></div>
      <div class="stat-row"><span class="k">frames today</span><span id="stat-count">—</span></div>
      <div class="stat-row"><span class="k">last update</span><span id="stat-update">—</span></div>
      <h2 style="margin-top:16px">Recent perception</h2>
      <div class="events" id="events"><div class="event"><div class="et">idle</div><div class="body">no recent events</div></div></div>
    </aside>
  </main>
  <h2 style="padding: 0 20px; margin: 12px 0 8px; font-size: 13px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted);">Timeline</h2>
  <div class="timeline" id="timeline"></div>

  <script>
    let activeBase = null;

    function tsFromBase(base) {
      const m = base.match(/(\\d{2})-(\\d{2})-(\\d{2})/);
      return m ? `${m[1]}:${m[2]}:${m[3]}` : "";
    }

    function setZone(id, val) {
      const el = document.getElementById(id);
      if (!el) return;
      const valEl = el.querySelector('.val');
      const bar = el.querySelector('.bar > div');
      if (val === null || val === undefined) {
        valEl.textContent = '—';
        bar.style.width = '0';
        el.classList.remove('warn', 'ok');
        return;
      }
      valEl.textContent = val.toFixed(2);
      bar.style.width = (val * 100).toFixed(0) + '%';
      el.classList.toggle('warn', val >= 0.6);
      el.classList.toggle('ok', val < 0.3);
    }

    function showFrame(frame) {
      const rgb = document.getElementById('rgb-img');
      const rgbP = document.getElementById('rgb-placeholder');
      const depth = document.getElementById('depth-img');
      const depthP = document.getElementById('depth-placeholder');
      if (frame && frame.rgb_url) {
        rgb.src = frame.rgb_url; rgb.style.display = ''; rgbP.style.display = 'none';
      } else {
        rgb.style.display = 'none'; rgbP.style.display = '';
      }
      if (frame && frame.depth_url) {
        depth.src = frame.depth_url; depth.style.display = ''; depthP.style.display = 'none';
      } else {
        depth.style.display = 'none'; depthP.style.display = '';
      }
      document.getElementById('rgb-name').textContent = frame ? frame.rgb : '—';
      document.getElementById('rgb-ts').textContent = frame ? new Date(frame.mtime_iso).toLocaleTimeString() : '—';
      activeBase = frame ? frame.base : null;
      document.querySelectorAll('.thumb').forEach(t => t.classList.toggle('active', t.dataset.base === activeBase));
    }

    function renderEvents(events) {
      const root = document.getElementById('events');
      if (!events || !events.length) {
        root.innerHTML = '<div class="event"><div class="et">idle</div><div class="body">no recent events</div></div>';
        return;
      }
      root.innerHTML = events.slice(0, 12).map(ev => {
        const t = ev.event_type || ev.type || 'event';
        const summary = ev.summary || ev.content || ev.message || JSON.stringify(ev).slice(0, 200);
        const time = (ev.ts || ev.time || '').replace('T', ' ').slice(11, 19);
        const warn = /lost|rejected|fail/.test(t);
        return `<div class="event ${warn ? 'warn' : ''}"><div class="et">${t}<span class="ts">${time}</span></div><div class="body">${escape(summary)}</div></div>`;
      }).join('');
    }

    function escape(s) {
      return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }

    async function refreshLatest() {
      try {
        const r = await fetch('/api/vision/latest');
        const d = await r.json();
        if (d.frame) showFrame(d.frame);
        const p = d.proximity || {};
        if (p.available) {
          setZone('zone-left', p.left);
          setZone('zone-center', p.center);
          setZone('zone-right', p.right);
          document.getElementById('stat-dist').textContent = p.min_distance_cm != null ? p.min_distance_cm.toFixed(1) + ' cm' : '—';
          document.getElementById('stat-compute').textContent = p.compute_time_ms != null ? p.compute_time_ms + ' ms' : '—';
        } else {
          setZone('zone-left', null); setZone('zone-center', null); setZone('zone-right', null);
          document.getElementById('stat-dist').textContent = '—';
          document.getElementById('stat-compute').textContent = p.reason || '—';
        }
        document.getElementById('stat-update').textContent = new Date(d.server_time).toLocaleTimeString();
        renderEvents(d.perception_events);
      } catch (e) {
        document.getElementById('live-indicator').textContent = '● offline';
        document.getElementById('live-indicator').style.color = 'var(--warn)';
      }
    }

    async function refreshTimeline() {
      try {
        const r = await fetch('/api/vision/timeline?limit=24');
        const d = await r.json();
        document.getElementById('stat-count').textContent = d.count;
        const tl = document.getElementById('timeline');
        tl.innerHTML = d.frames.map(f => `
          <div class="thumb ${f.base === activeBase ? 'active' : ''}" data-base="${f.base}" onclick='showFrame(${JSON.stringify(f).replace(/'/g, "&#39;")})'>
            <img src="${f.rgb_url}" loading="lazy" />
            <span class="ts">${tsFromBase(f.base)}</span>
          </div>
        `).join('');
      } catch (e) { /* keep prior timeline */ }
    }

    refreshLatest();
    refreshTimeline();
    setInterval(refreshLatest, 3000);
    setInterval(refreshTimeline, 15000);
  </script>
</body>
</html>
"""


@vision_bp.route("/vision")
def vision_page() -> Response:
    return Response(_HTML, mimetype="text/html")
