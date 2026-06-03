"""SkillLoader — injects a SkillPackage into every identity layer.

This is the actual "download" mechanism. Given a SkillPackage, the
loader atomically writes to:

  1. ava_brain.json         — personality traits, dimensions, guidelines
  2. consciousness_state.json — drives, interests, goals, traits
  3. learned_behaviors.json — behavior templates as synthetic experience
  4. semantic_memory.json   — factual knowledge entries
  5. node2040_brain.json    — immediate working context
  6. skill_manifest.json    — record of installed skills
  7. brain/skills/user/*.md — prompt plugin for the existing OpenClaw-style
                              SkillLoader (so the skill appears in agent
                              system prompts automatically)
"""

from __future__ import annotations

import json
import logging
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

from .skill_package import (
    SkillPackage,
    InstallResult,
    BehaviorTemplate,
    KnowledgeEntry,
)

logger = logging.getLogger(__name__)

# Maximum number of behavior entries to keep
MAX_BEHAVIOR_ENTRIES = 500


class SkillLoader:
    """Downloads a SkillPackage into the agent's identity."""

    def __init__(
        self,
        brain_path: str = None,
        workspace_path: str = None,
    ):
        from repryntt.paths import brain_dir as _brain_dir, operator_dir as _operator_dir
        self.brain_path = Path(brain_path) if brain_path else _brain_dir()
        self.workspace_path = Path(workspace_path) if workspace_path else _operator_dir()

        # Derived paths
        self.ava_brain_path = self.brain_path / "ava_brain.json"
        self.consciousness_path = self.workspace_path / "consciousness_state.json"
        self.behaviors_path = self.workspace_path / "learned_behaviors.json"
        self.semantic_path = self.brain_path / "semantic_memory.json"
        self.node_brain_path = self.brain_path / "node2040_brain.json"
        self.manifest_path = self.brain_path / "skill_manifest.json"

    # ── Public API ────────────────────────────────────────────────

    def install(self, package: SkillPackage) -> InstallResult:
        """Install a skill package into all identity layers."""
        result = InstallResult(
            skill_id=package.skill_id,
            skill_name=package.name,
            timestamp=time.time(),
        )

        # Check prerequisites
        if package.prerequisites:
            manifest = self._load_manifest()
            installed = {s["skill_id"] for s in manifest.get("installed", [])}
            missing = [p for p in package.prerequisites if p not in installed]
            if missing:
                result.errors.append(f"Missing prerequisites: {missing}")
                return result

        # Check if already installed
        manifest = self._load_manifest()
        for s in manifest.get("installed", []):
            if s["skill_id"] == package.skill_id:
                result.warnings.append(
                    f"Skill {package.name} v{package.version} already installed — reinstalling"
                )
                break

        try:
            # Phase 1: Personality injection
            pc = self._inject_personality(package)
            result.personality_changes = pc

            # Phase 2: Consciousness adjustment
            cc = self._inject_consciousness(package)
            result.consciousness_changes = cc

            # Phase 3: Behavior templates
            ba = self._inject_behaviors(package)
            result.behaviors_added = ba

            # Phase 4: Knowledge entries
            ke = self._inject_knowledge(package)
            result.knowledge_entries = ke

            # Phase 5: Working memory context
            self._inject_working_context(package)

            # Phase 6: Record in manifest
            self._record_installation(package, result)

            # Phase 7: Generate prompt-plugin .md for the OpenClaw-style SkillLoader
            self._generate_prompt_plugin(package)

            result.verification_total = len(package.verification)
            result.success = True

            logger.info(
                "Skill installed: %s v%s — %d personality, %d knowledge, "
                "%d behaviors, %d consciousness",
                package.name,
                package.version,
                pc, ke, ba, cc,
            )

        except Exception as exc:
            result.errors.append(str(exc))
            logger.exception("Skill installation failed: %s", package.name)

        return result

    def uninstall(self, skill_id: str) -> bool:
        """Remove a skill from the manifest (soft uninstall)."""
        manifest = self._load_manifest()
        before = len(manifest.get("installed", []))
        manifest["installed"] = [
            s for s in manifest.get("installed", [])
            if s["skill_id"] != skill_id
        ]
        if len(manifest["installed"]) < before:
            self._save_json(self.manifest_path, manifest)
            logger.info("Skill %s uninstalled from manifest", skill_id)
            return True
        return False

    def list_installed(self) -> List[Dict[str, Any]]:
        """Return list of installed skills."""
        manifest = self._load_manifest()
        return manifest.get("installed", [])

    # ── Phase 1: Personality ──────────────────────────────────────

    def _inject_personality(self, pkg: SkillPackage) -> int:
        changes = 0
        pers = pkg.personality

        if not (pers.traits_add or pers.traits_modify or pers.dimensions
                or pers.behavioral_guidelines):
            return 0

        brain = self._load_json(self.ava_brain_path, default={"personality": {}})
        personality = brain.setdefault("personality", {})
        reason = f"skill_download:{pkg.name}:v{pkg.version}"

        # Add new traits
        traits = personality.setdefault("traits", [])
        for trait in pers.traits_add:
            if trait not in traits:
                traits.append(trait)
                self._log_evolution(brain, "trait_added", {"trait": trait, "reason": reason})
                changes += 1

        # Modify existing traits
        for old_name, new_value in pers.traits_modify.items():
            for i, t in enumerate(traits):
                if t.lower() == old_name.lower():
                    traits[i] = new_value
                    self._log_evolution(
                        brain, "trait_modified",
                        {"from": old_name, "to": new_value, "reason": reason},
                    )
                    changes += 1
                    break

        # Set dimensions
        dims = personality.setdefault("dimensions", {})
        for dim_name, value in pers.dimensions.items():
            clamped = max(0.0, min(1.0, value))
            old_val = dims.get(dim_name)
            dims[dim_name] = clamped
            self._log_evolution(
                brain, "dimension_evolved",
                {"dimension": dim_name, "from": old_val, "to": clamped, "reason": reason},
            )
            changes += 1

        # Append behavioral guidelines
        guidelines = personality.setdefault("behavioral_guidelines", [])
        for gl in pers.behavioral_guidelines:
            if gl not in guidelines:
                guidelines.append(gl)
                self._log_evolution(
                    brain, "guideline_added",
                    {"guideline": gl[:80], "reason": reason},
                )
                changes += 1

        self._save_json(self.ava_brain_path, brain)
        return changes

    # ── Phase 2: Consciousness ────────────────────────────────────

    def _inject_consciousness(self, pkg: SkillPackage) -> int:
        changes = 0
        cons = pkg.consciousness

        if not (cons.drives or cons.interests or cons.goals or cons.traits):
            return 0

        state = self._load_json(self.consciousness_path, default={})

        # Adjust drives (additive delta, clamped)
        drives = state.setdefault("drives", {})
        for drive_name, delta in cons.drives.items():
            current = drives.get(drive_name, 0.5)
            drives[drive_name] = max(0.0, min(1.0, current + delta))
            changes += 1

        # Set interest levels
        interests = state.setdefault("interests", {})
        for interest_name, level in cons.interests.items():
            interests[interest_name] = max(0.0, min(1.0, level))
            changes += 1

        # Add new goals
        goals = state.setdefault("goals", [])
        existing_ids = {g.get("id") for g in goals}
        for goal in cons.goals:
            if goal.get("id") not in existing_ids:
                goals.append(goal)
                changes += 1

        # Update consciousness-level traits
        traits = state.setdefault("traits", {})
        for trait_name, value in cons.traits.items():
            traits[trait_name] = max(0.0, min(1.0, value))
            changes += 1

        self._save_json(self.consciousness_path, state)
        return changes

    # ── Phase 3: Behaviors ────────────────────────────────────────

    def _inject_behaviors(self, pkg: SkillPackage) -> int:
        if not pkg.behaviors:
            return 0

        data = self._load_json(self.behaviors_path, default={
            "version": 1, "total_experiences": 0, "experiences": []
        })
        experiences = data.setdefault("experiences", [])
        now = time.time()

        for bt in pkg.behaviors:
            entry = {
                "ts": now,
                "score": bt.score,
                "task": bt.task,
                "pillars": bt.pillars,
                "plan": bt.plan,
                "tools": bt.tools,
                "tool_count": len(bt.tools),
                "critique": bt.critique,
                "chain": f"skill_download:{pkg.name}",
                "source": "skill_package",
            }
            experiences.append(entry)

        # Prune old entries if over limit
        if len(experiences) > MAX_BEHAVIOR_ENTRIES:
            experiences.sort(key=lambda e: e.get("ts", 0))
            data["experiences"] = experiences[-MAX_BEHAVIOR_ENTRIES:]

        data["total_experiences"] = len(data["experiences"])
        data["version"] = data.get("version", 0) + 1
        data["updated"] = now

        self._save_json(self.behaviors_path, data)
        return len(pkg.behaviors)

    # ── Phase 4: Knowledge ────────────────────────────────────────

    def _inject_knowledge(self, pkg: SkillPackage) -> int:
        if not pkg.knowledge:
            return 0

        sem = self._load_json(self.semantic_path, default={"memories": {}})
        memories = sem.setdefault("memories", {})
        now = time.time()

        for ke in pkg.knowledge:
            key = ke.topic.lower().replace(" ", "_")
            memories[key] = {
                "topic": ke.topic,
                "content": ke.content,
                "domain": ke.domain,
                "confidence": ke.confidence,
                "key_facts": ke.key_facts,
                "related_topics": ke.related_topics,
                "timestamp": now,
                "source": f"skill_package:{pkg.name}",
            }

        self._save_json(self.semantic_path, sem)
        return len(pkg.knowledge)

    # ── Phase 5: Working context ──────────────────────────────────

    def _inject_working_context(self, pkg: SkillPackage) -> None:
        """Push skill summary into node2040_brain preload for immediate use."""
        brain = self._load_json(self.node_brain_path, default={})
        preload = brain.setdefault("preload", {})
        skill_context = preload.setdefault("skill_downloads", [])

        # Build compact summary
        summary = {
            "skill": pkg.name,
            "version": pkg.version,
            "installed": time.time(),
            "capabilities": pkg.description,
            "tags": pkg.tags,
        }

        # Replace if same skill already in preload
        skill_context[:] = [
            s for s in skill_context if s.get("skill") != pkg.name
        ]
        skill_context.append(summary)

        # Keep only last 20 skill contexts
        if len(skill_context) > 20:
            brain["preload"]["skill_downloads"] = skill_context[-20:]

        self._save_json(self.node_brain_path, brain)

    # ── Phase 6: Manifest ─────────────────────────────────────────

    def _record_installation(self, pkg: SkillPackage, result: InstallResult) -> None:
        manifest = self._load_manifest()
        installed = manifest.setdefault("installed", [])

        # Remove previous version of same skill
        installed[:] = [
            s for s in installed if s["skill_id"] != pkg.skill_id
        ]

        installed.append({
            "skill_id": pkg.skill_id,
            "name": pkg.name,
            "version": pkg.version,
            "author": pkg.author,
            "installed_at": result.timestamp,
            "personality_changes": result.personality_changes,
            "knowledge_entries": result.knowledge_entries,
            "behaviors_added": result.behaviors_added,
            "consciousness_changes": result.consciousness_changes,
        })

        manifest["last_updated"] = time.time()
        self._save_json(self.manifest_path, manifest)

    # ── Phase 7: Prompt plugin (.md for OpenClaw SkillLoader) ─────

    def _generate_prompt_plugin(self, pkg: SkillPackage) -> None:
        """Generate a markdown prompt-plugin in brain/skills/user/.

        This bridges the Neo-style identity injection with the existing
        OpenClaw-style SkillLoader that injects .md files into agent
        system prompts. The generated file uses the HTML comment metadata
        format the SkillLoader expects.
        """
        import re

        skills_dir = self.brain_path / "skills" / "user"
        skills_dir.mkdir(parents=True, exist_ok=True)

        safe_name = re.sub(r'[^a-z0-9_]', '_', pkg.name.lower())
        filepath = skills_dir / f"{safe_name}.md"

        # Build metadata header
        tags_str = ", ".join(pkg.tags) if pkg.tags else "general"
        lines = [
            f"<!-- skill:name = {safe_name} -->",
            f"<!-- skill:departments = {tags_str} -->",
            "<!-- skill:activation = auto -->",
            f"<!-- skill:priority = 10 -->",
            "",
            f"# {pkg.name}",
            "",
            f"{pkg.description}",
            "",
        ]

        # Add knowledge summaries
        if pkg.knowledge:
            lines.append("## Knowledge")
            lines.append("")
            for ke in pkg.knowledge:
                lines.append(f"### {ke.topic}")
                lines.append(f"{ke.content}")
                if ke.key_facts:
                    lines.append("")
                    for fact in ke.key_facts:
                        lines.append(f"- {fact}")
                lines.append("")

        # Add behavioral guidelines
        if pkg.personality.behavioral_guidelines:
            lines.append("## Guidelines")
            lines.append("")
            for gl in pkg.personality.behavioral_guidelines:
                lines.append(f"- {gl}")
            lines.append("")

        # Add behavior templates as workflow patterns
        if pkg.behaviors:
            lines.append("## Workflows")
            lines.append("")
            for bt in pkg.behaviors:
                lines.append(f"### {bt.task}")
                lines.append(f"{bt.plan}")
                if bt.tools:
                    lines.append(f"**Tools**: {', '.join(bt.tools)}")
                if bt.critique:
                    lines.append(f"**Note**: {bt.critique}")
                lines.append("")

        filepath.write_text("\n".join(lines), encoding="utf-8")
        logger.info("Generated prompt plugin: %s", filepath)

    # ── Helpers ───────────────────────────────────────────────────

    def _load_manifest(self) -> Dict[str, Any]:
        return self._load_json(self.manifest_path, default={
            "installed": [], "last_updated": 0,
        })

    def _load_json(self, path: Path, default: Any = None) -> Any:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                logger.warning("Corrupt JSON at %s — using default", path)
        return deepcopy(default) if default is not None else {}

    def _save_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def _log_evolution(self, brain: Dict, event_type: str, details: Dict) -> None:
        personality = brain.setdefault("personality", {})
        log = personality.setdefault("personality_evolution_log", [])
        log.append({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event_type": event_type,
            **details,
        })
        # Keep last 200 evolution events
        if len(log) > 200:
            personality["personality_evolution_log"] = log[-200:]
