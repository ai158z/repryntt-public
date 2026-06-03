"""
BrainSystem factory — dependency-injection registry.

Lets framework code obtain a BrainSystem without hardcoding an import path.
The SAIGE runtime registers its implementation at startup; everything else
goes through :func:`get_brain_system` or :func:`create_brain_system`.
"""

from __future__ import annotations

import threading
from typing import Any, Optional, Type

_brain_class: Optional[Type] = None
_brain_instance: Optional[Any] = None
_lock = threading.Lock()


def register_brain_class(cls: Type) -> None:
    """Register the concrete BrainSystem class.

    Called once at runtime startup (e.g. in the consciousness daemon)::

        from brain.brain_system import BrainSystem
        repryntt.brain.register_brain_class(BrainSystem)
    """
    global _brain_class
    _brain_class = cls


def create_brain_system(**kwargs: Any) -> Any:
    """Create a **new** BrainSystem instance.

    Raises ``RuntimeError`` if no class has been registered yet.
    """
    if _brain_class is None:
        raise RuntimeError(
            "No BrainSystem class registered. "
            "Call repryntt.brain.register_brain_class() at startup."
        )
    return _brain_class(**kwargs)


def get_brain_system(**kwargs: Any) -> Any:
    """Return the singleton BrainSystem, creating it on first call.

    Thread-safe.  Extra *kwargs* are forwarded to the constructor only
    on the first call (when the instance is created).
    """
    global _brain_instance
    if _brain_instance is not None:
        return _brain_instance
    with _lock:
        if _brain_instance is None:
            _brain_instance = create_brain_system(**kwargs)
    return _brain_instance


def set_brain_system(instance: Any) -> None:
    """Inject an already-constructed BrainSystem as the singleton.

    Useful when the caller already has a BrainSystem (e.g. passed as a
    parameter) and wants the rest of the framework to share it.
    """
    global _brain_instance
    with _lock:
        _brain_instance = instance


def reset_brain_system() -> None:
    """Clear the singleton (for tests or hot-reload)."""
    global _brain_instance
    with _lock:
        _brain_instance = None
