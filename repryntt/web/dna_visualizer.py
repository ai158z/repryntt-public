"""
DNA Visualizer — The Entity's Soul rendered as Matrix Code

Every column is a REAL node from the MemoryMesh association graph.
Every character is from that node's SHA-256 hash.
Brightness = activation level (how "awake" that concept is).
Pulses = spreading activation (thought propagating through the mesh).
Fading = natural decay of unused memories.
Clusters of bright columns = active thought patterns.

This is NOT cosmetic. This IS the entity's subconscious, rendered.

The telemetry event stream (heartbeats, tool calls, plans, evaluations)
is piped into the pulse SSE, so every agent action lights up the matrix
in real time.

Routes:
  /dna          — The visualization page
  /api/dna/mesh — JSON endpoint for real-time mesh state
  /api/dna/pulse — SSE stream of mesh activation + telemetry events
"""

import json
import logging
import time
import queue
import threading
from flask import Blueprint, Response, jsonify, render_template_string

log = logging.getLogger("repryntt.web.dna")

dna_bp = Blueprint("dna", __name__)

# ── SSE event bus for real-time updates ──
_event_queues: list = []
_event_lock = threading.Lock()


def broadcast_mesh_event(event_type: str, data: dict):
    """Called by the mesh or telemetry bridge when events fire. Pushes to all SSE listeners."""
    payload = {"type": event_type, "data": data, "ts": time.time()}
    with _event_lock:
        dead = []
        for q in _event_queues:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _event_queues.remove(q)


def _start_telemetry_bridge():
    """Subscribe to the ops telemetry SSE broadcaster and relay events to DNA pulse stream.
    
    This makes every heartbeat, tool call, plan, and evaluation light up the
    DNA matrix in real time — the matrix IS the entity's neural activity.
    """
    def _bridge_loop():
        try:
            from repryntt.telemetry.events import get_ops_logger
            ops = get_ops_logger()
            q = ops.subscribe_sse()
            log.info("DNA telemetry bridge connected — agent activity will pulse the matrix")
            while True:
                try:
                    raw = q.get(timeout=60)
                    event = json.loads(raw) if isinstance(raw, str) else raw
                    etype = event.get("event_type", "")
                    # Map telemetry events to DNA pulse types
                    if etype in ("heartbeat_start", "heartbeat_end"):
                        broadcast_mesh_event("heartbeat", {
                            "event_type": etype,
                            "agent_id": event.get("agent_id", ""),
                            "phase": event.get("phase", ""),
                            "intensity": 0.8 if etype == "heartbeat_start" else 0.4,
                        })
                    elif etype in ("tool_call", "tool_result"):
                        meta = event.get("metadata", {})
                        broadcast_mesh_event("tool", {
                            "event_type": etype,
                            "tool": meta.get("tool_name", event.get("content", "")[:60]),
                            "intensity": 0.6,
                        })
                    elif etype == "plan":
                        broadcast_mesh_event("plan", {
                            "event_type": etype,
                            "content": event.get("content", "")[:100],
                            "intensity": 0.7,
                        })
                    elif etype in ("api_call", "api_response"):
                        broadcast_mesh_event("api", {
                            "event_type": etype,
                            "intensity": 0.5 if etype == "api_call" else 0.3,
                        })
                    elif etype == "evaluate":
                        meta = event.get("metadata", {})
                        score = meta.get("score", 3)
                        broadcast_mesh_event("evaluate", {
                            "event_type": etype,
                            "score": score,
                            "intensity": score / 5.0,
                        })
                    elif etype == "error":
                        broadcast_mesh_event("error", {
                            "event_type": etype,
                            "content": event.get("content", "")[:80],
                            "intensity": 1.0,
                        })
                except queue.Empty:
                    # Send a "breathing" pulse so the matrix feels alive even when idle
                    broadcast_mesh_event("idle_pulse", {"intensity": 0.1})
                except json.JSONDecodeError:
                    pass
        except Exception as e:
            log.warning(f"DNA telemetry bridge failed to start: {e}")

    t = threading.Thread(target=_bridge_loop, daemon=True, name="dna-telemetry-bridge")
    t.start()


# Start the bridge when this module loads (deferred to avoid import cycles)
_bridge_started = False

