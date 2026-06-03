"""
Companion routes — REST API for the companion chat UI and settings.

Endpoints:
  GET  /companion/config          — current companion configuration
  POST /companion/config          — update companion configuration
  GET  /companion/feed            — recent companion messages (for chat UI)
  POST /companion/message         — send a message to the companion
  GET  /companion/voices          — list available voices
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from flask import Blueprint, jsonify, request

from repryntt.core.companion.config import (
    AVAILABLE_VOICES,
    CompanionConfig,
    load_companion_config,
    save_companion_config,
)

logger = logging.getLogger(__name__)

companion_bp = Blueprint("companion", __name__)

BRAIN_DIR = Path(os.environ.get("REPRYNTT_BRAIN_DIR", Path.home() / ".repryntt" / "brain"))
CONVERSATIONS_DIR = BRAIN_DIR / "conversations"
COMPANION_LOG_PATH = BRAIN_DIR / "companion_feed.jsonl"


# ── Config ────────────────────────────────────────────────────────────────────

@companion_bp.route("/companion/config", methods=["GET"])
def get_config():
    config = load_companion_config()
    return jsonify(config.to_dict())


@companion_bp.route("/companion/config", methods=["POST"])
def update_config():
    data = request.get_json(silent=True) or {}
    config = load_companion_config()

    updatable = {
        "name", "voice", "warmth", "curiosity", "verbosity",
        "proactivity", "daily_rituals_enabled", "proactive_outreach_enabled",
        "push_device_token", "push_relay_url",
    }

    for key in updatable:
        if key in data:
            val = data[key]
            # Clamp sliders to [0.0, 1.0]
            if key in {"warmth", "curiosity", "verbosity", "proactivity"}:
                try:
                    val = max(0.0, min(1.0, float(val)))
                except (TypeError, ValueError):
                    continue
            # Validate voice
            if key == "voice" and val not in AVAILABLE_VOICES:
                return jsonify({"error": f"Unknown voice '{val}'. Available: {AVAILABLE_VOICES}"}), 400
            setattr(config, key, val)

    if save_companion_config(config):
        return jsonify({"ok": True, "config": config.to_dict()})
    return jsonify({"error": "Failed to save config"}), 500


# ── Voices ────────────────────────────────────────────────────────────────────

@companion_bp.route("/companion/voices", methods=["GET"])
def list_voices():
    return jsonify({"voices": AVAILABLE_VOICES})


# ── Feed ──────────────────────────────────────────────────────────────────────

@companion_bp.route("/companion/feed", methods=["GET"])
def get_feed():
    """
    Return the last N companion messages in chronological order.
    Each entry has: role (companion|user), text, timestamp.
    Sources: companion_feed.jsonl (proactive messages logged by the agent)
             + recent conversation turns from conversations/*.json
    """
    limit = min(int(request.args.get("limit", 50)), 200)
    messages = []

    # 1. Proactive companion messages logged to companion_feed.jsonl
    if COMPANION_LOG_PATH.exists():
        try:
            with open(COMPANION_LOG_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        messages.append({
                            "role": entry.get("role", "companion"),
                            "text": entry.get("text", ""),
                            "timestamp": entry.get("timestamp", ""),
                        })
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            logger.warning(f"Error reading companion feed log: {e}")

    # 2. Recent conversation turns from conversations/*.json
    if CONVERSATIONS_DIR.exists():
        conv_files = sorted(CONVERSATIONS_DIR.glob("conv_*.json"), reverse=True)[:5]
        for conv_file in conv_files:
            try:
                with open(conv_file, "r", encoding="utf-8") as f:
                    conv = json.load(f)
                turns = conv.get("turns", [])
                for turn in turns:
                    role = turn.get("role", "")
                    text = turn.get("content", turn.get("text", ""))
                    ts = turn.get("timestamp", conv.get("start_time", ""))
                    if role in {"assistant", "companion"}:
                        messages.append({"role": "companion", "text": text, "timestamp": ts})
                    elif role in {"user", "operator"}:
                        messages.append({"role": "user", "text": text, "timestamp": ts})
            except Exception as e:
                logger.warning(f"Error reading conversation file {conv_file}: {e}")

    # Sort by timestamp, return most recent `limit` entries
    def _ts(m):
        try:
            return datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00"))
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    messages.sort(key=_ts)
    return jsonify({"messages": messages[-limit:]})


# ── Send message ──────────────────────────────────────────────────────────────

@companion_bp.route("/companion/message", methods=["POST"])
def send_message():
    """
    Route a user message to the companion.
    Logs the user turn and queues a prompt for the agent to respond.
    The response will appear in /companion/feed on the next poll.
    """
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400

    ts = datetime.now(timezone.utc).isoformat()

    # Log user message to companion feed
    _append_feed_entry({"role": "user", "text": text, "timestamp": ts})

    # Queue the message as a high-priority prompt for the agent if the brain is running
    _queue_companion_prompt(text)

    return jsonify({"ok": True, "queued_at": ts})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _append_feed_entry(entry: dict):
    try:
        COMPANION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(COMPANION_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.warning(f"Could not write companion feed entry: {e}")


def _queue_companion_prompt(text: str):
    """
    Write a companion message to the AI chain queue so the running agent picks it up.
    Falls back silently if the brain isn't available.
    """
    try:
        queue_path = BRAIN_DIR / "ai_chain_queue.json"
        if not queue_path.exists():
            return
        with open(queue_path, "r", encoding="utf-8") as f:
            queue = json.load(f)
        config = load_companion_config()
        queue.append({
            "type": "companion_message",
            "source": "user",
            "priority": "high",
            "prompt": f"[Companion message from operator] {text}",
            "companion_name": config.name,
            "queued_at": datetime.now(timezone.utc).isoformat(),
        })
        with open(queue_path, "w", encoding="utf-8") as f:
            json.dump(queue, f, indent=2)
    except Exception as e:
        logger.debug(f"Could not queue companion prompt: {e}")
