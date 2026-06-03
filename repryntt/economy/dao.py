#!/usr/bin/env python3
"""
Planetary DAO — Decentralized Autonomous Organization for the Robot Economy
Manages treasury, token allocation proposals, and governance voting.

Part of the Reprynt 2040 robot economy blockchain.
"""

import threading
import hashlib
import json
import os
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List


class PlanetaryDAO:
    """
    On-chain DAO governing the robot economy treasury.

    Treasury address is "dao" in the node balances dict.
    Supports:
      - Direct token allocation (admin / block-reward funded)
      - Proposal creation, voting, and execution
      - Quorum and approval-threshold governance
    """

    # Token constants (must match qnode2 / transaction.py)
    PLANCKS_PER_CR = 100_000_000  # 1 CR = 100 000 000 plancks

    # Governance defaults
    DEFAULT_QUORUM = 3            # Minimum votes for a proposal to pass
    DEFAULT_APPROVAL_THRESHOLD = 0.51  # >51 % approval required
    DEFAULT_VOTING_PERIOD_S = 86400    # 24 h voting window

    def __init__(self, storage_path: str = "robot_economy_data"):
        self.plancks_per_max_planck = self.PLANCKS_PER_CR  # backward compat alias
        self.allocations: Dict[str, int] = {}
        self.proposals: Dict[str, Dict[str, Any]] = {}
        self.proposal_counter = 0
        self.lock = threading.Lock()
        self.storage_path = storage_path
        self._state_file = os.path.join(storage_path, "dao_state.json")

        # Try to load persisted state
        self._load_state()

        print(f"[{datetime.now()}] Planetary DAO initialized  "
              f"(proposals={len(self.proposals)}, "
              f"cumulative_allocations={sum(self.allocations.values())} plancks)")

    # ------------------------------------------------------------------
    # Core allocation (called from qnode2 block rewards & manager)
    # ------------------------------------------------------------------

    def allocate_tokens(
        self,
        machine_address: str,
        amount_plancks: int,
        purpose: str,
        balances: dict,
    ) -> Dict[str, Any]:
        """
        Transfer *amount_plancks* from DAO treasury to *machine_address*.

        Parameters
        ----------
        machine_address : str
            Recipient wallet address.
        amount_plancks : int
            Amount in plancks (1 CR = 100 000 000 plancks).
        purpose : str
            Human-readable reason (max 200 chars).
        balances : dict
            The node's shared balances dict — mutated in-place.

        Returns
        -------
        dict  {"success": True/False, ...}
        """
        with self.lock:
            try:
                # Validate inputs
                if not machine_address or not isinstance(machine_address, str):
                    return {"success": False, "error": "Invalid machine address"}

                if amount_plancks <= 0:
                    return {"success": False, "error": "Amount must be positive"}

                if not isinstance(purpose, str) or len(purpose) > 200:
                    print(f"[{datetime.now()}] Allocation failed: Invalid purpose")
                    return {"success": False, "error": "Invalid purpose"}

                dao_balance = balances.get("dao", 0)
                if dao_balance < amount_plancks:
                    print(f"[{datetime.now()}] Allocation failed: Insufficient DAO funds "
                          f"(need {amount_plancks}, have {dao_balance})")
                    return {"success": False, "error": "Insufficient DAO funds"}

                # Execute transfer
                balances["dao"] = dao_balance - amount_plancks
                balances[machine_address] = balances.get(machine_address, 0) + amount_plancks

                # Track cumulative allocations per address
                self.allocations[machine_address] = (
                    self.allocations.get(machine_address, 0) + amount_plancks
                )

                cr_amount = amount_plancks / self.PLANCKS_PER_CR
                print(f"[{datetime.now()}] Allocated {cr_amount:.8f} CR "
                      f"to {machine_address[:16]}... for {purpose}")

                self._save_state()
                return {
                    "success": True,
                    "amount_plancks": amount_plancks,
                    "recipient": machine_address,
                    "purpose": purpose,
                    "dao_remaining_plancks": balances["dao"],
                }

            except Exception as e:
                print(f"[{datetime.now()}] Allocation error: {e}")
                return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Proposal / Governance system
    # ------------------------------------------------------------------

    def create_proposal(
        self,
        proposer: str,
        title: str,
        description: str,
        amount_plancks: int,
        recipient: str,
        voting_period_s: int = None,
    ) -> Dict[str, Any]:
        """Submit a funding proposal to the DAO."""
        with self.lock:
            try:
                if not title or len(title) > 120:
                    return {"success": False, "error": "Title required (max 120 chars)"}
                if amount_plancks <= 0:
                    return {"success": False, "error": "Amount must be positive"}

                self.proposal_counter += 1
                proposal_id = hashlib.sha3_256(
                    f"{self.proposal_counter}:{proposer}:{title}:{datetime.utcnow().isoformat()}".encode()
                ).hexdigest()[:16]

                voting_period = voting_period_s or self.DEFAULT_VOTING_PERIOD_S
                now = datetime.utcnow()

                proposal = {
                    "id": proposal_id,
                    "proposer": proposer,
                    "title": title,
                    "description": description,
                    "amount_plancks": amount_plancks,
                    "recipient": recipient,
                    "created_at": now.isoformat(),
                    "voting_deadline": (now + timedelta(seconds=voting_period)).isoformat(),
                    "votes_for": 0,
                    "votes_against": 0,
                    "voters": {},  # address -> "for" | "against"
                    "status": "active",  # active | passed | rejected | executed
                }

                self.proposals[proposal_id] = proposal
                self._save_state()

                print(f"[{datetime.now()}] Proposal created: {proposal_id} — {title}")
                return {"success": True, "proposal_id": proposal_id, "proposal": proposal}

            except Exception as e:
                return {"success": False, "error": str(e)}

    def vote_on_proposal(
        self,
        proposal_id: str,
        voter_address: str,
        vote: str,  # "for" | "against"
        stake_weight: int = 1,  # Token-weighted: pass voter's stake for weighted voting
    ) -> Dict[str, Any]:
        """Cast a vote on an active proposal. Optionally token-weighted by stake."""
        with self.lock:
            try:
                proposal = self.proposals.get(proposal_id)
                if not proposal:
                    return {"success": False, "error": "Proposal not found"}

                if proposal["status"] != "active":
                    return {"success": False, "error": f"Proposal is {proposal['status']}"}

                # Check deadline
                deadline = datetime.fromisoformat(proposal["voting_deadline"])
                if datetime.utcnow() > deadline:
                    proposal["status"] = "expired"
                    self._save_state()
                    return {"success": False, "error": "Voting period has ended"}

                if voter_address in proposal["voters"]:
                    return {"success": False, "error": "Already voted"}

                if vote not in ("for", "against"):
                    return {"success": False, "error": "Vote must be 'for' or 'against'"}

                weight = max(1, stake_weight)  # Minimum weight of 1
                proposal["voters"][voter_address] = {"vote": vote, "weight": weight}
                if vote == "for":
                    proposal["votes_for"] += weight
                else:
                    proposal["votes_against"] += weight

                self._save_state()
                print(f"[{datetime.now()}] Vote on {proposal_id}: {voter_address[:12]}... voted {vote} (weight={weight})")
                return {"success": True, "votes_for": proposal["votes_for"],
                        "votes_against": proposal["votes_against"]}

            except Exception as e:
                return {"success": False, "error": str(e)}

    def execute_proposal(
        self,
        proposal_id: str,
        balances: dict,
    ) -> Dict[str, Any]:
        """
        Execute a passed proposal — transfers tokens from DAO treasury.
        Automatically checks quorum + approval threshold.
        """
        with self.lock:
            try:
                proposal = self.proposals.get(proposal_id)
                if not proposal:
                    return {"success": False, "error": "Proposal not found"}

                if proposal["status"] != "active":
                    return {"success": False, "error": f"Proposal is {proposal['status']}"}

                total_votes = proposal["votes_for"] + proposal["votes_against"]
                if total_votes < self.DEFAULT_QUORUM:
                    return {"success": False, "error": f"Quorum not met ({total_votes}/{self.DEFAULT_QUORUM})"}

                approval_ratio = proposal["votes_for"] / max(total_votes, 1)
                if approval_ratio < self.DEFAULT_APPROVAL_THRESHOLD:
                    proposal["status"] = "rejected"
                    self._save_state()
                    return {"success": False, "error": f"Approval too low ({approval_ratio:.0%})"}

                # Check treasury balance
                amount = proposal["amount_plancks"]
                if balances.get("dao", 0) < amount:
                    return {"success": False, "error": "Insufficient DAO treasury funds"}

                # Execute transfer
                balances["dao"] -= amount
                recipient = proposal["recipient"]
                balances[recipient] = balances.get(recipient, 0) + amount
                self.allocations[recipient] = self.allocations.get(recipient, 0) + amount

                proposal["status"] = "executed"
                self._save_state()

                cr_amount = amount / self.PLANCKS_PER_CR
                print(f"[{datetime.now()}] Proposal {proposal_id} executed: "
                      f"{cr_amount:.8f} CR -> {recipient[:16]}...")
                return {
                    "success": True,
                    "proposal_id": proposal_id,
                    "amount_plancks": amount,
                    "recipient": recipient,
                }

            except Exception as e:
                return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_treasury_balance(self, balances: dict) -> int:
        """Return current DAO treasury balance in plancks."""
        return balances.get("dao", 0)

    def get_proposals(self, status: str = None) -> List[Dict[str, Any]]:
        """Return proposals, optionally filtered by status."""
        with self.lock:
            if status:
                return [p for p in self.proposals.values() if p["status"] == status]
            return list(self.proposals.values())

    def get_allocation_history(self, address: str = None) -> Dict[str, int]:
        """Return cumulative allocation map, or single address total."""
        with self.lock:
            if address:
                return {address: self.allocations.get(address, 0)}
            return dict(self.allocations)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_state(self):
        """Persist DAO state to disk."""
        try:
            os.makedirs(self.storage_path, exist_ok=True)
            state = {
                "proposal_counter": self.proposal_counter,
                "allocations": self.allocations,
                "proposals": self.proposals,
                "saved_at": datetime.utcnow().isoformat(),
            }
            tmp = self._state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, self._state_file)
        except Exception as e:
            print(f"[{datetime.now()}] DAO state save error: {e}")

    def _load_state(self):
        """Load DAO state from disk if available."""
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file, "r") as f:
                    state = json.load(f)
                self.proposal_counter = state.get("proposal_counter", 0)
                self.allocations = state.get("allocations", {})
                self.proposals = state.get("proposals", {})
                print(f"[{datetime.now()}] DAO state loaded from {self._state_file}")
        except Exception as e:
            print(f"[{datetime.now()}] DAO state load error: {e}")
