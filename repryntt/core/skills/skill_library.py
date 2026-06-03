"""
Skill Library — Composable behavior registry for the autonomous agent.

Instead of re-planning common multi-step procedures from scratch every heartbeat,
successful procedures get saved as reusable skills. This is the OPTIONS FRAMEWORK
from hierarchical RL (Sutton et al. 1999).

Skill format:
    {
        "name": "deploy_python_tool",
        "description": "Write, test, and save a Python script",
        "steps": ["write code", "test with real input", "save to code_sandbox", "verify output"],
        "preconditions": ["task involves creating a script"],
        "avg_score": 4.2,
        "times_used": 7,
        "times_succeeded": 6,
        "tags": ["coding", "automation"],
        "source_heartbeats": [15, 23, 31],
        "created": 1712000000,
        "last_used": 1712100000
    }

Storage: ~/.repryntt/brain/skills/learned/
  - Each skill = one JSON file with human-readable name
  - Bundled skills = ~/.repryntt/brain/skills/bundled/*.md (system-provided)
  - User skills = ~/.repryntt/brain/skills/user/*.md (operator-installed)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Canonical location — ALL skill storage is under brain_dir()/skills/
from repryntt.paths import brain_dir as _brain_dir
SKILLS_ROOT = _brain_dir() / "skills"
LEARNED_DIR = SKILLS_ROOT / "learned"
BUNDLED_DIR = SKILLS_ROOT / "bundled"
USER_DIR = SKILLS_ROOT / "user"

MAX_SKILLS = 200
MIN_SCORE_TO_LEARN = 4  # Only learn from score 4+ heartbeats
MIN_USES_TO_TRUST = 3   # Need 3+ successful uses before injecting as "trusted"
MATCH_THRESHOLD = 0.3   # Keyword overlap threshold for skill suggestion


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r'[^a-z0-9\s_-]', '', text)
    text = re.sub(r'[\s_-]+', '_', text)
    return text[:60]


class SkillLibrary:
    """Manages learned, bundled, and user skills for the autonomous agent."""

    def __init__(self):
        LEARNED_DIR.mkdir(parents=True, exist_ok=True)
        BUNDLED_DIR.mkdir(parents=True, exist_ok=True)
        USER_DIR.mkdir(parents=True, exist_ok=True)

        self._learned: Dict[str, Dict] = {}
        self._bundled: Dict[str, Dict] = {}
        self._load()
        self._cleanup_garbage_skills()

    def _load(self):
        """Load all learned skills from disk."""
        self._learned = {}
        if LEARNED_DIR.exists():
            for f in LEARNED_DIR.glob("*.json"):
                try:
                    with open(f, 'r') as fh:
                        skill = json.load(fh)
                    name = skill.get("name", f.stem)
                    self._learned[name] = skill
                except Exception as e:
                    logger.debug(f"Failed to load skill {f}: {e}")

        # Load bundled skill metadata (not full content — just for matching)
        self._bundled = {}
        for d in (BUNDLED_DIR, USER_DIR):
            if d.exists():
                for f in d.glob("*.md"):
                    try:
                        content = f.read_text(encoding="utf-8")
                        meta = self._parse_md_metadata(content)
                        name = meta.get("name", f.stem)
                        self._bundled[name] = {
                            "name": name,
                            "path": str(f),
                            "keywords": meta.get("keywords", []),
                            "departments": meta.get("departments", []),
                            "content": content,
                        }
                    except Exception:
                        pass

        logger.info(f"📚 SkillLibrary loaded: {len(self._learned)} learned, "
                     f"{len(self._bundled)} bundled/user")

    def _cleanup_garbage_skills(self):
        """Remove skills with garbage descriptions or too few steps.

        Runs once at startup. Skills that captured section headers
        (like '**INNER MONOLOGUE**') instead of real task descriptions
        get their descriptions fixed or get pruned.
        """
        junk_patterns = {"inner monologue", "planning phase", "self-evaluation",
                         "task selection", "no tools", "just thinking"}
        cleaned = 0
        removed = 0
        for name, skill in list(self._learned.items()):
            desc = skill.get("description", "")
            desc_lower = desc.lower().strip("*#_- ")
            is_junk = any(j in desc_lower for j in junk_patterns)

            if is_junk:
                # Try to fix from steps
                steps = skill.get("steps", [])
                if steps and len(steps) >= 2:
                    skill["description"] = f"Procedure: {steps[0]}"
                    self._save_skill(name, skill)
                    cleaned += 1
                elif skill.get("times_used", 0) <= 1:
                    # Single-use garbage — remove
                    path = LEARNED_DIR / f"{_slugify(name)}.json"
                    try:
                        path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    del self._learned[name]
                    removed += 1

            # Also prune skills with fewer than 2 steps and only 1 use
            elif len(skill.get("steps", [])) < 2 and skill.get("times_used", 0) <= 1:
                path = LEARNED_DIR / f"{_slugify(name)}.json"
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
                del self._learned[name]
                removed += 1

        if cleaned or removed:
            logger.info(f"📚 Skill cleanup: {cleaned} descriptions fixed, "
                         f"{removed} garbage skills removed")

    @staticmethod
    def _parse_md_metadata(content: str) -> Dict:
        """Extract skill metadata from HTML comments in .md files."""
        meta = {}
        for match in re.finditer(r'<!--\s*skill:(\w+)\s*=\s*(.+?)\s*-->', content):
            key = match.group(1).strip()
            value = match.group(2).strip()
            if key in ("departments", "tools", "keywords"):
                meta[key] = [item.strip().lower() for item in value.split(",")]
            else:
                meta[key] = value
        return meta

    # ── Learning from successful work ───────────────────────

    def learn_from_heartbeat(self, score: int, topic: str, plan: str,
                             report: str, tool_names: List[str],
                             heartbeat_num: int) -> Optional[str]:
        """Extract a reusable skill from a high-scoring heartbeat.

        Only learns from score 4+ heartbeats where a clear procedure exists.
        Returns the skill name if learned, None otherwise.
        """
        if score < MIN_SCORE_TO_LEARN:
            return None
        if not plan or not report:
            return None

        # Extract steps from the plan (look for numbered lists)
        steps = self._extract_steps(plan)
        if len(steps) < 2:
            return None  # Too simple to be a reusable skill

        # Generate skill name from topic + plan
        name = self._generate_skill_name(topic, plan)
        if not name:
            return None

        # Check if similar skill already exists — update instead of duplicate
        existing = self._find_similar_skill(name, steps)
        if existing:
            self._reinforce_skill(existing, score, heartbeat_num)
            return existing

        # Create new skill
        tags = self._extract_tags(topic, plan, tool_names)
        skill = {
            "name": name,
            "description": self._extract_description(plan),
            "steps": steps,
            "preconditions": self._extract_preconditions(plan),
            "tool_names": tool_names[:10],
            "tags": tags,
            "avg_score": float(score),
            "times_used": 1,
            "times_succeeded": 1 if score >= 3 else 0,
            "source_heartbeats": [heartbeat_num],
            "created": time.time(),
            "last_used": time.time(),
        }

        self._learned[name] = skill
        self._save_skill(name, skill)
        logger.info(f"📚 New skill learned: '{name}' ({len(steps)} steps, "
                     f"{len(tags)} tags)")
        return name

    def record_skill_usage(self, skill_name: str, score: int,
                           heartbeat_num: int):
        """Record that a skill was used and its outcome."""
        if skill_name not in self._learned:
            return

        skill = self._learned[skill_name]
        skill["times_used"] = skill.get("times_used", 0) + 1
        if score >= 3:
            skill["times_succeeded"] = skill.get("times_succeeded", 0) + 1
        skill["last_used"] = time.time()

        # Update running average score
        old_avg = skill.get("avg_score", 3.0)
        n = skill["times_used"]
        skill["avg_score"] = round(old_avg + (score - old_avg) / n, 2)

        # Track source heartbeats (limited)
        hbs = skill.get("source_heartbeats", [])
        hbs.append(heartbeat_num)
        skill["source_heartbeats"] = hbs[-20:]

        self._save_skill(skill_name, skill)

    # ── Skill suggestion for planning ───────────────────────

    def suggest_skills(self, task_description: str,
                       max_results: int = 3) -> List[Dict]:
        """Find relevant skills for a given task description.

        Returns skills sorted by relevance * reliability.
        """
        task_words = set(task_description.lower().split())
        candidates = []

        for name, skill in self._learned.items():
            # Compute keyword overlap
            skill_words = set()
            for tag in skill.get("tags", []):
                skill_words.update(tag.lower().split())
            for step in skill.get("steps", []):
                skill_words.update(step.lower().split()[:5])
            skill_words.add(name.lower().replace("_", " "))

            overlap = len(task_words & skill_words)
            if overlap == 0:
                continue

            relevance = overlap / max(len(task_words), 1)
            if relevance < MATCH_THRESHOLD:
                continue

            # Reliability: success rate * times used
            reliability = 0.5
            if skill.get("times_used", 0) >= MIN_USES_TO_TRUST:
                success_rate = skill.get("times_succeeded", 0) / max(skill["times_used"], 1)
                reliability = success_rate

            # Recency bonus
            age_days = (time.time() - skill.get("last_used", 0)) / 86400
            recency = max(0.5, 1.0 - age_days / 30)

            combined = relevance * reliability * recency * skill.get("avg_score", 3) / 5
            candidates.append({
                "name": name,
                "score": round(combined, 3),
                "skill": skill,
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:max_results]

    def get_skill_context(self, task_description: str) -> str:
        """Get prompt injection text with relevant skills for a task."""
        suggestions = self.suggest_skills(task_description)
        if not suggestions:
            return ""

        lines = [
            "\n## 📚 Relevant Skills from Library (proven procedures — consider using these)",
        ]
        for s in suggestions:
            sk = s["skill"]
            reliability = ""
            if sk.get("times_used", 0) >= MIN_USES_TO_TRUST:
                rate = sk.get("times_succeeded", 0) / max(sk["times_used"], 1)
                reliability = f" | reliability: {rate:.0%}"

            lines.append(f"\n**{sk['name']}** (avg score: {sk.get('avg_score', '?')}/5, "
                         f"used {sk.get('times_used', 0)}x{reliability})")
            lines.append(f"  {sk.get('description', 'No description')}")
            if sk.get("steps"):
                for i, step in enumerate(sk["steps"][:5], 1):
                    lines.append(f"  {i}. {step}")
                if len(sk.get("steps", [])) > 5:
                    lines.append(f"  ... ({len(sk['steps'])} steps total)")

        lines.append("\n→ You can follow these procedures or adapt them. "
                     "Using proven skills saves time.\n")
        return "\n".join(lines)

    # ── Stats / inventory ───────────────────────────────────

    def get_stats(self) -> Dict:
        """Return skill library statistics."""
        learned = list(self._learned.values())
        return {
            "total_learned": len(learned),
            "total_bundled": len(self._bundled),
            "trusted_skills": sum(1 for s in learned
                                  if s.get("times_used", 0) >= MIN_USES_TO_TRUST),
            "avg_score": round(
                sum(s.get("avg_score", 0) for s in learned) / len(learned), 2
            ) if learned else 0,
            "most_used": sorted(
                [(s["name"], s.get("times_used", 0)) for s in learned],
                key=lambda x: -x[1]
            )[:5],
            "recently_learned": sorted(
                [(s["name"], s.get("created", 0)) for s in learned],
                key=lambda x: -x[1]
            )[:5],
        }

    def list_skills(self) -> List[Dict]:
        """Return all skills with summary info."""
        result = []
        for name, skill in sorted(self._learned.items()):
            result.append({
                "name": name,
                "description": skill.get("description", ""),
                "avg_score": skill.get("avg_score", 0),
                "times_used": skill.get("times_used", 0),
                "tags": skill.get("tags", []),
                "trusted": skill.get("times_used", 0) >= MIN_USES_TO_TRUST,
            })
        return result

    # ── Internal helpers ────────────────────────────────────

    def _extract_steps(self, plan: str) -> List[str]:
        """Extract numbered/bulleted steps from a plan."""
        steps = []
        for line in plan.split("\n"):
            line = line.strip()
            # Match numbered lists: "1. Do thing" or "Step 1: Do thing"
            m = re.match(r'^(?:\d+[\.\)]\s*|Step\s+\d+[:\s]+|[-*]\s+)(.*)', line)
            if m:
                step = m.group(1).strip()
                if len(step) > 5 and len(step) < 200:
                    steps.append(step)
        return steps[:15]

    def _generate_skill_name(self, topic: str, plan: str) -> str:
        """Generate a human-readable skill name."""
        # Try to extract from TASK: line in plan
        for line in plan.split("\n"):
            if line.strip().startswith("TASK:"):
                task_desc = line.strip()[5:].strip()
                if task_desc:
                    return _slugify(task_desc)

        # Fall back to topic
        if topic and topic != "unknown":
            return _slugify(topic)

        return ""

    def _find_similar_skill(self, name: str, steps: List[str]) -> Optional[str]:
        """Find an existing skill that's similar enough to update."""
        # Exact name match
        if name in self._learned:
            return name

        # Check step overlap
        new_steps_set = set(s.lower()[:30] for s in steps)
        for existing_name, existing in self._learned.items():
            existing_steps = set(s.lower()[:30] for s in existing.get("steps", []))
            if not existing_steps:
                continue
            overlap = len(new_steps_set & existing_steps) / max(len(existing_steps), 1)
            if overlap >= 0.6:
                return existing_name

        return None

    def _reinforce_skill(self, skill_name: str, score: int, heartbeat_num: int):
        """Reinforce an existing skill with a new successful use."""
        self.record_skill_usage(skill_name, score, heartbeat_num)
        logger.info(f"📚 Skill reinforced: '{skill_name}' (score {score})")

    def _extract_description(self, plan: str) -> str:
        """Extract a one-line description from the plan."""
        # Junk patterns that indicate we captured a section header, not a task
        _junk = {"inner monologue", "planning phase", "self-evaluation",
                 "task selection", "no tools", "just thinking"}
        for line in plan.split("\n"):
            line = line.strip()
            if line.startswith("TASK:"):
                desc = line[5:].strip()[:200]
                if desc and not any(j in desc.lower() for j in _junk):
                    return desc
        # First non-empty, non-junk line
        for line in plan.split("\n"):
            line = line.strip()
            # Skip markdown formatting, headers, and section labels
            clean = line.strip("*#_- ")
            if len(clean) > 15 and not any(j in clean.lower() for j in _junk):
                return clean[:200]
        return "Learned procedure"

    def _extract_preconditions(self, plan: str) -> List[str]:
        """Extract preconditions from the plan context."""
        preconditions = []
        for line in plan.split("\n"):
            line = line.strip().lower()
            if "if " in line and ("need" in line or "require" in line or "must" in line):
                preconditions.append(line[:100])
            if len(preconditions) >= 3:
                break
        return preconditions

    def _extract_tags(self, topic: str, plan: str,
                      tool_names: List[str]) -> List[str]:
        """Extract tags from topic, plan, and tools used."""
        tags = set()

        # Topic as tag
        if topic and topic != "unknown":
            tags.add(topic.lower())

        # Tool categories as tags
        tool_tags = {
            "web_search": "research",
            "write_file": "coding",
            "read_file": "coding",
            "run_code": "coding",
            "gmail": "email",
            "speak": "voice",
            "capture_camera": "vision",
            "forge": "project",
            "append_daily_memory": "journaling",
        }
        for tool in tool_names:
            for prefix, tag in tool_tags.items():
                if prefix in tool.lower():
                    tags.add(tag)

        # Keywords from plan
        coding_words = {"script", "code", "function", "class", "test", "debug", "fix"}
        plan_words = set(plan.lower().split())
        for word in coding_words & plan_words:
            tags.add(word)

        return sorted(tags)[:10]

    def _save_skill(self, name: str, skill: Dict):
        """Save a single skill to disk."""
        path = LEARNED_DIR / f"{_slugify(name)}.json"
        try:
            tmp = str(path) + ".tmp"
            with open(tmp, 'w') as f:
                json.dump(skill, f, indent=2)
            os.replace(tmp, str(path))
        except Exception as e:
            logger.warning(f"Failed to save skill '{name}': {e}")

        # Prune if over limit
        if len(self._learned) > MAX_SKILLS:
            self._prune_old_skills()

    def _prune_old_skills(self):
        """Remove lowest-value skills when over limit."""
        # Score: avg_score * times_used * recency
        scored = []
        for name, skill in self._learned.items():
            age_days = (time.time() - skill.get("last_used", 0)) / 86400
            value = (skill.get("avg_score", 0) *
                     min(skill.get("times_used", 1), 10) *
                     max(0.1, 1.0 - age_days / 60))
            scored.append((name, value))

        scored.sort(key=lambda x: x[1])

        # Remove bottom 20%
        to_remove = scored[:len(scored) // 5]
        for name, _ in to_remove:
            path = LEARNED_DIR / f"{_slugify(name)}.json"
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            self._learned.pop(name, None)

        logger.info(f"📚 Pruned {len(to_remove)} low-value skills, "
                     f"{len(self._learned)} remaining")
