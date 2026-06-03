"""Dynamic capability self-awareness for conversation mode.

Primary source: bootstrap/CAPABILITIES.md (AI-managed, self-evolving).
Fallback: programmatic generation from the tool registry if the bootstrap
file is missing or empty.

The bootstrap file approach allows Andrew to update his own understanding
of capabilities/limitations as he discovers them — true self-awareness.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

BRAIN_DIR = Path(os.environ.get("REPRYNTT_BRAIN", str(Path.home() / ".repryntt" / "brain")))
BOOTSTRAP_DIR = Path(os.environ.get(
    "REPRYNTT_BOOTSTRAP",
    str(Path(__file__).resolve().parents[3] / "bootstrap")
))
SELF_AWARENESS_FILE = "SELF_AWARENESS.md"

# Cache to avoid re-reading file every conversation turn
_cache: Dict[str, object] = {"content": None, "time": 0.0, "mtime": 0.0}
_CACHE_TTL = 120  # 2 minutes


def _find_self_awareness_file() -> Optional[Path]:
    """Locate SELF_AWARENESS.md in bootstrap directory."""
    # Check brain/bootstrap first (runtime copy — AI can edit this one)
    brain_path = BRAIN_DIR / "bootstrap" / SELF_AWARENESS_FILE
    if brain_path.exists():
        return brain_path
    # Fall back to package bootstrap dir (source template)
    pkg_path = BOOTSTRAP_DIR / SELF_AWARENESS_FILE
    if pkg_path.exists():
        return pkg_path
    return None


def _read_self_awareness_file() -> Optional[str]:
    """Read and cache the SELF_AWARENESS.md bootstrap file."""
    global _cache
    now = time.time()

    sa_file = _find_self_awareness_file()
    if not sa_file:
        return None

    file_mtime = sa_file.stat().st_mtime
    if (_cache["content"] and
            (now - _cache["time"]) < _CACHE_TTL and
            _cache["mtime"] == file_mtime):
        return _cache["content"]

    try:
        content = sa_file.read_text(encoding="utf-8")
        if len(content.strip()) < 50:
            return None
        _cache = {"content": content, "time": now, "mtime": file_mtime}
        return content
    except Exception as e:
        logger.debug(f"Failed to read SELF_AWARENESS.md: {e}")
        return None


def generate_conversation_capability_context(
    registry_names: Optional[Set[str]] = None,
    conversation_tools: Optional[List[str]] = None,
) -> str:
    """Generate capability context for the conversation system prompt.

    Reads from bootstrap/CAPABILITIES.md (AI-managed).
    Falls back to programmatic generation if the file is missing.
    """
    content = _read_self_awareness_file()
    if content:
        return "\n" + content

    # Fallback: generate from registry if bootstrap file missing
    if registry_names:
        return _generate_fallback_context(registry_names, conversation_tools)

    return ""


def _generate_fallback_context(
    registry_names: Set[str],
    conversation_tools: Optional[List[str]] = None,
) -> str:
    """Programmatic fallback when CAPABILITIES.md doesn't exist.

    Uses tool registry to infer capabilities. Less nuanced than the
    bootstrap file, but better than nothing.
    """
    conversation_set = set(conversation_tools) if conversation_tools else set()

    # Infer capabilities from registered tool names
    can_now = []
    can_later = []

    CAPABILITY_SIGNALS = {
        "Send and read email": ["gmail_read_inbox", "gmail_send", "gmail_reply"],
        "Search the web": ["google_web_search", "web_search_results_only", "knowledge_search"],
        "Remember things": ["append_daily_memory", "memory_search"],
        "See through camera": ["capture_camera", "analyze_image"],
        "Navigate and find places": ["google_maps_search", "find_nearby_places"],
        "Check crypto/trading": ["sim_portfolio", "sim_price_check"],
        "Manage tasks": ["task_queue_status", "add_task"],
        "Speak and listen": ["speak", "listen"],
        "Check time": ["get_current_time"],
    }

    for display_name, tool_names in CAPABILITY_SIGNALS.items():
        has_any = any(tn in registry_names for tn in tool_names)
        if not has_any:
            continue
        in_conversation = any(tn in conversation_set for tn in tool_names)
        if in_conversation:
            can_now.append(display_name)
        else:
            can_later.append(display_name)

    lines = [
        "",
        "## Your Capabilities (auto-generated — CAPABILITIES.md not found)",
        "",
        "**What you can do RIGHT NOW:**",
    ]
    for cap in can_now:
        lines.append(f"- {cap}")

    if can_later:
        lines.append("")
        lines.append("**What you can do AFTER this conversation:**")
        for cap in can_later:
            lines.append(f"- {cap}")

    lines.append("")
    lines.append("**CANNOT do:** Send money (Venmo/PayPal/Zelle), "
                 "make phone calls, install apps, order deliveries.")
    lines.append("")
    lines.append("RULE: Never promise to do something not listed above.")
    lines.append("")

    return "\n".join(lines)


# Keep the old function signature for backwards compat
def generate_capability_summary(
    registry_names: Set[str],
    conversation_tools: Optional[List[str]] = None,
) -> Dict[str, List[str]]:
    """Structured capability data (used by tests or programmatic access)."""
    content = _read_self_awareness_file()
    if content:
        # Parse sections from the markdown
        can_do = []
        cannot_do = []
        can_later = []

        current_section = None
        for line in content.split("\n"):
            if "Can Do RIGHT NOW" in line:
                current_section = "now"
            elif "Can Do AFTER" in line:
                current_section = "later"
            elif "CANNOT Do" in line:
                current_section = "cannot"
            elif "Honesty Rules" in line:
                current_section = None
            elif line.startswith("- ") and current_section:
                item = line[2:].strip()
                if current_section == "now":
                    can_do.append(item)
                elif current_section == "later":
                    can_later.append(item)
                elif current_section == "cannot":
                    cannot_do.append(item)

        return {
            "can_do": can_do,
            "cannot_do": cannot_do,
            "can_do_after_conversation": can_later,
        }

    # Fallback
    return {
        "can_do": [],
        "cannot_do": [
            "Send money via Venmo, PayPal, Zelle, or bank transfer",
            "Make phone calls or send SMS",
            "Install apps or modify the OS",
        ],
        "can_do_after_conversation": [],
    }
