#!/usr/bin/env python3
"""
Context Compaction for SAIGE Tier 1 (8GB / Mobile)

Ported from OpenClaw's compaction.ts — adapted for Python and 4K context windows.
Aggressively compacts conversation history to fit within small context budgets
while preserving critical information (active tasks, decisions, identifiers).

Usage:
    from repryntt.core.memory.context_compaction import ContextCompactor
    compactor = ContextCompactor(context_window=4096, llm_endpoint="http://localhost:8080")
    compacted = compactor.compact_messages(messages, previous_summary="...")
"""

import re
import json
import logging
import math
from typing import List, Dict, Optional, Tuple, Any

logger = logging.getLogger(__name__)

# --- Constants (from OpenClaw) ---
BASE_CHUNK_RATIO = 0.4
MIN_CHUNK_RATIO = 0.15
SAFETY_MARGIN = 1.2  # 20% buffer for token estimation inaccuracy
SUMMARIZATION_OVERHEAD_TOKENS = 512  # Lower than OpenClaw's 4096 — we have a tiny context
DEFAULT_SUMMARY_FALLBACK = "No prior history."
DEFAULT_PARTS = 2

MERGE_SUMMARIES_INSTRUCTIONS = """Merge these partial summaries into a single cohesive summary.

MUST PRESERVE:
- Active tasks and their current status (in-progress, blocked, pending)
- Batch operation progress (e.g., '5/17 items completed')
- The last thing the user requested and what was being done about it
- Decisions made and their rationale
- TODOs, open questions, and constraints
- Any commitments or follow-ups promised
- Tool call results and their outcomes

PRIORITIZE recent context over older history. The agent needs to know
what it was doing, not just what was discussed."""

IDENTIFIER_PRESERVATION_INSTRUCTIONS = (
    "Preserve all opaque identifiers exactly as written (no shortening or reconstruction), "
    "including UUIDs, hashes, IDs, tokens, API keys, hostnames, IPs, ports, URLs, and file names."
)

# Regex patterns for identifiers that must never be truncated
IDENTIFIER_PATTERNS = [
    re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'),  # UUID
    re.compile(r'[0-9a-fA-F]{40,64}'),  # SHA hashes
    re.compile(r'https?://\S+'),  # URLs
    re.compile(r'/[\w./\-]+\.\w+'),  # File paths
    re.compile(r'\b0x[0-9a-fA-F]{6,}\b'),  # Hex addresses (Solana, etc.)
    re.compile(r'\b[A-Za-z0-9]{32,50}\b'),  # Base58 addresses (Solana pubkeys)
]


def estimate_tokens(text: str) -> int:
    """Estimate token count. ~4 chars per token for English, with safety margin."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 3.5))


def estimate_messages_tokens(messages: List[Dict]) -> int:
    """Estimate total tokens across all messages."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, dict):
            total += estimate_tokens(json.dumps(content))
        # Role/name overhead ~4 tokens per message
        total += 4
    return total


def split_messages_by_token_share(
    messages: List[Dict], parts: int = DEFAULT_PARTS
) -> List[List[Dict]]:
    """
    Split messages into roughly equal token-weight chunks.
    Ensures no chunk boundary splits a logical exchange.
    """
    if not messages:
        return []

    parts = max(1, min(parts, len(messages)))
    if parts <= 1:
        return [messages]

    total_tokens = estimate_messages_tokens(messages)
    target_tokens = total_tokens / parts

    chunks: List[List[Dict]] = []
    current: List[Dict] = []
    current_tokens = 0

    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            msg_tokens = estimate_tokens(content) + 4
        else:
            msg_tokens = estimate_tokens(json.dumps(content)) + 4

        if (
            len(chunks) < parts - 1
            and current
            and current_tokens + msg_tokens > target_tokens
        ):
            chunks.append(current)
            current = []
            current_tokens = 0

        current.append(msg)
        current_tokens += msg_tokens

    if current:
        chunks.append(current)

    return chunks


