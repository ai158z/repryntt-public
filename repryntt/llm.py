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


AI_CONFIG_PATH = Path.home() / ".repryntt" / "brain" / "ai_config.json"
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
            return {
                "provider": prov,
                "endpoint": section["endpoint"],
                "api_key": section.get("api_key", "") or "",
                "model": model,
            }
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


def call_llm(messages: List[Dict[str, str]],
             provider_info: Dict[str, str],
             max_tokens: int = 2000,
             temperature: float = 0.3,
             frequency_penalty: float = 0.0) -> Optional[str]:
    """POST messages to an OpenAI-compatible chat-completions endpoint.
    Returns the response text or None on hard failure.
    """
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
        "temperature": temperature,
    }
    if frequency_penalty and provider not in ("xai",):
        body["frequency_penalty"] = frequency_penalty

    for attempt in range(MAX_RETRIES + 1):
        try:
            r = requests.post(endpoint, json=body, headers=headers, timeout=DEFAULT_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                choices = data.get("choices", [])
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


# ── Underscore aliases for back-compat with historical callers ───────

_load_ai_config = load_ai_config
_resolve_provider = resolve_provider
_call_llm = call_llm
