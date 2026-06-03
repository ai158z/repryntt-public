#!/usr/bin/env python3
"""
SAIGE Gmail Watcher — Background IMAP poller that checks for new emails
and pushes them into the HookRouter as HookMessages.

Uses the same App Password config as brain/gmail_integration.py:
    ~/.saige/gmail/app_password.json  →  {"email": "...", "app_password": "..."}

Architecture:
    Gmail IMAP (poll every N minutes)
        → new unread emails
        → parse_gmail() → HookMessage
        → HookRouter.dispatch()
        → AgentDaemon.invoke_jarvis()
        → response routed back via gmail reply handler
"""

from __future__ import annotations
import email as email_lib
import imaplib
import json
import logging
import os
import threading
import time
from email.header import decode_header
from typing import Dict, List, Optional, Set

logger = logging.getLogger("hooks.gmail_watcher")

from repryntt.paths import get_data_dir as _get_data_dir

GMAIL_DIR = str(_get_data_dir() / "gmail")
APP_PASSWORD_PATH = os.path.join(GMAIL_DIR, "app_password.json")
SEEN_IDS_PATH = os.path.join(GMAIL_DIR, "hook_seen_ids.json")

# Poll interval in seconds
DEFAULT_POLL_INTERVAL = 300  # 5 minutes
MAX_SEEN_IDS = 5000