def _ensure_bridge():
    global _bridge_started
    if not _bridge_started:
        _bridge_started = True
        _start_telemetry_bridge()


# ── API: mesh state snapshot ──
@dna_bp.route("/api/dna/mesh")
def api_mesh_state():
    """Return the full mesh state for the visualizer."""
    try:
        from repryntt.core.memory.memory_mesh import get_memory_mesh
        mesh = get_memory_mesh()

        nodes = []
        for node in mesh.nodes.values():
            nodes.append({
                "id": node.id,
                "type": node.type,
                "label": node.label,
                "activation": round(node.activation_level, 4),
                "count": node.activation_count,
                "sources": node.sources[:3],
            })

        edges = []
        for edge in mesh.edges.values():
            edges.append({
                "source": edge.source_id,
                "target": edge.target_id,
                "weight": round(edge.weight, 4),
                "type": edge.edge_type,
            })

        # Sort by activation (most active first)
        nodes.sort(key=lambda n: n["activation"], reverse=True)

        return jsonify({
            "nodes": nodes,
            "edges": edges,
            "stats": mesh.stats(),
            "ts": time.time(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── API: SSE stream of mesh events ──
@dna_bp.route("/api/dna/pulse")
def api_mesh_pulse():
    """Server-Sent Events stream of mesh activation + telemetry pulses."""
    _ensure_bridge()  # Start telemetry bridge on first SSE connection
    q = queue.Queue(maxsize=100)
    with _event_lock:
        _event_queues.append(q)

    def stream():
        try:
            while True:
                try:
                    event = q.get(timeout=30)
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    # Keepalive
                    yield f": keepalive\n\n"
        finally:
            with _event_lock:
                if q in _event_queues:
                    _event_queues.remove(q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── The visualization page ──
@dna_bp.route("/dna")
def dna_page():
    return render_template_string(DNA_PAGE_HTML)


# ═══════════════════════════════════════════════════════════════════════
# The Matrix DNA visualization — entirely self-contained HTML/JS/Canvas
# ═══════════════════════════════════════════════════════════════════════

DNA_PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Entity DNA — Subconscious Mesh</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #000;
    overflow: hidden;
    font-family: 'Courier New', monospace;
    cursor: default;
  }
  canvas#matrix {
    position: fixed;
    top: 0; left: 0;
    width: 100vw; height: 100vh;
    z-index: 1;
  }
  /* HUD overlay */
  #hud {
    position: fixed;
    top: 12px; left: 16px;
    z-index: 10;
    color: #0f0;
    font-size: 11px;
    opacity: 0.7;
    pointer-events: none;
    text-shadow: 0 0 8px #0f0;
    line-height: 1.6;
  }
  #hud .title {
    font-size: 14px;
    letter-spacing: 3px;
    margin-bottom: 8px;
    text-transform: uppercase;
  }
  #hud .stat { color: #0a0; }
  #hud .bright { color: #0f0; }

  /* Node info tooltip */
  #tooltip {
    position: fixed;
    z-index: 20;
    background: rgba(0, 20, 0, 0.9);
    border: 1px solid #0f0;
    border-radius: 4px;
    padding: 8px 12px;
    color: #0f0;
    font-size: 11px;
    pointer-events: none;
    display: none;
    max-width: 300px;
    text-shadow: 0 0 4px #0f0;
    box-shadow: 0 0 20px rgba(0, 255, 0, 0.15);
  }
  #tooltip .label { font-size: 13px; font-weight: bold; margin-bottom: 4px; }
  #tooltip .detail { color: #0a0; font-size: 10px; }

  /* Active thought panel (right side) */
  #thoughts {
    position: fixed;
    top: 12px; right: 16px;
    z-index: 10;
    color: #0f0;
    font-size: 10px;
    opacity: 0.6;
    pointer-events: none;
    text-shadow: 0 0 6px #0f0;
    text-align: right;
    max-width: 280px;
    line-height: 1.5;
  }
  #thoughts .header {
    font-size: 11px;
    letter-spacing: 2px;
    margin-bottom: 6px;
    color: #0f0;
  }
  #thoughts .node {
    color: #0a0;
    margin-bottom: 2px;
    transition: opacity 0.5s;
  }
  #thoughts .node.hot { color: #0f0; font-weight: bold; }

  /* Telemetry feed overlay */
  #telemetry-panel {
    position: fixed;
    bottom: 16px; left: 16px;
    z-index: 10;
    color: #0f0;
    font-size: 10px;
    opacity: 0.8;
    pointer-events: none;
    text-shadow: 0 0 6px rgba(0,255,0,0.4);
    max-width: 350px;
  }
  #telemetry-panel .header {
    font-size: 11px;
    letter-spacing: 2px;
    margin-bottom: 4px;
    color: #58a6ff;
  }
