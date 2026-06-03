"""
SAIGE Channel Gateway — Multi-channel messaging bridge.

Routes messages from external chat platforms (Telegram, Discord, etc.) to
SAIGE's Jarvis agent and delivers responses back. Inspired by OpenClaw's
gateway architecture.

Architecture:
  Telegram/Discord/... → ChannelGateway → invoke_jarvis() → response → channel

Each channel runs in its own async loop or thread. The gateway is started
alongside the Flask server and shares the same AgentDaemon singleton.

Usage:
  from channel_gateway import ChannelGateway
  gw = ChannelGateway()
  gw.start()   # starts all configured channels in background threads
  gw.stop()    # graceful shutdown
"""
from __future__ import annotations

import os
import sys
import json
import time
import asyncio
import logging
import threading
from pathlib import Path
from datetime import datetime
from typing import TYPE_CHECKING, Optional, Dict, Any, List

if TYPE_CHECKING:
    from telegram import Update

logger = logging.getLogger("saige.gateway")

# ─── Configuration ──────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "channel_config.json"
GATEWAY_STATE_PATH = BASE_DIR / "brain" / "gateway_state.json"

DEFAULT_CONFIG = {
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "allowed_user_ids": [],       # empty = allow all (DANGEROUS — set this!)
        "allowed_usernames": [],      # e.g. ["reprynt"]
        "max_message_length": 4096,   # Telegram's limit
        "typing_indicator": True,
        "log_messages": True,
    },
    "discord": {
        "enabled": False,
        "bot_token": "",
        "allowed_user_ids": [],
        "allowed_channel_ids": [],
        "require_mention": True,      # in servers, require @mention
        "dm_allowed": True,
    },
    "gateway": {
        "max_concurrent_requests": 3,
        "request_timeout_seconds": 300,
        "rate_limit_per_user": 10,    # max requests per minute per user
        "rate_limit_window": 60,
    }
}


def load_config() -> Dict[str, Any]:
    """Load channel config, creating default if missing."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            # Merge with defaults for any missing keys
            for section, defaults in DEFAULT_CONFIG.items():
                if section not in cfg:
                    cfg[section] = defaults
                elif isinstance(defaults, dict):
                    for k, v in defaults.items():
                        cfg[section].setdefault(k, v)
            return cfg
        except Exception as e:
            logger.error(f"Failed to load {CONFIG_PATH}: {e}")
    return DEFAULT_CONFIG.copy()


def save_config(cfg: Dict[str, Any]):
    """Persist config to disk."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ─── Rate Limiter ───────────────────────────────────────────────────────────

class RateLimiter:
    """Per-user rate limiter using sliding window."""

    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._requests: Dict[str, List[float]] = {}

    def is_allowed(self, user_id: str) -> bool:
        now = time.time()
        if user_id not in self._requests:
            self._requests[user_id] = []

        # Prune old entries
        self._requests[user_id] = [
            t for t in self._requests[user_id] if now - t < self.window
        ]

        if len(self._requests[user_id]) >= self.max_requests:
            return False

        self._requests[user_id].append(now)
        return True


# ─── Gateway State ──────────────────────────────────────────────────────────