class GmailWatcher:
    """Background thread that polls IMAP for new emails and dispatches hooks."""

    def __init__(self, poll_interval: int = DEFAULT_POLL_INTERVAL):
        self._poll_interval = poll_interval
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._seen_ids: Set[str] = set()
        self._load_seen_ids()

    # ──────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────

    def start(self):
        if self._thread and self._thread.is_alive():
            logger.warning("GmailWatcher already running")
            return
        cfg = self._get_config()
        if not cfg:
            logger.warning("Gmail App Password not configured, watcher not started")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="gmail-watcher"
        )
        self._thread.start()
        logger.info(
            f"GmailWatcher started (poll every {self._poll_interval}s)"
        )

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._save_seen_ids()
        logger.info("GmailWatcher stopped")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ──────────────────────────────────────────────
    # Config
    # ──────────────────────────────────────────────

    @staticmethod
    def _get_config() -> Optional[Dict]:
        if not os.path.exists(APP_PASSWORD_PATH):
            return None
        try:
            with open(APP_PASSWORD_PATH, "r") as f:
                cfg = json.load(f)
            email_addr = cfg.get("email", "").strip()
            pwd = cfg.get("app_password", "").strip()
            if email_addr and pwd:
                return {"email": email_addr, "app_password": pwd}
        except Exception as e:
            logger.warning(f"Failed to load app_password.json: {e}")
        return None

    # ──────────────────────────────────────────────
    # Seen IDs persistence (avoid re-processing)
    # ──────────────────────────────────────────────

    def _load_seen_ids(self):
        if os.path.exists(SEEN_IDS_PATH):
            try:
                with open(SEEN_IDS_PATH, "r") as f:
                    data = json.load(f)
                self._seen_ids = set(data.get("ids", []))
                logger.debug(f"Loaded {len(self._seen_ids)} seen email IDs")
            except Exception:
                self._seen_ids = set()

    def _save_seen_ids(self):
        try:
            os.makedirs(GMAIL_DIR, exist_ok=True)
            # Trim to max
            ids_list = list(self._seen_ids)
            if len(ids_list) > MAX_SEEN_IDS:
                ids_list = ids_list[-MAX_SEEN_IDS:]
                self._seen_ids = set(ids_list)
            with open(SEEN_IDS_PATH, "w") as f:
                json.dump({"ids": ids_list}, f)
        except Exception as e:
            logger.warning(f"Failed to save seen IDs: {e}")

    # ──────────────────────────────────────────────
    # IMAP polling
    # ──────────────────────────────────────────────

    def _poll_loop(self):
        """Main loop — runs in background thread."""
        # Small initial delay to let other systems start
        self._stop_event.wait(10)

        while not self._stop_event.is_set():
            try:
                new_emails = self._fetch_unread()
                if new_emails:
                    logger.info(f"Gmail watcher found {len(new_emails)} new email(s)")
                    self._dispatch_emails(new_emails)
                    self._save_seen_ids()
            except Exception as e:
                logger.error(f"Gmail poll error: {e}")

            self._stop_event.wait(self._poll_interval)

    def _fetch_unread(self) -> List[Dict]:
        """Fetch unread emails from INBOX, return only unseen ones."""
        cfg = self._get_config()
        if not cfg:
            return []

        try:
            imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
            imap.login(cfg["email"], cfg["app_password"])
            imap.select("INBOX", readonly=True)

            status, msg_ids = imap.search(None, "UNSEEN")
            if status != "OK" or not msg_ids[0]:
                imap.close()
                imap.logout()
                return []

            id_list = msg_ids[0].split()
            # Limit to 20 per poll to avoid overload
            id_list = id_list[-20:]

            new_emails = []
            for msg_id in id_list:
                # Quick header-only check for message-id
                _, header_data = imap.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                if header_data and header_data[0]:
                    raw_header = header_data[0][1] if isinstance(header_data[0], tuple) else b""
                    mid = self._extract_message_id(raw_header)
                    if mid and mid in self._seen_ids:
                        continue

                # Fetch full message
                _, msg_data = imap.fetch(msg_id, "(BODY.PEEK[] FLAGS)")
                if not msg_data or not msg_data[0]:
                    continue

                raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                if not isinstance(raw, bytes):
                    continue

                parsed = email_lib.message_from_bytes(raw)
                email_dict = self._parse_email(parsed)

                if email_dict["message_id"] in self._seen_ids:
                    continue

                self._seen_ids.add(email_dict["message_id"])
                new_emails.append(email_dict)

            imap.close()
            imap.logout()
            return new_emails

        except imaplib.IMAP4.error as e:
            logger.error(f"IMAP error: {e}")
            return []
        except Exception as e:
            logger.error(f"Gmail fetch error: {e}")
            return []

    @staticmethod
    def _extract_message_id(raw_header: bytes) -> str:
        try:
            header_str = raw_header.decode("utf-8", errors="replace")
            for line in header_str.split("\n"):
                if line.lower().startswith("message-id:"):
                    return line.split(":", 1)[1].strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _parse_email(parsed) -> Dict:
        """Extract fields from a parsed email.message.Message."""
        # Subject
        subj_raw = parsed.get("Subject", "")
        subj_parts = decode_header(subj_raw)
        subject = ""
        for part, charset in subj_parts:
            if isinstance(part, bytes):
                subject += part.decode(charset or "utf-8", errors="replace")
            else:
                subject += str(part)

        # From
        sender = parsed.get("From", "")

        # Message-ID
        message_id = parsed.get("Message-ID", "").strip()

        # Date
        date_str = parsed.get("Date", "")

        # Body
        body = ""
        if parsed.is_multipart():
            for part in parsed.walk():
                ct = part.get_content_type()
                if ct == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode("utf-8", errors="replace")
                        break
        else:
            payload = parsed.get_payload(decode=True)
            if payload:
                body = payload.decode("utf-8", errors="replace")

        # Truncate very long bodies for the hook
        if len(body) > 3000:
            body = body[:3000] + "\n... [truncated]"

        return {
            "from": sender,
            "subject": subject,
            "body": body,
            "message_id": message_id,
            "date": date_str,
            "to": parsed.get("To", ""),
            "cc": parsed.get("Cc", ""),
            "has_attachments": any(
                p.get_content_disposition() == "attachment"
                for p in (parsed.walk() if parsed.is_multipart() else [])
            ),
        }

    # ──────────────────────────────────────────────
    # Dispatch to hook system
    # ──────────────────────────────────────────────

    def _dispatch_emails(self, emails: List[Dict]):
        from repryntt.comms.hooks.parsers import parse_gmail
        from repryntt.comms.hooks.router import get_hook_router

        router = get_hook_router()
        for email_dict in emails:
            hook = parse_gmail(email_dict)
            if hook:
                logger.info(
                    f"Dispatching email hook: from={email_dict.get('from', '?')} "
                    f"subj={email_dict.get('subject', '?')[:60]}"
                )
                router.dispatch(hook)

    # ──────────────────────────────────────────────
    # Status
    # ──────────────────────────────────────────────

    def status(self) -> Dict:
        return {
            "running": self.running,
            "poll_interval": self._poll_interval,
            "seen_ids_count": len(self._seen_ids),
            "config_present": self._get_config() is not None,
        }


# ──────────────────────────────────────────────────
# Module-level singleton
# ──────────────────────────────────────────────────

_watcher: Optional[GmailWatcher] = None
_watcher_lock = threading.Lock()


def get_gmail_watcher(poll_interval: int = DEFAULT_POLL_INTERVAL) -> GmailWatcher:
    global _watcher
    if _watcher is None:
        with _watcher_lock:
            if _watcher is None:
                _watcher = GmailWatcher(poll_interval=poll_interval)
    return _watcher
