"""
Persistent dict storage for the external API.

Replaces the in-memory dicts (API_KEYS, TRADE_ORDERS, etc.) with JSON-file-backed
dicts that survive process restarts.  Each collection is a separate JSON file
written atomically (tmp + os.replace).

All reads come from the in-memory dict (fast); writes trigger an immediate
serialized save.  For in-place mutations of nested values, call .sync() to
flush the current in-memory state to disk.

Storage location: ~/.repryntt/data/ext_api/
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

from repryntt.paths import data_dir as _data_dir

_STORE_DIR = str(_data_dir() / "ext_api")


class PersistentDict(dict):
    """A dict subclass that auto-persists to a JSON file on every write.

    Usage::

        store = PersistentDict("/path/to/data.json")
        store["key"] = {"foo": "bar"}          # saved immediately
        store["key"]["foo"] = "baz"            # in-memory only!
        store.sync()                           # flush to disk

    Thread-safe for writes (single lock around save).  Reads are lock-free
    because they hit the in-memory dict.
    """

    def __init__(self, filepath: str):
        super().__init__()
        self._filepath = filepath
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        self._load()

    # ------------------------------------------------------------------
    # Disk I/O
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if os.path.exists(self._filepath):
            try:
                with open(self._filepath, "r") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    super().update(data)
                    logger.info(f"Loaded {len(data)} entries from {self._filepath}")
            except (json.JSONDecodeError, IOError) as exc:
                logger.warning(f"Could not load {self._filepath}: {exc}")

    def _save(self) -> None:
        tmp = self._filepath + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(dict(self), f, default=str, ensure_ascii=False)
            os.replace(tmp, self._filepath)
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # Dict overrides that trigger persistence
    # ------------------------------------------------------------------

    def __setitem__(self, key: str, value: Any) -> None:
        with self._lock:
            super().__setitem__(key, value)
            self._save()

    def __delitem__(self, key: str) -> None:
        with self._lock:
            super().__delitem__(key)
            self._save()

    def update(self, *args, **kwargs) -> None:  # type: ignore[override]
        with self._lock:
            super().update(*args, **kwargs)
            self._save()

    def pop(self, *args):
        with self._lock:
            result = super().pop(*args)
            self._save()
            return result

    def clear(self) -> None:
        with self._lock:
            super().clear()
            self._save()

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default  # triggers __setitem__ → _save
        return super().__getitem__(key)

    # ------------------------------------------------------------------
    # Explicit flush for in-place nested mutations
    # ------------------------------------------------------------------

    def sync(self) -> None:
        """Persist the current in-memory state to disk.

        Call this after mutating a nested value::

            store["order"]["status"] = "cancelled"
            store.sync()
        """
        with self._lock:
            self._save()
