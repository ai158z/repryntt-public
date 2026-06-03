#!/usr/bin/env python3
"""
SAIGE Hook Parsers — Transform raw webhook/channel payloads into HookMessages.

Each parser is a pure function:  raw_payload → HookMessage
Adding a new channel = writing one parser function (~20-30 lines).

OpenClaw-inspired: their hookPresetMappings + messageTemplate pattern,
adapted for Python.
"""

from __future__ import annotations
import json
import logging
import time
from typing import Any, Dict, Optional

from repryntt.comms.hooks.message import HookMessage

logger = logging.getLogger("hooks.parsers")


# ──────────────────────────────────────────────────
# Gmail parser
# ──────────────────────────────────────────────────

def parse_gmail(payload: Dict[str, Any]) -> Optional[HookMessage]:
    """Parse a Gmail notification (from IMAP watcher or external webhook).

    Expected payload:
        {"from": "...", "subject": "...", "body": "...", "snippet": "...",
         "message_id": "...", "date": "...", "to": "...", "labels": [...]}
    """
    sender = payload.get("from", "")
    subject = payload.get("subject", "")
    body = payload.get("body", "") or payload.get("snippet", "")
    msg_id = payload.get("message_id", payload.get("id", ""))

    if not sender and not subject and not body:
        logger.warning("Gmail parser: empty payload, skipping")
        return None

    return HookMessage(
        source="gmail",
        event="new_email",
        sender=sender,
        subject=subject,
        body=body,
        session_key=f"gmail:{msg_id}" if msg_id else "",
        priority=4,  # emails are fairly important
        reply_channel="gmail",
        reply_to=sender,
        metadata={
            "message_id": msg_id,
            "date": payload.get("date", ""),
            "to": payload.get("to", ""),
            "cc": payload.get("cc", ""),
            "labels": payload.get("labels", []),
            "has_attachments": payload.get("has_attachments", False),
        },
    )


# ──────────────────────────────────────────────────
# Telegram parser
# ──────────────────────────────────────────────────

def parse_telegram(payload: Dict[str, Any]) -> Optional[HookMessage]:
    """Parse a Telegram webhook update.

    Expected payload: Telegram Bot API Update object
        {"message": {"from": {"username": "...", "id": ...}, "text": "...", "chat": {"id": ...}}}
    """
    message = payload.get("message", {})
    if not message:
        # Could be callback_query, edited_message, etc.
        message = payload.get("edited_message", {})
    if not message:
        return None

    from_user = message.get("from", {})
    chat = message.get("chat", {})
    text = message.get("text", "")
    caption = message.get("caption", "")

    username = from_user.get("username", "")
    display = from_user.get("first_name", "")
    if from_user.get("last_name"):
        display += f" {from_user['last_name']}"
    sender = f"@{username}" if username else display

    return HookMessage(
        source="telegram",
        event="message",
        sender=sender,
        subject="",
        body=text or caption or "[media]",
        session_key=f"telegram:{chat.get('id', '')}:{message.get('message_id', '')}",
        priority=3,  # direct messages are high priority
        reply_channel="telegram",
        reply_to=str(chat.get("id", "")),
        metadata={
            "chat_id": chat.get("id"),
            "chat_type": chat.get("type", "private"),
            "user_id": from_user.get("id"),
            "username": username,
            "message_id": message.get("message_id"),
            "has_photo": bool(message.get("photo")),
            "has_voice": bool(message.get("voice") or message.get("audio")),
        },
    )


# ──────────────────────────────────────────────────
# Discord parser
# ──────────────────────────────────────────────────

