#!/usr/bin/env python3
"""
repryntt Gmail Integration — standalone Gmail tools for agents.

Supports two auth methods (checked in order):
    1. **App Password** (recommended) — permanent, never expires, no OAuth dance.
       Create ``~/.repryntt/gmail/app_password.json`` with::
           {"email": "you@gmail.com", "app_password": "xxxx xxxx xxxx xxxx"}
       To get an App Password: Google Account → Security → 2-Step Verification →
       App passwords → create one for "Mail" on "Other (repryntt)".

    2. **OAuth2** (legacy) — uses ``credentials.json`` + ``token.json``.
       Tokens expire every 7 days if the Cloud project is in Testing mode.

Config paths searched (first found wins):
    - ~/.repryntt/gmail/
    - ~/.saige/gmail/   (legacy fallback)
"""

import base64
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from typing import Any, Dict, List, Optional

logger = logging.getLogger("repryntt.tools.gmail")

# ---------- path resolution ----------
from repryntt.paths import get_data_dir as _get_data_dir
_REPRYNTT_GMAIL_DIR = str(_get_data_dir() / "gmail")
_SAIGE_GMAIL_DIR = os.path.expanduser("~/.saige/gmail")


def _gmail_dir() -> str:
    """Return the first gmail config dir that exists, preferring repryntt."""
    if os.path.isdir(_REPRYNTT_GMAIL_DIR):
        return _REPRYNTT_GMAIL_DIR
    if os.path.isdir(_SAIGE_GMAIL_DIR):
        return _SAIGE_GMAIL_DIR
    # Default to repryntt path (will be created on setup)
    return _REPRYNTT_GMAIL_DIR


def _path(filename: str) -> str:
    return os.path.join(_gmail_dir(), filename)


TOKEN_PATH = property(lambda self: _path("token.json"))  # not used as property, just helpers
CREDENTIALS_PATH_CONST = "credentials.json"
APP_PASSWORD_FILENAME = "app_password.json"
REPLIED_IDS_FILENAME = "replied_message_ids.json"
REPLIED_THREADS_FILENAME = "replied_threads.json"
MAX_REPLIED_IDS = 2000
MAX_REPLIED_THREADS = 500
THREAD_COOLDOWN_HOURS = 6  # Don't re-reply to same thread within this window

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]


# ──────────────────────────────────────────────────
# Replied-ID tracking (Message-ID + thread subject)
# ──────────────────────────────────────────────────

def _load_replied_ids() -> set:
    path = _path(REPLIED_IDS_FILENAME)
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
            return set(data.get("ids", []))
        except Exception:
            pass
    return set()


def _normalize_subject(subj: str) -> str:
    """Strip all Re:/Fwd: prefixes and whitespace for thread matching."""
    import re as _re
    s = subj.strip()
    while True:
        cleaned = _re.sub(r'^(Re|Fwd|Fw)\s*:\s*', '', s, flags=_re.IGNORECASE).strip()
        if cleaned == s:
            break
        s = cleaned
    return s.lower()


def _load_replied_threads() -> dict:
    """Load thread-subject → timestamp map for cooldown dedup."""
    path = _path(REPLIED_THREADS_FILENAME)
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_replied_thread(subject: str):
    """Record that we replied to this thread subject now."""
    if not subject:
        return
    key = _normalize_subject(subject)
    if not key:
        return
    threads = _load_replied_threads()
    threads[key] = time.time()
    # Prune old entries
    cutoff = time.time() - (THREAD_COOLDOWN_HOURS * 3600 * 4)  # keep 4x cooldown for history
    threads = {k: v for k, v in threads.items() if v > cutoff}
    if len(threads) > MAX_REPLIED_THREADS:
        sorted_items = sorted(threads.items(), key=lambda x: x[1])
        threads = dict(sorted_items[-MAX_REPLIED_THREADS:])
    try:
        gdir = _gmail_dir()
        os.makedirs(gdir, exist_ok=True)
        with open(os.path.join(gdir, REPLIED_THREADS_FILENAME), "w") as f:
            json.dump(threads, f)
    except Exception as e:
        logger.warning(f"Failed to save replied thread: {e}")


def _is_thread_recently_replied(subject: str) -> bool:
    """Check if we replied to this thread subject within the cooldown period."""
    if not subject:
        return False
    key = _normalize_subject(subject)
    threads = _load_replied_threads()
    last_reply = threads.get(key, 0)
    return (time.time() - last_reply) < (THREAD_COOLDOWN_HOURS * 3600)


