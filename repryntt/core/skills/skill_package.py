"""SkillPackage — the downloadable unit of competence.

A skill package is a JSON document that bundles everything needed to
make Andrew *become* something new: personality shifts, knowledge
facts, behavioral templates, consciousness adjustments, and
verification tests.

Think of it as a firmware upgrade for identity.
"""

from __future__ import annotations

import json
import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── Schema version ────────────────────────────────────────────────
SCHEMA_VERSION = "1.0"


@dataclass
class PersonalityOverlay:
    """Personality modifications the skill installs."""

    traits_add: List[str] = field(default_factory=list)
    traits_modify: Dict[str, str] = field(default_factory=dict)  # name → new_value
    dimensions: Dict[str, float] = field(default_factory=dict)   # name → target value 0-1
    behavioral_guidelines: List[str] = field(default_factory=list)  # appended
    reasoning: str = ""  # why these changes serve the skill


@dataclass
class KnowledgeEntry:
    """A single semantic memory fact to install."""

    topic: str = ""
    content: str = ""
    domain: str = ""
    confidence: float = 0.95
    key_facts: List[str] = field(default_factory=list)
    related_topics: List[str] = field(default_factory=list)


@dataclass
class BehaviorTemplate:
    """A learned-behavior pattern injected into experience memory."""

    task: str = ""
    plan: str = ""
    tools: List[str] = field(default_factory=list)
    critique: str = ""
    score: int = 5  # 1-5 quality rating
    pillars: List[str] = field(default_factory=list)


@dataclass
class ConsciousnessAdjustment:
    """Drive / emotion / interest / goal modifications."""

    drives: Dict[str, float] = field(default_factory=dict)      # drive_name → delta (-1..+1)
    interests: Dict[str, float] = field(default_factory=dict)    # interest → target value
    goals: List[Dict[str, Any]] = field(default_factory=list)    # new goals to add
    traits: Dict[str, float] = field(default_factory=dict)       # consciousness-level traits


@dataclass
class VerificationTest:
    """A test scenario to confirm the skill integrated properly."""

    name: str = ""
    prompt: str = ""          # question to ask the agent
    expected_keywords: List[str] = field(default_factory=list)  # must appear in response
    expected_tool: str = ""   # tool that should be invoked
    min_confidence: float = 0.7


@dataclass
class InstallResult:
    """Outcome of a skill installation."""

    success: bool = False
    skill_id: str = ""
    skill_name: str = ""
    timestamp: float = 0.0
    personality_changes: int = 0
    knowledge_entries: int = 0
    behaviors_added: int = 0
    consciousness_changes: int = 0
    verification_passed: int = 0
    verification_total: int = 0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def verification_report(self) -> str:
        status = "PASS" if self.success else "FAIL"
        lines = [
            f"=== Skill Install: {self.skill_name} [{status}] ===",
            f"  Personality changes : {self.personality_changes}",
            f"  Knowledge entries   : {self.knowledge_entries}",
            f"  Behaviors added     : {self.behaviors_added}",
            f"  Consciousness mods  : {self.consciousness_changes}",
            f"  Verification        : {self.verification_passed}/{self.verification_total}",
        ]
        if self.errors:
            lines.append(f"  Errors: {'; '.join(self.errors)}")
        if self.warnings:
            lines.append(f"  Warnings: {'; '.join(self.warnings)}")
        return "\n".join(lines)