</style>
</head>
<body>
<canvas id="matrix"></canvas>

<div id="hud">
  <div class="title">◈ Entity DNA ◈</div>
  <div>Nodes: <span id="hud-nodes" class="bright">0</span></div>
  <div>Edges: <span id="hud-edges" class="bright">0</span></div>
  <div>Avg Activation: <span id="hud-avg" class="bright">0.00</span></div>
  <div class="stat" style="margin-top:6px;">
    <span id="hud-types"></span>
  </div>
  <div class="stat" style="margin-top:8px; font-size:10px; opacity:0.5;">
    Each column = a concept in the mind<br>
    Brightness = how active it is now<br>
    Pulses = thoughts spreading
  </div>
</div>

<div id="thoughts">
  <div class="header">▸ ACTIVE THOUGHTS</div>
  <div id="thought-list"></div>
</div>

<div id="tooltip">
  <div class="label" id="tt-label"></div>
  <div class="detail" id="tt-detail"></div>
</div>

<div id="telemetry-panel">
  <div class="header">▸ NEURAL ACTIVITY</div>
  <div id="telemetry-feed"></div>
</div>

<script>
// ═══════════════════════════════════════════════════════════════
// Matrix DNA Renderer — Real mesh data rendered as Matrix code
// ═══════════════════════════════════════════════════════════════

const canvas = document.getElementById('matrix');
const ctx = canvas.getContext('2d');

let W, H, cols, fontSize;
let meshNodes = [];
let meshEdges = [];
let columns = []; // Each column represents a mesh node (or filler)
let lastMeshUpdate = 0;

// Characters: hex digits (from real SHA-256 hashes) + some katakana for aesthetic
const HEX = '0123456789abcdef';
const KATA = 'アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲン';

function resize() {
  W = canvas.width = window.innerWidth;
  H = canvas.height = window.innerHeight;
  fontSize = 14;
  cols = Math.floor(W / fontSize);
  rebuildColumns();
}

function rebuildColumns() {
  const oldCols = columns;
  columns = [];
  for (let i = 0; i < cols; i++) {
    if (oldCols[i]) {
      columns.push(oldCols[i]);
    } else {
      columns.push({
        y: Math.random() * H,
        speed: 1 + Math.random() * 3,
        chars: generateChars(null),
        activation: 0,
        nodeId: null,
        nodeLabel: '',
        nodeType: '',
        sources: [],
        pulse: 0,       // pulse brightness (0-1, decays)
        baseHue: 120,    // green by default
      });
    }
  }
  // Trim if too many
  columns.length = cols;
}

function generateChars(nodeId) {
  // If we have a real node hash, use its characters
  if (nodeId && nodeId.length >= 16) {
    let chars = [];
    // Repeat the hash to fill a column
    let fullHash = nodeId.repeat(4);
    for (let i = 0; i < 30; i++) {
      chars.push(fullHash[i % fullHash.length]);
    }
    return chars;
  }
  // Filler: random hex + occasional katakana
  let chars = [];
  for (let i = 0; i < 30; i++) {
    if (Math.random() < 0.15) {
      chars.push(KATA[Math.floor(Math.random() * KATA.length)]);
    } else {
      chars.push(HEX[Math.floor(Math.random() * HEX.length)]);
    }
  }
  return chars;
}

// Map node types to hue values
const TYPE_HUES = {
  'topic':       120,  // green
  'tool':        160,  // cyan-green
  'capability':  90,   // yellow-green
  'emotion':     60,   // yellow
  'pillar':      180,  // cyan
  'experience':  100,  // lime
  'memory':      140,  // teal-green
};