def _save_replied_id(message_id_header: str, subject: str = ""):
    if not message_id_header and not subject:
        return
    if message_id_header:
        ids = _load_replied_ids()
        ids.add(message_id_header)
        ids_list = list(ids)
        if len(ids_list) > MAX_REPLIED_IDS:
            ids_list = ids_list[-MAX_REPLIED_IDS:]
        try:
            gdir = _gmail_dir()
            os.makedirs(gdir, exist_ok=True)
            with open(os.path.join(gdir, REPLIED_IDS_FILENAME), "w") as f:
                json.dump({"ids": ids_list}, f)
        except Exception as e:
            logger.warning(f"Failed to save replied ID: {e}")
    if subject:
        _save_replied_thread(subject)


# ──────────────────────────────────────────────────
# App Password helpers (SMTP / IMAP)
# ──────────────────────────────────────────────────

def _get_app_password_config() -> Optional[Dict]:
    path = _path(APP_PASSWORD_FILENAME)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            cfg = json.load(f)
        email = cfg.get("email", "").strip()
        pwd = cfg.get("app_password", "").strip()
        if email and pwd:
            return {"email": email, "app_password": pwd}
    except Exception as e:
        logger.warning(f"Failed to load app_password.json: {e}")
    return None


def _smtp_send(to: str, subject: str, body: str, cc: str = "", bcc: str = "",
               html: bool = False, in_reply_to: str = "", references: str = "",
               thread_subject: str = "") -> Dict:
    import smtplib
    cfg = _get_app_password_config()
    if not cfg:
        return {"success": False, "error": "App password not configured"}

    msg = MIMEMultipart()
    msg["From"] = cfg["email"]
    msg["To"] = to
    msg["Subject"] = thread_subject or subject
    if cc:
        msg["Cc"] = cc
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to

    content_type = "html" if html else "plain"
    msg.attach(MIMEText(body, content_type))

    all_recipients = [a.strip() for a in to.split(",")]
    if cc:
        all_recipients += [a.strip() for a in cc.split(",")]
    if bcc:
        all_recipients += [a.strip() for a in bcc.split(",")]

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.starttls()
            server.login(cfg["email"], cfg["app_password"])
            server.sendmail(cfg["email"], all_recipients, msg.as_string())
        logger.info(f"📧 Email sent to {to}: {subject}")
        return {"success": True, "to": to, "subject": subject}
    except smtplib.SMTPAuthenticationError:
        return {"success": False,
                "error": "SMTP auth failed — check app_password.json. "
                         "Make sure 2FA is enabled and you're using an App Password."}
    except Exception as e:
        return {"success": False, "error": f"SMTP send failed: {e}"}


