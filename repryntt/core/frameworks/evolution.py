"""
repryntt.core.frameworks.evolution — Outcome-driven spec evolution.

The framework registry already tracks per-spec ``runs / wins / losses /
win_rate`` and supports forking via ``registry.fork()``. This module closes
the feedback loop: it scans completed instances, finds underperforming
specs, diagnoses *which state* is failing them most often, and proposes a
fork with a targeted mutation likely to improve outcomes.

The default mutation strategy is deterministic and conservative — bump the
``max_heartbeats`` budget on the bottleneck state. That fixes the most
common failure mode (gate too strict for the budget) without changing
semantics, so an LLM is not required. A strategy hook is exposed for
projects that want to plug in LLM-driven mutations later.

Public API
----------
    loop = EvolutionLoop()
    report = loop.review(min_runs=5, max_win_rate=0.5)
    # report["evolved"]: list of new framework_ids forked this run
    # report["skipped"]: list of {framework_id, reason}

CLI::

    python -m repryntt.core.frameworks.evolution --review
    python -m repryntt.core.frameworks.evolution --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from repryntt.core.frameworks.registry import FrameworkRegistry, get_registry
from repryntt.core.frameworks.runtime import _instances_dir
from repryntt.core.frameworks.schema import (
    Framework,
    FrameworkInstance,
    FrameworkState,
    InstanceStatus,
)

logger = logging.getLogger("repryntt.frameworks.evolution")


# ── Diagnostic types ─────────────────────────────────────────────────────

@dataclass
class FailureProfile:
    """Aggregate diagnostic for one framework's recent losses."""
    framework_id: str
    runs: int
    wins: int
    losses: int
    win_rate: float
    bottleneck_state: Optional[str] = None       # state cited in most failures
    bottleneck_failures: int = 0
    bottleneck_avg_heartbeats: float = 0.0
    failure_states: Dict[str, int] = field(default_factory=dict)


@dataclass
class MutationProposal:
    """A concrete patch the loop wants to apply to a spec."""
    base_id: str
    new_id: str
    rationale: str
    spec_patch: Dict[str, Any]


# ── Strategy signature ───────────────────────────────────────────────────

# A strategy receives (framework_spec, failure_profile) and returns a
# MutationProposal or None if it has no suggestion.
MutationStrategy = Callable[[Framework, FailureProfile], Optional[MutationProposal]]


# ── Loop ─────────────────────────────────────────────────────────────────

