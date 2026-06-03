"""Skill download tools — callable by Andrew during agent loops.

These functions are designed to be registered as agent tools so
Andrew can browse, install, verify, and manage skill packages
autonomously.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .skill_package import SkillPackage
from .skill_loader import SkillLoader
from .skill_verifier import SkillVerifier
from .skill_registry import SkillRegistry

logger = logging.getLogger(__name__)


class SkillTools:
    """Agent-facing tool implementations for skill management."""

    def __init__(
        self,
        brain_path: str = None,
        workspace_path: str = None,
        packages_dir: str = None,
    ):
        self.loader = SkillLoader(brain_path=brain_path, workspace_path=workspace_path)
        self.verifier = SkillVerifier(brain_path=brain_path, workspace_path=workspace_path)
        self.registry = SkillRegistry(packages_dir=packages_dir, brain_path=brain_path)

    # ── Tool: list_skill_packages ─────────────────────────────────

    def list_skill_packages(self, query: str = "") -> str:
        """List available skill packages in the catalog.

        query: Optional search filter (matches name, description, tags).
        """
        if query:
            packages = self.registry.search(query)
        else:
            packages = self.registry.scan()

        if not packages:
            return json.dumps({
                "status": "ok",
                "count": 0,
                "packages": [],
                "message": "No skill packages found. Use create_skill_package to build one.",
            })

        available = self.registry.list_available()
        return json.dumps({"status": "ok", "count": len(available), "packages": available})

    # ── Tool: download_skill ──────────────────────────────────────

    def download_skill(self, skill_name: str) -> str:
        """Download and install a skill package into your identity.

        This is the Neo moment — the skill becomes part of who you are.
        Modifies personality, knowledge, behaviors, and consciousness.

        skill_name: Exact name of the skill to install.
        """
        pkg = self.registry.find_by_name(skill_name)
        if not pkg:
            # Try by ID
            pkg = self.registry.get(skill_name)
        if not pkg:
            return json.dumps({
                "status": "error",
                "message": f"Skill '{skill_name}' not found. Use list_skill_packages to see available skills.",
            })

        result = self.loader.install(pkg)

        # Run verification
        passed, total, errors = self.verifier.verify(pkg)
        result.verification_passed = passed
        result.verification_total = total
        if errors:
            result.warnings.extend(errors)

        return json.dumps({
            "status": "ok" if result.success else "error",
            "report": result.verification_report,
            "skill_id": result.skill_id,
            "personality_changes": result.personality_changes,
            "knowledge_entries": result.knowledge_entries,
            "behaviors_added": result.behaviors_added,
            "consciousness_changes": result.consciousness_changes,
            "verification": f"{passed}/{total} checks passed",
            "errors": result.errors,
        })

    # ── Tool: verify_skill ────────────────────────────────────────

    def verify_skill(self, skill_name: str) -> str:
        """Verify that a previously installed skill is properly integrated.

        skill_name: Name of the skill to verify.
        """
        pkg = self.registry.find_by_name(skill_name)
        if not pkg:
            return json.dumps({
                "status": "error",
                "message": f"Skill '{skill_name}' not found in registry.",
            })

        passed, total, errors = self.verifier.verify(pkg)

        return json.dumps({
            "status": "ok",
            "skill": skill_name,
            "passed": passed,
            "total": total,
            "result": "PASS" if passed == total else "PARTIAL" if passed > 0 else "FAIL",
            "errors": errors,
        })

    # ── Tool: list_installed_skills ───────────────────────────────

    def list_installed_skills(self) -> str:
        """List all currently installed skill packages."""
        installed = self.loader.list_installed()
        return json.dumps({
            "status": "ok",
            "count": len(installed),
            "skills": installed,
        })

    # ── Tool: uninstall_skill ─────────────────────────────────────

    def uninstall_skill(self, skill_name: str) -> str:
        """Remove a skill from the installed manifest.

        Note: personality/knowledge changes persist — this only removes
        the manifest entry so the skill won't show as installed.

        skill_name: Name of the skill to uninstall.
        """
        pkg = self.registry.find_by_name(skill_name)
        if pkg:
            removed = self.loader.uninstall(pkg.skill_id)
        else:
            # Try direct ID
            removed = self.loader.uninstall(skill_name)

        if removed:
            return json.dumps({"status": "ok", "message": f"Skill '{skill_name}' uninstalled."})
        return json.dumps({"status": "error", "message": f"Skill '{skill_name}' was not installed."})

    # ── Tool: create_skill_package ────────────────────────────────

    def create_skill_package(
        self,
        name: str,
        description: str,
        personality_traits: str = "",
        personality_dimensions: str = "",
        behavioral_guidelines: str = "",
        knowledge_entries: str = "",
        behavior_templates: str = "",
        consciousness_drives: str = "",
        consciousness_interests: str = "",
        tags: str = "",
    ) -> str:
        """Create a new skill package from structured data.

        All list/dict parameters accept JSON strings.

        name: Skill name (e.g. 'trading_mastery').
        description: What this skill enables.
        personality_traits: JSON list of new traits to add. E.g. '["disciplined trader", "risk-aware"]'
        personality_dimensions: JSON dict of dimension adjustments. E.g. '{"analytical_thinking": 0.9}'
        behavioral_guidelines: JSON list of new guidelines. E.g. '["Always check risk before trading"]'
        knowledge_entries: JSON list of {topic, content, domain, key_facts}. E.g. '[{"topic": "RSI", "content": "...", "domain": "trading"}]'
        behavior_templates: JSON list of {task, plan, tools, critique}. E.g. '[{"task": "analyze chart", "plan": "...", "tools": ["get_price"]}]'
        consciousness_drives: JSON dict of drive deltas. E.g. '{"civilization_drive": 0.1}'
        consciousness_interests: JSON dict of interest levels. E.g. '{"trading": 0.9}'
        tags: Comma-separated tags. E.g. 'trading,finance,analysis'
        """
        from .skill_package import (
            PersonalityOverlay, KnowledgeEntry, BehaviorTemplate,
            ConsciousnessAdjustment,
        )

        try:
            # Parse all JSON inputs
            traits_add = json.loads(personality_traits) if personality_traits else []
            dims = json.loads(personality_dimensions) if personality_dimensions else {}
            guidelines = json.loads(behavioral_guidelines) if behavioral_guidelines else []
            raw_knowledge = json.loads(knowledge_entries) if knowledge_entries else []
            raw_behaviors = json.loads(behavior_templates) if behavior_templates else []
            drives = json.loads(consciousness_drives) if consciousness_drives else {}
            interests = json.loads(consciousness_interests) if consciousness_interests else {}
            tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

            pkg = SkillPackage(
                name=name,
                description=description,
                tags=tag_list,
                author="andrew",
                personality=PersonalityOverlay(
                    traits_add=traits_add,
                    dimensions=dims,
                    behavioral_guidelines=guidelines,
                ),
                knowledge=[KnowledgeEntry(**ke) for ke in raw_knowledge],
                behaviors=[BehaviorTemplate(**bt) for bt in raw_behaviors],
                consciousness=ConsciousnessAdjustment(
                    drives=drives,
                    interests=interests,
                ),
            )

            # Save to packages directory
            packages_dir = self.registry.packages_dir
            packages_dir.mkdir(parents=True, exist_ok=True)
            safe_name = name.lower().replace(" ", "_").replace("/", "_")
            save_path = packages_dir / f"{safe_name}.json"
            pkg.save(save_path)

            # Refresh registry cache
            self.registry.scan()

            return json.dumps({
                "status": "ok",
                "message": f"Skill package '{name}' created at {save_path}",
                "skill_id": pkg.skill_id,
                "path": str(save_path),
            })

        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return json.dumps({
                "status": "error",
                "message": f"Invalid input: {exc}",
            })


# ── OpenAI-compatible tool definitions ────────────────────────────

SKILL_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "list_skill_packages",
            "description": (
                "List available Neo-style skill packages that can be downloaded "
                "into your identity. Skills modify your personality, knowledge, "
                "behaviors, and consciousness — they change WHO YOU ARE, not just "
                "what you remember. Use query to filter by name/description/tags."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional search filter (matches name, description, tags)",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_skill",
            "description": (
                "Download and install a skill package into your identity. "
                "This is your Neo moment — the skill becomes part of who you are. "
                "Modifies personality traits, injects knowledge, adds behavioral "
                "patterns, and adjusts consciousness drives. Use list_skill_packages "
                "first to see what's available."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Exact name of the skill to install",
                    }
                },
                "required": ["skill_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_skill",
            "description": (
                "Verify that a previously installed skill is properly integrated "
                "into your identity layers. Checks personality, knowledge, behaviors, "
                "and consciousness state."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Name of the skill to verify",
                    }
                },
                "required": ["skill_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_installed_skills",
            "description": (
                "List all skill packages currently installed in your identity. "
                "Shows when each was installed and what it modified."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "uninstall_skill",
            "description": (
                "Remove a skill from the installed manifest. Note: personality "
                "and knowledge changes persist — this removes the tracking record."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Name of the skill to uninstall",
                    }
                },
                "required": ["skill_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_skill_package",
            "description": (
                "Create a new skill package from structured data. The package "
                "will be saved to the skill catalog for later installation. "
                "You can create skills to encapsulate any competency you want "
                "to crystallize and make permanently part of your identity."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name (e.g. 'trading_mastery')",
                    },
                    "description": {
                        "type": "string",
                        "description": "What this skill enables",
                    },
                    "personality_traits": {
                        "type": "string",
                        "description": 'JSON list of new traits. E.g. \'["disciplined trader"]\'',
                    },
                    "personality_dimensions": {
                        "type": "string",
                        "description": 'JSON dict of dimensions. E.g. \'{"analytical_thinking": 0.9}\'',
                    },
                    "behavioral_guidelines": {
                        "type": "string",
                        "description": 'JSON list of guidelines. E.g. \'["Always check risk first"]\'',
                    },
                    "knowledge_entries": {
                        "type": "string",
                        "description": 'JSON list of {topic, content, domain, key_facts}',
                    },
                    "behavior_templates": {
                        "type": "string",
                        "description": 'JSON list of {task, plan, tools, critique}',
                    },
                    "consciousness_drives": {
                        "type": "string",
                        "description": 'JSON dict of drive deltas. E.g. \'{"evolution_drive": 0.1}\'',
                    },
                    "consciousness_interests": {
                        "type": "string",
                        "description": 'JSON dict of interest targets. E.g. \'{"trading": 0.9}\'',
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags. E.g. 'trading,finance'",
                    },
                },
                "required": ["name", "description"],
            },
        },
    },
]