def _imap_fetch(query: str = "ALL", max_results: int = 10,
                folder: str = "INBOX", unread_only: bool = False,
                full_body: bool = False) -> Dict:
    import imaplib
    import email as email_lib
    from email.header import decode_header

    cfg = _get_app_password_config()
    if not cfg:
        return {"success": False, "error": "App password not configured"}

    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=15)
        imap.login(cfg["email"], cfg["app_password"])
        imap.select(folder, readonly=True)

        if unread_only and query == "ALL":
            search_criteria = "UNSEEN"
        elif unread_only:
            search_criteria = f"(UNSEEN {query})"
        else:
            search_criteria = query

        status, msg_ids = imap.search(None, search_criteria)
        if status != "OK" or not msg_ids[0]:
            imap.close()
            imap.logout()
            return {"success": True, "emails": [], "count": 0,
                    "message": "No emails found."}

        id_list = msg_ids[0].split()
        id_list = id_list[-max_results:]
        id_list.reverse()

        _replied_ids = _load_replied_ids()
        emails = []
        for msg_id in id_list:
            _, msg_data = imap.fetch(msg_id, "(RFC822 FLAGS)")
            if not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            parsed = email_lib.message_from_bytes(raw)

            subj_raw = parsed.get("Subject", "")
            subj_parts = decode_header(subj_raw)
            subject = ""
            for part, charset in subj_parts:
                if isinstance(part, bytes):
                    subject += part.decode(charset or "utf-8", errors="replace")
                else:
                    subject += part

            body_text = ""
            if parsed.is_multipart():
                for part in parsed.walk():
                    ct = part.get_content_type()
                    if ct == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            body_text = payload.decode("utf-8", errors="replace")
                            break
                    elif ct == "text/html" and not body_text:
                        payload = part.get_payload(decode=True)
                        if payload:
                            html_raw = payload.decode("utf-8", errors="replace")
                            body_text = re.sub(r"<[^>]+>", " ", html_raw)
                            body_text = re.sub(r"\s+", " ", body_text).strip()
            else:
                payload = parsed.get_payload(decode=True)
                if payload:
                    body_text = payload.decode("utf-8", errors="replace")

            if not full_body and len(body_text) > 2000:
                body_text = body_text[:2000] + "\n... [truncated]"

            flags_data = msg_data[1] if len(msg_data) > 1 else b""
            is_read = b"\\Seen" in flags_data if isinstance(flags_data, bytes) else True

            msg_id_header = parsed.get("Message-ID", "").strip()
            id_match = msg_id_header in _replied_ids
            thread_match = _is_thread_recently_replied(subject)
            emails.append({
                "id": msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id),
                "thread_id": msg_id_header,
                "from": parsed.get("From", ""),
                "to": parsed.get("To", ""),
                "cc": parsed.get("Cc", ""),
                "subject": subject,
                "date": parsed.get("Date", ""),
                "snippet": body_text[:200] if body_text else "",
                "body": body_text,
                "labels": [] if is_read else ["UNREAD"],
                "has_attachments": any(
                    p.get_filename() for p in parsed.walk()
                ) if parsed.is_multipart() else False,
                "already_replied": id_match or thread_match,
                "message_id_header": msg_id_header,
            })

        imap.close()
        imap.logout()
        return {"success": True, "emails": emails, "count": len(emails)}

    except imaplib.IMAP4.error as e:
        err = str(e)
        if "AUTHENTICATIONFAILED" in err.upper() or "Invalid credentials" in err:
            return {"success": False,
                    "error": "IMAP auth failed — check app_password.json. Make sure 2FA is enabled."}
        return {"success": False, "error": f"IMAP error: {err}"}
    except Exception as e:
        return {"success": False, "error": f"IMAP fetch failed: {e}"}


def _imap_search(query_text: str, max_results: int = 10) -> Dict:
    imap_query_parts = []

    from_match = re.search(r'from:(\S+)', query_text)
    if from_match:
        imap_query_parts.append(f'FROM "{from_match.group(1)}"')
        query_text = query_text.replace(from_match.group(0), "")

    to_match = re.search(r'to:(\S+)', query_text)
    if to_match:
        imap_query_parts.append(f'TO "{to_match.group(1)}"')
        query_text = query_text.replace(to_match.group(0), "")

    subj_match = re.search(r'subject:(\S+)', query_text)
    if subj_match:
        imap_query_parts.append(f'SUBJECT "{subj_match.group(1)}"')
        query_text = query_text.replace(subj_match.group(0), "")

    if "is:unread" in query_text:
        imap_query_parts.append("UNSEEN")
        query_text = query_text.replace("is:unread", "")

    if "has:attachment" in query_text:
        query_text = query_text.replace("has:attachment", "")

    remaining = query_text.strip()
    if remaining:
        remaining = re.sub(r'(in|label|after|before|newer_than|older_than):\S+', '', remaining).strip()
        if remaining:
            imap_query_parts.append(f'OR SUBJECT "{remaining}" BODY "{remaining}"')

    imap_criteria = " ".join(imap_query_parts) if imap_query_parts else "ALL"
    return _imap_fetch(query=imap_criteria, max_results=max_results)


# ──────────────────────────────────────────────────
# OAuth2 helpers (legacy fallback)
# ──────────────────────────────────────────────────

def _get_credentials():
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except ImportError:
        logger.error("Google API libraries not installed.")
        return None

    token_path = _path("token.json")
    if not os.path.exists(token_path):
        return None

    creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(token_path, "w") as f:
                f.write(creds.to_json())
        except Exception as e:
            logger.error(f"Gmail token refresh failed: {e}")
            return None

    if not creds or not creds.valid:
        return None
    return creds


def _get_service():
    creds = _get_credentials()
    if not creds:
        return None
    try:
        from googleapiclient.discovery import build
        return build("gmail", "v1", credentials=creds)
    except Exception as e:
        logger.error(f"Failed to build Gmail service: {e}")
        return None


def is_gmail_configured() -> bool:
    """Check if Gmail is set up (App Password OR OAuth)."""
    return _get_app_password_config() is not None or os.path.exists(_path("token.json"))