def chunk_messages_by_max_tokens(
    messages: List[Dict], max_tokens: int
) -> List[List[Dict]]:
    """
    Chunk messages so each chunk fits within max_tokens (with safety margin).
    """
    if not messages:
        return []

    effective_max = max(1, int(max_tokens / SAFETY_MARGIN))

    chunks: List[List[Dict]] = []
    current_chunk: List[Dict] = []
    current_tokens = 0

    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            msg_tokens = estimate_tokens(content) + 4
        else:
            msg_tokens = estimate_tokens(json.dumps(content)) + 4

        if current_chunk and current_tokens + msg_tokens > effective_max:
            chunks.append(current_chunk)
            current_chunk = []
            current_tokens = 0

        current_chunk.append(msg)
        current_tokens += msg_tokens

        # Oversized single message — push it as its own chunk
        if msg_tokens > effective_max:
            chunks.append(current_chunk)
            current_chunk = []
            current_tokens = 0

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def compute_adaptive_chunk_ratio(
    messages: List[Dict], context_window: int
) -> float:
    """
    Adjust chunk ratio based on average message size.
    Large messages → smaller chunks to avoid exceeding model limits.
    """
    if not messages:
        return BASE_CHUNK_RATIO

    total_tokens = estimate_messages_tokens(messages)
    avg_tokens = total_tokens / len(messages)
    safe_avg = avg_tokens * SAFETY_MARGIN
    avg_ratio = safe_avg / context_window

    if avg_ratio > 0.1:
        reduction = min(avg_ratio * 2, BASE_CHUNK_RATIO - MIN_CHUNK_RATIO)
        return max(MIN_CHUNK_RATIO, BASE_CHUNK_RATIO - reduction)

    return BASE_CHUNK_RATIO


def extract_identifiers(text: str) -> List[str]:
    """Extract all identifiers that must be preserved during compaction."""
    identifiers = []
    for pattern in IDENTIFIER_PATTERNS:
        identifiers.extend(pattern.findall(text))
    return list(set(identifiers))


def format_messages_for_summary(messages: List[Dict]) -> str:
    """Format messages into a readable transcript for summarization."""
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, dict):
            content = json.dumps(content, indent=2)
        # Truncate extremely long individual messages
        if len(content) > 2000:
            content = content[:1900] + "\n... [truncated]"
        lines.append(f"[{role}]: {content}")
    return "\n\n".join(lines)