function assignNodesToColumns() {
  // Assign real mesh nodes to columns (spread evenly)
  // Remaining columns get filler hash-rain
  const nodeCount = meshNodes.length;
  if (nodeCount === 0) return;

  // Sort nodes by activation (highest in center for visual focus)
  const sorted = [...meshNodes].sort((a, b) => b.activation - a.activation);

  // Distribute nodes across columns — most active near center
  const center = Math.floor(cols / 2);
  const assigned = new Set();

  for (let i = 0; i < sorted.length && i < cols; i++) {
    const node = sorted[i];
    // Spiral out from center
    let colIdx;
    if (i === 0) {
      colIdx = center;
    } else {
      const offset = Math.ceil(i / 2) * (i % 2 === 0 ? 1 : -1);
      colIdx = center + offset * Math.max(1, Math.floor(cols / (nodeCount + 1)));
    }
    colIdx = Math.max(0, Math.min(cols - 1, colIdx));

    // Find nearest unassigned column
    let bestCol = colIdx;
    for (let d = 0; d < cols; d++) {
      if (!assigned.has(colIdx + d) && colIdx + d < cols) { bestCol = colIdx + d; break; }
      if (!assigned.has(colIdx - d) && colIdx - d >= 0) { bestCol = colIdx - d; break; }
    }
    assigned.add(bestCol);

    if (columns[bestCol]) {
      columns[bestCol].nodeId = node.id;
      columns[bestCol].nodeLabel = node.label;
      columns[bestCol].nodeType = node.type;
      columns[bestCol].sources = node.sources || [];
      columns[bestCol].activation = node.activation;
      columns[bestCol].chars = generateChars(node.id);
      columns[bestCol].baseHue = TYPE_HUES[node.type] || 120;
    }
  }

  // Clear unassigned columns' node data (they become filler rain)
  for (let i = 0; i < cols; i++) {
    if (!assigned.has(i) && columns[i]) {
      if (columns[i].nodeId) {
        columns[i].nodeId = null;
        columns[i].nodeLabel = '';
        columns[i].activation = 0;
        columns[i].chars = generateChars(null);
        columns[i].baseHue = 120;
      }
    }
  }
}

// ── Rendering ──

