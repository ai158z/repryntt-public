"""SkillVerifier — confirms a skill integrated properly.

After installation, the verifier checks:
  1. Personality traits/dimensions actually appear in ava_brain.json
  2. Knowledge entries exist in semantic_memory.json
  3. Behaviors landed in learned_behaviors.json
  4. Consciousness state reflects new drives/interests/goals
  5. Optional prompt-based verification (requires LLM callback)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .skill_package import SkillPackage, VerificationTest

logger = logging.getLogger(__name__)


class SkillVerifier:
    """Post-installation verification for skill packages."""

    def __init__(
        self,
        brain_path: str = None,
        workspace_path: str = None,
    ):
        from repryntt.paths import brain_dir as _brain_dir, operator_dir as _operator_dir
        self.brain_path = Path(brain_path) if brain_path else _brain_dir()
        self.workspace_path = Path(workspace_path) if workspace_path else _operator_dir()

    def verify(
        self,
        package: SkillPackage,
        llm_callback: Optional[Callable[[str], str]] = None,
    ) -> Tuple[int, int, List[str]]:
        """Verify skill installation.

        Returns:
            (passed, total, error_messages)
        """
        passed = 0
        total = 0
        errors: List[str] = []

        # Structural checks
        p, t, e = self._verify_personality(package)
        passed += p; total += t; errors.extend(e)

        p, t, e = self._verify_consciousness(package)
        passed += p; total += t; errors.extend(e)

        p, t, e = self._verify_knowledge(package)
        passed += p; total += t; errors.extend(e)

        p, t, e = self._verify_behaviors(package)
        passed += p; total += t; errors.extend(e)

        # Prompt-based verification (if LLM available)
        if package.verification and llm_callback:
            p, t, e = self._verify_prompts(package, llm_callback)
            passed += p; total += t; errors.extend(e)

        return passed, total, errors

    # ── Personality ───────────────────────────────────────────────

    def _verify_personality(self, pkg: SkillPackage) -> Tuple[int, int, List[str]]:
        passed = total = 0
        errors = []
        pers = pkg.personality

        if not (pers.traits_add or pers.dimensions or pers.behavioral_guidelines):
            return 0, 0, []

        brain_path = self.brain_path / "ava_brain.json"
        brain = self._load_json(brain_path)
        personality = brain.get("personality", {})

        # Check added traits
        traits = personality.get("traits", [])
        for trait in pers.traits_add:
            total += 1
            if trait in traits:
                passed += 1
            else:
                errors.append(f"Trait not found: {trait}")

        # Check dimensions
        dims = personality.get("dimensions", {})
        for dim_name, expected in pers.dimensions.items():
            total += 1
            actual = dims.get(dim_name)
            if actual is not None and abs(actual - expected) < 0.01:
                passed += 1
            else:
                errors.append(f"Dimension {dim_name}: expected {expected}, got {actual}")

        # Check guidelines
        guidelines = personality.get("behavioral_guidelines", [])
        for gl in pers.behavioral_guidelines:
            total += 1
            if gl in guidelines:
                passed += 1
            else:
                errors.append(f"Guideline not found: {gl[:60]}...")

        return passed, total, errors

    # ── Consciousness ─────────────────────────────────────────────

    def _verify_consciousness(self, pkg: SkillPackage) -> Tuple[int, int, List[str]]:
        passed = total = 0
        errors = []
        cons = pkg.consciousness

        if not (cons.drives or cons.interests or cons.goals):
            return 0, 0, []

        state_path = self.workspace_path / "consciousness_state.json"
        state = self._load_json(state_path)

        # Check drives exist (we applied deltas, so just verify key exists)
        drives = state.get("drives", {})
        for drive_name in cons.drives:
            total += 1
            if drive_name in drives:
                passed += 1
            else:
                errors.append(f"Drive not found: {drive_name}")

        # Check interests
        interests = state.get("interests", {})
        for name, expected in cons.interests.items():
            total += 1
            actual = interests.get(name)
            if actual is not None and abs(actual - expected) < 0.05:
                passed += 1
            else:
                errors.append(f"Interest {name}: expected {expected}, got {actual}")

        # Check goals
        goals = state.get("goals", [])
        goal_ids = {g.get("id") for g in goals}
        for goal in cons.goals:
            total += 1
            if goal.get("id") in goal_ids:
                passed += 1
            else:
                errors.append(f"Goal not found: {goal.get('id')}")

        return passed, total, errors

    # ── Knowledge ─────────────────────────────────────────────────

    def _verify_knowledge(self, pkg: SkillPackage) -> Tuple[int, int, List[str]]:
        passed = total = 0
        errors = []

        if not pkg.knowledge:
            return 0, 0, []

        sem_path = self.brain_path / "semantic_memory.json"
        sem = self._load_json(sem_path)
        memories = sem.get("memories", {})

        for ke in pkg.knowledge:
            total += 1
            key = ke.topic.lower().replace(" ", "_")
            if key in memories:
                passed += 1
            else:
                errors.append(f"Knowledge missing: {ke.topic}")

        return passed, total, errors

    # ── Behaviors ─────────────────────────────────────────────────

    def _verify_behaviors(self, pkg: SkillPackage) -> Tuple[int, int, List[str]]:
        passed = total = 0
        errors = []

        if not pkg.behaviors:
            return 0, 0, []

        beh_path = self.workspace_path / "learned_behaviors.json"
        data = self._load_json(beh_path)
        experiences = data.get("experiences", [])
        task_names = {e.get("task") for e in experiences}

        for bt in pkg.behaviors:
            total += 1
            if bt.task in task_names:
                passed += 1
            else:
                errors.append(f"Behavior missing: {bt.task}")

        return passed, total, errors

    # ── Prompt verification ───────────────────────────────────────

    def _verify_prompts(
        self,
        pkg: SkillPackage,
        llm_callback: Callable[[str], str],
    ) -> Tuple[int, int, List[str]]:
        passed = total = 0
        errors = []

        for vt in pkg.verification:
            total += 1
            try:
                response = llm_callback(vt.prompt)
                response_lower = response.lower()

                # Check expected keywords
                found_keywords = sum(
                    1 for kw in vt.expected_keywords
                    if kw.lower() in response_lower
                )
                ratio = (
                    found_keywords / len(vt.expected_keywords)
                    if vt.expected_keywords else 1.0
                )

                if ratio >= vt.min_confidence:
                    passed += 1
                else:
                    errors.append(
                        f"Verification '{vt.name}': only {found_keywords}/"
                        f"{len(vt.expected_keywords)} keywords found"
                    )
            except Exception as exc:
                errors.append(f"Verification '{vt.name}' error: {exc}")

        return passed, total, errors

    # ── Helpers ───────────────────────────────────────────────────

    def _load_json(self, path: Path) -> Dict[str, Any]:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {}
