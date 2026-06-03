#!/usr/bin/env python3
"""
SAIGE Hook Message — Standardized event envelope.

Every channel (Gmail, Telegram, Discord, Twitter, custom webhooks)
normalizes its payload into a HookMessage before dispatch.
One format, any source.  OpenClaw-inspired.
"""

from __future__ import annotations
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional


@dataclass
class HookMessage:
    """Standard event envelope — the single format every hook source produces."""

    # ── Identity ──
    source: str              # "gmail", "telegram", "discord", "twitter", "custom"
    event: str               # "new_email", "message", "mention", "webhook"

    # ── Content ──
    sender: str = ""         # who triggered it (email, username, display name)
    subject: str = ""        # email subject, tweet text preview, etc.
    body: str = ""           # full message body
    body_html: str = ""      # optional HTML version

    # ── Routing ──
    agent_id: str = "jarvis"     # which agent to dispatch to
    session_key: str = ""        # dedup key (e.g. "gmail:<msg_id>")
    wake_mode: str = "now"       # "now" = immediate, "next-heartbeat" = queue for next cycle
    priority: int = 5            # 1 (urgent) – 10 (background), default 5

    # ── Metadata ──
    hook_id: str = ""            # unique ID for this hook event
    timestamp: float = 0.0       # epoch seconds
    metadata: Dict[str, Any] = field(default_factory=dict)  # source-specific extras

    # ── Response routing ──
    reply_channel: str = ""      # "telegram", "gmail", etc. — where to send the response
    reply_to: str = ""           # chat_id, email address, etc.

    def __post_init__(self):
        if not self.hook_id:
            self.hook_id = f"hook_{uuid.uuid4().hex[:12]}"
        if not self.timestamp:
            self.timestamp = time.time()
        if not self.session_key:
            self.session_key = f"{self.source}:{self.hook_id}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_prompt(self) -> str:
        """Format as a prompt string for agent cold-call injection."""
        lines = [f"📩 Incoming {self.source.upper()} hook — {self.event}"]
        if self.sender:
            lines.append(f"From: {self.sender}")
        if self.subject:
            lines.append(f"Subject: {self.subject}")
        if self.body:
            # Truncate body for prompt (agent can use tools to get full content)
            body_preview = self.body[:2000]
            if len(self.body) > 2000:
                body_preview += "\n... [truncated — use gmail_read_message for full content]"
            lines.append(f"\n{body_preview}")
        if self.reply_channel and self.reply_to:
            lines.append(f"\nReply via: {self.reply_channel} → {self.reply_to}")
        return "\n".join(lines)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> HookMessage:
        """Create from dict, ignoring unknown keys."""
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)