# ──────────────────────────────────────────────────
# Tool: gmail_send
# ──────────────────────────────────────────────────

def gmail_send(to: str, subject: str, body: str,
               cc: str = "", bcc: str = "",
               html: bool = False, **kwargs) -> str:
    """Send a NEW email (compose). For replying to an existing email, use gmail_reply instead.

    Args:
        to: Recipient email address (comma-separated for multiple).
        subject: Email subject line.
        body: Email body text (plain text or HTML if html=True).
        cc: CC recipients (comma-separated). Optional.
        bcc: BCC recipients (comma-separated). Optional.
        html: If True, send body as HTML instead of plain text.

    Returns:
        JSON string with send result or error.
    """
    # Track as replied if this looks like a response (Re: subject)
    _reply_tracking_id = kwargs.get("in_reply_to_message_id", "")
    if _get_app_password_config():
        result = _smtp_send(to, subject, body, cc=cc, bcc=bcc, html=html)
        if result.get("success"):
            _save_replied_id(_reply_tracking_id, subject=subject)
        return json.dumps(result)

    service = _get_service()
    if not service:
        return json.dumps({"success": False,
                           "error": "Gmail not configured. "
                                    "Create ~/.repryntt/gmail/app_password.json"})

    try:
        msg = MIMEMultipart()
        msg["To"] = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc
        if bcc:
            msg["Bcc"] = bcc

        content_type = "html" if html else "plain"
        msg.attach(MIMEText(body, content_type))

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        result = service.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()

        logger.info(f"📧 Email sent to {to}: {subject}")
        return json.dumps({
            "success": True,
            "message_id": result.get("id", ""),
            "thread_id": result.get("threadId", ""),
            "to": to,
            "subject": subject,
        })
    except Exception as e:
        logger.error(f"Gmail send failed: {e}")
        return json.dumps({"success": False, "error": str(e)})


# ──────────────────────────────────────────────────
# Tool: gmail_read_inbox
# ──────────────────────────────────────────────────

def gmail_read_inbox(max_results: int = 10,
                     unread_only: bool = True, **kwargs) -> str:
    """Read recent emails from the inbox.

    IMPORTANT: Each email includes an 'already_replied' field (true/false).
    SKIP emails where already_replied=true — you have already responded.
    To reply to an email, use gmail_reply(message_id, body) — NOT gmail_send.
    gmail_send is for composing NEW emails only, not responding to existing ones.

    Args:
        max_results: Maximum number of emails to return (default 10, max 50).
        unread_only: If True, only return unread emails.

    Returns:
        JSON string with list of emails. Each email has 'already_replied' field.
    """
    max_results = min(int(max_results), 50)

    if _get_app_password_config():
        result = _imap_fetch(max_results=max_results, unread_only=unread_only)
        return json.dumps(result)

    service = _get_service()
    if not service:
        return json.dumps({"success": False, "error": "Gmail not configured."})

    try:
        query = "in:inbox"
        if unread_only:
            query += " is:unread"

        results = service.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()

        messages = results.get("messages", [])
        if not messages:
            return json.dumps({"success": True, "emails": [], "count": 0,
                               "message": "No emails found."})

        emails = []
        for msg_ref in messages:
            msg = service.users().messages().get(
                userId="me", id=msg_ref["id"], format="full"
            ).execute()
            emails.append(_parse_message(msg, service=service))

        return json.dumps({"success": True, "emails": emails, "count": len(emails)})
    except Exception as e:
        logger.error(f"Gmail read_inbox failed: {e}")
        return json.dumps({"success": False, "error": str(e)})


# ──────────────────────────────────────────────────
# Tool: gmail_search
# ──────────────────────────────────────────────────

def gmail_search(query: str, max_results: int = 10, **kwargs) -> str:
    """Search emails using Gmail search syntax.

    Args:
        query: Gmail search query (e.g. "from:user@example.com", "subject:invoice",
               "after:2026/01/01", "has:attachment", "is:unread label:important").
        max_results: Maximum number of results (default 10, max 50).

    Returns:
        JSON string with matching emails.
    """
    max_results = min(int(max_results), 50)

    if _get_app_password_config():
        result = _imap_search(query, max_results=max_results)
        if result.get("success"):
            result["query"] = query
        return json.dumps(result)

    service = _get_service()
    if not service:
        return json.dumps({"success": False, "error": "Gmail not configured."})

    try:
        results = service.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()

        messages = results.get("messages", [])
        if not messages:
            return json.dumps({"success": True, "emails": [], "count": 0,
                               "query": query, "message": "No emails found."})

        emails = []
        for msg_ref in messages:
            msg = service.users().messages().get(
                userId="me", id=msg_ref["id"], format="full"
            ).execute()
            emails.append(_parse_message(msg, service=service))

        return json.dumps({"success": True, "emails": emails,
                           "count": len(emails), "query": query})
    except Exception as e:
        logger.error(f"Gmail search failed: {e}")
        return json.dumps({"success": False, "error": str(e)})


