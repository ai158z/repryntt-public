"""
Ops Dashboard — Real-time agent observability.

Serves the visual dashboard and SSE/REST endpoints for the telemetry system.
Designed to be registered as a Flask Blueprint on the Nexus app.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import time
from pathlib import Path
from flask import Blueprint, Response, jsonify, request, render_template_string
from repryntt.telemetry import get_ops_logger

ops_bp = Blueprint("ops", __name__)


# ── Dashboard HTML ──────────────────────────────────────────────────────────

_OPS_DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OPS — Agent Operations</title>
<style>
  :root {
    --bg: #0a0e17;
    --bg2: #111827;
    --bg3: #1a2332;
    --border: #1e2d3d;
    --text: #c9d1d9;
    --text-dim: #6b7b8d;
    --accent: #58a6ff;
    --green: #3fb950;
    --orange: #d29922;
    --red: #f85149;
    --purple: #bc8cff;
    --cyan: #39d0d0;
    --yellow: #e3b341;
    --font: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--font);
    font-size: 13px;
    line-height: 1.5;
    overflow: hidden;
    height: 100vh;
  }

  /* ── Top Bar ── */
  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 16px;
    background: var(--bg2);
    border-bottom: 1px solid var(--border);
    height: 44px;
  }
  .topbar .title {
    font-size: 15px;
    font-weight: 700;
    color: var(--accent);
    letter-spacing: 2px;
  }
  .topbar .status {
    display: flex;
    align-items: center;
    gap: 12px;
    font-size: 11px;
    color: var(--text-dim);
  }
  .topbar .dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    display: inline-block;
  }
  .dot.live { background: var(--green); animation: pulse 2s infinite; }
  .dot.off { background: var(--red); }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }

  /* ── Layout ── */
  .layout {
    display: grid;
    grid-template-columns: 260px 1fr 320px;
    height: calc(100vh - 44px);
  }

  /* ── Left Panel ── */
  .left-panel {
    background: var(--bg2);
    border-right: 1px solid var(--border);
    overflow-y: auto;
    padding: 12px;
  }
  .section-title {
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: var(--text-dim);
    margin: 16px 0 8px;
  }
  .section-title:first-child { margin-top: 0; }

  .agent-card {
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px 12px;
    margin-bottom: 6px;
    cursor: pointer;
    transition: border-color 0.15s;
  }
  .agent-card:hover, .agent-card.active { border-color: var(--accent); }
  .agent-card .agent-name {
    font-weight: 600;
    font-size: 12px;
    color: var(--accent);
  }
  .agent-card .agent-phase {
    font-size: 10px;
    color: var(--text-dim);
    margin-top: 2px;
  }
  .agent-card .agent-tools {
    font-size: 10px;
    color: var(--green);
    margin-top: 2px;
  }

  /* Stats */
  .stat-row {
    display: flex;
    justify-content: space-between;
    padding: 4px 0;
    font-size: 11px;
    border-bottom: 1px solid var(--border);
  }
  .stat-row:last-child { border-bottom: none; }
  .stat-label { color: var(--text-dim); }
  .stat-value { color: var(--text); font-weight: 600; }

  /* ── Main Feed ── */
  .main-feed {
    overflow-y: auto;
    padding: 16px;
    scroll-behavior: smooth;
  }

  .event-card {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 6px;
    margin-bottom: 8px;
    overflow: hidden;
    transition: border-color 0.15s;
    animation: fadeSlide 0.3s ease;
  }
  @keyframes fadeSlide {
    from { opacity:0; transform:translateY(-8px); }
    to { opacity:1; transform:translateY(0); }
  }
  .event-card:hover { border-color: var(--accent); }

  .event-header {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    cursor: pointer;
    user-select: none;
  }
  .event-badge {
    font-size: 9px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 3px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    white-space: nowrap;
  }
  .badge-plan { background: var(--purple); color: #fff; }
  .badge-act_start, .badge-act_end { background: var(--accent); color: #fff; }
  .badge-tool_call { background: var(--cyan); color: #0a0e17; }
  .badge-tool_result { background: var(--green); color: #0a0e17; }
  .badge-api_call { background: var(--orange); color: #0a0e17; }
  .badge-api_response { background: var(--yellow); color: #0a0e17; }
  .badge-evaluate { background: var(--orange); color: #fff; }
  .badge-heartbeat_start { background: var(--green); color: #0a0e17; }
  .badge-heartbeat_end { background: var(--text-dim); color: #fff; }
  .badge-thought { background: #444c56; color: #e6edf3; }
  .badge-error { background: var(--red); color: #fff; }
  .badge-recovery { background: var(--red); color: #fff; }
  .badge-chain_load, .badge-chain_update { background: #553098; color: #fff; }
  .badge-memory_write { background: #1a4731; color: var(--green); }
  .badge-post { background: #0c2d6b; color: var(--accent); }
  .badge-duty_cycle { background: #3d2800; color: var(--orange); }
  .badge-agent_cycle_start, .badge-agent_cycle_end { background: #1a2332; color: var(--text); }

  .event-agent {
    font-size: 11px;
    color: var(--accent);
    font-weight: 600;
    min-width: 60px;
  }
  .event-time {
    font-size: 10px;
    color: var(--text-dim);
    margin-left: auto;
    white-space: nowrap;
  }
  .event-summary {
    font-size: 11px;
    color: var(--text);
    flex: 1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .event-duration {
    font-size: 10px;
    color: var(--orange);
    white-space: nowrap;
  }

  .event-body {
    display: none;
    padding: 0 12px 12px;
    font-size: 12px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-word;
    color: var(--text);
    max-height: 400px;
    overflow-y: auto;
    border-top: 1px solid var(--border);
    background: rgba(0,0,0,0.2);
  }
  .event-body.open { display: block; padding-top: 10px; }

  /* ── Right Panel (Timeline) ── */
  .right-panel {
    background: var(--bg2);
    border-left: 1px solid var(--border);
    overflow-y: auto;
    padding: 12px;
  }
  .heartbeat-card {
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px 12px;
    margin-bottom: 8px;
  }
  .heartbeat-card .hb-title {
    font-size: 12px;
    font-weight: 600;
    color: var(--accent);
  }
  .heartbeat-card .hb-meta {
    font-size: 10px;
    color: var(--text-dim);
    margin-top: 4px;
  }
  .heartbeat-card .hb-score {
    margin-top: 6px;
  }
  .score-bar {
    height: 4px;
    background: var(--border);
    border-radius: 2px;
    overflow: hidden;
  }
  .score-fill {
    height: 100%;
    border-radius: 2px;
    transition: width 0.5s;
  }
  .score-1 { width: 20%; background: var(--red); }
  .score-2 { width: 40%; background: var(--orange); }
  .score-3 { width: 60%; background: var(--yellow); }
  .score-4 { width: 80%; background: var(--accent); }
  .score-5 { width: 100%; background: var(--green); }

  .phase-timeline {
    display: flex;
    gap: 2px;
    margin-top: 8px;
  }
  .phase-block {
    height: 14px;
    border-radius: 2px;
    font-size: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #fff;
    font-weight: 600;
    min-width: 20px;
  }
  .phase-PLAN { background: var(--purple); }
  .phase-ACT { background: var(--accent); }
  .phase-EVALUATE { background: var(--orange); }
  .phase-RECOVERY { background: var(--red); }

  /* ── Filter bar ── */
  .filter-bar {
    display: flex;
    gap: 6px;
    padding: 8px 16px;
    background: var(--bg);
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
  }
  .filter-btn {
    font-family: var(--font);
    font-size: 10px;
    padding: 3px 10px;
    border-radius: 3px;
    border: 1px solid var(--border);
    background: var(--bg2);
    color: var(--text-dim);
    cursor: pointer;
    transition: all 0.15s;
  }
  .filter-btn.active {
    border-color: var(--accent);
    color: var(--accent);
    background: rgba(88,166,255,0.1);
  }
  .filter-btn:hover {
    border-color: var(--text-dim);
    color: var(--text);
  }

  /* ── Controls ── */
  .controls {
    display: flex;
    gap: 8px;
    align-items: center;
    margin-left: auto;
  }
  .controls button {
    font-family: var(--font);
    font-size: 10px;
    padding: 3px 12px;
    border-radius: 3px;
    border: 1px solid var(--border);
    background: var(--bg3);
    color: var(--text);
    cursor: pointer;
  }
  .controls button:hover { border-color: var(--accent); }

  /* ── Empty state ── */
  .empty-state {
    text-align: center;
    padding: 80px 20px;
    color: var(--text-dim);
  }
  .empty-state .icon { font-size: 48px; margin-bottom: 16px; }
  .empty-state .msg { font-size: 14px; }
  .empty-state .sub { font-size: 11px; margin-top: 8px; }

  /* ── Responsive ── */
  @media (max-width: 900px) {
    .layout { grid-template-columns: 1fr; }
    .left-panel, .right-panel { display: none; }
  }

  /* Scrollbar */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--text-dim); }

  /* ── Memory Log Panel ── */
  .memory-panel {
    position: fixed;
    bottom: 0;
    right: 24px;
    width: 420px;
    max-height: 70vh;
    background: var(--bg2);
    border: 1px solid var(--border);
    border-bottom: none;
    border-radius: 10px 10px 0 0;
    display: flex;
    flex-direction: column;
    z-index: 1000;
    box-shadow: 0 -4px 24px rgba(0,0,0,0.5);
    transition: transform 0.3s ease;
  }
  .memory-panel.collapsed {
    transform: translateY(calc(100% - 42px));
  }
  .memory-panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 14px;
    background: var(--bg3);
    border-radius: 10px 10px 0 0;
    border-bottom: 1px solid var(--border);
    cursor: pointer;
    user-select: none;
    min-height: 42px;
  }
  .memory-panel-header .mem-title {
    font-size: 12px;
    font-weight: 700;
    color: var(--cyan);
    letter-spacing: 1px;
  }
  .memory-panel-header .mem-badge {
    font-size: 10px;
    background: var(--cyan);
    color: var(--bg);
    padding: 1px 8px;
    border-radius: 10px;
    font-weight: 700;
    margin-left: 8px;
  }
  .memory-panel-header .mem-toggle {
    font-size: 16px;
    color: var(--text-dim);
    transition: transform 0.3s;
  }
  .memory-panel.collapsed .mem-toggle {
    transform: rotate(180deg);
  }
  .memory-panel-body {
    overflow-y: auto;
    padding: 10px;
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 10px;
    max-height: calc(70vh - 42px);
  }
  .mem-entry {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px;
    animation: fadeSlide 0.3s ease;
  }
  .mem-entry-header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
  }
  .mem-entry-time {
    font-size: 10px;
    color: var(--cyan);
    font-weight: 700;
    min-width: 40px;
  }
  .mem-entry-heading {
    font-size: 11px;
    font-weight: 600;
    color: var(--accent);
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .mem-entry-body {
    font-size: 11px;
    color: var(--text);
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 200px;
    overflow-y: auto;
  }
  .mem-entry-body::-webkit-scrollbar { width: 4px; }
  .mem-empty {
    text-align: center;
    padding: 30px 10px;
    color: var(--text-dim);
    font-size: 12px;
  }
  .mem-new-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--green);
    animation: pulse 2s infinite;
  }
  @media (max-width: 900px) {
    .memory-panel { width: 100%; right: 0; border-radius: 0; }
  }
</style>
</head>
<body>

<!-- Top Bar -->
<div class="topbar">
  <div class="title">OPS DASHBOARD</div>
  <div class="status">
    <span id="event-count">0 events</span>
    <span>|</span>
    <span id="conn-status"><span class="dot off" id="conn-dot"></span> connecting...</span>
  </div>
</div>

<!-- Filter Bar -->
<div class="filter-bar">
  <button class="filter-btn active" data-filter="all">ALL</button>
  <button class="filter-btn" data-filter="heartbeat_start">HEARTBEAT</button>
  <button class="filter-btn" data-filter="plan">PLAN</button>
  <button class="filter-btn" data-filter="tool_call">TOOLS</button>
  <button class="filter-btn" data-filter="api_call">API</button>
  <button class="filter-btn" data-filter="evaluate">EVAL</button>
  <button class="filter-btn" data-filter="error">ERRORS</button>
  <div class="controls">
    <button id="btn-auto" title="Auto-scroll">AUTO &#x25BC;</button>
    <button id="btn-clear" title="Clear feed">CLEAR</button>
    <button id="btn-pause" title="Pause/Resume">PAUSE</button>
  </div>
</div>

<!-- Layout -->
<div class="layout">

  <!-- Left Panel -->
  <div class="left-panel">
    <div class="section-title">Active Agents</div>
    <div id="agent-list"></div>

    <div class="section-title">Session Stats</div>
    <div id="session-stats">
      <div class="stat-row">
        <span class="stat-label">Events</span>
        <span class="stat-value" id="stat-events">0</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Tool Calls</span>
        <span class="stat-value" id="stat-tools">0</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">API Calls</span>
        <span class="stat-value" id="stat-api">0</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Heartbeats</span>
        <span class="stat-value" id="stat-heartbeats">0</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Errors</span>
        <span class="stat-value" id="stat-errors">0</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Uptime</span>
        <span class="stat-value" id="stat-uptime">--</span>
      </div>
    </div>

    <div class="section-title">Date</div>
    <div id="date-list"></div>
  </div>

  <!-- Main Feed -->
  <div class="main-feed" id="main-feed">
    <div class="empty-state" id="empty-state">
      <div class="icon">&#x1F50D;</div>
      <div class="msg">Waiting for agent activity...</div>
      <div class="sub">Events will appear here in real-time as Andrew operates.</div>
    </div>
  </div>

  <!-- Right Panel -->
  <div class="right-panel">
    <div class="section-title">Heartbeat Timeline</div>
    <div id="heartbeat-timeline"></div>
  </div>

</div>

<!-- Memory Log Chatbox -->
<div class="memory-panel" id="memory-panel">
  <div class="memory-panel-header" id="memory-panel-header">
    <div style="display:flex;align-items:center;">
      <span class="mem-title">&#x1F4DD; ANDREW'S MEMORY LOG</span>
      <span class="mem-badge" id="mem-count">0</span>
    </div>
    <span class="mem-toggle" id="mem-toggle">&#x25BC;</span>
  </div>
  <div class="memory-panel-body" id="memory-panel-body">
    <div class="mem-empty" id="mem-empty">No memory entries yet today.<br>Entries will appear here as Andrew writes them.</div>
  </div>
</div>

<script>
// ── State ──
const state = {
  events: [],
  agents: {},
  heartbeats: [],
  filter: 'all',
  agentFilter: null,
  autoScroll: true,
  paused: false,
  stats: { events: 0, tools: 0, api: 0, heartbeats: 0, errors: 0 },
  startTime: Date.now(),
  evtSource: null,
  seenEventIds: new Set(),
};

// ── Helpers ──
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('en-US', {hour12:false, hour:'2-digit', minute:'2-digit', second:'2-digit'});
}
function fmtDuration(ms) {
  if (!ms && ms !== 0) return '';
  if (ms < 1000) return ms.toFixed(0) + 'ms';
  return (ms/1000).toFixed(1) + 's';
}
function truncate(s, n) {
  if (!s) return '';
  return s.length > n ? s.substring(0, n) + '...' : s;
}
function escapeHtml(s) {
  if (!s) return '';
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Badge class ──
function badgeClass(type) {
  const base = type.replace(/[^a-z_]/g, '');
  return 'badge-' + base;
}

// ── Summary line for event ──
function eventSummary(evt) {
  const m = evt.metadata || {};
  switch(evt.event_type) {
    case 'heartbeat_start':
      return `Heartbeat #${m.cycle_number || '?'} starting (budget: ${m.budget || '?'})`;
    case 'heartbeat_end':
      return `Heartbeat done — ${m.tool_count || 0} tools, score ${m.score || '?'}/5 (${fmtDuration(evt.duration_ms)})`;
    case 'plan':
      return truncate(evt.content, 120);
    case 'act_start':
      return `ACT phase started (timeout: ${m.timeout_seconds || '?'}s)`;
    case 'act_end':
      return `ACT done — ${m.tool_calls || 0} tools, ${m.rounds || 0} rounds (${fmtDuration(evt.duration_ms)})`;
    case 'tool_call':
      return `${m.tool_name || 'unknown'}(${truncate(JSON.stringify(m.args || {}), 80)})`;
    case 'tool_result':
      const status = m.success ? '\u2705' : '\u274C';
      return `${status} ${m.tool_name || 'unknown'}: ${truncate(evt.content, 100)}`;
    case 'api_call':
      return `${m.provider || '?'} / ${m.model || '?'} (${m.message_count || '?'} messages, max_tokens=${m.max_tokens || '?'})`;
    case 'api_response':
      return `${m.tokens || '?'} tokens (${fmtDuration(evt.duration_ms)})`;
    case 'evaluate':
      return `Score: ${m.score || '?'}/5 — chain: ${m.chain_continue ? 'continues' : 'done'}`;
    case 'recovery':
      return `Recovery round — low score triggered retry`;
    case 'thought':
      return truncate(evt.content, 120);
    case 'chain_load':
      return `Loaded chain: ${truncate(m.topic || '', 80)} (step ${m.step || '?'})`;
    case 'chain_update':
      return `Chain ${m.action || 'updated'}: ${truncate(m.topic || '', 80)}`;
    case 'memory_write':
      return `Memory: ${truncate(evt.content, 100)}`;
    case 'error':
      return truncate(evt.content, 120);
    case 'agent_cycle_start':
      return `Agent cycle started for ${evt.agent_id}`;
    case 'agent_cycle_end':
      return `Agent cycle done — ${m.tool_count || 0} tools (${fmtDuration(evt.duration_ms)})`;
    default:
      return truncate(evt.content || evt.event_type, 100);
  }
}

// ── Render Event Card ──
function renderEvent(evt) {
  const card = document.createElement('div');
  card.className = 'event-card';
  card.dataset.type = evt.event_type;
  card.dataset.agent = evt.agent_id;

  const header = document.createElement('div');
  header.className = 'event-header';

  const badge = document.createElement('span');
  badge.className = 'event-badge ' + badgeClass(evt.event_type);
  badge.textContent = evt.event_type.replace(/_/g, ' ');

  const agent = document.createElement('span');
  agent.className = 'event-agent';
  agent.textContent = evt.agent_id;

  const summary = document.createElement('span');
  summary.className = 'event-summary';
  summary.textContent = eventSummary(evt);

  const dur = document.createElement('span');
  dur.className = 'event-duration';
  dur.textContent = fmtDuration(evt.duration_ms);

  const ts = document.createElement('span');
  ts.className = 'event-time';
  ts.textContent = fmtTime(evt.timestamp);

  header.appendChild(badge);
  header.appendChild(agent);
  header.appendChild(summary);
  if (evt.duration_ms) header.appendChild(dur);
  header.appendChild(ts);

  const body = document.createElement('div');
  body.className = 'event-body';
  // Build detail content
  let detail = '';
  if (evt.content) detail += escapeHtml(evt.content) + '\n';
  if (evt.metadata && Object.keys(evt.metadata).length) {
    detail += '\n--- metadata ---\n' + escapeHtml(JSON.stringify(evt.metadata, null, 2));
  }
  if (evt.parent_id) detail += '\nparent: ' + evt.parent_id;
  detail += '\nevent_id: ' + evt.event_id;
  body.textContent = ''; // clear
  body.innerHTML = '<pre style="margin:0;font-family:inherit;font-size:12px;white-space:pre-wrap;">' + detail + '</pre>';

  header.addEventListener('click', () => {
    body.classList.toggle('open');
  });

  card.appendChild(header);
  card.appendChild(body);
  return card;
}

// ── Update Agents Panel ──
function updateAgents(evt) {
  const a = state.agents[evt.agent_id] || { id: evt.agent_id, phase: '', tools: 0, lastSeen: 0 };
  a.lastSeen = evt.timestamp;
  if (evt.phase) a.phase = evt.phase;
  if (evt.event_type === 'tool_call') a.tools++;
  state.agents[evt.agent_id] = a;

  const container = document.getElementById('agent-list');
  container.innerHTML = '';
  for (const [id, ag] of Object.entries(state.agents)) {
    const card = document.createElement('div');
    card.className = 'agent-card' + (state.agentFilter === id ? ' active' : '');
    card.innerHTML = `
      <div class="agent-name">${escapeHtml(id)}</div>
      <div class="agent-phase">Phase: ${ag.phase || 'IDLE'}</div>
      <div class="agent-tools">${ag.tools} tool calls</div>
    `;
    card.addEventListener('click', () => {
      state.agentFilter = state.agentFilter === id ? null : id;
      refilterFeed();
      updateAgents(evt);
    });
    container.appendChild(card);
  }
}

// ── Update Heartbeat Timeline ──
function updateHeartbeats(evt) {
  if (evt.event_type === 'heartbeat_start') {
    state.heartbeats.push({
      id: evt.event_id,
      agent: evt.agent_id,
      start: evt.timestamp,
      end: null,
      score: null,
      tools: 0,
      phases: [{name:'PLAN', start: evt.timestamp}],
      meta: evt.metadata || {}
    });
  }
  if (evt.event_type === 'heartbeat_end' && state.heartbeats.length) {
    const hb = state.heartbeats[state.heartbeats.length - 1];
    hb.end = evt.timestamp;
    hb.score = (evt.metadata || {}).score || 0;
    hb.tools = (evt.metadata || {}).tool_count || 0;
  }

  const container = document.getElementById('heartbeat-timeline');
  container.innerHTML = '';
  for (const hb of [...state.heartbeats].reverse().slice(0, 20)) {
    const card = document.createElement('div');
    card.className = 'heartbeat-card';
    const dur = hb.end ? fmtDuration((hb.end - hb.start) * 1000) : 'running...';
    const score = hb.score || '?';
    card.innerHTML = `
      <div class="hb-title">${escapeHtml(hb.agent)} Heartbeat</div>
      <div class="hb-meta">${fmtTime(hb.start)} | ${dur} | ${hb.tools} tools</div>
      <div class="hb-score">
        <div class="score-bar"><div class="score-fill score-${score}"></div></div>
      </div>
    `;
    container.appendChild(card);
  }
}

// ── Update Stats ──
function updateStats(evt) {
  state.stats.events++;
  if (evt.event_type === 'tool_call') state.stats.tools++;
  if (evt.event_type === 'api_call') state.stats.api++;
  if (evt.event_type === 'heartbeat_start') state.stats.heartbeats++;
  if (evt.event_type === 'error') state.stats.errors++;

  document.getElementById('stat-events').textContent = state.stats.events;
  document.getElementById('stat-tools').textContent = state.stats.tools;
  document.getElementById('stat-api').textContent = state.stats.api;
  document.getElementById('stat-heartbeats').textContent = state.stats.heartbeats;
  document.getElementById('stat-errors').textContent = state.stats.errors;
  document.getElementById('event-count').textContent = state.stats.events + ' events';
}

// ── Filter ──
function matchesFilter(evt) {
  if (state.agentFilter && evt.agent_id !== state.agentFilter) return false;

  if (state.filter === 'all') return true;
  if (state.filter === 'tool_call') return evt.event_type === 'tool_call' || evt.event_type === 'tool_result';
  if (state.filter === 'api_call') return evt.event_type === 'api_call' || evt.event_type === 'api_response';
  if (state.filter === 'heartbeat_start') return evt.event_type === 'heartbeat_start' || evt.event_type === 'heartbeat_end';
  return evt.event_type === state.filter;
}

function refilterFeed() {
  const feed = document.getElementById('main-feed');
  for (const card of feed.querySelectorAll('.event-card')) {
    const show = matchesFilter({event_type: card.dataset.type, agent_id: card.dataset.agent});
    card.style.display = show ? '' : 'none';
  }
}

// ── Process Event ──
function processEvent(evt) {
  if (evt.event_id) {
    if (state.seenEventIds.has(evt.event_id)) return;
    state.seenEventIds.add(evt.event_id);
  }

  if (state.paused) { state.events.push(evt); return; }

  state.events.push(evt);
  const feed = document.getElementById('main-feed');
  const empty = document.getElementById('empty-state');
  if (empty) empty.remove();

  const card = renderEvent(evt);
  if (!matchesFilter(evt)) card.style.display = 'none';
  feed.appendChild(card);

  // Keep feed size manageable
  while (feed.children.length > 1000) feed.removeChild(feed.firstChild);

  if (state.autoScroll) feed.scrollTop = feed.scrollHeight;

  updateAgents(evt);
  updateStats(evt);
  updateHeartbeats(evt);
}

// ── SSE Connection ──
function connectSSE() {
  const dot = document.getElementById('conn-dot');
  const status = document.getElementById('conn-status');

  if (state.evtSource) state.evtSource.close();
  state.evtSource = new EventSource('/api/ops/stream');

  state.evtSource.onopen = () => {
    dot.className = 'dot live';
    status.innerHTML = '<span class="dot live" id="conn-dot"></span> live';
  };
  state.evtSource.onmessage = (e) => {
    try {
      const evt = JSON.parse(e.data);
      processEvent(evt);
    } catch(err) { /* ignore parse errors */ }
  };
  state.evtSource.onerror = () => {
    dot.className = 'dot off';
    status.innerHTML = '<span class="dot off" id="conn-dot"></span> reconnecting...';
    // EventSource auto-reconnects
  };
}

// ── Load History ──
async function loadHistory() {
  try {
    const resp = await fetch('/api/ops/events?limit=200');
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.events && data.events.length) {
      for (const evt of data.events) { processEvent(evt); }
    }
  } catch(e) { /* offline */ }

  // Load available dates
  try {
    const resp = await fetch('/api/ops/dates');
    if (!resp.ok) return;
    const data = await resp.json();
    const container = document.getElementById('date-list');
    container.innerHTML = '';
    for (const d of (data.dates || []).reverse()) {
      const btn = document.createElement('button');
      btn.className = 'filter-btn';
      btn.textContent = d;
      btn.addEventListener('click', () => loadDate(d));
      container.appendChild(btn);
    }
  } catch(e) {}
}

async function loadDate(date) {
  try {
    const resp = await fetch('/api/ops/events?date=' + date + '&limit=500');
    if (!resp.ok) return;
    const data = await resp.json();
    // Clear current feed
    const feed = document.getElementById('main-feed');
    feed.innerHTML = '';
    state.events = [];
    state.stats = { events: 0, tools: 0, api: 0, heartbeats: 0, errors: 0 };
    state.heartbeats = [];
    state.agents = {};
    state.seenEventIds.clear();
    for (const evt of (data.events || [])) { processEvent(evt); }
  } catch(e) {}
}

// ── Uptime ──
setInterval(() => {
  const elapsed = Date.now() - state.startTime;
  const mins = Math.floor(elapsed / 60000);
  const hrs = Math.floor(mins / 60);
  const m = mins % 60;
  document.getElementById('stat-uptime').textContent = hrs > 0 ? `${hrs}h ${m}m` : `${m}m`;
}, 10000);

// ── Event Bindings ──
document.querySelectorAll('.filter-btn[data-filter]').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.filter-btn[data-filter]').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    state.filter = btn.dataset.filter;
    refilterFeed();
  });
});

document.getElementById('btn-auto').addEventListener('click', () => {
  state.autoScroll = !state.autoScroll;
  document.getElementById('btn-auto').textContent = state.autoScroll ? 'AUTO \u25BC' : 'AUTO \u25A0';
});

document.getElementById('btn-clear').addEventListener('click', () => {
  document.getElementById('main-feed').innerHTML = '';
  state.events = [];
  state.seenEventIds.clear();
});

document.getElementById('btn-pause').addEventListener('click', () => {
  state.paused = !state.paused;
  document.getElementById('btn-pause').textContent = state.paused ? 'RESUME' : 'PAUSE';
  if (!state.paused) {
    // Flush buffered events
    const buffered = [...state.events];
    for (const evt of buffered) {
      const feed = document.getElementById('main-feed');
      const card = renderEvent(evt);
      if (!matchesFilter(evt)) card.style.display = 'none';
      feed.appendChild(card);
    }
    if (state.autoScroll) {
      document.getElementById('main-feed').scrollTop = document.getElementById('main-feed').scrollHeight;
    }
  }
});

// ── Init ──
loadHistory().then(() => connectSSE());

// ── Memory Log Panel ──
(function() {
  const panel = document.getElementById('memory-panel');
  const header = document.getElementById('memory-panel-header');
  const body = document.getElementById('memory-panel-body');
  const countBadge = document.getElementById('mem-count');
  const emptyMsg = document.getElementById('mem-empty');
  let lastEntryCount = 0;
  let isCollapsed = false;

  header.addEventListener('click', () => {
    isCollapsed = !isCollapsed;
    panel.classList.toggle('collapsed', isCollapsed);
  });

  function truncate(text, max) {
    if (!text || text.length <= max) return text || '';
    return text.substring(0, max) + '…';
  }

  function renderMemEntry(entry) {
    const div = document.createElement('div');
    div.className = 'mem-entry';
    const timeStr = entry.time || '--:--';
    const heading = entry.heading || 'Note';
    // Truncate body for display, keep full text in title
    const bodyText = truncate(entry.body, 1500);
    div.innerHTML = `
      <div class="mem-entry-header">
        <span class="mem-entry-time">${timeStr}</span>
        <span class="mem-entry-heading" title="${heading.replace(/"/g, '&quot;')}">${heading}</span>
        <span class="mem-new-dot"></span>
      </div>
      <div class="mem-entry-body">${bodyText.replace(/</g, '&lt;').replace(/>/g, '&gt;')}</div>
    `;
    return div;
  }

  async function pollMemory() {
    try {
      const resp = await fetch('/api/ops/memory-log');
      if (!resp.ok) return;
      const data = await resp.json();
      const entries = data.entries || [];
      countBadge.textContent = entries.length;

      if (entries.length === 0) {
        emptyMsg.style.display = 'block';
        return;
      }
      emptyMsg.style.display = 'none';

      // Only re-render if new entries appeared
      if (entries.length !== lastEntryCount) {
        body.innerHTML = '';
        for (const entry of entries) {
          body.appendChild(renderMemEntry(entry));
        }
        // Scroll to bottom
        body.scrollTop = body.scrollHeight;
        // Auto-expand on first entries
        if (lastEntryCount === 0 && entries.length > 0) {
          isCollapsed = false;
          panel.classList.remove('collapsed');
        }
        lastEntryCount = entries.length;
      }
    } catch (e) {
      // Silently retry on next poll
    }
  }

  // Poll immediately, then every 15 seconds
  pollMemory();
  setInterval(pollMemory, 15000);
})();
</script>
</body>
</html>
"""


