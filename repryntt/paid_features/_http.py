"""
repryntt.paid_features._http — Shared HTTPS client + license-key plumbing.

Used by every paid-feature router (forge, video, etc.). One place to:
  • look up the operator's API key (env var → license.json → None)
  • POST/GET to api.repryntt.com with auth header
  • normalize errors into a consistent shape
  • produce the paywall response when no key is configured

Keep this module dependency-light — only ``requests`` from outside stdlib.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("repryntt.paid_features.http")


# ── Configuration ─────────────────────────────────────────────────────

API_BASE_URL = os.environ.get(
    "REPRYNTT_API_BASE", "https://api.repryntt.com"
).rstrip("/")

# Where to look for the API key when REPRYNTT_API_KEY env var is unset.
LICENSE_FILE = Path.home() / ".repryntt" / "license.json"

# How long we wait on the hosted service before giving up. Generous so a
# slow build doesn't time out mid-call, but bounded so a hung server
# doesn't hang the agent.
DEFAULT_TIMEOUT = 60

# Signup destination shown in paywall messages.
SIGNUP_URL = "https://repryntt.com"

# Per-feature signup URLs — paid features link to the relevant page so
# users land on the right pricing/product page rather than the generic root.
FEATURE_SIGNUP = {
    "codeforge":        "https://repryntt.com/codeforge",
    "video":            "https://repryntt.com/video",
    "video production": "https://repryntt.com/video",
    "coherence":        "https://repryntt.com/coherence",
    "coherence cloud":  "https://repryntt.com/coherence",
}


# ── API key resolution ────────────────────────────────────────────────


def load_api_key() -> Optional[str]:
    """Return the operator's API key, or None if not configured.

    Precedence:
      1. ``REPRYNTT_API_KEY`` environment variable
      2. ``~/.repryntt/license.json`` field ``api_key``
      3. None
    """
    env_key = (os.environ.get("REPRYNTT_API_KEY") or "").strip()
    if env_key:
        return env_key
    try:
        if LICENSE_FILE.exists():
            with open(LICENSE_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            key = (data.get("api_key") or "").strip()
            if key:
                return key
    except Exception:
        logger.debug("license.json read failed (non-fatal)", exc_info=True)
    return None


# ── Paywall response ──────────────────────────────────────────────────


def paywall_response(feature: str,
                     message: Optional[str] = None) -> Dict[str, Any]:
    """Build the standard paywall payload returned when:
       • no local implementation is present (OSS install), AND
       • no API key is configured.

    Callers SHOULD pass the human-readable feature name so the message
    is specific (e.g. "CodeForge", "Video Production").
    """
    signup = FEATURE_SIGNUP.get(feature.lower(), SIGNUP_URL)
    return {
        "success": False,
        "paid_feature": True,
        "feature": feature,
        "error": message or f"{feature} is a paid hosted feature.",
        "signup_url": signup,
        "docs": (
            "Set REPRYNTT_API_KEY in your environment OR add an api_key "
            "field to ~/.repryntt/license.json after signup."
        ),
    }


def service_unavailable_response(feature: str, detail: str) -> Dict[str, Any]:
    """Returned when an API key IS configured but the hosted service
    didn't respond cleanly (network error, 5xx, etc.). Different from
    paywall — operator already paid, the service is just sick right now.
    """
    return {
        "success": False,
        "paid_feature": True,
        "feature": feature,
        "service_unavailable": True,
        "error": f"{feature} hosted service is currently unavailable.",
        "detail": detail[:500],
        "retry": "Try again in a few minutes, or check status.repryntt.com.",
    }


# ── HTTPS client ──────────────────────────────────────────────────────


def post(path: str, payload: Dict[str, Any], api_key: str,
         feature: str = "service",
         timeout: int = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """POST to ``{API_BASE_URL}{path}`` with the given JSON body and
    Bearer auth. Returns the parsed JSON response, or a normalized error
    payload on transport / HTTP-level failure.
    """
    return _request("POST", path, payload, api_key, feature, timeout)


def get(path: str, params: Optional[Dict[str, Any]], api_key: str,
        feature: str = "service",
        timeout: int = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    return _request("GET", path, params or {}, api_key, feature, timeout)


def _request(method: str, path: str, body_or_params: Dict[str, Any],
             api_key: str, feature: str, timeout: int) -> Dict[str, Any]:
    try:
        import requests  # local import — keep startup cheap
    except ImportError:
        return service_unavailable_response(
            feature, "python 'requests' package not installed",
        )

    url = f"{API_BASE_URL}{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "repryntt-client/1.0",
    }

    try:
        if method == "GET":
            r = requests.get(url, params=body_or_params, headers=headers, timeout=timeout)
        else:
            r = requests.post(url, json=body_or_params, headers=headers, timeout=timeout)
    except requests.exceptions.Timeout:
        return service_unavailable_response(feature, f"timed out after {timeout}s")
    except requests.exceptions.ConnectionError as e:
        return service_unavailable_response(feature, f"connection error: {e}")
    except Exception as e:
        return service_unavailable_response(feature, f"request error: {e}")

    if r.status_code == 401 or r.status_code == 403:
        return {
            "success": False,
            "paid_feature": True,
            "feature": feature,
            "auth_failed": True,
            "error": "REPRYNTT_API_KEY is invalid, expired, or lacks access to this feature.",
            "status_code": r.status_code,
            "signup_url": FEATURE_SIGNUP.get(feature.lower(), SIGNUP_URL),
        }
    if r.status_code == 402:
        return {
            "success": False,
            "paid_feature": True,
            "feature": feature,
            "plan_limit": True,
            "error": "Plan quota exceeded for this billing period.",
            "status_code": 402,
            "upgrade_url": FEATURE_SIGNUP.get(feature.lower(), SIGNUP_URL),
        }
    if r.status_code >= 500:
        return service_unavailable_response(
            feature, f"server returned {r.status_code}: {r.text[:200]}",
        )

    try:
        data = r.json()
    except Exception:
        return service_unavailable_response(
            feature, f"non-JSON response (status {r.status_code}): {r.text[:200]}",
        )

    # Allow successful responses without 'success' key — wrap them
    if isinstance(data, dict) and "success" not in data and r.status_code < 400:
        data = {"success": True, **data}
    return data
