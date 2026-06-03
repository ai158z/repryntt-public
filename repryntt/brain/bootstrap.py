"""
repryntt.brain.bootstrap — auto-register the standalone ReprynttBrainSystem.

Call :func:`ensure_brain_registered` early in any process that needs
the BrainSystem.  It is idempotent.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_registered = False


def ensure_brain_registered() -> bool:
    """Import and register the ReprynttBrainSystem.  Returns True on success."""
    global _registered
    if _registered:
        return True

    from repryntt.brain.factory import _brain_class, register_brain_class
    if _brain_class is not None:
        _registered = True
        return True

    try:
        from repryntt.brain.brain_impl import ReprynttBrainSystem
        register_brain_class(ReprynttBrainSystem)
        logger.info("ReprynttBrainSystem registered (standalone)")
        _registered = True
        return True
    except Exception as e:
        logger.warning("Could not register ReprynttBrainSystem: %s", e)
        return False