# ── API Endpoints ───────────────────────────────────────────────────────────

@ops_bp.route("/ops")
def ops_dashboard():
    """Serve the real-time operations dashboard."""
    return render_template_string(_OPS_DASHBOARD_HTML)


@ops_bp.route("/api/ops/events")
def ops_events():
    """Get recent or historical events.
    
    Query params:
        limit: max events (default 100)
        date: YYYY-MM-DD for historical (default: today's buffer)
        since: unix timestamp to get events after
    """
    ops = get_ops_logger()
    limit = min(int(request.args.get("limit", 100)), 2000)
    date = request.args.get("date")
    since = request.args.get("since")

    if since:
        events = ops.since(float(since))
    elif date:
        events = ops.history(date=date, limit=limit)
    else:
        events = ops.recent(n=limit)
        # Fall back to today's JSONL if in-memory buffer is empty (e.g. after restart)
        if not events:
            from datetime import datetime as _dt
            events = ops.history(date=_dt.now().strftime("%Y-%m-%d"), limit=limit)

    return jsonify({"events": events, "count": len(events)})


@ops_bp.route("/api/ops/stream")
def ops_stream():
    """SSE endpoint for real-time event streaming.

    Nexus and the agent daemon run as separate processes. The in-memory
    broadcaster only sees events emitted inside the Nexus process, so also tail
    today's JSONL file to stream daemon-originated telemetry without requiring
    a page refresh.
    """
    ops = get_ops_logger()
    q = ops.subscribe_sse()
    telemetry_dir = getattr(ops, "_dir", Path.home() / ".repryntt" / "workspace" / "telemetry")
    seen_event_ids: set[str] = set()
    last_event_id = request.headers.get("Last-Event-ID", "").strip()

    def _position_after_event(path: Path, event_id: str) -> int:
        """Find the byte offset immediately after a known JSONL event."""
        if not event_id or not path.exists():
            return path.stat().st_size if path.exists() else 0
        try:
            with open(path, "r") as f:
                while True:
                    line = f.readline()
                    if not line:
                        break
                    next_pos = f.tell()
                    if f'"event_id": "{event_id}"' in line:
                        return next_pos
            return 0
        except OSError:
            return 0

    def _sse_payload(data: str) -> str | None:
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            return None
        event_id = str(event.get("event_id") or "")
        if event_id:
            if event_id in seen_event_ids:
                return None
            seen_event_ids.add(event_id)
        return f"id: {event_id}\ndata: {json.dumps(event, default=str)}\n\n"

    def generate():
        current_path: Path | None = None
        file_pos = 0
        pending = ""
        last_keepalive = time.time()

        def read_file_events():
            nonlocal current_path, file_pos, pending
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            path = telemetry_dir / f"{today}.jsonl"

            if path != current_path:
                current_path = path
                file_pos = _position_after_event(path, last_event_id)
                pending = ""

            if not path.exists():
                return []

            try:
                size = path.stat().st_size
                if size < file_pos:
                    file_pos = 0
                    pending = ""

                with open(path, "r") as f:
                    f.seek(file_pos)
                    chunk = f.read()
                    file_pos = f.tell()
            except OSError:
                return []

            if not chunk:
                return []

            chunk = pending + chunk
            pending = ""
            if not chunk.endswith("\n"):
                split_at = chunk.rfind("\n")
                if split_at == -1:
                    pending = chunk
                    return []
                pending = chunk[split_at + 1:]
                chunk = chunk[:split_at + 1]

            return [line.strip() for line in chunk.splitlines() if line.strip()]

        try:
            while True:
                emitted = False
                try:
                    payload = _sse_payload(q.get(timeout=1))
                    if payload:
                        emitted = True
                        yield payload
                except Exception:
                    pass

                for data in read_file_events():
                    payload = _sse_payload(data)
                    if payload:
                        emitted = True
                        yield payload

                if not emitted and time.time() - last_keepalive >= 30:
                    last_keepalive = time.time()
                    yield ": keepalive\n\n"
        finally:
            ops.unsubscribe_sse(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@ops_bp.route("/api/ops/dates")
def ops_dates():
    """List available telemetry dates."""
    ops = get_ops_logger()
    return jsonify({"dates": ops.available_dates()})


def _jarvis_memory_dir() -> Path:
    """Resolve the runtime Jarvis memory directory."""
    brain_dir = Path.home() / ".repryntt" / "brain"
    return brain_dir.parent / "workspace" / "agents" / "JARVIS" / "memory"


def _parse_memory_entries(text: str) -> list[dict]:
    """Parse a daily memory markdown file into individual entries."""
    entries: list[dict] = []
    # Split on ## or ### headings
    parts = re.split(r'^(#{2,3}\s+.+)$', text, flags=re.MULTILINE)
    # parts = [preamble, heading1, body1, heading2, body2, ...]
    for i in range(1, len(parts) - 1, 2):
        heading = parts[i].lstrip('#').strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        # Extract time from heading like "Heartbeat #1 — 03:00" or "03:00"
        time_match = re.search(r'(\d{1,2}:\d{2})', heading)
        entries.append({
            "heading": heading,
            "time": time_match.group(1) if time_match else "",
            "body": body,
        })
    return entries


@ops_bp.route("/api/ops/memory-log")
def ops_memory_log():
    """Get Jarvis's daily memory log entries.

    Query params:
        date: YYYY-MM-DD (default: today)
    """
    date_str = request.args.get("date", datetime.date.today().isoformat())
    # Validate date format
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return jsonify({"entries": [], "error": "Invalid date format"}), 400

    memory_dir = _jarvis_memory_dir()
    memory_file = memory_dir / f"{date_str}.md"

    if not memory_file.is_file():
        return jsonify({"entries": [], "date": date_str, "raw": ""})

    raw = memory_file.read_text(errors="replace")
    entries = _parse_memory_entries(raw)
    return jsonify({"entries": entries, "date": date_str, "raw": raw})
