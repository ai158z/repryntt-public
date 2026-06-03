"""
Merkle Tree — Cryptographic transaction verification for SAIGE blockchain.

Enables light clients to verify a transaction is in a block WITHOUT
downloading the entire block. Critical for 1M+ node scale where most
devices (robots, phones, IoT) can't store the full chain.

How it works:
    Block has 1000 transactions → Merkle root is 32 bytes
    To prove tx #547 is in the block, you only need ~10 hashes (log₂ 1000)
    instead of downloading all 1000 transactions.

    Light client stores: block headers (tiny) + Merkle proofs (tiny)
    Full node stores: entire blockchain
"""

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple


def _hash(data: str) -> str:
    """SHA3-256 hash (32 bytes hex = 64 chars). Faster than SHA3-512 for Merkle."""
    return hashlib.sha3_256(data.encode()).hexdigest()


def _hash_pair(left: str, right: str) -> str:
    """Hash two child nodes together to produce parent."""
    return _hash(left + right)


def compute_merkle_root(tx_hashes: List[str]) -> str:
    """
    Compute the Merkle root of a list of transaction hashes.

    Args:
        tx_hashes: List of hex transaction hashes.

    Returns:
        Hex string of the Merkle root. Empty string if no transactions.
    """
    if not tx_hashes:
        return _hash("empty")

    # Work on a copy
    level = list(tx_hashes)

    while len(level) > 1:
        next_level = []
        for i in range(0, len(level), 2):
            left = level[i]
            # If odd number of nodes, duplicate the last one
            right = level[i + 1] if i + 1 < len(level) else level[i]
            next_level.append(_hash_pair(left, right))
        level = next_level

    return level[0]


def compute_merkle_proof(tx_hashes: List[str], target_index: int) -> List[Tuple[str, str]]:
    """
    Compute a Merkle proof for a transaction at `target_index`.

    Returns:
        List of (hash, side) tuples where side is "left" or "right",
        representing the sibling hashes needed to reconstruct the root.
    """
    if not tx_hashes or target_index >= len(tx_hashes):
        return []

    proof = []
    level = list(tx_hashes)
    idx = target_index

    while len(level) > 1:
        next_level = []
        for i in range(0, len(level), 2):
            left = level[i]
            right = level[i + 1] if i + 1 < len(level) else level[i]

            if i == idx or i + 1 == idx:
                # This pair contains our target
                if idx % 2 == 0:
                    # Target is on the left, sibling is on the right
                    proof.append((right, "right"))
                else:
                    # Target is on the right, sibling is on the left
                    proof.append((left, "left"))

            next_level.append(_hash_pair(left, right))

        idx = idx // 2
        level = next_level

    return proof


def verify_merkle_proof(
    tx_hash: str,
    proof: List[Tuple[str, str]],
    expected_root: str,
) -> bool:
    """
    Verify that a transaction hash belongs to a Merkle tree with the given root.

    Args:
        tx_hash: The transaction hash to verify.
        proof: List of (sibling_hash, side) from compute_merkle_proof.
        expected_root: The Merkle root from the block header.

    Returns:
        True if the proof is valid.
    """
    current = tx_hash

    for sibling_hash, side in proof:
        if side == "left":
            current = _hash_pair(sibling_hash, current)
        else:
            current = _hash_pair(current, sibling_hash)

    return current == expected_root


class BlockHeader:
    """
    Lightweight block header for header-first sync and light clients.

    A full block can be megabytes (thousands of transactions).
    A header is ~200 bytes — stores just enough to verify chain integrity
    and enable Merkle proofs for individual transactions.
    """

    __slots__ = (
        "index", "previous_hash", "timestamp", "merkle_root",
        "miner_address", "tx_count", "hash",
    )

    def __init__(
        self,
        index: int,
        previous_hash: str,
        timestamp: float,
        merkle_root: str,
        miner_address: str,
        tx_count: int,
    ):
        self.index = index
        self.previous_hash = previous_hash
        self.timestamp = timestamp
        self.merkle_root = merkle_root
        self.miner_address = miner_address
        self.tx_count = tx_count
        self.hash = self._calculate_hash()

    def _calculate_hash(self) -> str:
        header_data = (
            f"{self.index}:{self.previous_hash}:{self.timestamp}:"
            f"{self.merkle_root}:{self.miner_address}:{self.tx_count}"
        )
        return hashlib.sha3_256(header_data.encode()).hexdigest()

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "previous_hash": self.previous_hash,
            "timestamp": self.timestamp,
            "merkle_root": self.merkle_root,
            "miner_address": self.miner_address,
            "tx_count": self.tx_count,
            "hash": self.hash,
        }

    @staticmethod
    def from_dict(d: dict) -> "BlockHeader":
        h = BlockHeader(
            index=d["index"],
            previous_hash=d["previous_hash"],
            timestamp=d["timestamp"],
            merkle_root=d["merkle_root"],
            miner_address=d["miner_address"],
            tx_count=d["tx_count"],
        )
        h.hash = d.get("hash", h._calculate_hash())
        return h

    @staticmethod
    def from_block(block) -> "BlockHeader":
        """Create header from a full Block object."""
        tx_hashes = [tx.tx_hash for tx in block.transactions]
        merkle_root = compute_merkle_root(tx_hashes)
        return BlockHeader(
            index=block.index,
            previous_hash=block.previous_hash,
            timestamp=block.timestamp,
            merkle_root=merkle_root,
            miner_address=block.miner_address,
            tx_count=len(block.transactions),
        )

    def __repr__(self):
        return f"<BlockHeader #{self.index} hash={self.hash[:16]}... txs={self.tx_count}>"
