"""
repryntt.core.frameworks.registry — Disk-backed spec registry.

Holds every Framework spec the system knows about. Specs live as JSON files
under ``~/.repryntt/frameworks/*.json`` and are hot-reloaded on demand.

Seed specs (shipped with the code) are loaded from
``repryntt/core/frameworks/seeds/`` on first run and copied into the user
store; after that the user store is authoritative. This lets the agent
mutate a seed without losing the original.

Public API
----------
    registry = get_registry()
    registry.all()                            → List[Framework]
    registry.get(framework_id)                → Framework | None
    registry.match(goal_text)                 → List[Framework]   (keyword match)
    registry.save(framework)                  → None               (writes JSON)
    registry.fork(base_id, new_id, mutator)   → Framework          (creates descendant)
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Callable, Dict, List, Optional

from repryntt.core.frameworks.schema import Framework

logger = logging.getLogger("repryntt.frameworks.registry")


# ── Paths ────────────────────────────────────────────────────────────────

def _user_frameworks_dir() -> Path:
    p = Path.home() / ".repryntt" / "frameworks"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _seeds_dir() -> Path:
    return Path(__file__).parent / "seeds"


# ── Registry ─────────────────────────────────────────────────────────────

class FrameworkRegistry:
    """In-memory cache of Framework specs, backed by JSON on disk."""

    def __init__(self, store_dir: Optional[Path] = None):
        self.store_dir = store_dir or _user_frameworks_dir()
        self._cache: Dict[str, Framework] = {}
        self._seed_from_builtins()
        self._reload()

    # ── Init helpers ──

    def _seed_from_builtins(self) -> None:
        """Copy bundled seed JSON into the user store on first run."""
        seeds = _seeds_dir()
        if not seeds.exists():
            return
        for src in seeds.glob("*.json"):
            dst = self.store_dir / src.name
            if not dst.exists():
                try:
                    shutil.copyfile(src, dst)
                    logger.info(f"Seeded framework spec: {src.name}")
                except Exception as e:
                    logger.warning(f"Failed to seed {src.name}: {e}")

    def _reload(self) -> None:
        self._cache.clear()
        for path in self.store_dir.glob("*.json"):
            try:
                with open(path, "r") as f:
                    spec = Framework.from_dict(json.load(f))
                self._cache[spec.id] = spec
            except Exception as e:
                logger.warning(f"Failed to load framework spec {path.name}: {e}")

    # ── Queries ──

    def all(self) -> List[Framework]:
        return list(self._cache.values())

    def get(self, framework_id: str) -> Optional[Framework]:
        if framework_id not in self._cache:
            self._reload()
        return self._cache.get(framework_id)

    def match(self, goal: str, *, limit: int = 3) -> List[Framework]:
        """Return frameworks whose match_keywords appear in ``goal`` (case-insensitive).

        Ranked by (keyword-hit count, win_rate, recency).
        """
        goal_l = (goal or "").lower()
        hits: List[tuple[int, float, Framework]] = []
        for fw in self._cache.values():
            score = sum(1 for kw in fw.match_keywords if kw.lower() in goal_l)
            if score > 0:
                hits.append((score, fw.win_rate, fw))
        hits.sort(key=lambda t: (t[0], t[1], t[2].created), reverse=True)
        return [h[2] for h in hits[:limit]]

    # ── Mutations ──

    def save(self, framework: Framework) -> Path:
        """Persist a framework spec to disk. Bumps version if id already exists."""
        existing = self._cache.get(framework.id)
        if existing is not None and existing is not framework:
            framework.version = max(framework.version, existing.version + 1)
        path = self.store_dir / f"{framework.id}.json"
        with open(path, "w") as f:
            json.dump(framework.to_dict(), f, indent=2, sort_keys=True)
        self._cache[framework.id] = framework
        logger.info(
            f"Saved framework spec: {framework.id} v{framework.version} "
            f"({len(framework.states)} states)"
        )
        return path

    def fork(self, base_id: str, new_id: str,
             mutator: Callable[[Framework], Framework]) -> Framework:
        """Create a descendant framework from ``base_id`` via ``mutator``.

        The new framework records ``base_id`` in its lineage and starts at
        version 1. ``mutator`` receives a deep copy and returns the edited
        version; ``new_id`` must not already exist.
        """
        if new_id in self._cache:
            raise ValueError(f"framework id already exists: {new_id}")
        base = self.get(base_id)
        if base is None:
            raise ValueError(f"unknown base framework: {base_id}")
        # Deep copy via round-trip
        child = Framework.from_dict(base.to_dict())
        child.id = new_id
        child.version = 1
        child.lineage = list(base.lineage) + [base_id]
        child.runs = 0
        child.wins = 0
        child.losses = 0
        child.author = "agent"
        child = mutator(child)
        self.save(child)
        return child

    def update_outcome(self, framework_id: str, *, score: int) -> None:
        """Record a completed-instance outcome against the spec."""
        fw = self.get(framework_id)
        if fw is None:
            return
        fw.runs += 1
        if score >= 3:
            fw.wins += 1
        else:
            fw.losses += 1
        self.save(fw)


# ── Singleton ────────────────────────────────────────────────────────────

_registry_instance: Optional[FrameworkRegistry] = None


def get_registry() -> FrameworkRegistry:
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = FrameworkRegistry()
    return _registry_instance


__all__ = ["FrameworkRegistry", "get_registry"]
