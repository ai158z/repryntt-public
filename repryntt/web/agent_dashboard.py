"""
Agent Dashboard — Web UI for operator control of the Jarvis autonomous agent.

Features:
    - Task queue management (add/complete/skip/reorder operator tasks)
    - Consciousness state viewer (drives, interests, emotions)
    - Operator feedback on topics (thumbs up/down → RL weight adjustment)
    - RL metrics visualization
    
URL prefix: /agent
"""

import json
import logging
import os
import time
from datetime import datetime, date
from pathlib import Path

from flask import Blueprint, render_template_string, request, jsonify

logger = logging.getLogger("repryntt.agent_dashboard")

agent_bp = Blueprint('agent_dashboard', __name__)

# ── Paths ──
from repryntt.paths import operator_dir as _operator_dir, brain_dir as _brain_dir
WORKSPACE = _operator_dir()
STATE_FILE = WORKSPACE / "consciousness_state.json"
QUEUE_FILE = WORKSPACE / "task_queue.json"
CHECKPOINT_DIR = WORKSPACE / "consciousness_checkpoints"

# ── Helpers ──

def _load_consciousness_state():
    """Load consciousness state from disk."""
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def _get_task_queue():
    """Get a TaskQueue instance."""
    try:
        from repryntt.agents.task_queue import TaskQueue
        return TaskQueue(str(WORKSPACE))
    except Exception as e:
        logger.warning(f"TaskQueue init failed: {e}")
        return None


def _get_consciousness():
    """Get a JarvisConsciousness instance."""
    try:
        from repryntt.core.hormones.consciousness import JarvisConsciousness
        return JarvisConsciousness(str(WORKSPACE))
    except Exception as e:
        logger.warning(f"Consciousness init failed: {e}")
        return None


# ── API Routes ──

@agent_bp.route('/api/agent/state')
def api_agent_state():
    """Full consciousness state + task queue status."""
    state = _load_consciousness_state()
    queue_data = {}
    try:
        if QUEUE_FILE.exists():
            with open(QUEUE_FILE, 'r') as f:
                queue_data = json.load(f)
    except Exception:
        pass

    tasks = queue_data.get("tasks", [])
    return jsonify({
        "consciousness": {
            "drives": state.get("drives", {}),
            "interests": state.get("interests", {}),
            "emotions": state.get("emotions", {}),
            "mood": state.get("mood", "unknown"),
            "total_cycles": state.get("total_autonomous_cycles", 0),
            "total_tools": state.get("total_tool_calls", 0),
            "last_update": state.get("last_update", 0),
            "rl_metrics": state.get("rl_metrics", {
                "utility_avg": 0.5, "novelty_avg": 0.5, "operator_sat": 0.5,
                "utility_samples": 0, "novelty_samples": 0, "operator_samples": 0,
            }),
        },
        "queue": {
            "day": queue_data.get("day", date.today().isoformat()),
            "current": next((t for t in tasks if t["status"] == "in_progress"), None),
            "queued": [t for t in tasks if t["status"] == "queued"],
            "completed": [t for t in tasks if t["status"] == "completed"],
            "failed": [t for t in tasks if t["status"] == "failed"],
            "skipped": [t for t in tasks if t["status"] == "skipped"],
        },
    })


@agent_bp.route('/api/agent/task', methods=['POST'])
def api_add_task():
    """Add an operator task (highest priority — overrides drive-based selection)."""
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title or len(title) < 5:
        return jsonify({"error": "Title must be at least 5 characters"}), 400

    description = (data.get("description") or "").strip()
    priority = int(data.get("priority", 0))  # Default: operator priority (highest)

    queue = _get_task_queue()
    if not queue:
        return jsonify({"error": "Task queue not available"}), 500

    task = queue.add_task(
        title=title,
        description=description,
        priority=priority,
        source="operator",
    )
    return jsonify({"ok": True, "task": task})


