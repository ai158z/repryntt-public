"""
Neo-style instant skill download system.

Downloads structured skill packages directly into Andrew's identity,
personality, knowledge, and behavioral systems — not just memory,
but WHO HE IS.

Usage:
    from repryntt.core.skills import SkillPackage, SkillLoader, SkillRegistry

    loader = SkillLoader(brain_path="brain")
    pkg = SkillPackage.from_file("skills/trading_mastery.json")
    result = loader.install(pkg)
    print(result.verification_report)
"""

from .skill_package import SkillPackage, InstallResult
from .skill_loader import SkillLoader
from .skill_verifier import SkillVerifier
from .skill_registry import SkillRegistry

__all__ = [
    "SkillPackage",
    "SkillLoader",
    "SkillVerifier",
    "SkillRegistry",
    "InstallResult",
]