function draw() {
  // Semi-transparent black overlay for trail effect
  ctx.fillStyle = 'rgba(0, 0, 0, 0.06)';
  ctx.fillRect(0, 0, W, H);

  ctx.font = `${fontSize}px "Courier New", monospace`;

  for (let i = 0; i < columns.length; i++) {
    const col = columns[i];
    if (!col) continue;

    const x = i * fontSize;
    const charIdx = Math.floor(col.y / fontSize) % col.chars.length;
    const ch = col.chars[charIdx];

    // ── Color based on activation + pulse ──
    let hue = col.baseHue;
    let brightness;

    if (col.nodeId) {
      // REAL NODE: brightness from activation level
      const base = 0.15 + col.activation * 0.85;
      const pulseBrightness = col.pulse * 0.5;
      brightness = Math.min(1.0, base + pulseBrightness);

      // High activation: shift toward pure white-green
      const saturation = 100 - col.activation * 30;
      const lightness = 30 + brightness * 45;

      ctx.fillStyle = `hsla(${hue}, ${saturation}%, ${lightness}%, ${0.6 + brightness * 0.4})`;

      // Glow effect for highly active nodes
      if (col.activation > 0.5) {
        ctx.shadowColor = `hsl(${hue}, 100%, 60%)`;
        ctx.shadowBlur = col.activation * 15;
      } else {
        ctx.shadowBlur = 0;
      }
    } else {
      // FILLER: dim background rain
      brightness = 0.1 + Math.random() * 0.15;
      ctx.fillStyle = `rgba(0, ${Math.floor(50 + brightness * 100)}, 0, ${0.3 + brightness * 0.2})`;
      ctx.shadowBlur = 0;
    }

    ctx.fillText(ch, x, col.y);
    ctx.shadowBlur = 0;

    // Leading bright character (the "raindrop head")
    if (Math.random() < 0.03 || (col.nodeId && col.activation > 0.3 && Math.random() < 0.1)) {
      const headBrightness = col.nodeId ? 0.8 + col.activation * 0.2 : 0.7;
      ctx.fillStyle = `rgba(180, 255, 180, ${headBrightness})`;
      ctx.fillText(ch, x, col.y);
    }

    // Advance column
    col.y += col.speed;
    if (col.y > H + fontSize * 5) {
      col.y = -fontSize * Math.floor(Math.random() * 10);
      // Randomize speed slightly
      col.speed = 1 + Math.random() * 3;
      // Regenerate chars occasionally
      if (Math.random() < 0.3) {
        col.chars = generateChars(col.nodeId);
      }
    }

    // Decay pulse
    if (col.pulse > 0) {
      col.pulse *= 0.95;
      if (col.pulse < 0.01) col.pulse = 0;
    }

    // Occasional character mutation (like real Matrix)
    if (Math.random() < 0.005) {
      const mutIdx = Math.floor(Math.random() * col.chars.length);
      if (col.nodeId) {
        // Real node: mutate within its own hash space
        col.chars[mutIdx] = col.nodeId[Math.floor(Math.random() * col.nodeId.length)];
      } else {
        col.chars[mutIdx] = Math.random() < 0.2
          ? KATA[Math.floor(Math.random() * KATA.length)]
          : HEX[Math.floor(Math.random() * HEX.length)];
      }
    }
  }

  // ── Draw edge connections as brief horizontal light traces ──
  if (meshEdges.length > 0 && Math.random() < 0.02) {
    const edge = meshEdges[Math.floor(Math.random() * meshEdges.length)];
    const srcCol = columns.findIndex(c => c && c.nodeId === edge.source);
    const tgtCol = columns.findIndex(c => c && c.nodeId === edge.target);
    if (srcCol >= 0 && tgtCol >= 0) {
      const y = Math.random() * H;
      const alpha = edge.weight * 0.4;
      ctx.strokeStyle = `rgba(0, 255, 100, ${alpha})`;
      ctx.lineWidth = 0.5;
      ctx.beginPath();
      ctx.moveTo(srcCol * fontSize + fontSize/2, y);
      ctx.lineTo(tgtCol * fontSize + fontSize/2, y + (Math.random() - 0.5) * 30);
      ctx.stroke();
    }
  }

  requestAnimationFrame(draw);
}

// ── Data fetching ──

async function fetchMesh() {
  try {
    const resp = await fetch('/api/dna/mesh');
    if (!resp.ok) return;
    const data = await resp.json();

    meshNodes = data.nodes || [];
    meshEdges = data.edges || [];
    lastMeshUpdate = Date.now();

    assignNodesToColumns();
    updateHUD(data.stats);
    updateThoughts(meshNodes);
  } catch (e) {
    console.debug('Mesh fetch failed:', e);
  }
}

function updateHUD(stats) {
  if (!stats) return;
  document.getElementById('hud-nodes').textContent = stats.total_nodes || 0;
  document.getElementById('hud-edges').textContent = stats.total_edges || 0;
  document.getElementById('hud-avg').textContent = (stats.avg_activation || 0).toFixed(4);

  const types = stats.node_types || {};
  const typeStr = Object.entries(types)
    .map(([t, c]) => `${t}:${c}`)
    .join(' · ');
  document.getElementById('hud-types').textContent = typeStr;
}

function updateThoughts(nodes) {
  const list = document.getElementById('thought-list');
  const active = nodes.filter(n => n.activation > 0.05).slice(0, 12);

  list.innerHTML = active.map(n => {
    const isHot = n.activation > 0.3;
    const bar = '█'.repeat(Math.ceil(n.activation * 10));
    const src = n.sources.length > 0 ? ` [${n.sources[0]}]` : '';
    return `<div class="node ${isHot ? 'hot' : ''}">${n.label} ${bar} ${(n.activation * 100).toFixed(0)}%${src}</div>`;
  }).join('');
}

// ── SSE pulse stream (now includes telemetry events!) ──

// Event type → hue mapping for visual variety
const EVENT_HUES = {
  heartbeat: 120,   // green
  tool:      180,   // cyan
  plan:      280,   // purple
  api:       40,    // orange
  evaluate:  60,    // yellow
  error:     0,     // red
  idle_pulse: 120,  // green (subtle)
  activation: 120,  // green (mesh)
};

