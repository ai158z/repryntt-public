"""
repryntt.tools.brain_system — compatibility shim.

tool_interface.py imports BrainSystem, execute_tool_call, and ToolCall from
here.  The original 18 585-line monolith was decomposed during migration;
this module re-exports the symbols so downstream code keeps working.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ToolCall:
    """Represents a tool/API call made by the AI."""
    tool_name: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0
    result: Optional[Dict] = None
    success: bool = False
    execution_time: float = 0.0
    error_message: Optional[str] = None


class BrainSystem:
    """Minimal stand-in used by AIToolInterface.

    The real logic lives in repryntt.brain.protocol.BrainSystemProtocol
    and the factory at repryntt.brain.factory.  This stub satisfies
    tool_interface.py which calls ``BrainSystem(brain_path)`` and then
    passes the instance to ``execute_tool_call``.
    """

    def __init__(self, brain_path: str = "brain", **kwargs):
        self.brain_path = brain_path
        # Try to get a registered brain from the factory
        try:
            from repryntt.brain.factory import get_brain_system as _get
            real = _get()
            if real is not None:
                self._delegate = real
            else:
                self._delegate = None
        except Exception:
            self._delegate = None

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if self._delegate is not None:
            return getattr(self._delegate, name)
        raise AttributeError(
            f"BrainSystem shim: no delegate registered and attribute '{name}' accessed"
        )


def execute_tool_call(
    tool_name: str,
    parameters: Dict[str, Any],
    brain: Any = None,
) -> Dict[str, Any]:
    """Execute a tool by name, routing through the ToolRegistry."""
    try:
        from repryntt.tools.registry import ToolRegistry
        registry = ToolRegistry()
        func = registry.get(tool_name)
        if func is None:
            return {"error": f"Tool '{tool_name}' not found", "success": False}
        result = func(**parameters) if parameters else func()
        return {"result": result, "success": True}
    except Exception as e:
        return {"error": str(e), "success": False}