@agent_bp.route('/api/agent/task/<task_id>/complete', methods=['POST'])
def api_complete_task(task_id):
    """Complete a specific task."""
    data = request.get_json(silent=True) or {}
    summary = data.get("summary", "Completed by operator")
    queue = _get_task_queue()
    if not queue:
        return jsonify({"error": "Task queue not available"}), 500
    task = queue.complete_task(task_id, summary)
    if task:
        return jsonify({"ok": True, "task": task})
    return jsonify({"error": "Task not found or not in progress"}), 404


@agent_bp.route('/api/agent/task/<task_id>/skip', methods=['POST'])
def api_skip_task(task_id):
    """Skip a specific task."""
    data = request.get_json(silent=True) or {}
    reason = data.get("reason", "Skipped by operator")
    queue = _get_task_queue()
    if not queue:
        return jsonify({"error": "Task queue not available"}), 500
    task = queue.skip_task(task_id, reason)
    if task:
        return jsonify({"ok": True, "task": task})
    return jsonify({"error": "Task not found"}), 404


@agent_bp.route('/api/agent/feedback', methods=['POST'])
def api_operator_feedback():
    """Apply operator feedback on a topic (thumbs up/down → RL weight change)."""
    data = request.get_json(silent=True) or {}
    topic = (data.get("topic") or "").strip()
    positive = data.get("positive", True)

    if not topic:
        return jsonify({"error": "Topic is required"}), 400

    consciousness = _get_consciousness()
    if not consciousness:
        return jsonify({"error": "Consciousness not available"}), 500

    old_weight = consciousness.interests.get(
        consciousness._normalize_topic(topic), 0.0
    )
    consciousness.apply_operator_feedback(topic, positive)
    consciousness.save_state()

    new_weight = consciousness.interests.get(
        consciousness._normalize_topic(topic), 0.0
    )
    return jsonify({
        "ok": True,
        "topic": topic,
        "positive": positive,
        "old_weight": round(old_weight, 3),
        "new_weight": round(new_weight, 3),
    })


