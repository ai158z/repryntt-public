#!/usr/bin/env python3
"""
SMS Hook — Twilio integration for direct text messaging with Artemis.

Operator texts a Twilio number → webhook hits /api/hooks/sms →
parse_sms() → HookMessage → HookRouter → Artemis responds →
reply handler sends SMS back.

Setup:
    1. Get a Twilio number ($1/mo): https://console.twilio.com
    2. Save credentials to ~/.repryntt/comms/twilio.json:
       {"account_sid": "AC...", "auth_token": "...", "from_number": "+1..."}
    3. Set Twilio webhook URL to: https://<your-ip>:8089/api/hooks/sms
    4. Texts to that number now reach Artemis directly.

Cost: ~$0.0079/SMS sent, ~$0.0075/SMS received (US). Cheapest direct channel.
"""

import json
import logging
import os
from typing import Any, Dict, Optional

from repryntt.comms.hooks.message import HookMessage

logger = logging.getLogger("hooks.sms")

from repryntt.paths import get_data_dir as _get_data_dir

_CONFIG_PATH = str(_get_data_dir() / "comms" / "twilio.json")
_config_cache: Optional[Dict] = None


def _load_config() -> Optional[Dict]:
    """Load Twilio credentials from disk."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    if not os.path.exists(_CONFIG_PATH):
        return None
    try:
        with open(_CONFIG_PATH, "r") as f:
            _config_cache = json.load(f)
        return _config_cache
    except Exception as e:
        logger.error(f"Failed to load Twilio config: {e}")
        return None


# ──────────────────────────────────────────────────
# Parser: Twilio webhook → HookMessage
# ──────────────────────────────────────────────────

def parse_sms(payload: Dict[str, Any]) -> Optional[HookMessage]:
    """Parse an incoming Twilio SMS webhook.

    Twilio POST fields: From, To, Body, MessageSid, NumMedia, etc.
    """
    body = payload.get("Body", "").strip()
    sender = payload.get("From", "")
    msg_sid = payload.get("MessageSid", "")

    if not body:
        return None

    return HookMessage(
        source="sms",
        event="text_message",
        sender=sender,
        subject="",
        body=body,
        session_key=f"sms:{msg_sid}" if msg_sid else "",
        priority=1,  # SMS from operator = highest priority
        reply_channel="sms",
        reply_to=sender,
        metadata={
            "message_sid": msg_sid,
            "to": payload.get("To", ""),
            "num_media": payload.get("NumMedia", "0"),
        },
    )


# ──────────────────────────────────────────────────
# Reply handler: send SMS back via Twilio REST API
# ──────────────────────────────────────────────────

def send_sms(to_number: str, text: str) -> bool:
    """Send an SMS via Twilio REST API (no SDK needed)."""
    cfg = _load_config()
    if not cfg:
        logger.error("Twilio not configured — create ~/.repryntt/comms/twilio.json")
        return False

    account_sid = cfg.get("account_sid", "")
    auth_token = cfg.get("auth_token", "")
    from_number = cfg.get("from_number", "")

    if not all([account_sid, auth_token, from_number]):
        logger.error("Twilio config incomplete — need account_sid, auth_token, from_number")
        return False

    # Truncate to SMS limits (1600 chars for Twilio, split into segments)
    if len(text) > 1500:
        text = text[:1497] + "..."

    import requests
    try:
        resp = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
            auth=(account_sid, auth_token),
            data={
                "To": to_number,
                "From": from_number,
                "Body": text,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            logger.info(f"📱 SMS sent to {to_number}: {text[:80]}...")
            return True
        else:
            logger.error(f"Twilio SMS failed ({resp.status_code}): {resp.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"Twilio request failed: {e}")
        return False


def sms_reply_handler(reply_to: str, text: str) -> None:
    """Reply handler for HookRouter — sends SMS response back."""
    send_sms(reply_to, text)
