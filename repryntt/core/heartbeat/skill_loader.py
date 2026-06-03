#!/usr/bin/env python3
"""
Skill Loader for SAIGE Tier 1 (Local LLM)

Lightweight skill loading system for the evolution loop.
Reads SKILL.md files from brain/skills/ and injects relevant
skill context into prompts based on task keywords.

This is independent of the persistent_agents daemon — works standalone
for the local LLM's autonomous loop.

Skill format (brain/skills/bundled/example.md):
    <!-- skill:name = example -->
    <!-- skill:departments = trading, research -->
    <!-- skill:activation = auto -->
    <!-- skill:tools = web_search, call_jarvis -->
    <!-- skill:priority = 10 -->
    <!-- skill:keywords = trade, buy, sell, market -->
    
    ## Purpose
    You are skilled at...
    
    ## Guidelines
    1. ...
"""

import re
import os
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
# Canonical skill location: ~/.repryntt/brain/skills/ (NOT the source tree)
from repryntt.paths import brain_dir as _brain_dir
SKILLS_DIR = _brain_dir() / "skills"
BUNDLED_DIR = SKILLS_DIR / "bundled"
USER_DIR = SKILLS_DIR / "user"


def _parse_skill_metadata(content: str) -> Dict[str, Any]:
    """Extract skill metadata from HTML comments."""
    meta = {}
    for match in re.finditer(r'<!--\s*skill:(\w+)\s*=\s*(.+?)\s*-->', content):
        key = match.group(1).strip()
        value = match.group(2).strip()
        if key in ("departments", "tools", "keywords"):
            meta[key] = [item.strip().lower() for item in value.split(",")]
        elif key == "priority":
            try:
                meta[key] = int(value)
            except ValueError:
                meta[key] = 0
        else:
            meta[key] = value
    return meta


class EvolutionSkillLoader:
    """
    Lightweight skill loader for the evolution loop.
    
    Scans brain/skills/ for .md files, caches them, and returns
    relevant skill context based on task keywords or explicit department.
    """

    def __init__(self, skills_dir: Optional[Path] = None):
        self._skills_dir = skills_dir or SKILLS_DIR
        self._bundled_dir = self._skills_dir / "bundled"
        self._user_dir = self._skills_dir / "user"
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._last_scan: float = 0
        self._scan_interval: float = 120  # Re-scan every 2 minutes

    def scan(self, force: bool = False) -> Dict[str, Dict[str, Any]]:
        """Scan skill directories and cache results."""
        now = time.time()
        if not force and (now - self._last_scan) < self._scan_interval and self._cache:
            return self._cache

        skills = {}
        for source_dir, source_label in [
            (self._bundled_dir, "bundled"),
            (self._user_dir, "user"),
        ]:
            if not source_dir.exists():
                continue
            for md_file in sorted(source_dir.glob("*.md")):
                try:
                    content = md_file.read_text(encoding="utf-8")
                    meta = _parse_skill_metadata(content)
                    skill_name = meta.get("name", md_file.stem)

                    # Extract keywords from content if not in metadata
                    keywords = meta.get("keywords", [])
                    if not keywords:
                        # Auto-extract from headings and bold text
                        keywords = self._extract_keywords(content)

                    skills[skill_name] = {
                        "name": skill_name,
                        "path": str(md_file),
                        "source": source_label,
                        "departments": meta.get("departments", []),
                        "activation": meta.get("activation", "manual"),
                        "tools": meta.get("tools", []),
                        "priority": meta.get("priority", 0),
                        "keywords": keywords,
                        "content": content,
                    }
                except Exception as e:
                    logger.warning(f"[SkillLoader] Failed to load {md_file}: {e}")

        self._cache = skills
        self._last_scan = now
        logger.info(
            f"[EvolutionSkillLoader] Loaded {len(skills)} skills "
            f"({sum(1 for s in skills.values() if s['source'] == 'bundled')} bundled, "
            f"{sum(1 for s in skills.values() if s['source'] == 'user')} user)"
        )
        return skills

    def _extract_keywords(self, content: str) -> List[str]:
        """Auto-extract keywords from headings and bold text."""
        keywords = []
        # From ## headings
        for match in re.finditer(r'^##\s+(.+)$', content, re.MULTILINE):
            words = match.group(1).lower().split()
            keywords.extend(w for w in words if len(w) > 3)
        # From **bold** text
        for match in re.finditer(r'\*\*(.+?)\*\*', content):
            words = match.group(1).lower().split()
            keywords.extend(w for w in words if len(w) > 3)
        return list(set(keywords))[:20]

    def get_skills_for_task(self, task_text: str, max_skills: int = 3) -> List[Dict[str, Any]]:
        """
        Find relevant skills for a given task based on keyword matching.
        Returns up to max_skills sorted by relevance.
        """
        skills = self.scan()
        task_lower = task_text.lower()
        task_words = set(task_lower.split())

        scored = []
        for skill_name, skill in skills.items():
            # Only auto-activated skills
            if skill.get("activation") == "manual":
                continue

            relevance = 0

            # Keyword match
            for kw in skill.get("keywords", []):
                if kw in task_lower:
                    relevance += 2

            # Department match
            for dept in skill.get("departments", []):
                if dept in task_lower:
                    relevance += 3

            # Word overlap with skill name
            name_words = set(skill_name.lower().replace("_", " ").split())
            overlap = task_words & name_words
            relevance += len(overlap) * 2

            # Priority boost
            relevance += skill.get("priority", 0) * 0.1

            if relevance > 0:
                scored.append((relevance, skill))

        # Sort by relevance descending
        scored.sort(key=lambda x: x[0], reverse=True)
        return [s[1] for s in scored[:max_skills]]

    def get_skills_for_department(self, department: str) -> List[Dict[str, Any]]:
        """Get all auto-activated skills matching a department."""
        skills = self.scan()
        department_lower = department.lower()
        return [
            s for s in skills.values()
            if s.get("activation") == "auto"
            and department_lower in [d.lower() for d in s.get("departments", [])]
        ]

    def build_skill_context(
        self,
        task_text: str,
        max_tokens: int = 500,
        max_skills: int = 2,
    ) -> str:
        """
        Build a compact skill context string for injection into prompts.
        Keeps within token budget (critical for 4K context).
        """
        relevant = self.get_skills_for_task(task_text, max_skills=max_skills)
        if not relevant:
            return ""

        parts = []
        estimated_tokens = 0

        for skill in relevant:
            content = skill["content"]
            # Strip metadata comments
            content = re.sub(r'<!--.*?-->', '', content).strip()
            # Truncate to fit budget
            skill_tokens = len(content) // 4  # rough estimate
            remaining = max_tokens - estimated_tokens
            if skill_tokens > remaining:
                # Truncate at sentence boundary
                char_limit = remaining * 4
                content = content[:char_limit]
                last_period = content.rfind(".")
                if last_period > 100:
                    content = content[: last_period + 1]

            parts.append(content)
            estimated_tokens += len(content) // 4

            if estimated_tokens >= max_tokens:
                break

        return "\n\n---\n\n".join(parts)

    def list_all(self) -> List[Dict[str, str]]:
        """List all available skills (name + source)."""
        skills = self.scan()
        return [
            {"name": s["name"], "source": s["source"], "departments": s.get("departments", [])}
            for s in skills.values()
        ]
