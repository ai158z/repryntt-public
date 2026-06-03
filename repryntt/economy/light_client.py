"""
Light Client Protocol for repryntt blockchain.

A light client stores only block headers and Merkle proofs for its
own transactions. It can verify any transaction was included in the
chain without downloading full blocks.

Memory footprint at 1M blocks:
    Full node:  ~200 GB (all blocks + state)
    Light client: ~200 MB (headers only) + a few KB per own tx

This is how a resource-constrained robot (2GB RAM) participates in
the economy — it can verify payments, check balances, and submit
transactions without storing the entire chain.

Requires: merkle.py (Merkle proofs), protocol.py (handshake),
          header_sync.py (header download)
"""

import logging
import time
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("light_client")


@dataclass
class SPVProof:
    """Simplified Payment Verification proof for a single transaction."""
    tx_hash: str
    block_index: int
    block_hash: str
    merkle_proof: List[Tuple[str, str]]  # [(hash, side), ...] side = "left"|"right"
    merkle_root: str
    timestamp: float
    confirmations: int = 0

    def to_dict(self) -> dict:
        return {
            "tx_hash": self.tx_hash,
            "block_index": self.block_index,
            "block_hash": self.block_hash,
            "merkle_proof": self.merkle_proof,
            "merkle_root": self.merkle_root,
            "timestamp": self.timestamp,
            "confirmations": self.confirmations,
        }

    @staticmethod
    def from_dict(d: dict) -> "SPVProof":
        return SPVProof(
            tx_hash=d["tx_hash"],
            block_index=d["block_index"],
            block_hash=d["block_hash"],
            merkle_proof=[(h, s) for h, s in d["merkle_proof"]],
            merkle_root=d["merkle_root"],
            timestamp=d.get("timestamp", 0),
            confirmations=d.get("confirmations", 0),
        )


