"""
repryntt.brain.schema — Schema versioning for brain JSON state files.

Every persistent brain JSON file gets a ``schema_version`` field.
When loading, we check the version and warn (or migrate) if it differs
from what the current code expects.  This prevents silent data corruption
on schema changes.

Usage:
    from repryntt.brain.schema import stamp_version, check_version

    # On save:
    data = {"memories": [...]}
    stamp_version(data, "semantic_memory")
    json_write(path, data)

    # On load:
    data = json_read(path)
    check_version(data, "semantic_memory")
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── Schema registry ─────────────────────────────────────────────────
# Map file_key → current expected schema version.
# Bump these when you change the shape of a state file.

SCHEMA_VERSIONS: Dict[str, int] = {
    "ai_config": 1,
    "consciousness_state": 1,
    "conversational_awareness": 1,
    "cot_queue": 1,
    "daemon_state": 1,
    "experiment_tracker_state": 1,
    "learned_behaviors": 1,
    "memory_mesh": 1,
    "phase_state": 1,
    "reasoning_chain": 1,
    "semantic_memory": 1,
    "sleep_wake": 1,
    "guardian_rate_limits": 1,
}


def stamp_version(data: Dict[str, Any], file_key: str) -> Dict[str, Any]:
    """Set ``schema_version`` on *data* to the current expected version.

    Call this right before writing to disk.  Returns *data* for chaining.
    """
    expected = SCHEMA_VERSIONS.get(file_key, 1)
    data["schema_version"] = expected
    return data


def check_version(
    data: Dict[str, Any],
    file_key: str,
    *,
    auto_stamp: bool = True,
) -> Optional[int]:
    """Check ``schema_version`` in *data* against expected.

    Returns the version found (or None if missing).

    - If missing and *auto_stamp* is True, stamps the current version
      (first-time migration — file predates versioning).
    - If version < expected: logs a warning (future: run migrations).
    - If version > expected: logs an error (newer data than code).
    """
    expected = SCHEMA_VERSIONS.get(file_key, 1)
    found = data.get("schema_version")

    if found is None:
        if auto_stamp:
            data["schema_version"] = expected
            logger.debug("Stamped schema_version=%d on %s (first-time)", expected, file_key)
        return None

    if found < expected:
        logger.warning(
            "Schema migration needed for %s: found v%d, expected v%d",
            file_key, found, expected,
        )
        # Future: call migration functions here

    elif found > expected:
        logger.error(
            "Schema %s has v%d but code expects v%d — data may be from a newer version",
            file_key, found, expected,
        )

    return found