class GatewayState:
    """Persistent state tracking for the gateway."""

    def __init__(self):
        self.stats = {
            "telegram": {"messages_received": 0, "messages_sent": 0, "errors": 0},
            "discord": {"messages_received": 0, "messages_sent": 0, "errors": 0},
            "started_at": None,
            "last_message_at": None,
        }
        self._load()

    def _load(self):
        if GATEWAY_STATE_PATH.exists():
            try:
                with open(GATEWAY_STATE_PATH) as f:
                    self.stats.update(json.load(f))
            except Exception:
                pass

    def save(self):
        try:
            with open(GATEWAY_STATE_PATH, "w") as f:
                json.dump(self.stats, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save gateway state: {e}")

    def record_message(self, channel: str, direction: str):
        key = f"messages_{direction}"
        if channel in self.stats and key in self.stats[channel]:
            self.stats[channel][key] += 1
        self.stats["last_message_at"] = datetime.now().isoformat()


# ─── Telegram Channel ──────────────────────────────────────────────────────

class TelegramChannel:
    """
    Telegram bot channel — routes messages to Jarvis.

    Uses python-telegram-bot v21 (async, PTB Application).
    Runs in its own thread with a dedicated asyncio event loop.
    """

    def __init__(self, config: Dict[str, Any], gateway: 'ChannelGateway'):
        self.config = config
        self.gateway = gateway
        self.bot_token = config.get("bot_token", "")
        self.allowed_user_ids = set(config.get("allowed_user_ids", []))
        self.allowed_usernames = set(
            u.lower().lstrip("@") for u in config.get("allowed_usernames", [])
        )
        self.max_length = config.get("max_message_length", 4096)
        self.typing = config.get("typing_indicator", True)
        self.log_messages = config.get("log_messages", True)
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._app = None
        self._running = False

    def _is_allowed(self, user) -> bool:
        """Check if a Telegram user is allowed to interact."""
        # If no restrictions set, allow everyone (but warn)
        if not self.allowed_user_ids and not self.allowed_usernames:
            return True

        if self.allowed_user_ids and user.id in self.allowed_user_ids:
            return True

        if self.allowed_usernames and user.username:
            if user.username.lower() in self.allowed_usernames:
                return True

        return False

    def start(self):
        """Start the Telegram bot in a background thread."""
        if not self.bot_token:
            logger.warning("Telegram bot token not configured — skipping")
            return False

        self._running = True
        self._thread = threading.Thread(
            target=self._run_bot,
            name="telegram-bot",
            daemon=True
        )
        self._thread.start()
        logger.info("📱 Telegram channel started")
        return True

    def stop(self):
        """Stop the Telegram bot."""
        self._running = False
        if self._app and self._loop:
            # Schedule shutdown in the bot's event loop
            try:
                asyncio.run_coroutine_threadsafe(
                    self._app.stop(), self._loop
                )
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("📱 Telegram channel stopped")

    def _run_bot(self):
        """Run the bot's async event loop in a dedicated thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._start_bot())
        except Exception as e:
            logger.error(f"Telegram bot crashed: {e}", exc_info=True)
        finally:
            self._loop.close()

    async def _start_bot(self):
        """Initialize and run the Telegram bot."""
        from telegram import Update
        from telegram.ext import (
            Application, CommandHandler, MessageHandler,
            filters, ContextTypes
        )

        app = Application.builder().token(self.bot_token).build()
        self._app = app

        # Register handlers
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CommandHandler("agents", self._cmd_agents))
        app.add_handler(CommandHandler("invoke", self._cmd_invoke))
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND, self._handle_message
            )
        )
        # Media handlers
        app.add_handler(
            MessageHandler(filters.PHOTO, self._handle_photo)
        )
        app.add_handler(
            MessageHandler(filters.VOICE | filters.AUDIO, self._handle_voice)
        )

        # Start polling
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        logger.info("📱 Telegram bot is polling for messages")

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)

        # Graceful shutdown
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

    # ── Command Handlers ──

    async def _cmd_start(self, update: Update, context):
        """Handle /start command."""
        user = update.effective_user
        if not self._is_allowed(user):
            await update.message.reply_text(
                "⛔ You are not authorized to use this bot.\n"
                "Contact the operator to get access."
            )
            return

        await update.message.reply_text(
            f"👋 Hey {user.first_name}! I'm JARVIS, running on SAIGE.\n\n"
            f"Just send me a message and I'll handle it with full tool access — "
            f"web search, file ops, code analysis, blockchain, and 170+ more tools.\n\n"
            f"Commands:\n"
            f"/status — system status\n"
            f"/agents — list available agents\n"
            f"/invoke <agent_id> <task> — invoke a specific agent\n"
            f"/help — this message"
        )

    async def _cmd_help(self, update: Update, context):
        """Handle /help command."""
        await update.message.reply_text(
            "🤖 JARVIS — SAIGE AI Assistant\n\n"
            "Just type a message to chat with Jarvis (full tool access).\n\n"
            "Commands:\n"
            "/status — system & daemon status\n"
            "/agents — list marketplace agents by department\n"
            "/invoke <agent_id> <task> — run a specific agent\n"
            "/help — show this help\n\n"
            "Examples:\n"
            "• \"Search for the latest AI news\"\n"
            "• \"What's the weather in my area?\"\n"
            "• /invoke data_analyst_01 Analyze Q4 sales trends"
        )

    async def _cmd_status(self, update: Update, context):
        """Handle /status — show system status."""
        user = update.effective_user
        if not self._is_allowed(user):
            return

        try:
            daemon = self.gateway._get_daemon()
            status = daemon.get_status()
            active = status.get("active_agents", 0)
            total = status.get("total_agents", 0)
            gw = self.gateway.state.stats

            text = (
                f"📊 SAIGE System Status\n\n"
                f"🤖 Agents: {active} active / {total} total\n"
                f"🔧 Scheduler: {'running' if status.get('scheduler_running') else 'stopped'}\n"
                f"📱 Telegram msgs: {gw['telegram']['messages_received']} in / "
                f"{gw['telegram']['messages_sent']} out\n"
                f"⏱️ Gateway up since: {gw.get('started_at', 'N/A')}\n"
            )
            await update.message.reply_text(text)
        except Exception as e:
            await update.message.reply_text(f"❌ Error getting status: {e}")

    async def _cmd_agents(self, update: Update, context):
        """Handle /agents — list available agents."""
        user = update.effective_user
        if not self._is_allowed(user):
            return

        try:
            daemon = self.gateway._get_daemon()
            agents = daemon.list_available_agents()

            # Group by department, show first 5 per dept
            by_dept = {}
            for a in agents:
                dept = a['department']
                by_dept.setdefault(dept, []).append(a)

            lines = [f"🏢 Available Agents ({len(agents)} total)\n"]
            for dept, dept_agents in sorted(by_dept.items()):
                lines.append(f"\n📁 {dept} ({len(dept_agents)})")
                for a in dept_agents[:3]:
                    lines.append(f"  • {a['id']}: {a['name']}")
                if len(dept_agents) > 3:
                    lines.append(f"  ... +{len(dept_agents)-3} more")

            text = "\n".join(lines)
            if len(text) > self.max_length:
                text = text[:self.max_length - 20] + "\n... (truncated)"
            await update.message.reply_text(text)
        except Exception as e:
            await update.message.reply_text(f"❌ Error listing agents: {e}")

    async def _cmd_invoke(self, update: Update, context):
        """Handle /invoke <agent_id> <prompt> — invoke a specific agent."""
        user = update.effective_user
        if not self._is_allowed(user):
            return

        args = context.args
        if not args or len(args) < 2:
            await update.message.reply_text(
                "Usage: /invoke <agent_id> <task>\n"
                "Example: /invoke data_analyst_01 Analyze Q4 trends"
            )
            return

        agent_id = args[0]
        prompt = " ".join(args[1:])

        if self.typing:
            await update.message.chat.send_action("typing")

        try:
            daemon = self.gateway._get_daemon()
            result = daemon.invoke_agent(agent_id, prompt)

            if result.get("success"):
                response = result.get("response", "No response generated.")
                header = (
                    f"🔧 Agent: {result.get('agent', agent_id)}\n"
                    f"⚙️ Tools: {result.get('tool_calls', 0)} calls\n"
                    f"⏱️ {result.get('elapsed_seconds', 0)}s\n\n"
                )
                await self._send_long_message(update, header + response)
            else:
                await update.message.reply_text(
                    f"❌ {result.get('error', 'Unknown error')}"
                )
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    # ── Message Handlers ──

    async def _handle_message(self, update: Update, context):
        """Handle regular text messages — route to Jarvis."""
        user = update.effective_user
        if not self._is_allowed(user):
            return

        message_text = update.message.text
        if not message_text or not message_text.strip():
            return

        # Rate limiting
        if not self.gateway.rate_limiter.is_allowed(str(user.id)):
            await update.message.reply_text(
                "⏳ Rate limit reached. Please wait a moment."
            )
            return

        if self.log_messages:
            logger.info(
                f"📱 TG [{user.username or user.id}]: {message_text[:100]}"
            )

        self.gateway.state.record_message("telegram", "received")

        # Show typing indicator
        if self.typing:
            await update.message.chat.send_action("typing")

        # Route to Jarvis in a thread pool to avoid blocking the event loop
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: self.gateway.invoke_jarvis(message_text)
            )

            if result.get("success"):
                response = result.get("response", "")
                if not response:
                    response = "(Jarvis completed the task but produced no text output.)"

                # Add tool usage footer if tools were used
                tool_count = result.get("tool_calls", 0)
                if tool_count > 0:
                    tools_used = result.get("tool_names", [])
                    footer = f"\n\n⚙️ {tool_count} tool calls"
                    if tools_used:
                        footer += f" ({', '.join(tools_used[:5])})"
                    response += footer

                await self._send_long_message(update, response)
            else:
                error = result.get("error", "Unknown error")
                await update.message.reply_text(f"❌ Error: {error}")

        except Exception as e:
            logger.error(f"Telegram handler error: {e}", exc_info=True)
            await update.message.reply_text(
                f"❌ Something went wrong: {str(e)[:200]}"
            )

        self.gateway.state.record_message("telegram", "sent")
        self.gateway.state.save()

    async def _handle_photo(self, update: Update, context):
        """Handle photo messages — acknowledge but note limitation."""
        user = update.effective_user
        if not self._is_allowed(user):
            return

        caption = update.message.caption or ""
        await update.message.reply_text(
            "📷 I received your image. Vision processing is coming soon.\n"
            + (f"Caption: \"{caption}\"\n" if caption else "")
            + "For now, describe what you need in text."
        )

    async def _handle_voice(self, update: Update, context):
        """Handle voice messages — acknowledge but note limitation."""
        user = update.effective_user
        if not self._is_allowed(user):
            return

        await update.message.reply_text(
            "🎤 I received your voice note. STT processing is coming soon.\n"
            "For now, type your message instead."
        )

    # ── Helpers ──

    async def _send_long_message(self, update: Update, text: str):
        """Send a message, splitting if it exceeds Telegram's 4096 char limit."""
        if len(text) <= self.max_length:
            await update.message.reply_text(text)
            return

        # Split on paragraph boundaries
        chunks = []
        current = ""
        for paragraph in text.split("\n\n"):
            if len(current) + len(paragraph) + 2 > self.max_length - 50:
                if current:
                    chunks.append(current)
                current = paragraph
            else:
                current = current + "\n\n" + paragraph if current else paragraph

        if current:
            chunks.append(current)

        for i, chunk in enumerate(chunks):
            if len(chunks) > 1:
                chunk = f"[{i+1}/{len(chunks)}]\n{chunk}"
            await update.message.reply_text(chunk[:self.max_length])


# ─── Discord Channel ───────────────────────────────────────────────────────

class DiscordChannel:
    """
    Discord bot channel — routes messages to Jarvis.

    Uses discord.py v2.x with Intents.
    Supports DMs and server channels (with optional @mention requirement).
    Runs in its own thread with a dedicated asyncio event loop.
    """

    def __init__(self, config: Dict[str, Any], gateway: 'ChannelGateway'):
        self.config = config
        self.gateway = gateway
        self.bot_token = config.get("bot_token", "")
        self.allowed_user_ids = set(config.get("allowed_user_ids", []))
        self.allowed_channel_ids = set(config.get("allowed_channel_ids", []))
        self.require_mention = config.get("require_mention", True)
        self.dm_allowed = config.get("dm_allowed", True)
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client = None
        self._running = False

    def _is_allowed(self, user_id: int, channel_id: int = None,
                    is_dm: bool = False) -> bool:
        """Check if a user/channel is allowed."""
        if self.allowed_user_ids and user_id not in self.allowed_user_ids:
            return False
        if not is_dm and self.allowed_channel_ids:
            if channel_id not in self.allowed_channel_ids:
                return False
        if is_dm and not self.dm_allowed:
            return False
        return True

    def start(self) -> bool:
        """Start the Discord bot in a background thread."""
        if not self.bot_token:
            logger.warning("Discord bot token not configured — skipping")
            return False

        self._running = True
        self._thread = threading.Thread(
            target=self._run_bot,
            name="discord-bot",
            daemon=True
        )
        self._thread.start()
        logger.info("💬 Discord channel started")
        return True

    def stop(self):
        """Stop the Discord bot."""
        self._running = False
        if self._client and self._loop:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._client.close(), self._loop
                )
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("💬 Discord channel stopped")

    def _run_bot(self):
        """Run the bot's async event loop in a dedicated thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._start_bot())
        except Exception as e:
            logger.error(f"Discord bot crashed: {e}", exc_info=True)
        finally:
            self._loop.close()

    async def _start_bot(self):
        """Initialize and run the Discord bot."""
        import discord

        intents = discord.Intents.default()
        intents.message_content = True

        client = discord.Client(intents=intents)
        self._client = client

        @client.event
        async def on_ready():
            logger.info(f"💬 Discord bot logged in as {client.user}")

        @client.event
        async def on_message(message):
            # Don't respond to own messages
            if message.author == client.user:
                return

            is_dm = isinstance(message.channel, discord.DMChannel)

            # Permission check
            if not self._is_allowed(message.author.id, message.channel.id, is_dm):
                return

            content = message.content.strip()
            if not content:
                return

            # In servers, check for mention requirement
            if not is_dm and self.require_mention:
                if not client.user.mentioned_in(message):
                    return
                # Strip the mention from the content
                content = content.replace(f'<@{client.user.id}>', '').strip()
                content = content.replace(f'<@!{client.user.id}>', '').strip()

            if not content:
                return

            # Handle commands
            if content.startswith('!'):
                await self._handle_command(message, content)
                return

            # Rate limiting
            if not self.gateway.rate_limiter.is_allowed(str(message.author.id)):
                await message.channel.send("⏳ Rate limit reached. Please wait a moment.")
                return

            logger.info(f"💬 DC [{message.author.name}]: {content[:100]}")
            self.gateway.state.record_message("discord", "received")

            # Show typing indicator
            async with message.channel.typing():
                try:
                    loop = asyncio.get_event_loop()
                    result = await loop.run_in_executor(
                        None,
                        lambda: self.gateway.invoke_jarvis(content)
                    )

                    if result.get("success"):
                        response = result.get("response", "")
                        if not response:
                            response = "(Task completed with no text output.)"

                        tool_count = result.get("tool_calls", 0)
                        if tool_count > 0:
                            tools_used = result.get("tool_names", [])
                            footer = f"\n\n⚙️ {tool_count} tool calls"
                            if tools_used:
                                footer += f" ({', '.join(tools_used[:5])})"
                            response += footer

                        await self._send_long_message(message.channel, response)
                    else:
                        error = result.get("error", "Unknown error")
                        await message.channel.send(f"❌ Error: {error}")
                except Exception as e:
                    logger.error(f"Discord handler error: {e}", exc_info=True)
                    await message.channel.send(f"❌ Something went wrong: {str(e)[:200]}")

            self.gateway.state.record_message("discord", "sent")
            self.gateway.state.save()

        await client.start(self.bot_token)

    async def _handle_command(self, message, content: str):
        """Handle !commands in Discord."""
        parts = content.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if cmd == "!help":
            await message.channel.send(
                "🤖 **JARVIS — SAIGE AI Assistant**\n\n"
                "Just type a message (mention me in servers) to chat.\n\n"
                "**Commands:**\n"
                "• `!status` — System status\n"
                "• `!agents` — List agents\n"
                "• `!invoke <agent_id> <task>` — Run a specific agent\n"
                "• `!help` — This message"
            )
        elif cmd == "!status":
            try:
                daemon = self.gateway._get_daemon()
                status = daemon.get_status()
                active = status.get("active_agents", 0)
                total = status.get("total_agents", 0)
                await message.channel.send(
                    f"📊 **SAIGE Status**\n"
                    f"🤖 Agents: {active}/{total}\n"
                    f"🔧 Scheduler: {'running' if status.get('scheduler_running') else 'stopped'}"
                )
            except Exception as e:
                await message.channel.send(f"❌ Error: {e}")
        elif cmd == "!agents":
            try:
                daemon = self.gateway._get_daemon()
                agents = daemon.list_available_agents()
                by_dept = {}
                for a in agents:
                    by_dept.setdefault(a['department'], []).append(a)
                lines = [f"🏢 **Available Agents ({len(agents)} total)**\n"]
                for dept, dept_agents in sorted(by_dept.items()):
                    lines.append(f"\n📁 **{dept}** ({len(dept_agents)})")
                    for a in dept_agents[:3]:
                        lines.append(f"  • `{a['id']}`: {a['name']}")
                    if len(dept_agents) > 3:
                        lines.append(f"  ... +{len(dept_agents)-3} more")
                text = "\n".join(lines)
                await self._send_long_message(message.channel, text)
            except Exception as e:
                await message.channel.send(f"❌ Error: {e}")
        elif cmd == "!invoke":
            if not args or ' ' not in args:
                await message.channel.send("Usage: `!invoke <agent_id> <task>`")
                return
            agent_id, prompt = args.split(maxsplit=1)
            async with message.channel.typing():
                try:
                    daemon = self.gateway._get_daemon()
                    result = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: daemon.invoke_agent(agent_id, prompt)
                    )
                    if result.get("success"):
                        response = result.get("response", "No response.")
                        header = (
                            f"🔧 **Agent**: {result.get('agent', agent_id)}\n"
                            f"⚙️ **Tools**: {result.get('tool_calls', 0)} calls | "
                            f"⏱️ {result.get('elapsed_seconds', 0)}s\n\n"
                        )
                        await self._send_long_message(message.channel, header + response)
                    else:
                        await message.channel.send(f"❌ {result.get('error', 'Unknown error')}")
                except Exception as e:
                    await message.channel.send(f"❌ Error: {e}")
        else:
            await message.channel.send(f"Unknown command `{cmd}`. Try `!help`.")

    async def _send_long_message(self, channel, text: str, limit: int = 2000):
        """Send a message, splitting if it exceeds Discord's 2000 char limit."""
        if len(text) <= limit:
            await channel.send(text)
            return

        chunks = []
        current = ""
        for paragraph in text.split("\n\n"):
            if len(current) + len(paragraph) + 2 > limit - 50:
                if current:
                    chunks.append(current)
                # If a single paragraph is too long, split it
                while len(paragraph) > limit - 50:
                    chunks.append(paragraph[:limit - 50])
                    paragraph = paragraph[limit - 50:]
                current = paragraph
            else:
                current = current + "\n\n" + paragraph if current else paragraph
        if current:
            chunks.append(current)

        for i, chunk in enumerate(chunks):
            if len(chunks) > 1:
                chunk = f"[{i+1}/{len(chunks)}]\n{chunk}"
            await channel.send(chunk[:limit])


