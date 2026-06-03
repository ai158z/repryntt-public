"""
repryntt.core.consciousness.framework_evolution_subsystem
─────────────────────────────────────────────────────────

Daemon subsystem that runs the framework outcome-driven evolution loop on a
self-throttled cadence. Plugs into the existing ``SubsystemCoordinator``
(see ``daemon.py``) using the ``ISubsystem`` interface.

Why a subsystem and not OS cron:
    The daemon is already running, already logs, already handles safe mode
    and status reporting. Wiring evolution in here means a single process
    to operate, observable status, and coordinated shutdown.

Cadence:
    Default 6 hours between automatic reviews. Directives may force an
    immediate run (used by manual ops or by the meta-decision engine when
    framework outcomes degrade).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from repryntt.core.consciousness.daemon import (
    ConsciousnessDirective,
    ISubsystem,
    SubsystemResponse,
    SubsystemStatus,
)
from repryntt.core.frameworks.evolution import EvolutionLoop

logger = logging.getLogger("repryntt.consciousness.framework_evolution")


class FrameworkEvolutionSubsystem(ISubsystem):
    """Periodic, self-throttled framework evolution as a daemon subsystem."""

    SUPPORTED_ACTIONS = (
        "run_review",        # diagnose + fork (respects throttle unless force=True)
        "dry_run_review",    # diagnose only, no fork
        "get_status",
        "enter_safe_mode",
    )

    DEFAULT_INTERVAL_SECONDS = 6 * 60 * 60   # 6 hours
    DEFAULT_MIN_RUNS = EvolutionLoop.DEFAULT_MIN_RUNS
    DEFAULT_MAX_WIN_RATE = EvolutionLoop.DEFAULT_MAX_WIN_RATE

    def __init__(self,
                 loop: Optional[EvolutionLoop] = None,
                 *,
                 interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
                 min_runs: int = DEFAULT_MIN_RUNS,
                 max_win_rate: float = DEFAULT_MAX_WIN_RATE):
        self.loop = loop or EvolutionLoop()
        self.interval_seconds = float(interval_seconds)
        self.min_runs = int(min_runs)
        self.max_win_rate = float(max_win_rate)

        self.safe_mode = False
        self.last_run_at: float = 0.0
        self.last_report: Dict[str, Any] = {}
        self.total_runs: int = 0
        self.total_evolved: int = 0

    # ── ISubsystem ───────────────────────────────────────────────────

    @property
    def subsystem_name(self) -> str:
        return "framework_evolution"

    def can_handle_directive(self, directive: ConsciousnessDirective) -> bool:
        return directive.action in self.SUPPORTED_ACTIONS

    def request_attention(self, priority: int) -> bool:
        """Ask consciousness for a slot when an automatic run is due."""
        if self.safe_mode:
            return False
        return self._is_due()

    def receive_directive(self, directive: ConsciousnessDirective) -> SubsystemResponse:
        action = directive.action
        params = directive.parameters or {}

        try:
            if action == "run_review":
                force = bool(params.get("force", False))
                if self.safe_mode and not force:
                    return self._response(directive, success=False,
                                          result="safe_mode_active",
                                          status_update={"safe_mode": True})
                if not force and not self._is_due():
                    next_in = max(0.0, self.interval_seconds -
                                  (time.time() - self.last_run_at))
                    return self._response(
                        directive, success=True,
                        result={"status": "throttled",
                                "seconds_until_next": round(next_in, 1)},
                        status_update={"throttled": True},
                    )
                return self._do_review(directive, dry_run=False, params=params)

            if action == "dry_run_review":
                return self._do_review(directive, dry_run=True, params=params)

            if action == "get_status":
                return self._response(
                    directive, success=True,
                    result=self._status_dict(),
                    status_update={},
                )

            if action == "enter_safe_mode":
                reason = params.get("reason", "consciousness_directive")
                self.safe_mode = True
                logger.info(f"🛡️ Framework evolution entering safe mode: {reason}")
                return self._response(
                    directive, success=True,
                    result={"status": "safe_mode_entered", "reason": reason},
                    status_update={"safe_mode": True, "reason": reason},
                )

            return self._response(
                directive, success=False,
                result=f"unknown action: {action}",
                status_update={"error": "unsupported_directive"},
            )
        except Exception as e:
            logger.exception("framework_evolution directive failed")
            return self._response(
                directive, success=False, result=str(e),
                status_update={"error": str(e)},
            )

    def get_status(self) -> SubsystemStatus:
        try:
            health = 0.3 if self.safe_mode else 0.95
            ops: List[str] = []
            if self.safe_mode:
                ops.append("safe_mode")
            if self._is_due():
                ops.append("review_due")
            return SubsystemStatus(
                subsystem_name=self.subsystem_name,
                health_score=health,
                active_operations=ops,
                resource_usage={"cpu": 0.05, "memory": 0.02},
                capabilities=[
                    "outcome_diagnosis",
                    "spec_mutation_proposal",
                    "auto_fork_underperforming_specs",
                ],
                pending_requests=0,
                last_update=time.time(),
            )
        except Exception:
            return SubsystemStatus(
                subsystem_name=self.subsystem_name,
                health_score=0.0,
                active_operations=[],
                resource_usage={},
                capabilities=[],
                pending_requests=0,
                last_update=time.time(),
            )

    # ── Internals ────────────────────────────────────────────────────

    def _is_due(self) -> bool:
        return (time.time() - self.last_run_at) >= self.interval_seconds

    def _do_review(self, directive: ConsciousnessDirective, *,
                   dry_run: bool, params: Dict[str, Any]) -> SubsystemResponse:
        min_runs = int(params.get("min_runs", self.min_runs))
        max_win_rate = float(params.get("max_win_rate", self.max_win_rate))
        report = self.loop.review(
            min_runs=min_runs,
            max_win_rate=max_win_rate,
            dry_run=dry_run,
        )
        evolved_count = len(report.get("evolved", []))
        if not dry_run:
            self.last_run_at = time.time()
            self.total_runs += 1
            self.total_evolved += evolved_count
        self.last_report = report
        logger.info(
            f"🧬 framework_evolution {'dry-run ' if dry_run else ''}review: "
            f"diagnosed={len(report.get('diagnosed', []))} "
            f"evolved={evolved_count} skipped={len(report.get('skipped', []))}"
        )
        return self._response(
            directive, success=True,
            result={
                "diagnosed": len(report.get("diagnosed", [])),
                "evolved": evolved_count,
                "skipped": len(report.get("skipped", [])),
                "dry_run": dry_run,
                "evolved_ids": [e.get("new_framework_id") or e.get("would_create")
                                for e in report.get("evolved", [])],
            },
            status_update={
                "last_run_at": self.last_run_at,
                "last_evolved_count": evolved_count,
                "total_runs": self.total_runs,
            },
        )

    def _status_dict(self) -> Dict[str, Any]:
        return {
            "safe_mode": self.safe_mode,
            "interval_seconds": self.interval_seconds,
            "last_run_at": self.last_run_at,
            "seconds_since_last_run": (time.time() - self.last_run_at)
                                       if self.last_run_at else None,
            "total_runs": self.total_runs,
            "total_evolved": self.total_evolved,
            "due_now": self._is_due(),
        }

    @staticmethod
    def _response(directive: ConsciousnessDirective, *, success: bool,
                  result: Any, status_update: Dict[str, Any]) -> SubsystemResponse:
        return SubsystemResponse(
            directive_id=directive.directive_id,
            success=success,
            result=result,
            execution_time=0.0,
            status_update=status_update,
        )


__all__ = ["FrameworkEvolutionSubsystem"]