class EvolutionLoop:
    """Scan framework outcomes and propose targeted spec evolutions."""

    DEFAULT_MIN_RUNS = 5
    DEFAULT_MAX_WIN_RATE = 0.5
    DEFAULT_HEARTBEAT_BUMP = 2

    def __init__(
        self,
        registry: Optional[FrameworkRegistry] = None,
        instances_dir: Optional[Path] = None,
        strategies: Optional[List[MutationStrategy]] = None,
    ):
        self.registry = registry or get_registry()
        self.instances_dir = instances_dir or _instances_dir()
        self.strategies: List[MutationStrategy] = strategies or [
            self._strategy_bump_bottleneck_heartbeats,
        ]

    # ── Public entrypoint ────────────────────────────────────────────

    def review(
        self,
        *,
        min_runs: int = DEFAULT_MIN_RUNS,
        max_win_rate: float = DEFAULT_MAX_WIN_RATE,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Diagnose underperforming specs and (optionally) fork mutations.

        A spec is reviewed when ``runs >= min_runs and win_rate <
        max_win_rate``. For each, the loop runs strategies in order and
        applies the first proposal returned. Skips specs that already have
        an evolved descendant carrying this loop's marker tag.
        """
        evolved: List[Dict[str, Any]] = []
        skipped: List[Dict[str, str]] = []
        diagnosed: List[Dict[str, Any]] = []

        for fw in self.registry.all():
            if fw.runs < min_runs:
                continue
            if fw.win_rate >= max_win_rate:
                continue
            if self._already_evolved(fw):
                skipped.append({
                    "framework_id": fw.id,
                    "reason": "descendant from this loop already exists",
                })
                continue

            profile = self.diagnose(fw)
            diagnosed.append({
                "framework_id": fw.id,
                "win_rate": round(profile.win_rate, 3),
                "bottleneck_state": profile.bottleneck_state,
                "bottleneck_failures": profile.bottleneck_failures,
                "failure_states": profile.failure_states,
            })

            proposal = self._first_proposal(fw, profile)
            if proposal is None:
                skipped.append({
                    "framework_id": fw.id,
                    "reason": "no strategy produced a proposal",
                })
                continue

            if dry_run:
                evolved.append({
                    "framework_id": fw.id,
                    "would_create": proposal.new_id,
                    "rationale": proposal.rationale,
                    "patch_keys": sorted(proposal.spec_patch.keys()),
                    "dry_run": True,
                })
                continue

            try:
                child = self._apply_proposal(proposal)
                evolved.append({
                    "framework_id": fw.id,
                    "new_framework_id": child.id,
                    "rationale": proposal.rationale,
                })
                logger.info(
                    f"🧬 Evolved {fw.id} → {child.id} ({proposal.rationale})"
                )
            except Exception as e:
                skipped.append({
                    "framework_id": fw.id,
                    "reason": f"fork failed: {e}",
                })
                logger.warning(f"evolution fork failed for {fw.id}: {e}")

        return {
            "ok": True,
            "ran_at": time.time(),
            "min_runs": min_runs,
            "max_win_rate": max_win_rate,
            "dry_run": dry_run,
            "diagnosed": diagnosed,
            "evolved": evolved,
            "skipped": skipped,
        }

    # ── Diagnosis ────────────────────────────────────────────────────

    def diagnose(self, fw: Framework) -> FailureProfile:
        """Walk persisted instances of ``fw`` and find the bottleneck state."""
        failure_counter: Counter[str] = Counter()
        heartbeat_totals: Dict[str, int] = {}
        heartbeat_samples: Dict[str, int] = {}

        for inst in self._instances_for(fw.id):
            if inst.status not in (InstanceStatus.FAILED, InstanceStatus.ABANDONED):
                continue
            bottleneck = self._instance_bottleneck(inst)
            if bottleneck is None:
                continue
            state_name, hb = bottleneck
            failure_counter[state_name] += 1
            heartbeat_totals[state_name] = heartbeat_totals.get(state_name, 0) + hb
            heartbeat_samples[state_name] = heartbeat_samples.get(state_name, 0) + 1

        bottleneck_state: Optional[str] = None
        bottleneck_failures = 0
        bottleneck_avg = 0.0
        if failure_counter:
            bottleneck_state, bottleneck_failures = failure_counter.most_common(1)[0]
            samples = heartbeat_samples.get(bottleneck_state, 0)
            if samples:
                bottleneck_avg = heartbeat_totals[bottleneck_state] / samples

        return FailureProfile(
            framework_id=fw.id,
            runs=fw.runs,
            wins=fw.wins,
            losses=fw.losses,
            win_rate=fw.win_rate,
            bottleneck_state=bottleneck_state,
            bottleneck_failures=bottleneck_failures,
            bottleneck_avg_heartbeats=bottleneck_avg,
            failure_states=dict(failure_counter),
        )

    # ── Default strategy: bump bottleneck heartbeats ─────────────────

    def _strategy_bump_bottleneck_heartbeats(
        self, fw: Framework, profile: FailureProfile,
    ) -> Optional[MutationProposal]:
        if not profile.bottleneck_state:
            return None
        target = fw.get_state(profile.bottleneck_state)
        if target is None:
            return None

        bump = self.DEFAULT_HEARTBEAT_BUMP
        new_max = max(target.max_heartbeats, 1) + bump

        # Build a fully-replaced states list with the bumped target
        new_states: List[Dict[str, Any]] = []
        for s in fw.states:
            d = s.to_dict()
            if s.name == target.name:
                d["max_heartbeats"] = new_max
            new_states.append(d)

        new_id = self._next_descendant_id(fw.id)
        rationale = (
            f"bottleneck '{target.name}' caused {profile.bottleneck_failures} "
            f"failures (avg {profile.bottleneck_avg_heartbeats:.1f} hb); "
            f"raising max_heartbeats {target.max_heartbeats}→{new_max}"
        )
        return MutationProposal(
            base_id=fw.id,
            new_id=new_id,
            rationale=rationale,
            spec_patch={
                "label": f"{fw.label} (auto-evolved)",
                "states": new_states,
                "tags": sorted(set(list(fw.tags) + [self._marker_tag()])),
            },
        )

    # ── Internals ────────────────────────────────────────────────────

    @staticmethod
    def _marker_tag() -> str:
        return "auto_evolved"

    def _already_evolved(self, fw: Framework) -> bool:
        """Has this loop already produced a descendant of ``fw``?"""
        marker = self._marker_tag()
        for other in self.registry.all():
            if fw.id in other.lineage and marker in (other.tags or []):
                return True
        return False

    def _next_descendant_id(self, base_id: str) -> str:
        """Pick a non-colliding descendant id like ``base__evo1``."""
        i = 1
        while True:
            candidate = f"{base_id}__evo{i}"
            if self.registry.get(candidate) is None:
                return candidate
            i += 1

    def _first_proposal(
        self, fw: Framework, profile: FailureProfile,
    ) -> Optional[MutationProposal]:
        for strategy in self.strategies:
            try:
                proposal = strategy(fw, profile)
            except Exception as e:
                logger.warning(f"strategy {strategy.__name__} raised: {e}")
                continue
            if proposal is not None:
                return proposal
        return None

    def _apply_proposal(self, proposal: MutationProposal) -> Framework:
        """Fork the base spec and apply the patch to the descendant."""
        def _mutate(child: Framework) -> Framework:
            patch = proposal.spec_patch
            if "states" in patch:
                child.states = [FrameworkState.from_dict(s) for s in patch["states"]]
            for k in ("label", "description", "match_keywords",
                      "tags", "success_criteria"):
                if k in patch:
                    setattr(child, k, patch[k])
            return child

        return self.registry.fork(proposal.base_id, proposal.new_id, _mutate)

    def _instances_for(self, framework_id: str) -> List[FrameworkInstance]:
        out: List[FrameworkInstance] = []
        for path in self.instances_dir.glob("*.json"):
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                if data.get("framework_id") != framework_id:
                    continue
                out.append(FrameworkInstance.from_dict(data))
            except Exception:
                continue
        return out

    @staticmethod
    def _instance_bottleneck(inst: FrameworkInstance) -> Optional[tuple]:
        """Return (state_name, heartbeats_spent) where the instance got stuck."""
        # Prefer the last transition's *from* state if it ended in failure;
        # else fall back to current_state.
        if inst.transitions:
            last = inst.transitions[-1]
            to = last.get("to") or ""
            if to in ("<failed>", "<done>"):
                state_name = last.get("from") or inst.current_state
            else:
                state_name = to or inst.current_state
        else:
            state_name = inst.current_state
        if not state_name:
            return None
        return state_name, max(0, int(inst.heartbeats_in_state))


# ── CLI ──────────────────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m repryntt.core.frameworks.evolution",
        description="Outcome-driven framework evolution loop.",
    )
    p.add_argument("--min-runs", type=int, default=EvolutionLoop.DEFAULT_MIN_RUNS,
                   help="minimum runs before a spec is eligible (default: %(default)s)")
    p.add_argument("--max-win-rate", type=float, default=EvolutionLoop.DEFAULT_MAX_WIN_RATE,
                   help="only evolve specs with win_rate strictly below this (default: %(default)s)")
    p.add_argument("--dry-run", action="store_true",
                   help="diagnose and propose, but do not fork")
    p.add_argument("--json", action="store_true",
                   help="emit raw JSON report on stdout")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _build_arg_parser().parse_args(argv)
    loop = EvolutionLoop()
    report = loop.review(
        min_runs=args.min_runs,
        max_win_rate=args.max_win_rate,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"Diagnosed: {len(report['diagnosed'])}  "
              f"Evolved: {len(report['evolved'])}  "
              f"Skipped: {len(report['skipped'])}  "
              f"(dry_run={report['dry_run']})")
        for ev in report["evolved"]:
            print(f"  ✓ {ev}")
        for sk in report["skipped"]:
            print(f"  · skipped {sk['framework_id']}: {sk['reason']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "EvolutionLoop",
    "FailureProfile",
    "MutationProposal",
    "MutationStrategy",
]
