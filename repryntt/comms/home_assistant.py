"""
repryntt.comms.home_assistant — Home Assistant REST API Integration.

Gives Andrew direct control over the physical environment: lights, locks,
sensors, climate, covers, media players, scenes, and automations via
Home Assistant's REST API.

No HACS plugins or custom components needed — just a long-lived access
token from the HA UI (Profile -> Security -> Long-Lived Access Tokens).

Config in config/ai_config.json:
    "home_assistant": {
        "url": "http://homeassistant.local:8123",
        "token": "YOUR_LONG_LIVED_ACCESS_TOKEN",
        "enabled": true
    }
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)

ENTITY_CACHE_TTL = 300  # 5 minutes
REQUEST_TIMEOUT = 15
MAX_RETRIES = 2

# Domains that require extra caution — logged to telemetry
SENSITIVE_DOMAINS = frozenset({
    "lock", "alarm_control_panel", "siren",
})


class HomeAssistantClient:
    """REST API client for Home Assistant.

    Thread-safe with connection pooling. Entity state is cached
    to avoid hammering HA with redundant queries.
    """

    def __init__(self, url: str, token: str):
        self._base_url = url.rstrip("/")
        self._token = token
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        self._entity_cache: Dict[str, Dict] = {}
        self._cache_time: float = 0.0
        self._cache_lock = threading.Lock()

    def _url(self, path: str) -> str:
        return urljoin(self._base_url + "/", path.lstrip("/"))

    def _get(self, path: str, timeout: int = REQUEST_TIMEOUT) -> Any:
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self._session.get(self._url(path), timeout=timeout)
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as e:
                if attempt < MAX_RETRIES:
                    time.sleep(1)
                    continue
                raise ConnectionError(f"HA API error: {e}") from e

    def _post(self, path: str, data: Optional[Dict] = None,
              timeout: int = REQUEST_TIMEOUT) -> Any:
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self._session.post(
                    self._url(path), json=data or {}, timeout=timeout)
                resp.raise_for_status()
                try:
                    return resp.json()
                except ValueError:
                    return {"status": "ok"}
            except requests.RequestException as e:
                if attempt < MAX_RETRIES:
                    time.sleep(1)
                    continue
                raise ConnectionError(f"HA API error: {e}") from e

    # ── Connection Test ──────────────────────────────────────────

    def ping(self) -> bool:
        """Test connectivity to Home Assistant. Returns True if reachable."""
        try:
            result = self._get("api/")
            return result.get("message") == "API running."
        except Exception:
            return False

    # ── Entity State ─────────────────────────────────────────────

    def _refresh_cache(self):
        """Refresh the entity state cache."""
        with self._cache_lock:
            if time.time() - self._cache_time < ENTITY_CACHE_TTL:
                return
            try:
                states = self._get("api/states")
                self._entity_cache = {
                    s["entity_id"]: s for s in states
                }
                self._cache_time = time.time()
                logger.debug(f"HA cache refreshed: {len(self._entity_cache)} entities")
            except Exception as e:
                logger.warning(f"HA cache refresh failed: {e}")

    def get_all_states(self) -> Dict[str, Dict]:
        """Get all entity states (cached)."""
        self._refresh_cache()
        return self._entity_cache

    def get_state(self, entity_id: str) -> Optional[Dict]:
        """Get current state of a specific entity (live, not cached)."""
        try:
            return self._get(f"api/states/{entity_id}")
        except Exception as e:
            logger.warning(f"HA get_state({entity_id}) failed: {e}")
            return None

    def list_entities(self, domain: str = "") -> List[Dict]:
        """List entities, optionally filtered by domain.

        Returns compact dicts: {entity_id, state, friendly_name, domain}.
        """
        self._refresh_cache()
        results = []
        for eid, state in self._entity_cache.items():
            if domain and not eid.startswith(f"{domain}."):
                continue
            attrs = state.get("attributes", {})
            results.append({
                "entity_id": eid,
                "state": state.get("state", "unknown"),
                "friendly_name": attrs.get("friendly_name", eid),
                "domain": eid.split(".")[0],
            })
        results.sort(key=lambda e: e["entity_id"])
        return results

    # ── Service Calls ────────────────────────────────────────────

    def call_service(self, domain: str, service: str,
                     entity_id: str = "",
                     data: Optional[Dict] = None) -> Dict:
        """Call a Home Assistant service.

        Examples:
            call_service("light", "turn_on", "light.living_room", {"brightness": 200})
            call_service("climate", "set_temperature", "climate.hvac", {"temperature": 72})
            call_service("lock", "lock", "lock.front_door")
        """
        payload = dict(data) if data else {}
        if entity_id:
            payload["entity_id"] = entity_id

        self._post(f"api/services/{domain}/{service}", payload)

        # Invalidate cache for affected entity
        with self._cache_lock:
            if entity_id and entity_id in self._entity_cache:
                del self._entity_cache[entity_id]
                self._cache_time = 0

        return {
            "service": f"{domain}.{service}",
            "entity_id": entity_id,
            "status": "called",
            "data": data,
        }

    def turn_on(self, entity_id: str, **attrs) -> Dict:
        """Turn on a device with optional attributes."""
        domain = entity_id.split(".")[0]
        return self.call_service(domain, "turn_on", entity_id, attrs or None)

    def turn_off(self, entity_id: str) -> Dict:
        """Turn off a device."""
        domain = entity_id.split(".")[0]
        return self.call_service(domain, "turn_off", entity_id)

    def activate_scene(self, scene_id: str) -> Dict:
        """Activate a Home Assistant scene."""
        if not scene_id.startswith("scene."):
            scene_id = f"scene.{scene_id}"
        return self.call_service("scene", "turn_on", scene_id)

    def close(self):
        """Close the HTTP session."""
        self._session.close()


# ── Singleton ────────────────────────────────────────────────────────

_client: Optional[HomeAssistantClient] = None
_client_lock = threading.Lock()


def get_ha_client() -> Optional[HomeAssistantClient]:
    """Get or create the singleton HA client from config.

    Returns None if HA is not configured or disabled.
    """
    global _client
    if _client is not None:
        return _client

    with _client_lock:
        if _client is not None:
            return _client

        try:
            import os
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "config", "ai_config.json"
            )
            if not os.path.isfile(config_path):
                return None

            with open(config_path) as f:
                config = json.load(f)

            ha_config = config.get("home_assistant", {})
            if not ha_config.get("enabled", False):
                return None

            url = ha_config.get("url", "")
            token = ha_config.get("token", "")
            if not url or not token or token == "YOUR_LONG_LIVED_ACCESS_TOKEN":
                logger.info("Home Assistant not configured (no URL or token)")
                return None

            _client = HomeAssistantClient(url, token)
            if _client.ping():
                logger.info(f"Home Assistant connected: {url}")
            else:
                logger.warning(f"Home Assistant unreachable at {url} -- tools will retry on use")

            return _client

        except Exception as e:
            logger.warning(f"Home Assistant init failed: {e}")
            return None