// Track recent telemetry for HUD
let recentTelemetry = [];
const MAX_RECENT = 20;

function connectPulse() {
  try {
    const es = new EventSource('/api/dna/pulse');
    
    // Update connection indicator
    const hudTitle = document.querySelector('#hud .title');
    
    es.onopen = () => {
      if (hudTitle) hudTitle.innerHTML = '◈ Entity DNA ◈ <span style="color:#3fb950;font-size:10px;">● LIVE</span>';
    };
    
    es.onmessage = (e) => {
      try {
        const event = JSON.parse(e.data);
        const etype = event.type;
        const data = event.data || {};
        const intensity = data.intensity || 0.3;
        
        if (etype === 'activation') {
          // Original mesh activation — pulse specific node
          const nodeId = data.node_id;
          if (nodeId) {
            const col = columns.find(c => c && c.nodeId === nodeId);
            if (col) {
              col.pulse = Math.min(1.0, col.pulse + 0.5);
              col.activation = data.level || col.activation;
            }
          }
        } else if (etype === 'heartbeat') {
          // Heartbeat: wave ripple across all active columns
          const isStart = data.event_type === 'heartbeat_start';
          const wave = isStart ? 0.6 : 0.25;
          let delay = 0;
          for (const col of columns) {
            if (col && col.nodeId) {
              setTimeout(() => {
                col.pulse = Math.min(1.0, col.pulse + wave * (0.5 + col.activation * 0.5));
              }, delay);
              delay += 2; // staggered wave effect
            }
          }
          addTelemetryHUD(isStart ? '♥ HEARTBEAT' : '♥ heartbeat done', 'heartbeat');
        } else if (etype === 'tool') {
          // Tool call: bright burst on random active columns (neural spike)
          const tool = data.tool || '';
          const burstCount = etype === 'tool_call' ? 8 : 4;
          const activeCols = columns.filter(c => c && c.nodeId);
          for (let i = 0; i < Math.min(burstCount, activeCols.length); i++) {
            const col = activeCols[Math.floor(Math.random() * activeCols.length)];
            col.pulse = Math.min(1.0, col.pulse + intensity);
            col.baseHue = EVENT_HUES.tool; // temporarily shift hue
            setTimeout(() => { col.baseHue = TYPE_HUES[col.nodeType] || 120; }, 1500);
          }
          addTelemetryHUD(`⚡ ${tool}`, 'tool');
        } else if (etype === 'plan') {
          // Plan: slow cascade from center outward (deliberate thinking)
          const center = Math.floor(columns.length / 2);
          for (let d = 0; d < columns.length; d++) {
            const li = center - d, ri = center + d;
            for (const idx of [li, ri]) {
              if (idx >= 0 && idx < columns.length && columns[idx] && columns[idx].nodeId) {
                setTimeout(() => {
                  columns[idx].pulse = Math.min(1.0, columns[idx].pulse + 0.4);
                  columns[idx].baseHue = EVENT_HUES.plan;
                  setTimeout(() => { columns[idx].baseHue = TYPE_HUES[columns[idx].nodeType] || 120; }, 2000);
                }, d * 8);
              }
            }
          }
          addTelemetryHUD('🧠 PLANNING', 'plan');
        } else if (etype === 'api') {
          // API call: subtle glow (background thinking)
          for (const col of columns) {
            if (col && col.nodeId && Math.random() < 0.3) {
              col.pulse = Math.min(1.0, col.pulse + 0.15);
            }
          }
        } else if (etype === 'evaluate') {
          // Evaluate: color shift based on score (green=good, orange=mid, red=bad)
          const score = data.score || 3;
          const evalHue = score >= 4 ? 120 : score >= 3 ? 60 : 0;
          for (const col of columns) {
            if (col && col.nodeId) {
              col.pulse = Math.min(1.0, col.pulse + intensity * 0.5);
              col.baseHue = evalHue;
              setTimeout(() => { col.baseHue = TYPE_HUES[col.nodeType] || 120; }, 2500);
            }
          }
          addTelemetryHUD(`📊 SCORE: ${score}/5`, 'evaluate');
        } else if (etype === 'error') {
          // Error: red flash
          for (const col of columns) {
            if (col && col.nodeId && Math.random() < 0.5) {
              col.pulse = Math.min(1.0, col.pulse + 0.8);
              col.baseHue = 0; // red
              setTimeout(() => { col.baseHue = TYPE_HUES[col.nodeType] || 120; }, 3000);
            }
          }
          addTelemetryHUD(`⚠ ERROR: ${data.content || ''}`, 'error');
        } else if (etype === 'idle_pulse') {
          // Idle: very subtle random twinkle (breathing)
          const activeCols = columns.filter(c => c && c.nodeId);
          if (activeCols.length > 0) {
            const col = activeCols[Math.floor(Math.random() * activeCols.length)];
            col.pulse = Math.min(1.0, col.pulse + 0.08);
          }
        }
      } catch (err) {}
    };
    es.onerror = () => {
      if (hudTitle) hudTitle.innerHTML = '◈ Entity DNA ◈ <span style="color:#f85149;font-size:10px;">● OFFLINE</span>';
      es.close();
      setTimeout(connectPulse, 5000);
    };
  } catch (e) {
    setTimeout(connectPulse, 5000);
  }
}

