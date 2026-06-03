"""
Header-First Chain Sync for repryntt blockchain.

New nodes download the full header chain first (~200 bytes per block)
instead of gigabytes of full blocks. After verifying header continuity,
they selectively download block bodies they need.

Sync process:
    1. Download all headers from best peer           (fast — ~200 bytes each)
    2. Verify header chain: hash links, timestamps   (no full blocks needed)
    3. Download full blocks in batches of 500         (only what's needed)
    4. Verify block transactions against Merkle root  (from header)
    5. Apply transactions to rebuild UTXO/balance set

For light clients: stop at step 2, only download proofs for own txs.
"""

import logging
import struct
import json
import time
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Callable

logger = logging.getLogger("header_sync")

# ── Constants ────────────────────────────────────────────────────
HEADER_BATCH_SIZE = 2000      # Headers to request at once
BLOCK_BATCH_SIZE = 500        # Full blocks to request at once
SYNC_TIMEOUT = 30             # seconds per batch request
MAX_REORG_DEPTH = 100         # Don't reorg deeper than this


@dataclass
class SyncState:
    """Tracks progress of an ongoing chain sync."""
    peer: tuple                       # (host, port)
    peer_height: int                  # Height claimed by peer
    headers_downloaded: int = 0       # Headers received so far
    blocks_downloaded: int = 0        # Full blocks received so far
    verified_height: int = 0          # Highest verified header height
    status: str = "idle"              # idle, syncing_headers, syncing_blocks, done, failed
    started_at: float = 0.0
    error: str = ""