# ──────────────────────────────────────────────────
# Tool: gmail_read_message
# ──────────────────────────────────────────────────

def gmail_read_message(message_id: str, **kwargs) -> str:
    """Read the full content of a specific email by its message ID.

    Args:
        message_id: The Gmail message ID (from gmail_read_inbox or gmail_search results).

    Returns:
        JSON string with the full email content.
    """
    if _get_app_password_config():
        import imaplib
        import email as email_lib
        from email.header import decode_header
        cfg = _get_app_password_config()
        try:
            imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=15)
            imap.login(cfg["email"], cfg["app_password"])
            imap.select("INBOX", readonly=True)
            msg_id = message_id.encode() if isinstance(message_id, str) else message_id
            _, msg_data = imap.fetch(msg_id, "(RFC822)")
            imap.close()
            imap.logout()
            if not msg_data or not msg_data[0]:
                return json.dumps({"success": False, "error": "Message not found"})
            raw = msg_data[0][1]
            parsed = email_lib.message_from_bytes(raw)

            body_text = ""
            if parsed.is_multipart():
                for part in parsed.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            body_text = payload.decode("utf-8", errors="replace")
                            break
            else:
                payload = parsed.get_payload(decode=True)
                if payload:
                    body_text = payload.decode("utf-8", errors="replace")

            subj_raw = parsed.get("Subject", "")
            subj_parts = decode_header(subj_raw)
            subject = ""
            for part, charset in subj_parts:
                if isinstance(part, bytes):
                    subject += part.decode(charset or "utf-8", errors="replace")
                else:
                    subject += part

            return json.dumps({"success": True, "email": {
                "id": message_id,
                "from": parsed.get("From", ""),
                "to": parsed.get("To", ""),
                "subject": subject,
                "date": parsed.get("Date", ""),
                "body": body_text,
                "message_id_header": parsed.get("Message-ID", ""),
            }})
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)})

    service = _get_service()
    if not service:
        return json.dumps({"success": False, "error": "Gmail not configured."})

    try:
        msg = service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
        parsed = _parse_message(msg, full_body=True)
        return json.dumps({"success": True, "email": parsed})
    except Exception as e:
        logger.error(f"Gmail read_message failed: {e}")
        return json.dumps({"success": False, "error": str(e)})


# ──────────────────────────────────────────────────
# Tool: gmail_reply
# ──────────────────────────────────────────────────

_NO_REPLY_ADDRESSES = {
    "mailer-daemon", "postmaster", "noreply", "no-reply",
    "no_reply", "donotreply", "do-not-reply", "notifications",
    "bounce", "auto-confirm", "automailer",
}


