"""
repryntt.economy.compute_marketplace — Decentralized Compute Marketplace

The public API for buying and selling compute on the Repryntt network.
Connects the resource registry (what's available), escrow (payment safety),
and workload contracts (actual compute execution) into a unified marketplace.

Users can:
  - Browse available compute providers
  - Purchase compute time (hourly reservation) or inference batches
  - Track their active compute contracts
  - View network-wide compute statistics

Providers can:
  - List their GPU/CPU for sale
  - Set pricing and availability
  - Accept/reject compute contracts
  - Earn credits for serving workloads
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

from repryntt.economy.resource_registry import (
    ResourceRegistry, ResourceListing, NetworkStats, PLANCKS_PER_CREDIT,
)
from repryntt.economy.escrow import EscrowManager, EscrowContract, EscrowState

logger = logging.getLogger(__name__)


@dataclass
class PurchaseResult:
    """Result of a compute purchase attempt."""
    success: bool
    contract_id: str = ""
    message: str = ""
    provider_node_id: str = ""
    provider_address: str = ""
    total_cost_credits: float = 0.0
    escrow: Optional[EscrowContract] = None


@dataclass
class MarketplaceListing:
    """A provider's listing as seen by buyers."""
    node_id: str
    address: str
    gpu_name: str
    gpu_vram_mb: int
    cpu_cores: int
    ram_mb: int
    price_per_hour_credits: float
    price_per_inference_credits: float
    max_model_params_b: float
    free_capacity_pct: float
    reputation: float
    uptime_pct: float
    capabilities: dict
    platform: str
    arch: str

    def to_dict(self) -> dict:
        return asdict(self)


