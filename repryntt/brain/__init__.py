"""
repryntt.brain — BrainSystem protocol and dependency-injection factory.

Defines the contract that any BrainSystem implementation must satisfy,
plus a singleton registry so framework code never hardcodes an import path.

Usage in SAIGE runtime (startup):
    from repryntt.brain import ensure_brain_registered
    ensure_brain_registered()  # registers ReprynttBrainSystem

Usage in framework code:
    from repryntt.brain import get_brain_system
    brain = get_brain_system()
    brain._call_ai_service("Hello", include_tools=False)

Usage for type hints:
    from repryntt.brain import BrainSystemProtocol
    def my_func(brain: BrainSystemProtocol) -> None: ...
"""

from repryntt.brain.protocol import BrainSystemProtocol
from repryntt.brain.factory import (
    register_brain_class,
    create_brain_system,
    get_brain_system,
    set_brain_system,
    reset_brain_system,
)
from repryntt.brain.bootstrap import ensure_brain_registered

__all__ = [
    "BrainSystemProtocol",
    "register_brain_class",
    "create_brain_system",
    "get_brain_system",
    "set_brain_system",
    "reset_brain_system",
]
