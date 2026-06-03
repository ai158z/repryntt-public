"""
repryntt.agents.error_signatures — Shared error-fingerprinting for stuck-loop
detection.

Originally lived in codeforge.agent_loop where it was used to escalate
"try a fundamentally different approach" when consecutive module-generation
attempts failed with the same error. Moved here so the heartbeat tool-loop
in persistent_agents.py can use the same signature logic to early-detect
tool calls that keep failing the same way and either nudge the agent or
kill the cycle.

Public surface:
    error_signature(error_text)   -> short string, ""  if nothing parseable
    consecutive_match(a, b)       -> True if both non-empty and equal
"""
from __future__ import annotations

from typing import Optional


_EXCEPTION_TOKENS = (
    "Error", "error", "Exception", "Failed", "failed",
    "Traceback", "ImportError", "ModuleNotFoundError",
    "NameError", "AttributeError", "SyntaxError", "TypeError",
    "ValueError", "KeyError", "RuntimeError", "AssertionError",
    "TimeoutError", "ConnectionError", "PermissionError",
)


def error_signature(error_text: Optional[str]) -> str:
    """Cheap signature of an error: exception type + first ~40 chars of detail.

    Returns "" when nothing parseable. Two consecutive identical non-empty
    signatures mean the agent is stuck — callers should escalate.
    """
    if not error_text:
        return ""
    lines = [ln.strip() for ln in error_text.splitlines() if ln.strip()]
    if not lines:
        return ""
    for ln in reversed(lines):
        if ": " in ln and any(tok in ln for tok in _EXCEPTION_TOKENS):
            head, _, tail = ln.partition(":")
            return f"{head.strip()}:{tail.strip()[:40]}"
    return lines[-1][:60]


def consecutive_match(a: Optional[str], b: Optional[str]) -> bool:
    """True iff both are non-empty signatures and equal."""
    return bool(a) and bool(b) and a == b