class LightClient:
    """
    Minimal blockchain client that stores only headers + own tx proofs.

    Capabilities:
        - Verify any transaction was included in the chain (SPV)
        - Track balance via header-chain + Merkle proofs
        - Submit transactions to full nodes
        - Subscribe to new blocks matching watched addresses

    Cannot:
        - Mine blocks or validate full block contents
        - Serve full blocks to other nodes
        - Run smart contracts locally
    """

    def __init__(
        self,
        wallet_address: str,
        send_message_fn: Callable,
        verify_merkle_fn: Callable,
    ):
        self.wallet_address = wallet_address
        self.send_message = send_message_fn
        self.verify_merkle = verify_merkle_fn

        # State
        self.headers: List[dict] = []          # All block headers (lightweight)
        self.proofs: Dict[str, SPVProof] = {}  # tx_hash → SPV proof
        self.watched_addresses: Set[str] = {wallet_address}
        self.balance_cache: Dict[str, int] = {}
        self.connected_peers: List[tuple] = []

        self._lock = threading.Lock()
        self._callbacks: Dict[str, List[Callable]] = {
            "new_header": [],
            "tx_confirmed": [],
            "balance_changed": [],
        }

    # ── Header Management ───────────────────────────────────────

    def add_header(self, header: dict) -> bool:
        """Add a new block header to the chain."""
        with self._lock:
            index = header.get("index", -1)

            # Verify it links to our chain
            if self.headers:
                if header.get("previous_hash") != self.headers[-1].get("hash"):
                    logger.warning(f"Header {index} doesn't link to our chain")
                    return False
                if index != len(self.headers):
                    logger.warning(f"Header {index} out of sequence (expected {len(self.headers)})")
                    return False

            self.headers.append(header)

        # Notify callbacks
        for cb in self._callbacks["new_header"]:
            try:
                cb(header)
            except Exception:
                pass

        return True

    @property
    def chain_height(self) -> int:
        return len(self.headers) - 1 if self.headers else -1

    # ── SPV Verification ────────────────────────────────────────

    def verify_transaction(self, tx_hash: str, proof: SPVProof) -> bool:
        """
        Verify a transaction was included in the blockchain using SPV.

        Checks:
            1. Merkle proof is valid against the block's merkle_root
            2. Block header exists in our header chain
            3. Block header's merkle_root matches the proof
        """
        # Check we have the header
        if proof.block_index >= len(self.headers):
            logger.warning(f"Don't have header for block {proof.block_index}")
            return False

        header = self.headers[proof.block_index]

        # Check merkle root matches header
        if proof.merkle_root != header.get("merkle_root"):
            logger.warning(
                f"Merkle root mismatch: proof={proof.merkle_root[:16]}... "
                f"header={header.get('merkle_root', '')[:16]}..."
            )
            return False

        # Verify the Merkle proof itself
        if not self.verify_merkle(tx_hash, proof.merkle_proof, proof.merkle_root):
            logger.warning(f"Merkle proof invalid for tx {tx_hash[:16]}...")
            return False

        # Calculate confirmations
        proof.confirmations = self.chain_height - proof.block_index + 1

        logger.info(
            f"SPV verified: tx {tx_hash[:12]}... in block {proof.block_index} "
            f"({proof.confirmations} confirmations)"
        )
        return True

    def store_proof(self, proof: SPVProof):
        """Store a verified SPV proof for later reference."""
        with self._lock:
            self.proofs[proof.tx_hash] = proof

    def get_proof(self, tx_hash: str) -> Optional[SPVProof]:
        """Retrieve a stored proof."""
        return self.proofs.get(tx_hash)

    # ── Balance Queries ─────────────────────────────────────────

    def request_balance(self, address: str = None):
        """Request balance from a full node peer."""
        addr = address or self.wallet_address
        if not self.connected_peers:
            logger.warning("No connected peers to query balance")
            return

        msg = {
            "type": "get_balance",
            "address": addr,
        }
        self.send_message(self.connected_peers[0], msg)

    def handle_balance_response(self, response: dict):
        """Process balance response from full node."""
        address = response.get("address", "")
        balance = response.get("balance", 0)
        old_balance = self.balance_cache.get(address, 0)
        self.balance_cache[address] = balance

        if balance != old_balance:
            for cb in self._callbacks["balance_changed"]:
                try:
                    cb(address, old_balance, balance)
                except Exception:
                    pass

    # ── Transaction Submission ──────────────────────────────────

    def submit_transaction(self, tx_dict: dict) -> bool:
        """Submit a signed transaction to a full node for inclusion."""
        if not self.connected_peers:
            logger.warning("No connected peers to submit transaction")
            return False

        msg = {
            "type": "submit_tx",
            "transaction": tx_dict,
        }

        # Try multiple peers
        for peer in self.connected_peers[:3]:
            try:
                self.send_message(peer, msg)
                logger.info(f"Submitted tx to {peer[0]}:{peer[1]}")
                return True
            except Exception as e:
                logger.warning(f"Failed to submit tx to {peer}: {e}")

        return False

    # ── Proof Requests ──────────────────────────────────────────

    def request_tx_proof(self, tx_hash: str):
        """Request a Merkle proof for a transaction from a full node."""
        if not self.connected_peers:
            return

        msg = {
            "type": "get_tx_proof",
            "tx_hash": tx_hash,
        }
        self.send_message(self.connected_peers[0], msg)

    def handle_tx_proof(self, response: dict):
        """Process a received Merkle proof."""
        proof = SPVProof.from_dict(response.get("proof", {}))
        if self.verify_transaction(proof.tx_hash, proof):
            self.store_proof(proof)
            for cb in self._callbacks["tx_confirmed"]:
                try:
                    cb(proof)
                except Exception:
                    pass
        else:
            logger.warning(f"Received invalid proof for tx {proof.tx_hash[:12]}...")

    # ── Watch Addresses ─────────────────────────────────────────

    def watch_address(self, address: str):
        """Add an address to watch for incoming/outgoing transactions."""
        self.watched_addresses.add(address)

    def unwatch_address(self, address: str):
        """Stop watching an address."""
        self.watched_addresses.discard(address)
        if address == self.wallet_address:
            logger.warning("Cannot unwatch own wallet address")
            self.watched_addresses.add(self.wallet_address)

    # ── Event System ────────────────────────────────────────────

    def on(self, event: str, callback: Callable):
        """Register a callback for an event."""
        if event in self._callbacks:
            self._callbacks[event].append(callback)

    # ── State ───────────────────────────────────────────────────

    def get_stats(self) -> dict:
        return {
            "wallet": self.wallet_address,
            "chain_height": self.chain_height,
            "headers_stored": len(self.headers),
            "proofs_stored": len(self.proofs),
            "watched_addresses": len(self.watched_addresses),
            "connected_peers": len(self.connected_peers),
            "cached_balance": self.balance_cache.get(self.wallet_address, 0),
        }