class HeaderFirstSync:
    """
    Manages header-first chain synchronization.

    Usage:
        syncer = HeaderFirstSync(
            local_height=1000,
            get_header_fn=node.get_header,
            add_block_fn=node.add_verified_block,
            send_message_fn=node.send_to_peer,
        )
        syncer.start_sync(peer=("10.0.0.5", 5000), peer_height=50000)
    """

    def __init__(
        self,
        local_height: int,
        get_header_fn: Callable,
        add_block_fn: Callable,
        send_message_fn: Callable,
    ):
        self.local_height = local_height
        self.get_header = get_header_fn
        self.add_block = add_block_fn
        self.send_message = send_message_fn

        self._state: Optional[SyncState] = None
        self._pending_headers: List[dict] = []
        self._lock = threading.Lock()

    @property
    def is_syncing(self) -> bool:
        return self._state is not None and self._state.status.startswith("syncing")

    @property
    def progress(self) -> dict:
        if not self._state:
            return {"status": "idle"}
        s = self._state
        return {
            "status": s.status,
            "peer": f"{s.peer[0]}:{s.peer[1]}",
            "peer_height": s.peer_height,
            "headers_downloaded": s.headers_downloaded,
            "blocks_downloaded": s.blocks_downloaded,
            "verified_height": s.verified_height,
            "elapsed": round(time.time() - s.started_at, 1) if s.started_at else 0,
        }

    def start_sync(self, peer: tuple, peer_height: int):
        """Begin syncing headers from a peer who reported higher chain height."""
        with self._lock:
            if self.is_syncing:
                logger.info("Already syncing, ignoring new sync request")
                return

            if peer_height <= self.local_height:
                return

            self._state = SyncState(
                peer=peer,
                peer_height=peer_height,
                status="syncing_headers",
                started_at=time.time(),
            )
            self._pending_headers = []

        logger.info(
            f"Starting header sync from {peer[0]}:{peer[1]} — "
            f"local={self.local_height}, remote={peer_height}, "
            f"gap={peer_height - self.local_height} blocks"
        )

        # Request first batch of headers
        self._request_headers(self.local_height + 1)

    def _request_headers(self, from_height: int):
        """Request a batch of headers starting at from_height."""
        msg = {
            "type": "get_headers",
            "from_height": from_height,
            "count": HEADER_BATCH_SIZE,
        }
        try:
            self.send_message(self._state.peer, msg)
        except Exception as e:
            self._fail(f"Failed to request headers: {e}")

    def _request_blocks(self, from_height: int):
        """Request a batch of full blocks starting at from_height."""
        msg = {
            "type": "get_blocks",
            "from_height": from_height,
            "count": BLOCK_BATCH_SIZE,
        }
        try:
            self.send_message(self._state.peer, msg)
        except Exception as e:
            self._fail(f"Failed to request blocks: {e}")

    # ── Message Handlers (called by the node) ───────────────────

    def handle_headers(self, headers: List[dict]):
        """Process received headers batch."""
        with self._lock:
            if not self._state or self._state.status != "syncing_headers":
                return

            if not headers:
                # No more headers — move to block sync
                logger.info(
                    f"All {self._state.headers_downloaded} headers received, "
                    f"verifying chain..."
                )
                if self._verify_header_chain():
                    self._state.status = "syncing_blocks"
                    self._request_blocks(self.local_height + 1)
                else:
                    self._fail("Header chain verification failed")
                return

            # Verify each header links to previous
            for header in headers:
                if not self._verify_single_header(header):
                    self._fail(f"Invalid header at height {header.get('index')}")
                    return
                self._pending_headers.append(header)
                self._state.headers_downloaded += 1
                self._state.verified_height = header.get("index", 0)

            # Request next batch
            next_start = self._state.verified_height + 1
            if next_start <= self._state.peer_height:
                self._request_headers(next_start)
            else:
                # All headers received
                logger.info(
                    f"Header sync complete: {self._state.headers_downloaded} headers"
                )
                self._state.status = "syncing_blocks"
                self._request_blocks(self.local_height + 1)

    def handle_blocks(self, blocks: List[dict]):
        """Process received full blocks batch."""
        with self._lock:
            if not self._state or self._state.status != "syncing_blocks":
                return

            if not blocks:
                self._state.status = "done"
                logger.info(
                    f"Sync complete: {self._state.blocks_downloaded} blocks in "
                    f"{time.time() - self._state.started_at:.1f}s"
                )
                self.local_height = self._state.verified_height
                return

            for block_dict in blocks:
                block_index = block_dict.get("index", -1)

                # Verify block matches the header we already validated
                if block_index - (self.local_height + 1) < len(self._pending_headers):
                    expected_header = self._pending_headers[block_index - (self.local_height + 1)]
                    if block_dict.get("hash") != expected_header.get("hash"):
                        self._fail(f"Block {block_index} hash doesn't match header")
                        return

                try:
                    self.add_block(block_dict)
                    self._state.blocks_downloaded += 1
                except Exception as e:
                    self._fail(f"Failed to add block {block_index}: {e}")
                    return

            # Request next batch
            next_start = self.local_height + self._state.blocks_downloaded + 1
            if next_start <= self._state.peer_height:
                self._request_blocks(next_start)
            else:
                self._state.status = "done"
                self.local_height = self._state.verified_height
                elapsed = time.time() - self._state.started_at
                logger.info(
                    f"Sync COMPLETE: {self._state.blocks_downloaded} blocks, "
                    f"{elapsed:.1f}s ({self._state.blocks_downloaded/max(elapsed,1):.0f} blocks/s)"
                )

    # ── Verification ────────────────────────────────────────────

    def _verify_single_header(self, header: dict) -> bool:
        """Verify a header links to the previous one."""
        index = header.get("index", -1)

        if index <= 0:
            return True  # Genesis

        # Check it links to our last known header
        if index == self.local_height + len(self._pending_headers) + 1:
            if self._pending_headers:
                prev = self._pending_headers[-1]
            else:
                prev = self.get_header(self.local_height)
                if prev is None:
                    return True  # Can't verify, assume OK for now

            if header.get("previous_hash") != prev.get("hash"):
                logger.warning(
                    f"Header {index} previous_hash mismatch: "
                    f"expected {prev.get('hash')[:16]}..., "
                    f"got {header.get('previous_hash', '')[:16]}..."
                )
                return False

        # Timestamp sanity: not too far in the future
        if header.get("timestamp", 0) > time.time() + 600:
            logger.warning(f"Header {index} timestamp is >10 minutes in the future")
            return False

        return True

    def _verify_header_chain(self) -> bool:
        """Verify the entire downloaded header chain is internally consistent."""
        for i in range(1, len(self._pending_headers)):
            prev = self._pending_headers[i - 1]
            curr = self._pending_headers[i]
            if curr.get("previous_hash") != prev.get("hash"):
                logger.error(
                    f"Header chain broken at index {curr.get('index')}: "
                    f"previous_hash mismatch"
                )
                return False
            if curr.get("index") != prev.get("index", 0) + 1:
                logger.error(f"Header chain gap: {prev.get('index')} → {curr.get('index')}")
                return False
        return True

    def _fail(self, reason: str):
        """Mark sync as failed."""
        if self._state:
            self._state.status = "failed"
            self._state.error = reason
        logger.error(f"Sync failed: {reason}")


# ── Message Builders (for the serving side) ──────────────────────

def build_headers_response(headers: List[dict]) -> dict:
    """Build a response to a get_headers request."""
    return {
        "type": "headers",
        "headers": headers,
        "count": len(headers),
    }


def build_blocks_response(blocks: List[dict]) -> dict:
    """Build a response to a get_blocks request."""
    return {
        "type": "blocks",
        "blocks": blocks,
        "count": len(blocks),
    }
