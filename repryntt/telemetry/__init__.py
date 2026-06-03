"""
Telemetry — Real-time observability for agent operations.

Provides structured event logging, SSE broadcasting, and JSONL persistence
so operators can see exactly what Andrew is thinking and doing at every step.
"""

from repryntt.telemetry.events import (
    TelemetryEvent,
    EventType,
    Phase,
    OpsLogger,
    get_ops_logger,
)

__all__ = [
    "TelemetryEvent",
    "EventType",
    "Phase",
    "OpsLogger",
    "get_ops_logger",
]
