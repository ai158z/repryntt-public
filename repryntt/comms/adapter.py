"""
repryntt.comms.adapter — Abstract base class for channel adapters.

All messaging-platform integrations (Telegram, Discord, future Slack/Signal/etc.)
implement this interface, so the ChannelGateway can manage them uniformly.

To add a new channel:
  1. Subclass ChannelAdapter
  2. Implement the 5 abstract methods
  3. Register it in ChannelGateway.start() or via a plugin loader
"""
from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from repryntt.comms.channel_gateway import ChannelGateway

logger = logging.getLogger("repryntt.comms.adapter")


class ChannelAdapter(ABC):
    """
    Base class for all messaging-channel adapters.

    Lifecycle:  __init__ → start() → [running] → stop()
    """

    # Subclasses must set this (e.g. "telegram", "discord", "slack")
    channel_name: str = ""

    def __init__(self, config: Dict[str, Any], gateway: ChannelGateway):
        self.config = config
        self.gateway = gateway
        self._thread: Optional[threading.Thread] = None
        self._running = False

    # ── Required overrides ──────────────────────────────────────────────

    @abstractmethod
    def start(self) -> bool:
        """
        Start the channel in a background thread.

        Returns True if the channel started successfully.
        Must be non-blocking — spawn a thread/loop internally.
        """

    @abstractmethod
    def stop(self) -> None:
        """Stop the channel gracefully.  Block until shutdown is complete."""

    @abstractmethod
    def is_allowed(self, user_id: Any, **kwargs) -> bool:
        """
        Check whether a user/channel combination is permitted to interact.

        kwargs may include channel_id, is_dm, username, etc. depending
        on the platform.
        """

    @abstractmethod
    def send_message(self, target_id: Any, text: str) -> bool:
        """
        Send a message to a user or channel.

        Returns True on success.  Used for proactive notifications,
        agent-initiated messages, and reply-back flows.
        """

    @abstractmethod
    def get_status(self) -> Dict[str, Any]:
        """
        Return a status dict for diagnostics / the CLI ``repryntt status`` view.

        Should include at least: {"running": bool, "name": str}
        """

    # ── Provided helpers ────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running

    def invoke(self, prompt: str, max_tokens: int = 8000) -> Dict[str, Any]:
        """Forward a user message to the AI through the gateway."""
        return self.gateway.invoke_jarvis(prompt, max_tokens=max_tokens)

    def __repr__(self) -> str:
        state = "running" if self._running else "stopped"
        return f"<{type(self).__name__} [{self.channel_name}] {state}>"