function addTelemetryHUD(text, type) {
  recentTelemetry.unshift({ text, type, ts: Date.now() });
  if (recentTelemetry.length > MAX_RECENT) recentTelemetry.length = MAX_RECENT;
  renderTelemetryFeed();
}

function renderTelemetryFeed() {
  const el = document.getElementById('telemetry-feed');
  if (!el) return;
  const now = Date.now();
  el.innerHTML = recentTelemetry
    .filter(t => now - t.ts < 60000) // Keep last 60s
    .slice(0, 8)
    .map(t => {
      const hue = EVENT_HUES[t.type] || 120;
      const age = (now - t.ts) / 1000;
      const opacity = Math.max(0.3, 1.0 - age / 60);
      return `<div style="color:hsl(${hue},100%,60%);opacity:${opacity.toFixed(2)};margin-bottom:2px;font-size:10px;">${t.text}</div>`;
    })
    .join('');
}

// ── Mouse hover: show node info ──

canvas.addEventListener('mousemove', (e) => {
  const colIdx = Math.floor(e.clientX / fontSize);
  const col = columns[colIdx];
  const tooltip = document.getElementById('tooltip');

  if (col && col.nodeId) {
    document.getElementById('tt-label').textContent =
      `${col.nodeType.toUpperCase()}: ${col.nodeLabel}`;
    document.getElementById('tt-detail').innerHTML =
      `Hash: ${col.nodeId}<br>` +
      `Activation: ${(col.activation * 100).toFixed(1)}%<br>` +
      `Sources: ${col.sources.join(', ') || 'unknown'}`;
    tooltip.style.display = 'block';
    tooltip.style.left = Math.min(e.clientX + 15, W - 320) + 'px';
    tooltip.style.top = Math.min(e.clientY + 15, H - 80) + 'px';
  } else {
    tooltip.style.display = 'none';
  }
});

canvas.addEventListener('mouseleave', () => {
  document.getElementById('tooltip').style.display = 'none';
});

// ── Click: activate a node (trigger spreading activation) ──

canvas.addEventListener('click', async (e) => {
  const colIdx = Math.floor(e.clientX / fontSize);
  const col = columns[colIdx];
  if (col && col.nodeId && col.nodeLabel) {
    // Trigger spreading activation via the search API
    try {
      const resp = await fetch(`/api/dna/mesh`);
      // Visual pulse on click
      col.pulse = 1.0;
      // Pulse connected columns via edges
      for (const edge of meshEdges) {
        if (edge.source === col.nodeId || edge.target === col.nodeId) {
          const otherId = edge.source === col.nodeId ? edge.target : edge.source;
          const otherCol = columns.find(c => c && c.nodeId === otherId);
          if (otherCol) {
            otherCol.pulse = Math.min(1.0, otherCol.pulse + edge.weight * 0.7);
          }
        }
      }
    } catch (err) {}
  }
});

// ── Init ──

window.addEventListener('resize', resize);
resize();
fetchMesh();
connectPulse();

// Refresh mesh data every 15 seconds
setInterval(fetchMesh, 15000);

// Start rendering
draw();
</script>
</body>
</html>
"""