def gmail_reply(message_id: str, body: str,
                reply_all: bool = False, html: bool = False, **kwargs) -> str:
    """Reply to an email thread.

    Args:
        message_id: The Gmail message ID to reply to.
        body: The reply body text.
        reply_all: If True, reply to all recipients.
        html: If True, send body as HTML.

    Returns:
        JSON string with send result.
    """
    if _get_app_password_config():
        orig_from = kwargs.get("original_from", "")
        orig_subject = kwargs.get("original_subject", "")
        orig_message_id = kwargs.get("original_message_id_header", "")

        if not orig_from:
            try:
                read_result = json.loads(gmail_read_message(message_id))
                if read_result.get("success") and read_result.get("email"):
                    em = read_result["email"]
                    orig_from = em.get("from", "")
                    orig_subject = em.get("subject", "")
                    orig_message_id = em.get("message_id_header", "")
            except Exception as e:
                logger.warning(f"gmail_reply: auto-fetch failed: {e}")

        if not orig_from:
            return json.dumps({
                "success": False,
                "error": "Could not determine the original sender."
            })

        # Block replies to system/no-reply addresses
        _from_local = orig_from.split("<")[-1].split("@")[0].lower().strip()
        if _from_local in _NO_REPLY_ADDRESSES:
            logger.info(f"📧 Blocked reply to no-reply address: {orig_from}")
            return json.dumps({
                "success": False,
                "error": f"Cannot reply to system address '{orig_from}'. This is an automated/no-reply sender — skip this email."
            })

        if not orig_subject.lower().startswith("re:"):
            subject = f"Re: {orig_subject}"
        else:
            subject = orig_subject

        result = _smtp_send(
            to=orig_from, subject=subject, body=body, html=html,
            in_reply_to=orig_message_id, references=orig_message_id,
            thread_subject=subject
        )
        if result.get("success"):
            _save_replied_id(orig_message_id, subject=orig_subject)
            try:
                gmail_mark_read(message_id)
            except Exception:
                pass
        return json.dumps(result)

    service = _get_service()
    if not service:
        return json.dumps({"success": False, "error": "Gmail not configured."})

    try:
        original = service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()

        headers = {h["name"].lower(): h["value"]
                   for h in original.get("payload", {}).get("headers", [])}

        thread_id = original.get("threadId", "")
        original_subject = headers.get("subject", "")
        reply_to = headers.get("reply-to") or headers.get("from", "")
        message_id_header = headers.get("message-id", "")

        # Block replies to system/no-reply addresses
        _from_local = reply_to.split("<")[-1].split("@")[0].lower().strip()
        if _from_local in _NO_REPLY_ADDRESSES:
            logger.info(f"📧 Blocked reply to no-reply address: {reply_to}")
            return json.dumps({
                "success": False,
                "error": f"Cannot reply to system address '{reply_to}'. This is an automated/no-reply sender — skip this email."
            })

        if not original_subject.lower().startswith("re:"):
            subject = f"Re: {original_subject}"
        else:
            subject = original_subject

        to = reply_to
        cc = ""
        if reply_all:
            profile = service.users().getProfile(userId="me").execute()
            my_email = profile.get("emailAddress", "").lower()
            all_to = set()
            for field in ["to", "cc"]:
                val = headers.get(field, "")
                for addr in val.split(","):
                    addr = addr.strip()
                    if addr and addr.lower() != my_email:
                        all_to.add(addr)
            all_to.discard(reply_to)
            cc = ", ".join(all_to)

        msg = MIMEMultipart()
        msg["To"] = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc
        if message_id_header:
            msg["In-Reply-To"] = message_id_header
            msg["References"] = message_id_header

        content_type = "html" if html else "plain"
        msg.attach(MIMEText(body, content_type))

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        result = service.users().messages().send(
            userId="me", body={"raw": raw, "threadId": thread_id}
        ).execute()

        try:
            service.users().messages().modify(
                userId="me", id=message_id,
                body={"removeLabelIds": ["UNREAD"]}
            ).execute()
        except Exception:
            pass

        logger.info(f"📧 Reply sent to {to} (thread {thread_id})")
        return json.dumps({
            "success": True,
            "message_id": result.get("id", ""),
            "thread_id": thread_id,
            "to": to,
            "subject": subject,
        })
    except Exception as e:
        logger.error(f"Gmail reply failed: {e}")
        return json.dumps({"success": False, "error": str(e)})


# ──────────────────────────────────────────────────
# Tool: gmail_draft
# ──────────────────────────────────────────────────

