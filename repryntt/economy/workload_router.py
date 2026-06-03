"""
repryntt.economy.workload_router — Intelligent Workload Routing

Routes compute requests to the best available node(s) on the network.
Handles single-node routing and multi-node parallel splitting for large jobs.

Routing strategy:
  1. Filter nodes by capability (VRAM, model size, GPU requirement)
  2. Score by: reputation × (1/price) × free_capacity × (1/latency)
  3. For parallel jobs: split across N cheapest qualified nodes
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from repryntt.economy.resource_registry import ResourceRegistry, ResourceListing, PLANCKS_PER_CREDIT

logger = logging.getLogger(__name__)


@dataclass
class RouteDecision:
    """Result of a routing decision."""
    success: bool
    primary_node: Optional[ResourceListing] = None
    all_nodes: List[ResourceListing] = field(default_factory=list)
    reason: str = ""
    estimated_cost_credits: float = 0.0
    is_parallel: bool = False
    chunks: int = 1


@dataclass
class WorkloadSpec:
    """Describes what a workload needs."""
    # Compute requirements
    min_vram_mb: int = 0
    min_model_params_b: float = 0.0
    need_gpu: bool = False

    # Workload characteristics
    workload_type: str = "inference"     # inference, embedding, finetune, batch
    estimated_duration_s: float = 10.0   # Expected compute time
    parallelizable: bool = False         # Can this be split across nodes?
    chunk_count: int = 1                 # If parallel, how many chunks

    # Budget
    max_price_per_hour_credits: float = 0.0  # 0 = no limit

    # Preferences
    prefer_gpu: str = ""                 # Specific GPU preference (e.g., "RTX 4090")
    prefer_low_latency: bool = True      # Prioritize closest/fastest node
    prefer_cheap: bool = False           # Prioritize cheapest


class WorkloadRouter:
    """
    Routes workloads to optimal node(s) based on requirements, pricing,
    reputation, and available capacity.
    """

    def __init__(self, registry: ResourceRegistry):
        self.registry = registry
        self._route_history: List[dict] = []  # Track routing decisions for analytics

    def route(self, spec: WorkloadSpec) -> RouteDecision:
        """
        Find the best node(s) for a workload.

        For single workloads: returns the highest-scored single node.
        For parallel workloads: returns N nodes for splitting.
        """
        max_plancks = int(spec.max_price_per_hour_credits * PLANCKS_PER_CREDIT) if spec.max_price_per_hour_credits > 0 else 0

        candidates = self.registry.find_nodes_for_workload(
            min_vram_mb=spec.min_vram_mb,
            min_model_params_b=spec.min_model_params_b,
            need_gpu=spec.need_gpu,
            max_price_per_hour_plancks=max_plancks,
        )

        if not candidates:
            return RouteDecision(
                success=False,
                reason="No nodes available matching requirements",
            )

        # Apply GPU preference filter
        if spec.prefer_gpu:
            preferred = [n for n in candidates if spec.prefer_gpu.lower() in n.gpu_name.lower()]
            if preferred:
                candidates = preferred

        # Score each candidate
        scored = [(self._score_node(n, spec), n) for n in candidates]
        scored.sort(key=lambda x: x[0], reverse=True)

        if spec.parallelizable and spec.chunk_count > 1:
            # Parallel routing: pick top N nodes
            n_nodes = min(spec.chunk_count, len(scored))
            selected = [node for _, node in scored[:n_nodes]]
            est_cost = sum(
                n.price_per_hour_credits * (spec.estimated_duration_s / 3600)
                for n in selected
            )
            decision = RouteDecision(
                success=True,
                primary_node=selected[0],
                all_nodes=selected,
                reason=f"Parallel split across {n_nodes} nodes",
                estimated_cost_credits=est_cost,
                is_parallel=True,
                chunks=n_nodes,
            )
        else:
            # Single node routing
            best = scored[0][1]
            est_cost = best.price_per_hour_credits * (spec.estimated_duration_s / 3600)
            decision = RouteDecision(
                success=True,
                primary_node=best,
                all_nodes=[best],
                reason=f"Routed to {best.gpu_name} ({best.node_id[:8]})",
                estimated_cost_credits=est_cost,
            )

        # Track for analytics
        self._route_history.append({
            "timestamp": time.time(),
            "spec_type": spec.workload_type,
            "candidates": len(candidates),
            "selected": len(decision.all_nodes),
            "parallel": decision.is_parallel,
            "est_cost": decision.estimated_cost_credits,
        })
        # Keep last 1000 routes
        if len(self._route_history) > 1000:
            self._route_history = self._route_history[-1000:]

        return decision

    def route_inference(
        self,
        model_params_b: float = 7.0,
        max_price: float = 0.0,
    ) -> RouteDecision:
        """Convenience: route a single AI inference request."""
        return self.route(WorkloadSpec(
            min_model_params_b=model_params_b,
            need_gpu=model_params_b > 3.0,
            workload_type="inference",
            estimated_duration_s=10.0,
            max_price_per_hour_credits=max_price,
        ))

    def route_embedding_batch(
        self,
        doc_count: int,
        chunks: int = 4,
        max_price: float = 0.0,
    ) -> RouteDecision:
        """Convenience: route a parallelizable embedding job."""
        return self.route(WorkloadSpec(
            workload_type="embedding",
            estimated_duration_s=doc_count * 0.01,  # ~10ms per doc estimate
            parallelizable=True,
            chunk_count=chunks,
            max_price_per_hour_credits=max_price,
        ))

    def route_finetune(
        self,
        model_params_b: float = 3.0,
        estimated_hours: float = 1.0,
        max_price: float = 0.0,
    ) -> RouteDecision:
        """Convenience: route a fine-tuning job."""
        return self.route(WorkloadSpec(
            min_model_params_b=model_params_b,
            min_vram_mb=int(model_params_b * 2048),  # ~2GB VRAM per 1B params for QLoRA
            need_gpu=True,
            workload_type="finetune",
            estimated_duration_s=estimated_hours * 3600,
            max_price_per_hour_credits=max_price,
        ))

    def _score_node(self, node: ResourceListing, spec: WorkloadSpec) -> float:
        """
        Score a node for a given workload. Higher = better match.

        Components (all 0.0–1.0, weighted):
          - Reputation (30%): track record of reliable service
          - Free capacity (25%): prefer nodes with lots of headroom
          - Price efficiency (25%): cheaper is better (inverted)
          - Capability match (20%): better hardware scores higher
        """
        score = 0.0

        # Reputation (0-1, already normalized)
        rep_score = node.reputation
        score += 0.30 * rep_score

        # Free capacity
        cap_score = node.free_capacity_pct / 100.0
        score += 0.25 * cap_score

        # Price efficiency (invert: cheaper = higher score)
        if node.price_per_hour_plancks > 0:
            # Normalize against 10 CR/hour as "expensive" baseline
            max_price = 10 * PLANCKS_PER_CREDIT
            price_score = max(0.0, 1.0 - (node.price_per_hour_plancks / max_price))
        else:
            price_score = 1.0  # Free compute = max score
        score += 0.25 * price_score

        # Capability match (more VRAM/bigger model = better for demanding workloads)
        if spec.min_model_params_b > 0:
            overhead = node.capabilities.max_model_params_b / max(spec.min_model_params_b, 0.1)
            cap_match = min(1.0, overhead / 2.0)  # 2x overhead = perfect score
        elif node.gpu_vram_mb > 0:
            cap_match = min(1.0, node.gpu_vram_mb / 24576)  # 24GB = perfect
        else:
            cap_match = 0.3  # CPU-only gets baseline
        score += 0.20 * cap_match

        # Preference adjustments
        if spec.prefer_cheap:
            score += 0.10 * price_score
        if spec.prefer_low_latency:
            score += 0.05 * cap_score  # More free capacity ≈ lower queue time

        return round(score, 4)

    def get_routing_stats(self) -> dict:
        """Analytics about routing decisions."""
        if not self._route_history:
            return {"total_routes": 0}

        total = len(self._route_history)
        parallel = sum(1 for r in self._route_history if r["parallel"])
        avg_candidates = sum(r["candidates"] for r in self._route_history) / total
        avg_cost = sum(r["est_cost"] for r in self._route_history) / total

        return {
            "total_routes": total,
            "parallel_routes": parallel,
            "single_routes": total - parallel,
            "avg_candidates_per_route": round(avg_candidates, 1),
            "avg_estimated_cost_credits": round(avg_cost, 4),
        }