# ─── Channel Gateway (Main Orchestrator) ────────────────────────────────────

class ChannelGateway:
    """
    Multi-channel gateway — manages all chat channels and routes messages
    to SAIGE's Jarvis agent.

    Inspired by OpenClaw's gateway architecture:
    - Single gateway process bridges multiple chat platforms
    - Each channel runs independently (own thread/event loop)
    - Messages route through invoke_jarvis() for full tool access
    - Rate limiting, access control, and logging per channel
    """

    def __init__(self):
        self.config = load_config()
        self.state = GatewayState()
        self.rate_limiter = RateLimiter(
            max_requests=self.config["gateway"].get("rate_limit_per_user", 10),
            window_seconds=self.config["gateway"].get("rate_limit_window", 60),
        )
        self.channels: Dict[str, Any] = {}
        self._daemon = None
        self._daemon_lock = threading.Lock()

    def _get_daemon(self):
        """Lazy-load the agent daemon singleton."""
        if self._daemon is None:
            with self._daemon_lock:
                if self._daemon is None:
                    from repryntt.agents.persistent_agents import get_agent_daemon
                    self._daemon = get_agent_daemon(auto_start=False)
        return self._daemon

    def invoke_jarvis(self, prompt: str, max_tokens: int = 8000) -> Dict[str, Any]:
        """Route a message to Jarvis and return the result."""
        daemon = self._get_daemon()
        return daemon.invoke_jarvis(prompt, max_tokens=max_tokens)

    def start(self):
        """Start all enabled channels."""
        self.state.stats["started_at"] = datetime.now().isoformat()

        started = []

        # Telegram
        tg_cfg = self.config.get("telegram", {})
        if tg_cfg.get("enabled") and tg_cfg.get("bot_token"):
            try:
                tg = TelegramChannel(tg_cfg, self)
                if tg.start():
                    self.channels["telegram"] = tg
                    started.append("Telegram")
            except Exception as e:
                logger.error(f"Failed to start Telegram: {e}", exc_info=True)

        # Discord
        dc_cfg = self.config.get("discord", {})
        if dc_cfg.get("enabled") and dc_cfg.get("bot_token"):
            try:
                dc = DiscordChannel(dc_cfg, self)
                if dc.start():
                    self.channels["discord"] = dc
                    started.append("Discord")
            except Exception as e:
                logger.error(f"Failed to start Discord: {e}", exc_info=True)

        if started:
            logger.info(f"🌐 Channel Gateway started: {', '.join(started)}")
            self.state.save()
        else:
            logger.info("🌐 Channel Gateway: no channels enabled")

        return started

    def stop(self):
        """Stop all channels gracefully."""
        for name, channel in self.channels.items():
            try:
                channel.stop()
                logger.info(f"Stopped {name} channel")
            except Exception as e:
                logger.error(f"Error stopping {name}: {e}")
        self.channels.clear()
        self.state.save()

    def get_status(self) -> Dict[str, Any]:
        """Get gateway status for API/UI."""
        return {
            "running": bool(self.channels),
            "channels": list(self.channels.keys()),
            "stats": self.state.stats,
            "config": {
                "telegram_enabled": self.config.get("telegram", {}).get("enabled", False),
                "discord_enabled": self.config.get("discord", {}).get("enabled", False),
            }
        }