def parse_discord(payload: Dict[str, Any]) -> Optional[HookMessage]:
    """Parse a Discord webhook event.

    Expected payload:
        {"author": {"username": "...", "id": "..."}, "content": "...",
         "channel_id": "...", "guild_id": "...", "id": "..."}
    """
    author = payload.get("author", {})
    content = payload.get("content", "")
    if not content:
        return None

    return HookMessage(
        source="discord",
        event="message",
        sender=author.get("username", "unknown"),
        body=content,
        session_key=f"discord:{payload.get('channel_id', '')}:{payload.get('id', '')}",
        priority=4,
        reply_channel="discord",
        reply_to=payload.get("channel_id", ""),
        metadata={
            "guild_id": payload.get("guild_id", ""),
            "channel_id": payload.get("channel_id", ""),
            "message_id": payload.get("id", ""),
            "author_id": author.get("id", ""),
        },
    )


# ──────────────────────────────────────────────────
# Twitter/X parser
# ──────────────────────────────────────────────────

def parse_twitter(payload: Dict[str, Any]) -> Optional[HookMessage]:
    """Parse a Twitter/X notification (mention, DM, etc.).

    Expected payload:
        {"text": "...", "author": "@username", "tweet_id": "...",
         "event_type": "mention|dm|reply"}
    """
    text = payload.get("text", "")
    if not text:
        return None

    return HookMessage(
        source="twitter",
        event=payload.get("event_type", "mention"),
        sender=payload.get("author", ""),
        body=text,
        session_key=f"twitter:{payload.get('tweet_id', '')}",
        priority=5,
        reply_channel="twitter",
        reply_to=payload.get("author", ""),
        metadata={
            "tweet_id": payload.get("tweet_id", ""),
            "in_reply_to": payload.get("in_reply_to", ""),
        },
    )


# ──────────────────────────────────────────────────
# Generic/Custom webhook parser
# ──────────────────────────────────────────────────

def parse_custom(payload: Dict[str, Any]) -> Optional[HookMessage]:
    """Parse a generic webhook. Caller passes pre-formatted data.

    Expects at minimum: {"body": "..."}
    Optional: source, event, sender, subject, agent_id, priority, reply_channel, reply_to
    """
    body = payload.get("body", payload.get("text", payload.get("message", "")))
    if not body:
        return None

    return HookMessage(
        source=payload.get("source", "custom"),
        event=payload.get("event", "webhook"),
        sender=payload.get("sender", payload.get("from", "")),
        subject=payload.get("subject", ""),
        body=body,
        agent_id=payload.get("agent_id", "jarvis"),
        priority=int(payload.get("priority", 5)),
        reply_channel=payload.get("reply_channel", ""),
        reply_to=payload.get("reply_to", ""),
        metadata=payload.get("metadata", {}),
    )


# ──────────────────────────────────────────────────
# Parser registry — add new channels here
# ──────────────────────────────────────────────────

HOOK_PARSERS = {
    "gmail": parse_gmail,
    "telegram": parse_telegram,
    "discord": parse_discord,
    "twitter": parse_twitter,
    "custom": parse_custom,
}

# Register SMS parser
try:
    from repryntt.comms.hooks.sms_twilio import parse_sms
    HOOK_PARSERS["sms"] = parse_sms
except ImportError:
    pass

# Register trading parsers (lazy to avoid circular imports)
def _register_trading_parsers():
    try:
        from repryntt.comms.hooks.trading_parsers import (
            parse_trade_signal,
            parse_whale_alert,
            parse_trade_execution,
        )
        HOOK_PARSERS["trade_signal"] = parse_trade_signal
        HOOK_PARSERS["whale_alert"] = parse_whale_alert
        HOOK_PARSERS["trade_execution"] = parse_trade_execution
        HOOK_PARSERS["price_alert"] = parse_trade_signal  # alias
    except ImportError:
        pass

_register_trading_parsers()


def parse_hook(source: str, payload: Dict[str, Any]) -> Optional[HookMessage]:
    """Route to the correct parser by source name."""
    parser = HOOK_PARSERS.get(source, parse_custom)
    try:
        return parser(payload)
    except Exception as e:
        logger.error(f"Hook parser error ({source}): {e}")
        return None
