"""
PursuitStore — JSON-backed persistence for Pursuits.

Phase 1: standalone store at ~/.repryntt/workspace/agents/operator/pursuits.json.
Atomic writes (tmp + rename) so a crash mid-save never corrupts the pool.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .model import Pursuit

logger = logging.getLogger(__name__)


class PursuitStore:
    """Thread-safe JSON store for Pursuits."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: Dict[str, Pursuit] = {}
        self._load()

    # ── Persistence ────────────────────────────────────────

    def _load(self) -> None:
        with self._lock:
            self._cache = {}
            if not self.path.exists():
                return
            try:
                with open(self.path, "r") as f:
                    raw = json.load(f)
            except Exception as e:
                logger.warning(f"PursuitStore load failed ({self.path}): {e}")
                return
            items = raw.get("pursuits", []) if isinstance(raw, dict) else []
            for item in items:
                try:
                    p = Pursuit.from_dict(item)
                    self._cache[p.id] = p
                except Exception as e:
                    logger.warning(f"Skipping malformed pursuit: {e}")

    def save(self) -> None:
        with self._lock:
            data = {
                "version": 1,
                "pursuits": [p.to_dict() for p in self._cache.values()],
            }
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=".pursuits.", suffix=".tmp", dir=str(self.path.parent)
            )
            try:
                with os.fdopen(tmp_fd, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_path, self.path)
            except Exception as e:
                logger.warning(f"PursuitStore save failed: {e}")
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # ── CRUD ───────────────────────────────────────────────

    def upsert(self, pursuit: Pursuit, persist: bool = True) -> Pursuit:
        with self._lock:
            self._cache[pursuit.id] = pursuit
            if persist:
                self.save()
            return pursuit

    def get(self, pursuit_id: str) -> Optional[Pursuit]:
        with self._lock:
            return self._cache.get(pursuit_id)

    def remove(self, pursuit_id: str, persist: bool = True) -> bool:
        with self._lock:
            existed = self._cache.pop(pursuit_id, None) is not None
            if existed and persist:
                self.save()
            return existed

    def all(self) -> List[Pursuit]:
        with self._lock:
            return list(self._cache.values())

    def active(self) -> List[Pursuit]:
        return [p for p in self.all() if p.active]

    def by_source(self, source: str) -> List[Pursuit]:
        return [p for p in self.all() if p.source == source]

    def by_topic(self, topic: str) -> List[Pursuit]:
        topic_l = (topic or "").strip().lower()
        return [p for p in self.all() if p.topic.lower() == topic_l]

    def find_active_by_topic(self, topic: str) -> Optional[Pursuit]:
        for p in self.by_topic(topic):
            if p.active:
                return p
        return None