class ContextCompactor:
    """
    Manages context compaction for the local LLM's 4K context window.

    Keeps a rolling summary of conversation history and compacts when
    the context would exceed the budget. Uses the local LLM itself
    to generate summaries.
    """

    def __init__(
        self,
        context_window: int = 4096,
        llm_endpoint: str = "http://localhost:8080",
        reserve_for_response: int = 512,
        reserve_for_system: int = 512,
    ):
        self.context_window = context_window
        self.llm_endpoint = llm_endpoint
        self.reserve_for_response = reserve_for_response
        self.reserve_for_system = reserve_for_system
        self.available_for_history = (
            context_window - reserve_for_response - reserve_for_system - SUMMARIZATION_OVERHEAD_TOKENS
        )
        self._previous_summary: Optional[str] = None
        self._compaction_count: int = 0

    @property
    def previous_summary(self) -> Optional[str]:
        return self._previous_summary

    def needs_compaction(self, messages: List[Dict]) -> bool:
        """Check if messages exceed the available context budget."""
        msg_tokens = estimate_messages_tokens(messages)
        summary_tokens = estimate_tokens(self._previous_summary) if self._previous_summary else 0
        return (msg_tokens + summary_tokens) > self.available_for_history

    def compact_messages(
        self,
        messages: List[Dict],
        previous_summary: Optional[str] = None,
    ) -> Tuple[List[Dict], str]:
        """
        Compact messages to fit within context budget.

        Returns:
            (kept_messages, updated_summary)
            - kept_messages: recent messages that fit in remaining budget
            - updated_summary: summary of older messages
        """
        if previous_summary is not None:
            self._previous_summary = previous_summary

        total_tokens = estimate_messages_tokens(messages)
        summary_tokens = estimate_tokens(self._previous_summary) if self._previous_summary else 0

        # If everything fits, no compaction needed
        if (total_tokens + summary_tokens) <= self.available_for_history:
            return messages, self._previous_summary or DEFAULT_SUMMARY_FALLBACK

        # Find the split point: keep recent messages, summarize older ones
        budget_for_recent = int(self.available_for_history * 0.6)  # 60% for recent messages
        budget_for_summary = self.available_for_history - budget_for_recent

        # Walk backwards from the end to find how many recent messages fit
        kept_messages = []
        kept_tokens = 0
        split_idx = len(messages)

        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            content = msg.get("content", "")
            if isinstance(content, str):
                msg_tokens = estimate_tokens(content) + 4
            else:
                msg_tokens = estimate_tokens(json.dumps(content)) + 4

            if kept_tokens + msg_tokens > budget_for_recent:
                split_idx = i + 1
                break
            kept_tokens += msg_tokens
            split_idx = i

        # Messages to summarize (older) vs keep (recent)
        to_summarize = messages[:split_idx]
        kept_messages = messages[split_idx:]

        if not to_summarize:
            return kept_messages, self._previous_summary or DEFAULT_SUMMARY_FALLBACK

        # Generate summary of older messages
        new_summary = self._summarize(to_summarize, budget_for_summary)
        self._previous_summary = new_summary
        self._compaction_count += 1

        logger.info(
            f"📦 Compacted context: {len(messages)} msgs → {len(kept_messages)} kept + summary "
            f"(~{estimate_tokens(new_summary)} tokens). Compaction #{self._compaction_count}"
        )

        return kept_messages, new_summary

    def _summarize(self, messages: List[Dict], token_budget: int) -> str:
        """
        Generate a summary of messages using the local LLM.
        Falls back to extractive summary if LLM is unavailable.
        """
        transcript = format_messages_for_summary(messages)

        # Collect identifiers that must be preserved
        all_text = " ".join(
            msg.get("content", "") for msg in messages
            if isinstance(msg.get("content"), str)
        )
        identifiers = extract_identifiers(all_text)
        id_note = ""
        if identifiers:
            id_note = f"\n\nIDENTIFIERS TO PRESERVE: {', '.join(identifiers[:20])}"

        prompt = (
            f"Summarize this conversation concisely in under {token_budget} tokens.\n\n"
            f"{IDENTIFIER_PRESERVATION_INSTRUCTIONS}\n\n"
            f"{MERGE_SUMMARIES_INSTRUCTIONS}{id_note}\n\n"
            f"--- CONVERSATION ---\n{transcript}\n--- END ---\n\n"
            f"Summary:"
        )

        # Prepend previous summary if exists
        if self._previous_summary and self._previous_summary != DEFAULT_SUMMARY_FALLBACK:
            prompt = (
                f"Previous context summary:\n{self._previous_summary}\n\n"
                f"Now summarize the NEW conversation below, merging with the previous summary.\n\n"
                + prompt
            )

        try:
            summary = self._call_llm(prompt)
            if summary and len(summary.strip()) > 20:
                return summary.strip()
        except Exception as e:
            logger.warning(f"LLM summarization failed, using extractive fallback: {e}")

        # Extractive fallback: just keep the last few important messages
        return self._extractive_summary(messages, token_budget)

    def _call_llm(self, prompt: str) -> str:
        """Call the local LLM for summarization."""
        import requests

        response = requests.post(
            f"{self.llm_endpoint}/v1/chat/completions",
            json={
                "model": "local",
                "messages": [
                    {"role": "system", "content": "You are a precise summarizer. Be concise."},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 500,
                "temperature": 0.3,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

    def _extractive_summary(self, messages: List[Dict], token_budget: int) -> str:
        """Fallback: extract key sentences without LLM."""
        lines = []
        # Prioritize: user messages, tool results, assistant decisions
        priority_order = ["user", "tool", "assistant", "system"]

        for role in priority_order:
            for msg in messages:
                if msg.get("role") == role:
                    content = msg.get("content", "")
                    if isinstance(content, str) and len(content) > 10:
                        # Take first sentence or 150 chars
                        first_sentence = content.split(".")[0][:150]
                        lines.append(f"[{role}] {first_sentence}")

            if estimate_tokens("\n".join(lines)) > token_budget * 0.8:
                break

        return "\n".join(lines) if lines else DEFAULT_SUMMARY_FALLBACK

    def build_context_with_summary(
        self,
        system_prompt: str,
        messages: List[Dict],
        skill_context: str = "",
    ) -> List[Dict]:
        """
        Build the full context array for LLM inference, with compaction applied.

        Returns a list of messages ready for the chat completions API:
        [system_prompt, summary_message (if any), ...recent_messages]
        """
        # Compact if needed
        kept_messages, summary = self.compact_messages(messages)

        # Build the final context
        context = []

        # System prompt (with skill context injected)
        full_system = system_prompt
        if skill_context:
            full_system += f"\n\n## Active Skills\n{skill_context}"
        if summary and summary != DEFAULT_SUMMARY_FALLBACK:
            full_system += f"\n\n## Conversation History Summary\n{summary}"

        context.append({"role": "system", "content": full_system})
        context.extend(kept_messages)

        return context

    def get_stats(self) -> Dict[str, Any]:
        """Return compaction statistics."""
        return {
            "compaction_count": self._compaction_count,
            "has_summary": self._previous_summary is not None,
            "summary_tokens": estimate_tokens(self._previous_summary) if self._previous_summary else 0,
            "context_window": self.context_window,
            "available_for_history": self.available_for_history,
        }
