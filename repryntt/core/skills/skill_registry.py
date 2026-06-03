"""SkillRegistry — catalogs available and installed skill packages.

Scans a packages directory for .json skill files, validates them,
and provides search/list/download capabilities. Also tracks what's
currently installed via the skill manifest.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .skill_package import SkillPackage

logger = logging.getLogger(__name__)


class SkillRegistry:
    """Manages the catalog of available skill packages."""

    def __init__(
        self,
        packages_dir: str = None,
        brain_path: str = None,
    ):
        from repryntt.paths import brain_dir as _brain_dir
        _brain = Path(brain_path) if brain_path else _brain_dir()
        self.packages_dir = Path(packages_dir) if packages_dir else _brain / "skill_packages"
        self.brain_path = _brain
        self.manifest_path = self.brain_path / "skill_manifest.json"
        self._cache: Dict[str, SkillPackage] = {}
        # Create the packages dir on first init so framework calls (which
        # scan() repeatedly per heartbeat) don't log "does not exist yet"
        # on every single call. Empty dir is the valid first-run state.
        try:
            self.packages_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.debug(f"could not create skill packages dir: {e}")
        self._missing_warned = False

    def scan(self) -> List[SkillPackage]:
        """Scan packages directory and return all valid skill packages."""
        self._cache.clear()
        packages = []

        if not self.packages_dir.exists():
            # Should be rare since we mkdir in __init__, but log at most
            # once per registry instance so we don't spam the log.
            if not self._missing_warned:
                logger.info("Packages directory %s does not exist yet", self.packages_dir)
                self._missing_warned = True
            return packages

        for fp in sorted(self.packages_dir.glob("*.json")):
            try:
                pkg = SkillPackage.from_file(fp)
                self._cache[pkg.skill_id] = pkg
                packages.append(pkg)
            except Exception as exc:
                logger.warning("Invalid skill package %s: %s", fp.name, exc)

        logger.info("Scanned %d skill packages from %s", len(packages), self.packages_dir)
        return packages

    def get(self, skill_id: str) -> Optional[SkillPackage]:
        """Get a specific skill by ID."""
        if not self._cache:
            self.scan()
        return self._cache.get(skill_id)

    def find_by_name(self, name: str) -> Optional[SkillPackage]:
        """Find a skill package by exact name (case-insensitive)."""
        if not self._cache:
            self.scan()
        name_lower = name.lower()
        for pkg in self._cache.values():
            if pkg.name.lower() == name_lower:
                return pkg
        return None

    def search(self, query: str) -> List[SkillPackage]:
        """Search skills by name, description, or tags."""
        if not self._cache:
            self.scan()
        query_lower = query.lower()
        results = []
        for pkg in self._cache.values():
            if (
                query_lower in pkg.name.lower()
                or query_lower in pkg.description.lower()
                or any(query_lower in tag.lower() for tag in pkg.tags)
            ):
                results.append(pkg)
        return results

    def list_available(self) -> List[Dict[str, Any]]:
        """List all available skills with install status."""
        if not self._cache:
            self.scan()
        installed_ids = self._get_installed_ids()

        return [
            {
                "skill_id": pkg.skill_id,
                "name": pkg.name,
                "version": pkg.version,
                "description": pkg.description,
                "tags": pkg.tags,
                "installed": pkg.skill_id in installed_ids,
                "author": pkg.author,
                "knowledge_count": len(pkg.knowledge),
                "behavior_count": len(pkg.behaviors),
            }
            for pkg in self._cache.values()
        ]

    def list_installed(self) -> List[Dict[str, Any]]:
        """List currently installed skills."""
        if self.manifest_path.exists():
            try:
                data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                return data.get("installed", [])
            except (json.JSONDecodeError, OSError):
                pass
        return []

    def _get_installed_ids(self) -> set:
        installed = self.list_installed()
        return {s["skill_id"] for s in installed}