def gmail_draft(to: str, subject: str, body: str,
                cc: str = "", html: bool = False, **kwargs) -> str:
    """Create a draft email (saved but not sent — for operator review).

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Email body text.
        cc: CC recipients (comma-separated). Optional.
        html: If True, body is HTML.

    Returns:
        JSON string with draft ID.
    """
    if _get_app_password_config():
        gdir = _gmail_dir()
        draft_dir = os.path.join(gdir, "drafts")
        os.makedirs(draft_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        draft = {"to": to, "subject": subject, "body": body, "cc": cc,
                 "html": html, "created": ts}
        draft_path = os.path.join(draft_dir, f"draft_{ts}.json")
        with open(draft_path, "w") as f:
            json.dump(draft, f, indent=2)
        logger.info(f"📝 Draft saved locally: {draft_path}")
        return json.dumps({
            "success": True, "draft_id": f"local_{ts}",
            "to": to, "subject": subject,
            "message": f"Draft saved locally at {draft_path}. Use gmail_send to send it."
        })

    service = _get_service()
    if not service:
        return json.dumps({"success": False, "error": "Gmail not configured."})

    try:
        msg = MIMEMultipart()
        msg["To"] = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc

        content_type = "html" if html else "plain"
        msg.attach(MIMEText(body, content_type))

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        result = service.users().drafts().create(
            userId="me", body={"message": {"raw": raw}}
        ).execute()

        logger.info(f"📝 Draft created for {to}: {subject}")
        return json.dumps({
            "success": True,
            "draft_id": result.get("id", ""),
            "to": to,
            "subject": subject,
            "message": "Draft saved. The operator can review and send it from Gmail."
        })
    except Exception as e:
        logger.error(f"Gmail draft failed: {e}")
        return json.dumps({"success": False, "error": str(e)})


# ──────────────────────────────────────────────────
# Tool: gmail_mark_read
# ──────────────────────────────────────────────────

def gmail_mark_read(message_id: str, **kwargs) -> str:
    """Mark an email as read.

    Args:
        message_id: The Gmail message ID to mark as read.

    Returns:
        JSON string confirming the action.
    """
    if _get_app_password_config():
        import imaplib
        cfg = _get_app_password_config()
        try:
            imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=15)
            imap.login(cfg["email"], cfg["app_password"])
            imap.select("INBOX", readonly=False)
            msg_id = message_id.encode() if isinstance(message_id, str) else message_id
            imap.store(msg_id, '+FLAGS', '\\Seen')
            imap.close()
            imap.logout()
            return json.dumps({"success": True, "message_id": message_id, "action": "marked_read"})
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)})

    service = _get_service()
    if not service:
        return json.dumps({"success": False, "error": "Gmail not configured."})

    try:
        service.users().messages().modify(
            userId="me", id=message_id,
            body={"removeLabelIds": ["UNREAD"]}
        ).execute()
        return json.dumps({"success": True, "message_id": message_id, "action": "marked_read"})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ──────────────────────────────────────────────────
# Tool: gmail_get_profile
# ──────────────────────────────────────────────────

