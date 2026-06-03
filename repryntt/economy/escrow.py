"""
repryntt.economy.escrow — Escrow Contracts for Compute Purchases

Holds buyer's payment in escrow until work is verified complete.
Handles: time-based reservations, per-inference payments, auto-refund on failure.

States: PENDING → ACTIVE → COMPLETED | REFUNDED | DISPUTED
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

PLANCKS_PER_CREDIT = 100_000_000
DAO_FEE_PCT = 10  # 10% of all compute payments go to DAO treasury


class EscrowState(str, Enum):
    PENDING = "pending"        # Created, awaiting provider acceptance
    ACTIVE = "active"          # Provider accepted, work in progress
    COMPLETED = "completed"    # Work done, payment released
    REFUNDED = "refunded"      # Failed or timed out, buyer refunded
    DISPUTED = "disputed"      # Conflict, needs resolution


@dataclass
class EscrowContract:
    """A single escrow between a buyer and a compute provider."""
    contract_id: str
    buyer_address: str
    provider_address: str
    provider_node_id: str

    # Payment
    total_plancks: int               # Total locked in escrow
    dao_fee_plancks: int = 0         # 10% for DAO
    provider_payout_plancks: int = 0 # 90% for provider

    # Contract terms
    contract_type: str = "reservation"  # "reservation" (hourly) or "inference" (per-request)
    hours_reserved: float = 0.0         # For reservation type
    max_inferences: int = 0             # For inference type
    inferences_used: int = 0

    # Workload tracking
    workload_keys: List[str] = field(default_factory=list)  # Completed workload keys

    # State
    state: str = "pending"
    created_at: float = field(default_factory=time.time)
    activated_at: float = 0.0
    expires_at: float = 0.0          # Auto-refund deadline
    completed_at: float = 0.0

    # Verification
    buyer_confirmed: bool = False     # Buyer confirms work was satisfactory
    auto_release_after: float = 3600  # Auto-release 1 hour after completion if buyer doesn't dispute

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "EscrowContract":
        return EscrowContract(**d)


class EscrowManager:
    """
    Manages escrow contracts for compute purchases.

    Flow:
    1. Buyer creates escrow → funds locked from buyer balance
    2. Provider accepts → state ACTIVE, timer starts
    3. Work happens (tracked by workload keys or inference count)
    4. Provider marks complete → state COMPLETED
    5. After confirmation period: 90% to provider, 10% to DAO
    6. If timeout/failure: full refund to buyer (minus gas)
    """

    def __init__(self):
        self._contracts: Dict[str, EscrowContract] = {}
        self._lock = threading.Lock()
        self._by_buyer: Dict[str, List[str]] = {}     # address → [contract_ids]
        self._by_provider: Dict[str, List[str]] = {}   # address → [contract_ids]

    # ── Create ───────────────────────────────────────────────────────────

    def create_reservation_escrow(
        self,
        buyer_address: str,
        provider_address: str,
        provider_node_id: str,
        hours: float,
        price_per_hour_plancks: int,
        balances: Dict[str, int],
    ) -> Optional[EscrowContract]:
        """
        Create an escrow for a time-based compute reservation.
        Locks funds from buyer's balance immediately.
        """
        total = int(hours * price_per_hour_plancks)
        if total <= 0:
            return None

        buyer_bal = balances.get(buyer_address, 0)
        if buyer_bal < total:
            logger.warning(f"Escrow failed: {buyer_address} has {buyer_bal} plancks, needs {total}")
            return None

        contract_id = hashlib.sha256(
            f"{buyer_address}:{provider_address}:{time.time()}:{total}".encode()
        ).hexdigest()[:16]

        dao_fee = total * DAO_FEE_PCT // 100
        provider_payout = total - dao_fee

        escrow = EscrowContract(
            contract_id=contract_id,
            buyer_address=buyer_address,
            provider_address=provider_address,
            provider_node_id=provider_node_id,
            total_plancks=total,
            dao_fee_plancks=dao_fee,
            provider_payout_plancks=provider_payout,
            contract_type="reservation",
            hours_reserved=hours,
            expires_at=time.time() + (hours * 3600) + 300,  # +5 min buffer
        )

        # Lock funds
        balances[buyer_address] -= total

        with self._lock:
            self._contracts[contract_id] = escrow
            self._by_buyer.setdefault(buyer_address, []).append(contract_id)
            self._by_provider.setdefault(provider_address, []).append(contract_id)

        logger.info(f"Escrow {contract_id}: {buyer_address} → {provider_address}, "
                     f"{total / PLANCKS_PER_CREDIT:.2f} CR for {hours}h")
        return escrow

    def create_inference_escrow(
        self,
        buyer_address: str,
        provider_address: str,
        provider_node_id: str,
        max_inferences: int,
        price_per_inference_plancks: int,
        balances: Dict[str, int],
    ) -> Optional[EscrowContract]:
        """
        Create an escrow for a batch of inference requests.
        Locks funds for all inferences upfront.
        """
        total = max_inferences * price_per_inference_plancks
        if total <= 0:
            return None

        buyer_bal = balances.get(buyer_address, 0)
        if buyer_bal < total:
            logger.warning(f"Escrow failed: {buyer_address} insufficient balance")
            return None

        contract_id = hashlib.sha256(
            f"{buyer_address}:{provider_address}:inf:{time.time()}".encode()
        ).hexdigest()[:16]

        dao_fee = total * DAO_FEE_PCT // 100
        provider_payout = total - dao_fee

        escrow = EscrowContract(
            contract_id=contract_id,
            buyer_address=buyer_address,
            provider_address=provider_address,
            provider_node_id=provider_node_id,
            total_plancks=total,
            dao_fee_plancks=dao_fee,
            provider_payout_plancks=provider_payout,
            contract_type="inference",
            max_inferences=max_inferences,
            expires_at=time.time() + 86400,  # 24h expiry for inference batches
        )

        balances[buyer_address] -= total

        with self._lock:
            self._contracts[contract_id] = escrow
            self._by_buyer.setdefault(buyer_address, []).append(contract_id)
            self._by_provider.setdefault(provider_address, []).append(contract_id)

        logger.info(f"Escrow {contract_id}: {max_inferences} inferences, "
                     f"{total / PLANCKS_PER_CREDIT:.4f} CR")
        return escrow

    # ── Lifecycle ────────────────────────────────────────────────────────

    def accept_escrow(self, contract_id: str, provider_address: str) -> bool:
        """Provider accepts the escrow contract."""
        with self._lock:
            escrow = self._contracts.get(contract_id)
            if not escrow:
                return False
            if escrow.provider_address != provider_address:
                return False
            if escrow.state != EscrowState.PENDING:
                return False
            escrow.state = EscrowState.ACTIVE
            escrow.activated_at = time.time()
            if escrow.contract_type == "reservation":
                escrow.expires_at = time.time() + (escrow.hours_reserved * 3600) + 300
        logger.info(f"Escrow {contract_id}: ACTIVE")
        return True

    def record_inference(self, contract_id: str, workload_key: str = "") -> bool:
        """Record a completed inference under an escrow contract."""
        with self._lock:
            escrow = self._contracts.get(contract_id)
            if not escrow or escrow.state != EscrowState.ACTIVE:
                return False
            if escrow.contract_type == "inference" and escrow.inferences_used >= escrow.max_inferences:
                return False
            escrow.inferences_used += 1
            if workload_key:
                escrow.workload_keys.append(workload_key)
            # Auto-complete inference contracts when quota reached
            if (escrow.contract_type == "inference"
                    and escrow.inferences_used >= escrow.max_inferences):
                escrow.state = EscrowState.COMPLETED
                escrow.completed_at = time.time()
        return True

    def complete_escrow(
        self,
        contract_id: str,
        balances: Dict[str, int],
    ) -> bool:
        """
        Complete escrow and distribute funds.
        90% to provider, 10% to DAO.
        """
        with self._lock:
            escrow = self._contracts.get(contract_id)
            if not escrow:
                return False
            if escrow.state not in (EscrowState.ACTIVE, EscrowState.COMPLETED):
                return False

            escrow.state = EscrowState.COMPLETED
            escrow.completed_at = time.time()

            # Release funds INSIDE lock to prevent double-payment race condition
            balances[escrow.provider_address] = balances.get(escrow.provider_address, 0) + escrow.provider_payout_plancks
            balances["DAO"] = balances.get("DAO", 0) + escrow.dao_fee_plancks

        logger.info(f"Escrow {contract_id}: COMPLETED — "
                     f"{escrow.provider_payout_plancks / PLANCKS_PER_CREDIT:.4f} CR → provider, "
                     f"{escrow.dao_fee_plancks / PLANCKS_PER_CREDIT:.4f} CR → DAO")
        return True

    def refund_escrow(
        self,
        contract_id: str,
        balances: Dict[str, int],
    ) -> bool:
        """Refund buyer when escrow fails, times out, or is cancelled."""
        with self._lock:
            escrow = self._contracts.get(contract_id)
            if not escrow:
                return False
            if escrow.state == EscrowState.COMPLETED:
                return False  # Already paid out
            if escrow.state == EscrowState.REFUNDED:
                return False  # Already refunded

            escrow.state = EscrowState.REFUNDED

            # Full refund to buyer INSIDE lock to prevent double-refund race condition
            balances[escrow.buyer_address] = balances.get(escrow.buyer_address, 0) + escrow.total_plancks

        logger.info(f"Escrow {contract_id}: REFUNDED — "
                     f"{escrow.total_plancks / PLANCKS_PER_CREDIT:.4f} CR → {escrow.buyer_address}")
        return True

    def check_expirations(self, balances: Dict[str, int]) -> List[str]:
        """Check all active escrows for expiration. Auto-refund expired ones."""
        now = time.time()
        refunded = []
        with self._lock:
            expired = [
                eid for eid, e in self._contracts.items()
                if e.state in (EscrowState.PENDING, EscrowState.ACTIVE)
                and e.expires_at > 0 and now > e.expires_at
            ]

        for eid in expired:
            if self.refund_escrow(eid, balances):
                refunded.append(eid)
                logger.info(f"Escrow {eid}: auto-refunded (expired)")

        return refunded

    # ── Queries ──────────────────────────────────────────────────────────

    def get_contract(self, contract_id: str) -> Optional[EscrowContract]:
        with self._lock:
            return self._contracts.get(contract_id)

    def get_buyer_contracts(self, address: str) -> List[EscrowContract]:
        with self._lock:
            ids = self._by_buyer.get(address, [])
            return [self._contracts[i] for i in ids if i in self._contracts]

    def get_provider_contracts(self, address: str) -> List[EscrowContract]:
        with self._lock:
            ids = self._by_provider.get(address, [])
            return [self._contracts[i] for i in ids if i in self._contracts]

    def get_active_contracts(self) -> List[EscrowContract]:
        with self._lock:
            return [e for e in self._contracts.values()
                    if e.state in (EscrowState.PENDING, EscrowState.ACTIVE)]

    def get_stats(self) -> dict:
        with self._lock:
            contracts = list(self._contracts.values())
        total_locked = sum(e.total_plancks for e in contracts
                          if e.state in (EscrowState.PENDING, EscrowState.ACTIVE))
        total_paid = sum(e.provider_payout_plancks for e in contracts
                        if e.state == EscrowState.COMPLETED)
        total_dao = sum(e.dao_fee_plancks for e in contracts
                       if e.state == EscrowState.COMPLETED)
        return {
            "total_contracts": len(contracts),
            "active_contracts": len([e for e in contracts if e.state == EscrowState.ACTIVE]),
            "completed_contracts": len([e for e in contracts if e.state == EscrowState.COMPLETED]),
            "total_locked_credits": total_locked / PLANCKS_PER_CREDIT,
            "total_paid_to_providers_credits": total_paid / PLANCKS_PER_CREDIT,
            "total_dao_fees_credits": total_dao / PLANCKS_PER_CREDIT,
        }
