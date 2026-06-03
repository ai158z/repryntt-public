"""
Fee-priority mempool for repryntt blockchain.

Replaces flat FIFO transaction pool with a fee-aware priority queue.
Higher-fee transactions get included first when blocks are tight.

Block size limits enforced here — not in the node itself — so the
node just asks "give me a block's worth of transactions" and gets
the optimal set.
"""

import heapq
import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("fee_mempool")

# ── Constants ────────────────────────────────────────────────────
MAX_BLOCK_BYTES = 1_048_576        # 1 MB max block payload
MAX_BLOCK_TXS = 500                # Hard cap on tx count per block
MIN_FEE_PLANCKS = 1000             # 0.00001 CR — anti-spam floor
MAX_MEMPOOL_SIZE = 50_000          # Evict lowest-fee txs above this
TX_EXPIRY_SECONDS = 3600 * 24      # 24 hours — reject stale txs

# Fee-free transaction types (system operations)
FEE_EXEMPT_TYPES = frozenset({
    "reward", "genesis", "workload_completion", "faucet",
    "stake", "stake_withdraw"
})


class MempoolEntry:
    """Wraps a transaction with fee metadata for priority ordering."""

    __slots__ = ("tx", "fee_plancks", "fee_per_byte", "added_at", "size_bytes")

    def __init__(self, tx, fee_plancks: int, size_bytes: int):
        self.tx = tx
        self.fee_plancks = fee_plancks
        self.size_bytes = size_bytes
        self.fee_per_byte = fee_plancks / max(size_bytes, 1)
        self.added_at = time.time()

    def __lt__(self, other):
        # Higher fee_per_byte = higher priority (min-heap, so negate)
        return self.fee_per_byte > other.fee_per_byte


class FeeMempool:
    """
    Priority mempool ordered by fee-per-byte.

    Usage:
        pool = FeeMempool()
        pool.add_transaction(tx)
        txs, total_fees = pool.select_for_block()
        pool.remove_confirmed(tx_hashes)
    """

    def __init__(
        self,
        max_block_bytes: int = MAX_BLOCK_BYTES,
        max_block_txs: int = MAX_BLOCK_TXS,
        max_pool_size: int = MAX_MEMPOOL_SIZE,
    ):
        self.max_block_bytes = max_block_bytes
        self.max_block_txs = max_block_txs
        self.max_pool_size = max_pool_size

        self._heap: List[MempoolEntry] = []
        self._by_hash: Dict[str, MempoolEntry] = {}
        self._lock = threading.Lock()

    # ── Add / Remove ────────────────────────────────────────────

    def add_transaction(self, tx, fee_plancks: int = 0) -> bool:
        """
        Add a transaction to the mempool.
        Returns False if rejected (duplicate, too low fee, expired).
        """
        tx_hash = getattr(tx, "tx_hash", None)
        if not tx_hash:
            return False

        tx_type = getattr(tx, "tx_type", "transfer")

        # Fee-exempt types always accepted
        exempt = tx_type in FEE_EXEMPT_TYPES

        if not exempt and fee_plancks < MIN_FEE_PLANCKS:
            logger.debug(f"Rejected tx {tx_hash[:12]}...: fee {fee_plancks} < min {MIN_FEE_PLANCKS}")
            return False

        # Estimate serialized size
        size_bytes = self._estimate_tx_size(tx)

        with self._lock:
            if tx_hash in self._by_hash:
                return False  # duplicate

            entry = MempoolEntry(tx, fee_plancks, size_bytes)
            heapq.heappush(self._heap, entry)
            self._by_hash[tx_hash] = entry

            # Evict lowest-priority if pool is full
            if len(self._by_hash) > self.max_pool_size:
                self._evict_lowest()

        return True

    def remove_confirmed(self, tx_hashes: List[str]):
        """Remove transactions that were included in a block."""
        with self._lock:
            for h in tx_hashes:
                self._by_hash.pop(h, None)
            # Rebuild heap without removed entries
            self._heap = [e for e in self._heap if getattr(e.tx, "tx_hash", None) in self._by_hash]
            heapq.heapify(self._heap)

    # ── Block Selection ─────────────────────────────────────────

    def select_for_block(self) -> Tuple[List, int]:
        """
        Select the optimal set of transactions for the next block.
        Returns (transactions, total_fee_plancks).

        Greedy knapsack: take highest fee-per-byte txs until block is full.
        """
        with self._lock:
            # Work on a sorted copy
            candidates = sorted(self._by_hash.values())  # highest fee_per_byte first

        selected = []
        total_bytes = 0
        total_fees = 0
        now = time.time()

        for entry in candidates:
            # Skip expired transactions
            if now - entry.added_at > TX_EXPIRY_SECONDS:
                continue

            if len(selected) >= self.max_block_txs:
                break

            if total_bytes + entry.size_bytes > self.max_block_bytes:
                continue  # Skip this tx but try smaller ones

            selected.append(entry.tx)
            total_bytes += entry.size_bytes
            total_fees += entry.fee_plancks

        return selected, total_fees

    # ── Query ───────────────────────────────────────────────────

    def size(self) -> int:
        return len(self._by_hash)

    def contains(self, tx_hash: str) -> bool:
        return tx_hash in self._by_hash

    def get_stats(self) -> dict:
        """Return mempool statistics."""
        with self._lock:
            if not self._by_hash:
                return {
                    "size": 0,
                    "total_bytes": 0,
                    "min_fee_per_byte": 0,
                    "max_fee_per_byte": 0,
                    "median_fee_per_byte": 0,
                }
            entries = list(self._by_hash.values())
        fees = sorted(e.fee_per_byte for e in entries)
        total_bytes = sum(e.size_bytes for e in entries)
        return {
            "size": len(entries),
            "total_bytes": total_bytes,
            "min_fee_per_byte": round(fees[0], 4),
            "max_fee_per_byte": round(fees[-1], 4),
            "median_fee_per_byte": round(fees[len(fees) // 2], 4),
        }

    def purge_expired(self):
        """Remove transactions older than TX_EXPIRY_SECONDS."""
        now = time.time()
        with self._lock:
            expired = [
                h for h, e in self._by_hash.items()
                if now - e.added_at > TX_EXPIRY_SECONDS
            ]
            for h in expired:
                self._by_hash.pop(h, None)
            if expired:
                self._heap = [e for e in self._heap if getattr(e.tx, "tx_hash", None) in self._by_hash]
                heapq.heapify(self._heap)
                logger.info(f"Purged {len(expired)} expired transactions from mempool")

    # ── Internal ────────────────────────────────────────────────

    def _estimate_tx_size(self, tx) -> int:
        """Estimate serialized size of a transaction in bytes."""
        try:
            import json
            return len(json.dumps(tx.to_dict(), default=str).encode())
        except Exception:
            return 256  # conservative default

    def _evict_lowest(self):
        """Remove the lowest-fee-per-byte transaction (already holding lock)."""
        # Rebuild sorted list and drop the tail
        entries = sorted(self._by_hash.values())
        if entries:
            worst = entries[-1]
            worst_hash = getattr(worst.tx, "tx_hash", None)
            if worst_hash:
                self._by_hash.pop(worst_hash, None)
            self._heap = [e for e in self._heap if getattr(e.tx, "tx_hash", None) in self._by_hash]
            heapq.heapify(self._heap)