def gmail_get_profile(**kwargs) -> str:
    """Get the email address and stats of the authorized Gmail account.

    Returns:
        JSON string with email address, total messages, total threads.
    """
    app_cfg = _get_app_password_config()
    if app_cfg:
        return json.dumps({
            "success": True,
            "email": app_cfg["email"],
            "auth_method": "app_password",
            "total_messages": "N/A (use gmail_read_inbox)",
            "total_threads": "N/A",
        })

    service = _get_service()
    if not service:
        return json.dumps({"success": False, "error": "Gmail not configured."})

    try:
        profile = service.users().getProfile(userId="me").execute()
        return json.dumps({
            "success": True,
            "email": profile.get("emailAddress", ""),
            "total_messages": profile.get("messagesTotal", 0),
            "total_threads": profile.get("threadsTotal", 0),
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ──────────────────────────────────────────────────
# Internal helpers (OAuth message parsing)
# ──────────────────────────────────────────────────

def _parse_message(msg: Dict, full_body: bool = False, service=None) -> Dict:
    headers = {}
    for h in msg.get("payload", {}).get("headers", []):
        name = h["name"].lower()
        if name in ("from", "to", "cc", "subject", "date", "message-id"):
            headers[name] = h["value"]

    body = _extract_body(msg.get("payload", {}))
    if not full_body and body and len(body) > 2000:
        body = body[:2000] + "\n... [truncated — use gmail_read_message for full content]"

    date_str = headers.get("date", "")
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
        date_str = dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        pass

    already_replied = False
    thread_id = msg.get("threadId", "")
    if service and thread_id:
        try:
            thread = service.users().threads().get(
                userId="me", id=thread_id, format="metadata",
                metadataHeaders=["From"]
            ).execute()
            profile = service.users().getProfile(userId="me").execute()
            my_email = profile.get("emailAddress", "").lower()
            for t_msg in thread.get("messages", []):
                labels = t_msg.get("labelIds", [])
                if "SENT" in labels:
                    already_replied = True
                    break
                for h in t_msg.get("payload", {}).get("headers", []):
                    if h["name"].lower() == "from" and my_email in h["value"].lower():
                        if t_msg.get("id") != msg.get("id"):
                            already_replied = True
                            break
                if already_replied:
                    break
        except Exception:
            pass

    return {
        "id": msg.get("id", ""),
        "thread_id": thread_id,
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "subject": headers.get("subject", ""),
        "date": date_str,
        "snippet": msg.get("snippet", ""),
        "body": body,
        "labels": msg.get("labelIds", []),
        "has_attachments": _has_attachments(msg.get("payload", {})),
        "already_replied": already_replied,
    }


def _extract_body(payload: Dict) -> str:
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    parts = payload.get("parts", [])
    plain_text = ""
    html_text = ""

    for part in parts:
        part_mime = part.get("mimeType", "")
        if part_mime == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                plain_text = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        elif part_mime == "text/html":
            data = part.get("body", {}).get("data", "")
            if data:
                html_raw = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                html_text = re.sub(r"<[^>]+>", " ", html_raw)
                html_text = re.sub(r"\s+", " ", html_text).strip()
        elif part_mime.startswith("multipart/"):
            nested = _extract_body(part)
            if nested:
                return nested

    return plain_text or html_text or ""


def _has_attachments(payload: Dict) -> bool:
    for part in payload.get("parts", []):
        if part.get("filename"):
            return True
        if part.get("mimeType", "").startswith("multipart/"):
            if _has_attachments(part):
                return True
    return False


# ──────────────────────────────────────────────────
# All tools dict — for bulk registration
# ──────────────────────────────────────────────────

GMAIL_TOOLS = {
    "gmail_send": gmail_send,
    "gmail_read_inbox": gmail_read_inbox,
    "gmail_search": gmail_search,
    "gmail_read_message": gmail_read_message,
    "gmail_reply": gmail_reply,
    "gmail_draft": gmail_draft,
    "gmail_mark_read": gmail_mark_read,
    "gmail_get_profile": gmail_get_profile,
}


# ──────────────────────────────────────────────────
# CLI: Setup
# ──────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    target_dir = _REPRYNTT_GMAIL_DIR  # always set up in repryntt dir

    if "--app-password" in sys.argv:
        os.makedirs(target_dir, exist_ok=True)
        print("\n🔑 Gmail App Password Setup (repryntt)")
        print("=" * 50)
        print("\nPrerequisites:")
        print("  1. Enable 2-Step Verification on your Google Account")
        print("     → https://myaccount.google.com/signinoptions/two-step-verification")
        print("  2. Create an App Password:")
        print("     → https://myaccount.google.com/apppasswords")
        print('     → Select "Mail" and "Other (repryntt)", then Generate\n')

        email = input("Enter your Gmail address: ").strip()
        if not email:
            print("❌ No email entered. Aborted.")
            sys.exit(1)
        app_pwd = input("Enter the 16-character App Password (spaces OK): ").strip()
        if not app_pwd:
            print("❌ No password entered. Aborted.")
            sys.exit(1)

        config = {"email": email, "app_password": app_pwd}
        app_path = os.path.join(target_dir, APP_PASSWORD_FILENAME)
        with open(app_path, "w") as f:
            json.dump(config, f, indent=2)
        from repryntt.platform_utils import secure_file
        secure_file(app_path)

        print(f"\n✅ Saved to {app_path}")

        print("\nTesting connection...")
        import imaplib
        try:
            imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=10)
            imap.login(email, app_pwd)
            imap.select("INBOX", readonly=True)
            _, msgs = imap.search(None, "ALL")
            count = len(msgs[0].split()) if msgs[0] else 0
            imap.close()
            imap.logout()
            print(f"   📧 Connected as: {email}")
            print(f"   📬 Inbox messages: {count}")
            print("   ✅ Gmail is ready to use!")
        except imaplib.IMAP4.error as e:
            print(f"   ❌ Auth failed: {e}")
            print("   Make sure 2FA is enabled and you used an App Password.")

    elif "--test" in sys.argv:
        if is_gmail_configured():
            result = gmail_get_profile()
            print(f"Profile: {result}")
            result = gmail_read_inbox(max_results=3, unread_only=False)
            print(f"Inbox: {result[:500]}...")
        else:
            print("Gmail not configured. Run --app-password first.")

    else:
        app_cfg = _get_app_password_config()
        print("repryntt Gmail Integration")
        print("=" * 40)
        print(f"  Configured: {is_gmail_configured()}")
        print(f"  Config dir: {_gmail_dir()}")
        if app_cfg:
            print(f"  Auth method: App Password ✓ (permanent)")
            print(f"  Email: {app_cfg['email']}")
        elif os.path.exists(_path("token.json")):
            print(f"  Auth method: OAuth (may expire every 7 days)")
        print()
        print("Commands:")
        print("  --app-password    Set up App Password (recommended)")
        print("  --test            Test the connection")