@agent_bp.route('/api/agent/checkpoint', methods=['POST'])
def api_create_checkpoint():
    """Create a consciousness state checkpoint (backup)."""
    if not STATE_FILE.exists():
        return jsonify({"error": "No consciousness state to checkpoint"}), 404

    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    label = (request.get_json(silent=True) or {}).get("label", "manual")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    cp_name = f"checkpoint_{ts}_{label}.json"
    cp_path = CHECKPOINT_DIR / cp_name

    try:
        import shutil
        shutil.copy2(STATE_FILE, cp_path)
        return jsonify({"ok": True, "checkpoint": cp_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@agent_bp.route('/api/agent/checkpoints')
def api_list_checkpoints():
    """List available consciousness checkpoints."""
    if not CHECKPOINT_DIR.exists():
        return jsonify({"checkpoints": []})
    checkpoints = []
    for f in sorted(CHECKPOINT_DIR.glob("checkpoint_*.json"), reverse=True):
        stat = f.stat()
        checkpoints.append({
            "name": f.name,
            "size": stat.st_size,
            "created": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })
    return jsonify({"checkpoints": checkpoints[:20]})


@agent_bp.route('/api/agent/checkpoint/<name>/restore', methods=['POST'])
def api_restore_checkpoint(name):
    """Restore consciousness state from a checkpoint."""
    cp_path = CHECKPOINT_DIR / name
    if not cp_path.exists() or ".." in name:
        return jsonify({"error": "Checkpoint not found"}), 404

    try:
        # Validate it's valid JSON before overwriting
        with open(cp_path, 'r') as f:
            data = json.load(f)
        if "drives" not in data or "interests" not in data:
            return jsonify({"error": "Invalid checkpoint format"}), 400

        # Backup current before restoring
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        import shutil
        shutil.copy2(STATE_FILE, CHECKPOINT_DIR / f"checkpoint_{ts}_pre_restore.json")

        # Restore
        shutil.copy2(cp_path, STATE_FILE)
        return jsonify({"ok": True, "restored": name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@agent_bp.route('/api/agent/triple_loop')
def api_triple_loop_stats():
    """Get triple-loop engine state and statistics."""
    try:
        from repryntt.core.hormones.dual_loop import TripleLoopEngine
        engine = TripleLoopEngine(state_dir=str(WORKSPACE))
        stats = engine.get_stats()
        stats["active_capabilities"] = engine.active_capabilities[-10:]
        stats["recent_work_outputs"] = engine.work_outputs[-10:]
        stats["capability_updates"] = engine.capability_updates[-10:]
        stats["meta_insights"] = engine.meta_insights[-5:]
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@agent_bp.route('/api/agent/value_compass')
def api_value_compass():
    """Get Value Compass state — time budget, heartbeat log, current recommendation."""
    try:
        from repryntt.core.hormones.value_compass import ValueCompass
        bootstrap_dir = _brain_dir() / "bootstrap"
        vc = ValueCompass(bootstrap_dir=bootstrap_dir, state_dir=str(WORKSPACE))
        budget = vc.get_budget_status()
        return jsonify({
            "budget": budget,
            "heartbeat_log": vc.heartbeat_log[-20:],
            "today": vc.today,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Main Dashboard Page ──

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Agent Control — repryntt</title>
<style>
:root { --bg: #0a0a0f; --card: #12121a; --border: #1e1e2e; --text: #e0e0e0;
        --dim: #888; --accent: #4fc3f7; --green: #66bb6a; --red: #ef5350;
        --yellow: #ffca28; --purple: #ab47bc; }
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg);
       color: var(--text); padding: 16px; }
h1 { color: var(--accent); margin-bottom: 16px; font-size: 1.4em; }
h2 { color: var(--accent); font-size: 1.1em; margin-bottom: 8px; padding-bottom: 4px;
     border-bottom: 1px solid var(--border); }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
        padding: 16px; }
.full-width { grid-column: 1 / -1; }
.bar-container { display: flex; align-items: center; margin: 4px 0; gap: 8px; }
.bar-label { min-width: 140px; font-size: 0.85em; color: var(--dim); }
.bar-track { flex: 1; height: 16px; background: #1a1a2e; border-radius: 4px;
             overflow: hidden; position: relative; }
.bar-fill { height: 100%; border-radius: 4px; transition: width 0.5s ease; }
.bar-value { min-width: 45px; text-align: right; font-size: 0.85em; font-family: monospace; }
.practical .bar-fill { background: linear-gradient(90deg, #2196f3, #4fc3f7); }
.theoretical .bar-fill { background: linear-gradient(90deg, #7c4dff, #b388ff); }
.drive .bar-fill { background: linear-gradient(90deg, #ff9800, #ffca28); }
.emotion .bar-fill { background: linear-gradient(90deg, #e91e63, #f48fb1); }
.feedback-btn { display: inline-block; cursor: pointer; padding: 2px 6px;
                border-radius: 4px; font-size: 0.85em; border: 1px solid var(--border);
                background: transparent; color: var(--dim); margin-left: 4px; }
.feedback-btn:hover { color: var(--text); border-color: var(--accent); }
.feedback-btn.up:hover { color: var(--green); border-color: var(--green); }
.feedback-btn.down:hover { color: var(--red); border-color: var(--red); }
.task-list { list-style: none; }
.task-item { padding: 8px; margin: 4px 0; border-radius: 4px;
             display: flex; justify-content: space-between; align-items: center;
             font-size: 0.9em; border: 1px solid var(--border); }
.task-item.current { border-color: var(--accent); background: rgba(79,195,247,0.08); }
.task-item.queued { border-color: var(--border); }
.task-item.completed { border-color: var(--green); opacity: 0.6; }
.task-item .source { font-size: 0.75em; padding: 1px 6px; border-radius: 3px;
                      background: var(--border); color: var(--dim); }
.task-item .source.operator { background: rgba(79,195,247,0.2); color: var(--accent); }
.task-actions button { padding: 4px 10px; border-radius: 4px; border: 1px solid var(--border);
                        background: transparent; color: var(--dim); cursor: pointer;
                        font-size: 0.8em; margin-left: 4px; }
.task-actions button:hover { color: var(--text); border-color: var(--accent); }
.add-form { display: flex; gap: 8px; margin-top: 8px; }
.add-form input { flex: 1; padding: 8px 12px; border-radius: 4px; border: 1px solid var(--border);
                  background: var(--bg); color: var(--text); font-size: 0.9em; }
.add-form button { padding: 8px 16px; border-radius: 4px; border: none;
                    background: var(--accent); color: #000; cursor: pointer; font-weight: 600; }
.add-form button:hover { opacity: 0.9; }
.mood-badge { display: inline-block; padding: 3px 10px; border-radius: 12px;
              font-size: 0.85em; font-weight: 600; }
.stats { display: flex; gap: 16px; margin: 8px 0; }
.stat { text-align: center; }
.stat .num { font-size: 1.4em; font-weight: 700; color: var(--accent); }
.stat .label { font-size: 0.75em; color: var(--dim); }
.cp-list { max-height: 200px; overflow-y: auto; }
.cp-item { display: flex; justify-content: space-between; align-items: center;
           padding: 4px 0; font-size: 0.85em; border-bottom: 1px solid var(--border); }
.cp-item button { padding: 2px 8px; border-radius: 3px; border: 1px solid var(--border);
                  background: transparent; color: var(--dim); cursor: pointer; font-size: 0.8em; }
.cp-item button:hover { color: var(--yellow); border-color: var(--yellow); }
#status-msg { position: fixed; top: 10px; right: 10px; padding: 8px 16px;
              border-radius: 6px; font-size: 0.85em; display: none; z-index: 999; }
@media (max-width: 768px) { .grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<h1>🤖 Agent Control Panel</h1>
<div id="status-msg"></div>

<div class="grid">
    <!-- Task Queue -->
    <div class="card full-width">
        <h2>📋 Task Queue</h2>
        <div class="add-form">
            <input type="text" id="new-task" placeholder="Add operator task (highest priority)..."
                   maxlength="200" />
            <button onclick="addTask()">Add Task</button>
        </div>
        <div id="task-list" style="margin-top: 12px;"></div>
    </div>

    <!-- Value Compass -->
    <div class="card full-width">
        <h2>🧭 Value Compass — Time Budget</h2>
        <div style="display:flex;gap:24px;align-items:center;flex-wrap:wrap;margin-bottom:12px;">
            <div id="vc-budget-bars" style="flex:1;min-width:300px;"></div>
            <div id="vc-recommendation" style="text-align:center;min-width:120px;"></div>
        </div>
        <div style="margin-top:8px;">
            <div style="font-size:0.8em;color:var(--dim);margin-bottom:4px;">Recent heartbeat classifications:</div>
            <div id="vc-heartbeat-log" style="display:flex;gap:3px;flex-wrap:wrap;"></div>
        </div>
    </div>

    <!-- Drives -->
    <div class="card">
        <h2>⚡ Drives</h2>
        <div id="drives"></div>
    </div>

    <!-- Emotions -->
    <div class="card">
        <h2>💜 Emotions & Mood</h2>
        <div id="mood-display" style="margin-bottom: 8px;"></div>
        <div id="emotions"></div>
    </div>

    <!-- Interests (with feedback) -->
    <div class="card full-width">
        <h2>🧠 Interest Weights (click 👍/👎 to give RL feedback)</h2>
        <div id="interests"></div>
    </div>

    <!-- RL Stats -->
    <div class="card">
        <h2>📊 RL Metrics</h2>
        <div class="stats" id="rl-stats"></div>
    </div>

    <!-- Checkpoints -->
    <div class="card">
        <h2>💾 Consciousness Checkpoints</h2>
        <button onclick="createCheckpoint()" style="padding:4px 12px; border-radius:4px;
            border:1px solid var(--border); background:transparent; color:var(--accent);
            cursor:pointer; margin-bottom:8px;">Create Checkpoint</button>
        <div class="cp-list" id="checkpoints"></div>
    </div>
</div>

<script>
const API = '';
let lastState = null;

function showMsg(text, color) {
    const el = document.getElementById('status-msg');
    el.textContent = text;
    el.style.background = color || '#333';
    el.style.color = '#fff';
    el.style.display = 'block';
    setTimeout(() => el.style.display = 'none', 3000);
}

async function fetchState() {
    try {
        const r = await fetch(API + '/api/agent/state');
        lastState = await r.json();
        renderDrives(lastState.consciousness.drives);
        renderEmotions(lastState.consciousness.emotions, lastState.consciousness.mood);
        renderInterests(lastState.consciousness.interests);
        renderQueue(lastState.queue);
        renderStats(lastState.consciousness);
    } catch(e) { console.error(e); }
}

function renderBar(container, items, cssClass, tagFn) {
    const el = document.getElementById(container);
    el.innerHTML = Object.entries(items)
        .sort((a,b) => b[1] - a[1])
        .map(([k, v]) => {
            const label = k.replace(/_/g, ' ').replace(' drive','');
            const pct = Math.min(100, Math.max(0, v * 100));
            const extra = tagFn ? tagFn(k, v) : '';
            return `<div class="bar-container ${cssClass}">
                <span class="bar-label">${label}</span>
                <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
                <span class="bar-value">${v.toFixed(2)}</span>${extra}
            </div>`;
        }).join('');
}

function renderDrives(drives) { renderBar('drives', drives, 'drive'); }
function renderEmotions(emotions, mood) {
    document.getElementById('mood-display').innerHTML =
        `<span class="mood-badge" style="background:rgba(79,195,247,0.15);color:var(--accent);">${mood}</span>`;
    renderBar('emotions', emotions, 'emotion');
}

function renderInterests(interests) {
    const PRACTICAL = new Set(['artificial_intelligence','autonomous_agents','edge_computing',
        'system_optimization','cybersecurity','blockchain','open_source','robotics','economics']);
    renderBar('interests', interests, '', (k, v) => {
        const cls = PRACTICAL.has(k) ? 'practical' : 'theoretical';
        const tag = PRACTICAL.has(k) ? '⚙️' : '📚';
        return ` <button class="feedback-btn up" onclick="feedback('${k}',true)" title="More of this">👍</button>`
             + `<button class="feedback-btn down" onclick="feedback('${k}',false)" title="Less of this">👎</button>`;
    });
    // Re-apply classes
    document.querySelectorAll('#interests .bar-container').forEach(el => {
        const label = el.querySelector('.bar-label').textContent.replace(/ /g, '_');
        el.classList.add(PRACTICAL.has(label) ? 'practical' : 'theoretical');
    });
}

function renderQueue(q) {
    const el = document.getElementById('task-list');
    let html = '';
    if (q.current) {
        html += taskItem(q.current, 'current');
    }
    q.queued.forEach(t => html += taskItem(t, 'queued'));
    if (q.completed.length) {
        html += '<div style="margin-top:8px;color:var(--dim);font-size:0.8em;">Completed today:</div>';
        q.completed.slice(-5).forEach(t => html += taskItem(t, 'completed'));
    }
    if (!q.current && !q.queued.length && !q.completed.length) {
        html = '<div style="color:var(--dim);padding:8px;">No tasks — queue is empty</div>';
    }
    el.innerHTML = html;
}

function taskItem(t, cls) {
    const srcCls = t.source === 'operator' ? 'source operator' : 'source';
    let actions = '';
    if (cls === 'current') {
        actions = `<div class="task-actions">
            <button onclick="completeTask('${t.id}')">✅ Done</button>
            <button onclick="skipTask('${t.id}')">⏭ Skip</button></div>`;
    } else if (cls === 'queued') {
        actions = `<div class="task-actions">
            <button onclick="skipTask('${t.id}')">⏭ Skip</button></div>`;
    }
    return `<div class="task-item ${cls}">
        <div><span class="${srcCls}">${t.source}</span> ${t.title}</div>
        ${actions}
    </div>`;
}

function renderStats(c) {
    document.getElementById('rl-stats').innerHTML = `
        <div class="stat"><div class="num">${c.total_cycles}</div><div class="label">Heartbeats</div></div>
        <div class="stat"><div class="num">${c.total_tools.toLocaleString()}</div><div class="label">Tool Calls</div></div>
        <div class="stat"><div class="num">${new Date(c.last_update * 1000).toLocaleTimeString()}</div><div class="label">Last Update</div></div>
    `;
}

async function addTask() {
    const input = document.getElementById('new-task');
    const title = input.value.trim();
    if (!title || title.length < 5) return;
    try {
        const r = await fetch(API + '/api/agent/task', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({title, priority: 0})
        });
        const d = await r.json();
        if (d.ok) { input.value = ''; showMsg('Task added ✓', '#2e7d32'); fetchState(); }
        else showMsg(d.error, '#c62828');
    } catch(e) { showMsg('Failed', '#c62828'); }
}

async function completeTask(id) {
    await fetch(API + `/api/agent/task/${id}/complete`, {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({summary: 'Completed by operator'})
    });
    showMsg('Task completed ✓', '#2e7d32');
    fetchState();
}

async function skipTask(id) {
    await fetch(API + `/api/agent/task/${id}/skip`, {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({reason: 'Skipped by operator'})
    });
    showMsg('Task skipped', '#f57f17');
    fetchState();
}

async function feedback(topic, positive) {
    try {
        const r = await fetch(API + '/api/agent/feedback', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({topic: topic.replace(/_/g, ' '), positive})
        });
        const d = await r.json();
        if (d.ok) {
            showMsg(`${positive ? '👍' : '👎'} ${topic.replace(/_/g,' ')}: ${d.old_weight} → ${d.new_weight}`,
                    positive ? '#2e7d32' : '#c62828');
            fetchState();
        }
    } catch(e) { showMsg('Feedback failed', '#c62828'); }
}

async function createCheckpoint() {
    try {
        const r = await fetch(API + '/api/agent/checkpoint', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({label: 'manual'})
        });
        const d = await r.json();
        if (d.ok) { showMsg('Checkpoint created ✓', '#2e7d32'); loadCheckpoints(); }
    } catch(e) { showMsg('Checkpoint failed', '#c62828'); }
}

async function loadCheckpoints() {
    try {
        const r = await fetch(API + '/api/agent/checkpoints');
        const d = await r.json();
        const el = document.getElementById('checkpoints');
        el.innerHTML = d.checkpoints.map(cp =>
            `<div class="cp-item">
                <span>${cp.name.replace('checkpoint_','').replace('.json','')}</span>
                <button onclick="restoreCheckpoint('${cp.name}')">Restore</button>
            </div>`
        ).join('') || '<div style="color:var(--dim);font-size:0.85em;">No checkpoints yet</div>';
    } catch(e) {}
}

async function restoreCheckpoint(name) {
    if (!confirm('Restore this checkpoint? Current state will be backed up first.')) return;
    try {
        const r = await fetch(API + `/api/agent/checkpoint/${name}/restore`, {
            method: 'POST'
        });
        const d = await r.json();
        if (d.ok) { showMsg('Checkpoint restored ✓', '#2e7d32'); fetchState(); loadCheckpoints(); }
        else showMsg(d.error, '#c62828');
    } catch(e) { showMsg('Restore failed', '#c62828'); }
}

async function fetchValueCompass() {
    try {
        const r = await fetch(API + '/api/agent/value_compass');
        const d = await r.json();
        if (d.error) return;
        renderValueCompass(d);
    } catch(e) { console.error('ValueCompass fetch failed:', e); }
}

function renderValueCompass(data) {
    const b = data.budget || {};
    const total = b.total || 0;
    const barsEl = document.getElementById('vc-budget-bars');
    const recEl = document.getElementById('vc-recommendation');
    const logEl = document.getElementById('vc-heartbeat-log');

    // Budget bars
    const categories = [
        { key: 'duty', label: 'Duty', color: '#4fc3f7', count: b.duty || 0, pct: b.duty_pct || 0, target: b.duty_target || 0.7 },
        { key: 'growth', label: 'Growth', color: '#ab47bc', count: b.growth || 0, pct: b.growth_pct || 0, target: b.growth_target || 0.2 },
        { key: 'exploration', label: 'Explore', color: '#66bb6a', count: b.exploration || 0, pct: b.exploration_pct || 0, target: b.exploration_target || 0.1 },
    ];

    barsEl.innerHTML = categories.map(c => {
        const pct = total > 0 ? (c.pct * 100) : 0;
        const targetPct = c.target * 100;
        const overUnder = pct > targetPct + 5 ? ' ⚠️ over' : (pct < targetPct - 10 && total > 3 ? ' ↓ under' : '');
        return `<div class="bar-container" style="margin:4px 0;display:flex;align-items:center;gap:8px;">
            <span style="min-width:65px;font-size:0.85em;color:var(--dim);">${c.label}</span>
            <div style="flex:1;height:18px;background:#1a1a2e;border-radius:4px;overflow:hidden;position:relative;">
                <div style="height:100%;width:${pct}%;background:${c.color};border-radius:4px;transition:width 0.5s;"></div>
                <div style="position:absolute;top:0;left:${targetPct}%;height:100%;width:2px;background:rgba(255,255,255,0.3);" title="Target: ${targetPct}%"></div>
            </div>
            <span style="min-width:80px;font-size:0.85em;font-family:monospace;text-align:right;">${c.count} (${pct.toFixed(0)}%/${targetPct.toFixed(0)}%)${overUnder}</span>
        </div>`;
    }).join('');

    // Recommendation badge
    const rec = (b.recommendation || 'duty').toUpperCase();
    const recColors = { DUTY: '#4fc3f7', GROWTH: '#ab47bc', EXPLORATION: '#66bb6a' };
    recEl.innerHTML = total > 0
        ? `<div style="font-size:0.75em;color:var(--dim);margin-bottom:4px;">NEXT RECOMMENDED</div>
           <div style="display:inline-block;padding:6px 16px;border-radius:6px;font-weight:700;font-size:1.1em;
                background:${recColors[rec] || '#4fc3f7'}22;color:${recColors[rec] || '#4fc3f7'};
                border:1px solid ${recColors[rec] || '#4fc3f7'}44;">${rec}</div>
           <div style="font-size:0.75em;color:var(--dim);margin-top:4px;">${total} heartbeats today</div>`
        : `<div style="font-size:0.85em;color:var(--dim);">No heartbeats yet today</div>`;

    // Heartbeat log dots
    const log = data.heartbeat_log || [];
    const dotColors = { duty: '#4fc3f7', growth: '#ab47bc', exploration: '#66bb6a' };
    logEl.innerHTML = log.map(h => {
        const c = dotColors[h.category] || '#888';
        const title = `${h.category} — score ${h.score}/5${h.topic ? ' — ' + h.topic : ''}`;
        const size = 6 + (h.score || 3) * 2; // 8-16px based on score
        return `<div title="${title}" style="width:${size}px;height:${size}px;border-radius:50%;background:${c};opacity:${0.4 + (h.score || 3) * 0.12};cursor:help;"></div>`;
    }).join('');
}

// Auto-refresh
fetchState();
fetchValueCompass();
loadCheckpoints();
setInterval(fetchState, 15000);
setInterval(fetchValueCompass, 15000);
setInterval(loadCheckpoints, 60000);

// Enter key to add task
document.getElementById('new-task').addEventListener('keydown', e => {
    if (e.key === 'Enter') addTask();
});
</script>
</body>
</html>
"""


@agent_bp.route('/agent')
def agent_dashboard():
    """Serve the agent control panel."""
    return render_template_string(DASHBOARD_HTML)