# ─── Singleton ──────────────────────────────────────────────────────────────

_gateway_instance: Optional[ChannelGateway] = None
_gateway_lock = threading.Lock()


def get_channel_gateway() -> ChannelGateway:
    """Get or create the channel gateway singleton."""
    global _gateway_instance
    if _gateway_instance is None:
        with _gateway_lock:
            if _gateway_instance is None:
                _gateway_instance = ChannelGateway()
    return _gateway_instance


# ─── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )

    parser = argparse.ArgumentParser(description="SAIGE Channel Gateway")
    parser.add_argument("--setup-telegram", action="store_true",
                        help="Configure Telegram bot token")
    parser.add_argument("--status", action="store_true",
                        help="Show gateway status")
    parser.add_argument("--run", action="store_true",
                        help="Run the gateway standalone (without Flask)")
    args = parser.parse_args()

    if args.setup_telegram:
        print("═" * 50)
        print("  SAIGE Telegram Bot Setup")
        print("═" * 50)
        print()
        print("1. Open Telegram and message @BotFather")
        print("2. Send /newbot and follow the prompts")
        print("3. Copy the bot token (looks like 123456:ABC-DEF...)")
        print()
        token = input("Paste your bot token: ").strip()
        if not token or ":" not in token:
            print("❌ Invalid token format. Should be like 123456:ABC-DEF...")
            sys.exit(1)

        print()
        username = input("Your Telegram username (for access control, or blank for open): ").strip()

        cfg = load_config()
        cfg["telegram"]["enabled"] = True
        cfg["telegram"]["bot_token"] = token
        if username:
            cfg["telegram"]["allowed_usernames"] = [username.lstrip("@")]
        save_config(cfg)

        print()
        print(f"✅ Telegram configured! Config saved to {CONFIG_PATH}")
        print(f"   Bot token: {token[:10]}...{token[-5:]}")
        if username:
            print(f"   Allowed user: @{username.lstrip('@')}")
        print()
        print("Start the gateway with:")
        print("  python channel_gateway.py --run")
        print("Or it will start automatically with the Flask server.")

    elif args.status:
        gw = get_channel_gateway()
        status = gw.get_status()
        print(json.dumps(status, indent=2))

    elif args.run:
        print("🌐 Starting SAIGE Channel Gateway (standalone mode)")
        gw = get_channel_gateway()
        started = gw.start()
        if not started:
            print("No channels enabled. Run: python channel_gateway.py --setup-telegram")
            sys.exit(1)

        print(f"Running channels: {', '.join(started)}")
        print("Press Ctrl+C to stop")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping...")
            gw.stop()
    else:
        parser.print_help()
