"""
repryntt.llm — Shared LLM-config + call helpers.

These functions used to live in ``repryntt.codeforge.generator``, but
they're general-purpose (read ai_config.json, resolve a provider, make
a chat-completions request). Council, the agent-brain-builder wizard,
and other non-codeforge consumers need them — and codeforge is a paid
feature absent from the OSS install, so the originals can't be the
canonical home anymore.

This module IS shipped in OSS. It does not import codeforge. It does
the minimum needed: config loading + provider resolution + a single
HTTP POST to a chat-completions endpoint.

Public surface:
    load_ai_config()                                   → dict
    resolve_provider(config, provider="", model_override="") → dict
    call_llm(messages, provider_info, max_tokens=2000,
             temperature=0.3, frequency_penalty=0.0)   → str | None

Private aliases preserved for historical callers that used the
underscore-prefixed names:
    _load_ai_config, _resolve_provider, _call_llm
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("repryntt.llm")


# Env-overridable so an Enterprise runtime (per company / per AI employee)
# can point at its own ai_config.json instead of the operator's personal
# ~/.repryntt. Absent env = the OSS default, unchanged.
AI_CONFIG_PATH = Path(
    os.environ.get(
        "REPRYNTT_AI_CONFIG",
        str(Path.home() / ".repryntt" / "brain" / "ai_config.json"),
    )
)
DEFAULT_TIMEOUT = 180
MAX_RETRIES = 2


# ── Config ───────────────────────────────────────────────────────────


def load_ai_config() -> Dict[str, Any]:
    """Load ai_config.json from ~/.repryntt/brain/. Returns the
    ai_provider sub-dict so callers don't need to walk the nesting."""
    try:
        with open(AI_CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logger.warning(f"ai_config.json unreadable: {e}")
        return {}
    if isinstance(raw, dict) and "ai_provider" in raw and isinstance(raw["ai_provider"], dict):
        return raw["ai_provider"]
    return raw if isinstance(raw, dict) else {}


# ── Provider resolution ──────────────────────────────────────────────


def resolve_provider(config: Dict[str, Any],
                     provider: str = "",
                     model_override: str = "") -> Dict[str, str]:
    """Resolve (endpoint, api_key, model, provider) for a given provider
    name, with fallback to nvidia when the requested provider is not
    configured or has no endpoint.

    If ``model_override`` is non-empty it replaces whatever model the
    config picks. This is how callers force a specific model.
    """
    if not provider:
        provider = (config.get("andrew_provider")
                    or config.get("artemis_provider")
                    or config.get("provider", "nvidia"))

    def _try(prov: str) -> Optional[Dict[str, str]]:
        section = config.get(prov, {})
        if isinstance(section, dict) and section.get("endpoint"):
            model = section.get("coding_model") or section.get("model", "")
            pi = {
                "provider": prov,
                "endpoint": section["endpoint"],
                "api_key": section.get("api_key", "") or "",
                "model": model,
            }
            # Model-ladder rungs (all optional in ai_config.json): tier=
            # "grunt"/"orchestrator" in call_llm swaps to these; absent keys
            # = no-op so single-model configs behave exactly as before.
            for k in ("model_grunt", "model_mechanical",
                      "model_orchestrator", "model_fallback"):
                if section.get(k):
                    pi[k] = section[k]
            return pi
        return None

    result = _try(provider)
    if not result and provider != "nvidia":
        logger.warning(f"Provider '{provider}' missing endpoint; falling back to nvidia")
        result = _try("nvidia")
    if result:
        if model_override:
            result["model"] = model_override
        return result

    # Last resort: flat config
    return {
        "provider": provider,
        "endpoint": config.get("endpoint", ""),
        "api_key":  config.get("api_key", "") or "",
        "model":    model_override or config.get("model", ""),
    }


# ── Call ─────────────────────────────────────────────────────────────


import threading as _threading
from collections import deque as _deque

_RPM_LOCK = _threading.Lock()
_RPM_HISTORY: Dict[str, Any] = {}


def _rate_limit(key: str, rpm: int) -> None:
    """Proactive sliding-window rate limiter — block until sending one more request
    keeps `key` under `rpm` requests/minute. Prevents provider 429s (e.g. NVIDIA's
    free tier is ~40 RPM and an agentic shift bursts well past that) instead of only
    reacting after the throttle hits. Keyed per provider+key so tenants don't share
    each other's quota."""
    if not rpm or rpm <= 0:
        return
    while True:
        with _RPM_LOCK:
            now = time.time()
            dq = _RPM_HISTORY.setdefault(key, _deque())
            while dq and now - dq[0] > 60.0:
                dq.popleft()
            if len(dq) < rpm:
                dq.append(now)
                return
            wait = 60.0 - (now - dq[0]) + 0.05
        time.sleep(min(max(wait, 0.05), 60.0))


# ── Anthropic NATIVE path (prompt caching) ───────────────────────────
# Anthropic's OpenAI-compat endpoint does NOT support prompt caching —
# cache_control blocks exist only on the native /v1/messages API. Agent
# loops re-send the whole prefix every round, so on Opus-tier pricing the
# compat path costs ~5-10x more than the cached native path. Both call_llm
# functions route provider=anthropic through here: OpenAI shapes in, a
# native request with 3 cache breakpoints (tools, system, conversation
# tail) out, response translated back to the OpenAI message shape.

_ANTHROPIC_NATIVE = "https://api.anthropic.com/v1/messages"

# Split marker a caller embeds in a leading system message to divide the STABLE
# prefix (operator rulebook, honesty rules, tool grammar — byte-identical across
# every shift of every company → a fleet-wide cache read) from the VOLATILE tail
# (this company's brain, which is regenerated each shift). Splitting them lets the
# stable half cache across shifts instead of being re-written every time it shares
# a block with the churning brain. See _operator_system in operations.py.
CACHE_SPLIT = "\x1e<<REPRYNTT_STABLE_VOLATILE_SPLIT>>\x1e"


# ── Cooperative cancellation ─────────────────────────────────────────────────
# Background builds/shifts run in detached threads. Every LLM call is a natural
# stop point (it's where the money is spent), so a caller can register a zero-arg
# predicate; when it returns True the NEXT call_llm/call_llm_tools raises
# LLMCancelled and the whole build/shift unwinds — spend halts within one call.
# Opt-in via a contextvar, so the OSS daemon (which never sets it) is unaffected.
import contextvars as _contextvars


class LLMCancelled(BaseException):
    """Raised at an LLM call boundary when the registered cancel-check fires.

    Inherits BaseException (like KeyboardInterrupt) ON PURPOSE: the build/shift
    loops wrap LLM calls in broad ``except Exception`` handlers, and a cancel MUST
    propagate through those to unwind the whole loop — otherwise a stop would be
    swallowed and the spend would continue. Only an explicit ``except LLMCancelled``
    (or bare ``except:``) catches it."""


_cancel_check: "_contextvars.ContextVar" = _contextvars.ContextVar(
    "repryntt_llm_cancel_check", default=None)


def set_cancel_check(fn):
    """Register a zero-arg predicate for THIS thread/context; returns a token to
    pass to reset_cancel_check() in a finally block."""
    return _cancel_check.set(fn)


def reset_cancel_check(token) -> None:
    try:
        _cancel_check.reset(token)
    except Exception:
        pass


def _check_cancel() -> None:
    fn = _cancel_check.get()
    if fn is None:
        return
    try:
        stop = bool(fn())
    except LLMCancelled:
        raise
    except Exception:
        stop = False
    if stop:
        raise LLMCancelled()


def _strip_cache_split(messages):
    """The split marker is an Anthropic-native caching hint. On the OpenAI-compat
    path (grok/openai/google) it must never reach the model — replace it with a
    plain paragraph break. (Those providers do automatic prefix caching, which the
    stable-first ordering still helps; they just don't take a hand-placed marker.)"""
    if not messages:
        return messages
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str) and CACHE_SPLIT in c:
            m = {**m, "content": c.replace(CACHE_SPLIT, "\n\n")}
        out.append(m)
    return out


