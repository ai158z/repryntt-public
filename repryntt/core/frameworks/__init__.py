"""
repryntt.core.frameworks
========================
Layer 3 — the operational substrate.

What this is
------------
A unified, declarative schema for *behavior patterns* (frameworks) the
agent can execute, track, and evolve. Every operational pattern — deep
research, build-a-thing, diagnose-a-problem, physical exploration — is
expressible as an instance of one shape:

    Framework = {states, transitions, gates, recovery, lineage}

Specs live as JSON on disk under ``~/.repryntt/frameworks/``, so the
agent can read, mutate, and propose new ones without requiring a Python
edit. Running instances also live on disk under
``~/.repryntt/frameworks/instances/`` so state survives restarts.

Relation to Memory Mesh (Layer 1) and Tools (Layer 2)
-----------------------------------------------------
Framework nodes are *registered in memory mesh* alongside experience
nodes. Each instance's outcome becomes an edge: "framework X on target
Y → score Z". Spreading activation surfaces relevant past frameworks
when the agent encounters a similar situation.

The runtime does NOT execute tools directly. It emits *guidance text*
that gets injected into the agent's PLAN prompt; the agent uses its
existing tool registry (Layer 2) to do the work; the runtime reads back
``working_state`` to check gates and advance.

Public API
----------
    from repryntt.core.frameworks import (
        FrameworkRegistry, FrameworkRuntime, get_runtime,
        Framework, FrameworkState, FrameworkInstance,
    )

See ``tools.py`` for the four agent-facing tools:
    framework_list, framework_spawn, framework_status, framework_propose_mutation
"""

from repryntt.core.frameworks.schema import (
    Framework,
    FrameworkState,
    FrameworkInstance,
    GateResult,
    InstanceStatus,
)
from repryntt.core.frameworks.registry import FrameworkRegistry, get_registry
from repryntt.core.frameworks.runtime import FrameworkRuntime, get_runtime

__all__ = [
    "Framework",
    "FrameworkState",
    "FrameworkInstance",
    "GateResult",
    "InstanceStatus",
    "FrameworkRegistry",
    "FrameworkRuntime",
    "get_registry",
    "get_runtime",
]