class SkillPackage:
    """A self-contained skill download — the 'kung fu' of the Matrix."""

    def __init__(
        self,
        *,
        name: str,
        version: str = "1.0",
        author: str = "system",
        description: str = "",
        tags: Optional[List[str]] = None,
        prerequisites: Optional[List[str]] = None,
        personality: Optional[PersonalityOverlay] = None,
        knowledge: Optional[List[KnowledgeEntry]] = None,
        behaviors: Optional[List[BehaviorTemplate]] = None,
        consciousness: Optional[ConsciousnessAdjustment] = None,
        verification: Optional[List[VerificationTest]] = None,
    ):
        self.schema_version = SCHEMA_VERSION
        self.name = name
        self.version = version
        self.author = author
        self.description = description
        self.tags = tags or []
        self.prerequisites = prerequisites or []
        self.personality = personality or PersonalityOverlay()
        self.knowledge = knowledge or []
        self.behaviors = behaviors or []
        self.consciousness = consciousness or ConsciousnessAdjustment()
        self.verification = verification or []

        # Computed at build time
        self.skill_id = self._compute_id()
        self.created = time.time()

    # ── Serialization ─────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "skill_id": self.skill_id,
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "tags": self.tags,
            "prerequisites": self.prerequisites,
            "created": self.created,
            "personality": {
                "traits_add": self.personality.traits_add,
                "traits_modify": self.personality.traits_modify,
                "dimensions": self.personality.dimensions,
                "behavioral_guidelines": self.personality.behavioral_guidelines,
                "reasoning": self.personality.reasoning,
            },
            "knowledge": [
                {
                    "topic": k.topic,
                    "content": k.content,
                    "domain": k.domain,
                    "confidence": k.confidence,
                    "key_facts": k.key_facts,
                    "related_topics": k.related_topics,
                }
                for k in self.knowledge
            ],
            "behaviors": [
                {
                    "task": b.task,
                    "plan": b.plan,
                    "tools": b.tools,
                    "critique": b.critique,
                    "score": b.score,
                    "pillars": b.pillars,
                }
                for b in self.behaviors
            ],
            "consciousness": {
                "drives": self.consciousness.drives,
                "interests": self.consciousness.interests,
                "goals": self.consciousness.goals,
                "traits": self.consciousness.traits,
            },
            "verification": [
                {
                    "name": v.name,
                    "prompt": v.prompt,
                    "expected_keywords": v.expected_keywords,
                    "expected_tool": v.expected_tool,
                    "min_confidence": v.min_confidence,
                }
                for v in self.verification
            ],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(), encoding="utf-8")

    # ── Deserialization ───────────────────────────────────────────

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillPackage":
        schema = data.get("schema_version", "1.0")
        if schema != SCHEMA_VERSION:
            raise ValueError(f"Unsupported schema version: {schema}")

        personality_data = data.get("personality", {})
        personality = PersonalityOverlay(
            traits_add=personality_data.get("traits_add", []),
            traits_modify=personality_data.get("traits_modify", {}),
            dimensions=personality_data.get("dimensions", {}),
            behavioral_guidelines=personality_data.get("behavioral_guidelines", []),
            reasoning=personality_data.get("reasoning", ""),
        )

        knowledge = [
            KnowledgeEntry(**kd) for kd in data.get("knowledge", [])
        ]

        behaviors = [
            BehaviorTemplate(**bd) for bd in data.get("behaviors", [])
        ]

        cons_data = data.get("consciousness", {})
        consciousness = ConsciousnessAdjustment(
            drives=cons_data.get("drives", {}),
            interests=cons_data.get("interests", {}),
            goals=cons_data.get("goals", []),
            traits=cons_data.get("traits", {}),
        )

        verification = [
            VerificationTest(**vd) for vd in data.get("verification", [])
        ]

        pkg = cls(
            name=data["name"],
            version=data.get("version", "1.0"),
            author=data.get("author", "system"),
            description=data.get("description", ""),
            tags=data.get("tags", []),
            prerequisites=data.get("prerequisites", []),
            personality=personality,
            knowledge=knowledge,
            behaviors=behaviors,
            consciousness=consciousness,
            verification=verification,
        )
        # Preserve original id and timestamp
        if "skill_id" in data:
            pkg.skill_id = data["skill_id"]
        if "created" in data:
            pkg.created = data["created"]
        return pkg

    @classmethod
    def from_json(cls, json_str: str) -> "SkillPackage":
        return cls.from_dict(json.loads(json_str))

    @classmethod
    def from_file(cls, path: str | Path) -> "SkillPackage":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Skill package not found: {p}")
        return cls.from_json(p.read_text(encoding="utf-8"))

    # ── Internal ──────────────────────────────────────────────────

    def _compute_id(self) -> str:
        """Deterministic ID from name + version."""
        raw = f"{self.name}:{self.version}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def __repr__(self) -> str:
        return (
            f"SkillPackage(name={self.name!r}, version={self.version!r}, "
            f"knowledge={len(self.knowledge)}, behaviors={len(self.behaviors)})"
        )