def _to_anthropic_body(messages, tools, model, max_tokens):
    """OpenAI-shaped messages/tools → native /v1/messages body with caching.

    Caching is a PREFIX MATCH — one byte changing anywhere in a block invalidates
    it. So we keep the byte-stable content in its own cached blocks ahead of the
    volatile content, and spend the 4 available breakpoints where reuse is highest:

      1. tool definitions   — identical across the fleet
      2. stable system      — the operator rulebook (before CACHE_SPLIT)
      3. volatile system    — this company's brain (after CACHE_SPLIT); stable
                              WITHIN a shift, so the loop re-reads it each round
      4. conversation tail  — the growing transcript

    A system message that arrives AFTER the conversation has started (a mid-shift
    operator note — burn budget, clock) is emitted as a `<system-reminder>` in the
    tail instead of being hoisted into the top `system` block, so the cached prefix
    is never invalidated by volatile per-round context.
    """
    system_blocks: List[Dict[str, Any]] = []   # {"text", "stable"} — leading system
    out_msgs: List[Dict[str, Any]] = []
    pending_results: List[Dict[str, Any]] = []
    started = False  # has any user/assistant/tool content begun?

    def _flush_results():
        # Anthropic requires ALL tool_results of a turn in ONE user message.
        if pending_results:
            out_msgs.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for m in messages or []:
        role = m.get("role")
        if role == "system":
            content = str(m.get("content") or "")
            if not started:
                # Leading system → the cached top-of-prompt prefix.
                if CACHE_SPLIT in content:
                    stable, volatile = content.split(CACHE_SPLIT, 1)
                    if stable.strip():
                        system_blocks.append({"text": stable, "stable": True})
                    system_blocks.append({"text": volatile, "stable": False})
                else:
                    system_blocks.append({"text": content,
                                          "stable": bool(m.get("stable"))})
            else:
                # Mid-conversation system → tail reminder (keeps the prefix intact).
                _flush_results()
                out_msgs.append({"role": "user", "content": [
                    {"type": "text",
                     "text": f"<system-reminder>\n{content}\n</system-reminder>"}]})
            continue
        if role == "tool":
            started = True
            pending_results.append({
                "type": "tool_result",
                "tool_use_id": str(m.get("tool_call_id") or ""),
                "content": str(m.get("content") or "")[:60000],
            })
            continue
        _flush_results()
        started = True
        if role == "assistant":
            blocks: List[Dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": str(m["content"])})
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {"_raw": fn.get("arguments")}
                blocks.append({"type": "tool_use", "id": tc.get("id") or "",
                               "name": fn.get("name") or "", "input": args})
            out_msgs.append({"role": "assistant",
                             "content": blocks or [{"type": "text", "text": "…"}]})
        else:  # user (or unknown role → user)
            out_msgs.append({"role": "user",
                             "content": [{"type": "text",
                                          "text": str(m.get("content") or "")}]})
    _flush_results()
    if not out_msgs:
        out_msgs = [{"role": "user", "content": [{"type": "text", "text": ""}]}]

    # Breakpoint 4: the conversation tail.
    last_content = out_msgs[-1].get("content")
    if isinstance(last_content, list) and last_content:
        last_content[-1]["cache_control"] = {"type": "ephemeral"}

    body: Dict[str, Any] = {"model": model, "max_tokens": max_tokens,
                            "messages": out_msgs}
    if system_blocks:
        sys_out = [{"type": "text", "text": b["text"]} for b in system_blocks]
        # Breakpoint 2: the last STABLE block — cross-shift / cross-company reuse.
        last_stable = max((i for i, b in enumerate(system_blocks) if b["stable"]),
                          default=None)
        if last_stable is not None:
            sys_out[last_stable]["cache_control"] = {"type": "ephemeral"}
        # Breakpoint 3: the last system block (the brain) — within-shift reuse —
        # unless it IS the stable block (then breakpoint 2 already covers it).
        if last_stable != len(sys_out) - 1:
            sys_out[-1]["cache_control"] = {"type": "ephemeral"}
        body["system"] = sys_out
    if tools:
        a_tools = []
        for t in tools:
            fn = (t.get("function") or {}) if t.get("type") == "function" else t
            if not fn.get("name"):
                continue
            a_tools.append({"name": fn["name"],
                            "description": fn.get("description") or "",
                            "input_schema": fn.get("parameters") or {"type": "object"}})
        if a_tools:
            # Breakpoint 1: tool definitions (they render first in the prefix).
            a_tools[-1]["cache_control"] = {"type": "ephemeral"}
            body["tools"] = a_tools
    return body


def _from_anthropic_response(data) -> Dict[str, Any]:
    """Native response content blocks → OpenAI-style assistant message."""
    text_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for b in (data.get("content") or []):
        if b.get("type") == "text":
            text_parts.append(b.get("text") or "")
        elif b.get("type") == "tool_use":
            tool_calls.append({"id": b.get("id") or "", "type": "function",
                               "function": {"name": b.get("name") or "",
                                            "arguments": json.dumps(b.get("input") or {})}})
    msg: Dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if data.get("stop_reason"):
        msg["stop_reason"] = data["stop_reason"]
    return msg


def _call_anthropic_native(messages, provider_info, tools=None,
                           max_tokens: int = 2000,
                           temperature: float = 0.3) -> Optional[Dict[str, Any]]:
    """POST to the native Anthropic API with prompt-caching breakpoints.
    Returns an OpenAI-style assistant message dict, or None on hard failure."""
    try:
        import requests
    except ImportError:
        logger.error("python 'requests' package not installed")
        return None
    api_key = provider_info.get("api_key", "")
    model = provider_info.get("model", "")
    if not model:
        logger.error("anthropic native: no model")
        return None
    headers = {"Content-Type": "application/json", "x-api-key": api_key,
               "anthropic-version": "2023-06-01"}
    body = _to_anthropic_body(messages, tools, model, max_tokens)
    _m = model.lower()
    # 4.7+/Fable/Sonnet-5-class models reject sampling params (400).
    if not ("opus-4-7" in _m or "opus-4-8" in _m or "opus-5" in _m
            or "fable-5" in _m or "fable-6" in _m or "mythos-5" in _m
            or "claude-5-" in _m or "sonnet-5" in _m):
        body["temperature"] = temperature
    # ③ Context editing — on the agentic (tools) loop only, let the server clear
    # STALE tool results as the transcript grows. Two payoffs on long autonomous
    # runs: context stays bounded (no ballooning cost), and the cache's 20-block
    # lookback keeps reaching the prior cached prefix instead of silently missing.
    # Beta; model-gated to the families that support it + a kill-switch env.
    _betas: List[str] = []
    if tools and os.environ.get("REPRYNTT_CONTEXT_EDITING", "1") != "0" and (
            "opus-4-6" in _m or "opus-4-7" in _m or "opus-4-8" in _m
            or "sonnet-4-6" in _m or "sonnet-5" in _m
            or "fable-5" in _m or "mythos-5" in _m):
        _betas.append("context-management-2025-06-27")
        body["context_management"] = {"edits": [{"type": "clear_tool_uses_20250919"}]}
    # Refusal fallback — Fable/Mythos-class safety classifiers can decline a
    # benign request (stop_reason "refusal"). Autonomous workers can't press
    # "OK, use Opus" like a human in a chat, so opt into the server-side
    # fallback: a declined request is transparently re-served by the fallback
    # model inside the same call, with credit-style repricing. A client-side
    # backstop below covers the case where the beta itself is unavailable.
    _fallback_model = ""
    if "fable-5" in _m or "mythos-5" in _m:
        _fallback_model = (provider_info.get("model_fallback")
                           or "claude-opus-4-8")
        if os.environ.get("REPRYNTT_REFUSAL_FALLBACK", "1") != "0":
            _betas.append("server-side-fallback-2026-06-01")
            body["fallbacks"] = [{"model": _fallback_model}]
    if _betas:
        headers["anthropic-beta"] = ",".join(_betas)
    try:
        rpm = int(provider_info.get("rpm") or os.environ.get("REPRYNTT_LLM_RPM") or 0)
    except Exception:
        rpm = 0
    if rpm:
        import hashlib
        _key = f"anthropic:{hashlib.sha1((api_key or model).encode()).hexdigest()[:10]}"
        _rate_limit(_key, rpm)
    _client_fallback_used = False
    _budget_retry_used = False
    for attempt in range(MAX_RETRIES + 1):
        try:
            t0 = time.time()
            r = requests.post(_ANTHROPIC_NATIVE, json=body, headers=headers,
                              timeout=DEFAULT_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                u = data.get("usage") or {}
                # Surface caching so cost issues are diagnosable from logs:
                # cache_read bills ~0.1x — that number should dominate in loops.
                logger.info(
                    "💸 anthropic native: in=%s cache_read=%s cache_write=%s out=%s (%s)",
                    u.get("input_tokens"), u.get("cache_read_input_tokens"),
                    u.get("cache_creation_input_tokens"), u.get("output_tokens"),
                    data.get("model") or model)
                # Client-side refusal backstop: if the server-side fallback
                # didn't rescue it (beta off/unavailable), re-issue directly
                # on the fallback model — once.
                if (data.get("stop_reason") == "refusal" and _fallback_model
                        and not _client_fallback_used):
                    _client_fallback_used = True
                    logger.warning(
                        "anthropic native: %s refused — retrying on %s",
                        body.get("model"), _fallback_model)
                    _record_route("anthropic", body.get("model", model),
                                  provider_info.get("_tier", ""),
                                  provider_info.get("_task_type", ""),
                                  False, int((time.time() - t0) * 1000),
                                  u, note="refusal→fallback")
                    body["model"] = _fallback_model
                    body.pop("fallbacks", None)
                    continue
                # Sonnet-5-class models THINK by default on the native API — a
                # small max_tokens can be consumed entirely by thinking blocks,
                # returning ZERO text with stop_reason=max_tokens. That reads
                # as a silent empty reply at every call site (empty themes,
                # empty brains). Self-heal: retry once with triple the budget.
                if (data.get("stop_reason") == "max_tokens"
                        and not _budget_retry_used
                        and not any(b.get("type") == "text" and b.get("text")
                                    for b in (data.get("content") or []))):
                    _budget_retry_used = True
                    body["max_tokens"] = min(int(body.get("max_tokens") or 1000) * 3, 16000)
                    logger.warning(
                        "anthropic native: thinking consumed the entire token "
                        "budget (no text) — retrying with max_tokens=%s",
                        body["max_tokens"])
                    continue
                _record_route("anthropic", data.get("model") or model,
                              provider_info.get("_tier", ""),
                              provider_info.get("_task_type", ""),
                              data.get("stop_reason") != "refusal",
                              int((time.time() - t0) * 1000), u)
                return _from_anthropic_response(data)
            if r.status_code in (429, 529):
                wait = 15 * (attempt + 1)
                logger.warning(f"anthropic native {r.status_code}; waiting {wait}s")
                time.sleep(wait)
                continue
            logger.warning(f"anthropic native {r.status_code}: {r.text[:200]}")
            # Self-heal: if a beta surface (context editing, server-side
            # fallback) is rejected, drop the betas and retry the same request
            # plain — a shift never breaks over an optional optimization.
            # Caching (cache_control) is GA and stays. The client-side refusal
            # backstop above still covers fallback semantics.
            if r.status_code == 400 and (
                    body.pop("context_management", None) is not None
                    or body.pop("fallbacks", None) is not None):
                headers.pop("anthropic-beta", None)
                body.pop("context_management", None)
                body.pop("fallbacks", None)
                logger.warning("anthropic native 400 with beta features — retrying without them")
                continue
            if r.status_code >= 500 and attempt < MAX_RETRIES:
                time.sleep(5)
                continue
            return None
        except Exception as e:
            logger.warning(f"anthropic native attempt {attempt}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(5)
                continue
            return None
    return None


# ── Model ladder ─────────────────────────────────────────────────────
#
# Three tiers, routed by DECISION DENSITY, not by task: the expensive model
# plans/reviews/escalates (a sliver of the tokens), the mid model executes,
# the cheap model does high-volume low-ambiguity grunt work. Combined with
# a verification gate (call_llm_ladder) this is cheaper AND more reliable
# than one flagship doing everything: most sub-tasks clear at the cheap
# tier, and only the hard residue pays flagship prices.

TIERS = ("grunt", "worker", "orchestrator")

def escalate_tier(tier: str) -> Optional[str]:
    """The next tier up, or None if already at the top. Unknown/legacy tier
    names ("mechanical", "") escalate into the ladder at "worker"."""
    if tier in ("mechanical", "grunt"):
        return "worker"
    if tier in ("", "worker"):
        return "orchestrator"
    return None


def _tiered_provider(provider_info: Dict[str, str], tier: str) -> Dict[str, str]:
    """Task-tier model routing. A company runs one BYOK provider, but not every
    sub-task needs the flagship. Tiers:
      "grunt" (alias "mechanical"): deterministic / "baked" work (outline fill,
          schema extraction, classification) → "model_grunt" or legacy
          "model_mechanical";
      "worker" or "": the founder's configured model — no-op;
      "orchestrator": planning / decomposition / final review / escalation
          target → "model_orchestrator".
    Missing map entry = no-op, so single-model providers (xAI) and callers
    that pass no tier are unaffected."""
    if not provider_info:
        return provider_info
    swap = ""
    if tier in ("mechanical", "grunt"):
        swap = (provider_info.get("model_grunt")
                or provider_info.get("model_mechanical") or "")
    elif tier == "orchestrator":
        swap = provider_info.get("model_orchestrator") or ""
    if swap and swap != provider_info.get("model"):
        pi = dict(provider_info)
        pi["model"] = swap
        return pi
    return provider_info


# ── Routing telemetry ────────────────────────────────────────────────
#
# One JSONL line per model call: which tier/model handled which kind of
# task, whether it succeeded, what it cost. Over thousands of gated calls
# this becomes the routing table — the empirical record of which tier
# clears which work — which is the durable asset of tiered routing.

_ROUTE_LOG = os.environ.get(
    "REPRYNTT_ROUTE_LOG",
    os.path.expanduser("~/.repryntt/route_ledger.jsonl"))
_ROUTE_LOCK = _threading.Lock()

def _record_route(provider: str, model: str, tier: str, task_type: str,
                  ok: bool, latency_ms: int, usage: Optional[Dict] = None,
                  note: str = "") -> None:
    """Best-effort append; never lets telemetry break a model call."""
    try:
        rec = {"ts": round(time.time(), 3), "provider": provider,
               "model": model, "tier": tier or "worker",
               "task_type": task_type, "ok": bool(ok),
               "latency_ms": latency_ms}
        if usage:
            rec["tokens_in"] = usage.get("input_tokens") or usage.get("prompt_tokens")
            rec["tokens_out"] = usage.get("output_tokens") or usage.get("completion_tokens")
            if usage.get("cache_read_input_tokens") is not None:
                rec["tokens_cache_read"] = usage.get("cache_read_input_tokens")
        if note:
            rec["note"] = note
        line = json.dumps(rec, separators=(",", ":")) + "\n"
        with _ROUTE_LOCK:
            os.makedirs(os.path.dirname(_ROUTE_LOG), exist_ok=True)
            with open(_ROUTE_LOG, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


def call_llm(messages: List[Dict[str, str]],
             provider_info: Dict[str, str],
             max_tokens: int = 2000,
             temperature: float = 0.3,
             frequency_penalty: float = 0.0,
             tier: str = "",
             task_type: str = "") -> Optional[str]:
    """POST messages to an OpenAI-compatible chat-completions endpoint.
    Returns the response text or None on hard failure.
    Anthropic routes through the NATIVE messages API for prompt caching.
    ``tier`` routes the model ladder ("grunt"/"worker"/"orchestrator";
    legacy "mechanical" = grunt). ``task_type`` is a free-form label that
    lands in the routing ledger so tier hit-rates are analyzable per kind
    of work.
    """
    _check_cancel()  # stop point — abort before spending if the caller cancelled
    provider_info = dict(_tiered_provider(provider_info, tier))
    provider_info["_tier"] = tier
    provider_info["_task_type"] = task_type
    # Anthropic → native API (prompt caching); compat endpoint can't cache.
    if (provider_info.get("provider") == "anthropic"
            or "api.anthropic.com" in (provider_info.get("endpoint") or "")):
        msg = _call_anthropic_native(messages, provider_info, tools=None,
                                     max_tokens=max_tokens, temperature=temperature)
        return (msg.get("content") or "") if msg is not None else None
    messages = _strip_cache_split(messages)
    try:
        import requests
    except ImportError:
        logger.error("python 'requests' package not installed — call_llm requires it")
        return None

    endpoint = (provider_info.get("endpoint") or "").rstrip("/")
    if not endpoint:
        logger.error("call_llm: no endpoint")
        return None
    if not endpoint.endswith("/chat/completions"):
        endpoint = endpoint + "/chat/completions"
    api_key = provider_info.get("api_key", "")
    model = provider_info.get("model", "")
    provider = provider_info.get("provider", "")
    if not model:
        logger.error("call_llm: no model")
        return None

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    # Adaptive-thinking flagships (Opus 4.7+, Fable 5+, Mythos 5+) reject
    # the `temperature` param — extended thinking is always on. Sending
    # it returns 400. Skip for those models. Also skip `frequency_penalty`
    # because Anthropic's compat shim rejects that for the same models.
    _m = (model or "").lower()
    _temp_deprecated = (
        "opus-4-7" in _m or "opus-4-8" in _m or "opus-5" in _m
        or "fable-5" in _m or "fable-6" in _m or "mythos-5" in _m
        or "claude-5-" in _m
    )
    if not _temp_deprecated:
        body["temperature"] = temperature
    if frequency_penalty and provider not in ("xai",) and not _temp_deprecated:
        body["frequency_penalty"] = frequency_penalty

    # Proactively pace requests to the provider's RPM cap so we never trip the
    # throttle. rpm comes from the provider_info (set per plan) or a global env
    # default; keyed per provider+key so each tenant's quota is independent.
    try:
        rpm = int(provider_info.get("rpm") or os.environ.get("REPRYNTT_LLM_RPM") or 0)
    except Exception:
        rpm = 0
    if rpm:
        import hashlib
        _key = f"{provider}:{hashlib.sha1((api_key or model).encode()).hexdigest()[:10]}"
        _rate_limit(_key, rpm)

    for attempt in range(MAX_RETRIES + 1):
        try:
            t0 = time.time()
            r = requests.post(endpoint, json=body, headers=headers, timeout=DEFAULT_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                choices = data.get("choices", [])
                _record_route(provider, model, tier, task_type, bool(choices),
                              int((time.time() - t0) * 1000),
                              data.get("usage"))
                if choices:
                    return (choices[0].get("message", {}) or {}).get("content", "") or ""
                return ""
            if r.status_code == 429:
                wait = 15 * (attempt + 1)
                logger.warning(f"call_llm 429; waiting {wait}s")
                time.sleep(wait)
                continue
            logger.warning(f"call_llm {r.status_code}: {r.text[:200]}")
            if r.status_code >= 500 and attempt < MAX_RETRIES:
                time.sleep(5)
                continue
            return None
        except Exception as e:
            logger.warning(f"call_llm attempt {attempt}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(5)
                continue
            return None
    return None


def call_llm_ladder(messages: List[Dict[str, str]],
                    provider_info: Dict[str, str],
                    gate,
                    start_tier: str = "grunt",
                    max_tokens: int = 2000,
                    temperature: float = 0.3,
                    task_type: str = "") -> Optional[str]:
    """Gate-driven escalation: run the call at ``start_tier``; if ``gate``
    rejects the output, retry one tier up, until the ladder tops out.

    ``gate(text) -> bool`` is the caller's verification — a schema parse, a
    test run, a reconciliation check. The gate is what makes cheap tiers
    SAFE: most sub-tasks clear at the cheap tier, only the hard residue
    pays flagship prices, and a wrong cheap answer never escapes because
    the same gate guards every rung. Each attempt (and which tier finally
    passed) lands in the routing ledger via call_llm.

    Returns the first gate-passing text, or the LAST tier's text (even if
    gate-failing — callers may still salvage) or None on hard failure."""
    tier: Optional[str] = start_tier
    last: Optional[str] = None
    while tier is not None:
        text = call_llm(messages, provider_info, max_tokens=max_tokens,
                        temperature=temperature, tier=tier,
                        task_type=task_type or "ladder")
        if text is not None:
            last = text
            try:
                if gate(text):
                    return text
            except Exception as e:
                logger.warning("call_llm_ladder: gate raised (%s) — escalating", e)
        nxt = escalate_tier(tier)
        if nxt:
            logger.info("call_llm_ladder: %s failed gate at tier=%s — escalating to %s",
                        task_type or "task", tier, nxt)
        tier = nxt
    return last


_TOOLS_UNSUPPORTED = "__TOOLS_UNSUPPORTED__"


def call_llm_tools(messages: List[Dict[str, Any]],
                   provider_info: Dict[str, str],
                   tools: List[Dict[str, Any]],
                   max_tokens: int = 4000,
                   temperature: float = 0.3,
                   tier: str = "",
                   task_type: str = "") -> Any:
    """Native function-calling variant of :func:`call_llm`.

    POSTs an OpenAI-compatible request with ``tools`` + ``tool_choice="auto"``
    (mirroring the OSS ``provider_router`` native path) and returns the FULL
    assistant *message* dict so the caller can read structured ``tool_calls``.
    This is how the OSS drives agents end-to-end; a model emits real tool calls
    instead of hand-writing a JSON ``{"done":true}`` blob in text.

    Returns:
      - the assistant message dict on success (``content`` + maybe ``tool_calls``)
      - ``"__TOOLS_UNSUPPORTED__"`` if the provider rejects tool schemas
        (caller should fall back to the text protocol)
      - ``None`` on hard failure.
    """
    _check_cancel()  # stop point — abort the agent loop before the next spend
    provider_info = dict(_tiered_provider(provider_info, tier))
    provider_info["_tier"] = tier
    provider_info["_task_type"] = task_type
    # Anthropic → native API (prompt caching): agent loops re-send the whole
    # prefix every round, so this is where caching matters most.
    if (provider_info.get("provider") == "anthropic"
            or "api.anthropic.com" in (provider_info.get("endpoint") or "")):
        return _call_anthropic_native(messages, provider_info, tools=tools,
                                      max_tokens=max_tokens, temperature=temperature)

    messages = _strip_cache_split(messages)
    try:
        import requests
    except ImportError:
        logger.error("python 'requests' package not installed — call_llm_tools requires it")
        return None

    endpoint = (provider_info.get("endpoint") or "").rstrip("/")
    if not endpoint:
        logger.error("call_llm_tools: no endpoint")
        return None
    if not endpoint.endswith("/chat/completions"):
        endpoint = endpoint + "/chat/completions"
    api_key = provider_info.get("api_key", "")
    model = provider_info.get("model", "")
    provider = provider_info.get("provider", "")
    if not model:
        logger.error("call_llm_tools: no model")
        return None

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "tools": tools,
        "tool_choice": "auto",
    }
    _m = (model or "").lower()
    _temp_deprecated = (
        "opus-4-7" in _m or "opus-4-8" in _m or "opus-5" in _m
        or "fable-5" in _m or "fable-6" in _m or "mythos-5" in _m
        or "claude-5-" in _m
    )
    if not _temp_deprecated:
        body["temperature"] = temperature

    try:
        rpm = int(provider_info.get("rpm") or os.environ.get("REPRYNTT_LLM_RPM") or 0)
    except Exception:
        rpm = 0
    if rpm:
        import hashlib
        _key = f"{provider}:{hashlib.sha1((api_key or model).encode()).hexdigest()[:10]}"
        _rate_limit(_key, rpm)

    for attempt in range(MAX_RETRIES + 1):
        try:
            t0 = time.time()
            r = requests.post(endpoint, json=body, headers=headers, timeout=DEFAULT_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                choices = data.get("choices", [])
                _record_route(provider, model, tier, task_type, bool(choices),
                              int((time.time() - t0) * 1000),
                              data.get("usage"))
                if choices:
                    return (choices[0].get("message", {}) or {})
                return {}
            if r.status_code == 429:
                wait = 15 * (attempt + 1)
                logger.warning(f"call_llm_tools 429; waiting {wait}s")
                time.sleep(wait)
                continue
            # Some OpenAI-compat providers reject the `tools` field. Detect that
            # and signal the caller to fall back to the text protocol rather than
            # failing the whole task.
            if r.status_code in (400, 404, 422):
                low = (r.text or "").lower()
                if (("tool" in low or "function" in low)
                        and ("not support" in low or "unsupported" in low
                             or "unknown" in low or "invalid" in low
                             or "no such" in low or "not allowed" in low)):
                    logger.warning("call_llm_tools: provider rejects tools "
                                   f"({r.status_code}) — falling back to text protocol")
                    return _TOOLS_UNSUPPORTED
            logger.warning(f"call_llm_tools {r.status_code}: {r.text[:200]}")
            if r.status_code >= 500 and attempt < MAX_RETRIES:
                time.sleep(5)
                continue
            return None
        except Exception as e:
            logger.warning(f"call_llm_tools attempt {attempt}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(5)
                continue
            return None
    return None


# ── Underscore aliases for back-compat with historical callers ───────

_load_ai_config = load_ai_config
_resolve_provider = resolve_provider
_call_llm = call_llm