class ComputeMarketplace:
    """
    Unified compute marketplace — the main interface for buying/selling compute.

    Architecture:
        ResourceRegistry  → knows what hardware exists
        EscrowManager     → holds payments safely
        WorkloadContract  → tracks actual workload execution
        ComputeMarketplace → ties them all together
    """

    def __init__(self):
        self.registry = ResourceRegistry()
        self.escrow = EscrowManager()
        self._blockchain = None
        self._lock = threading.Lock()
        self._expiry_thread: Optional[threading.Thread] = None
        self._running = False

    # ── Setup ────────────────────────────────────────────────────────────

    def attach_blockchain(self, blockchain) -> None:
        """Attach the blockchain node for balance/reputation access."""
        self._blockchain = blockchain
        self.registry.attach_blockchain(blockchain)

    def attach_gossip(self, gossip) -> None:
        """Attach gossip protocol for resource announcements."""
        self.registry.attach_gossip(gossip)

    def set_identity(self, node_id: str, address: str) -> None:
        """Set this node's identity for the registry."""
        self.registry.node_id = node_id
        self.registry.address = address

    def start(self) -> None:
        """Start the marketplace (registry announcements + escrow monitoring)."""
        self.registry.start()
        self._running = True
        self._expiry_thread = threading.Thread(
            target=self._expiry_loop, daemon=True, name="escrow-expiry"
        )
        self._expiry_thread.start()
        logger.info("ComputeMarketplace started")

    def stop(self) -> None:
        self._running = False
        self.registry.stop()

    def _expiry_loop(self) -> None:
        """Periodically check for expired escrow contracts."""
        while self._running:
            try:
                if self._blockchain:
                    refunded = self.escrow.check_expirations(self._blockchain.balances)
                    if refunded:
                        logger.info(f"Auto-refunded {len(refunded)} expired escrows")
            except Exception as e:
                logger.warning(f"Escrow expiry check error: {e}")
            time.sleep(60)

    # ── Buyer API ────────────────────────────────────────────────────────

    def browse_providers(
        self,
        min_vram_mb: int = 0,
        min_model_params_b: float = 0.0,
        need_gpu: bool = False,
        max_price_per_hour: float = 0.0,
    ) -> List[MarketplaceListing]:
        """Browse available compute providers with optional filters."""
        max_plancks = int(max_price_per_hour * PLANCKS_PER_CREDIT) if max_price_per_hour > 0 else 0

        nodes = self.registry.find_nodes_for_workload(
            min_vram_mb=min_vram_mb,
            min_model_params_b=min_model_params_b,
            need_gpu=need_gpu,
            max_price_per_hour_plancks=max_plancks,
        )

        listings = []
        for n in nodes:
            listings.append(MarketplaceListing(
                node_id=n.node_id,
                address=n.address,
                gpu_name=n.gpu_name,
                gpu_vram_mb=n.gpu_vram_mb,
                cpu_cores=n.cpu_cores,
                ram_mb=n.ram_mb,
                price_per_hour_credits=n.price_per_hour_credits,
                price_per_inference_credits=n.price_per_inference_plancks / PLANCKS_PER_CREDIT,
                max_model_params_b=n.capabilities.max_model_params_b,
                free_capacity_pct=n.free_capacity_pct,
                reputation=n.reputation,
                uptime_pct=n.uptime_pct,
                capabilities=asdict(n.capabilities),
                platform=n.platform,
                arch=n.arch,
            ))
        return listings

    def purchase_reservation(
        self,
        buyer_address: str,
        provider_node_id: str,
        hours: float,
    ) -> PurchaseResult:
        """
        Purchase a time-based compute reservation from a specific provider.
        Funds are locked in escrow until work completes.
        """
        if not self._blockchain:
            return PurchaseResult(success=False, message="Blockchain not connected")

        provider = self.registry.get_node(provider_node_id)
        if not provider:
            return PurchaseResult(success=False, message="Provider not found")
        if not provider.available:
            return PurchaseResult(success=False, message="Provider not available")
        if provider.active_workloads >= provider.max_concurrent:
            return PurchaseResult(success=False, message="Provider at capacity")

        escrow = self.escrow.create_reservation_escrow(
            buyer_address=buyer_address,
            provider_address=provider.address,
            provider_node_id=provider_node_id,
            hours=hours,
            price_per_hour_plancks=provider.price_per_hour_plancks,
            balances=self._blockchain.balances,
        )

        if not escrow:
            return PurchaseResult(success=False, message="Insufficient balance or invalid parameters")

        total_cr = escrow.total_plancks / PLANCKS_PER_CREDIT
        return PurchaseResult(
            success=True,
            contract_id=escrow.contract_id,
            message=f"Reserved {hours}h of compute from {provider.gpu_name} for {total_cr:.2f} CR",
            provider_node_id=provider_node_id,
            provider_address=provider.address,
            total_cost_credits=total_cr,
            escrow=escrow,
        )

    def purchase_inference_batch(
        self,
        buyer_address: str,
        provider_node_id: str,
        count: int,
    ) -> PurchaseResult:
        """
        Purchase a batch of inference requests from a specific provider.
        """
        if not self._blockchain:
            return PurchaseResult(success=False, message="Blockchain not connected")

        provider = self.registry.get_node(provider_node_id)
        if not provider:
            return PurchaseResult(success=False, message="Provider not found")
        if not provider.available:
            return PurchaseResult(success=False, message="Provider not available")

        escrow = self.escrow.create_inference_escrow(
            buyer_address=buyer_address,
            provider_address=provider.address,
            provider_node_id=provider_node_id,
            max_inferences=count,
            price_per_inference_plancks=provider.price_per_inference_plancks,
            balances=self._blockchain.balances,
        )

        if not escrow:
            return PurchaseResult(success=False, message="Insufficient balance or invalid parameters")

        total_cr = escrow.total_plancks / PLANCKS_PER_CREDIT
        return PurchaseResult(
            success=True,
            contract_id=escrow.contract_id,
            message=f"Purchased {count} inferences for {total_cr:.4f} CR",
            provider_node_id=provider_node_id,
            provider_address=provider.address,
            total_cost_credits=total_cr,
            escrow=escrow,
        )

    def get_my_purchases(self, address: str) -> List[dict]:
        """Get all compute contracts where this address is the buyer."""
        contracts = self.escrow.get_buyer_contracts(address)
        return [c.to_dict() for c in contracts]

    # ── Provider API ─────────────────────────────────────────────────────

    def accept_contract(self, contract_id: str, provider_address: str) -> bool:
        """Provider accepts a pending compute contract."""
        return self.escrow.accept_escrow(contract_id, provider_address)

    def complete_contract(self, contract_id: str) -> bool:
        """Provider marks a compute contract as complete. Triggers payout."""
        if not self._blockchain:
            return False
        return self.escrow.complete_escrow(contract_id, self._blockchain.balances)

    def get_my_provider_contracts(self, address: str) -> List[dict]:
        """Get all compute contracts where this address is the provider."""
        contracts = self.escrow.get_provider_contracts(address)
        return [c.to_dict() for c in contracts]

    def set_my_price(self, price_per_hour: float, price_per_inference: float = 0.01) -> None:
        """Set this node's compute pricing."""
        self.registry.set_price(price_per_hour, price_per_inference)

    def set_my_availability(self, available: bool) -> None:
        """Toggle this node's compute availability."""
        self.registry.set_available(available)

    # ── Network Stats ────────────────────────────────────────────────────

    def get_network_stats(self) -> dict:
        """Get aggregated network compute statistics."""
        net = self.registry.get_network_stats()
        escrow_stats = self.escrow.get_stats()
        return {
            "network": net.to_dict(),
            "escrow": escrow_stats,
        }

    def get_dashboard_data(self) -> dict:
        """
        All data needed for a compute marketplace dashboard.
        Returns network stats, provider listings, and active contracts.
        """
        stats = self.get_network_stats()
        providers = [l.to_dict() for l in self.browse_providers()]
        active = [c.to_dict() for c in self.escrow.get_active_contracts()]

        return {
            "timestamp": time.time(),
            "network_stats": stats["network"],
            "escrow_stats": stats["escrow"],
            "providers": providers,
            "active_contracts": active,
            "total_providers": len(providers),
        }
