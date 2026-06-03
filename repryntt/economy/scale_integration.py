"""
Scale Integration — Wires gossip, Merkle, DHT, header-sync, light-client,
fee-mempool, and protocol versioning into the existing ProofOfPowerBlockchain.

Instead of rewriting qnode2.py (risky), this module extends it:
    1. Import after qnode2 classes are defined
    2. Monkey-patch the node with scale-aware methods
    3. Replace broadcast_* with gossip relay
    4. Add Merkle roots to new blocks
    5. Add new message handlers (get_headers, get_blocks, get_tx_proof, etc.)
    6. Start DHT peer discovery alongside LAN discovery

Usage in qnode2.py:
    node = ProofOfPowerBlockchain(host, port)
    from repryntt.economy.scale_integration import upgrade_node_for_scale
    upgrade_node_for_scale(node)
"""

import hashlib
import json
import logging
import os
import threading
import time
from typing import Optional, Dict, List, Any

logger = logging.getLogger("scale_integration")

# Import scale modules
from repryntt.economy.gossip import GossipProtocol
from repryntt.economy.merkle import compute_merkle_root, compute_merkle_proof, verify_merkle_proof, BlockHeader
from repryntt.economy.chain_storage import create_storage
from repryntt.economy.fee_mempool import FeeMempool, FEE_EXEMPT_TYPES
from repryntt.economy.protocol import (
    ProtocolNegotiator, PeerCapabilities, generate_node_id,
    PROTOCOL_VERSION, build_version_message, parse_version_message,
)
from repryntt.economy.header_sync import HeaderFirstSync, build_headers_response, build_blocks_response
from repryntt.economy.kademlia import KademliaDHT, DHTNode
from repryntt.economy.logging_config import blockchain_logger
from repryntt.economy.compute_marketplace import ComputeMarketplace
from repryntt.economy.workload_router import WorkloadRouter


def upgrade_node_for_scale(node, enable_gossip=True, enable_dht=True,
                           enable_header_sync=True, dht_port=5100):
    """
    Attach scale infrastructure to an existing ProofOfPowerBlockchain node.

    This is non-destructive — all original functionality continues to work.
    Scale features are added on top.
    """
    blockchain_logger.info("=" * 60)
    blockchain_logger.info("🚀 SCALE UPGRADE: Attaching production-scale infrastructure")
    blockchain_logger.info(f"   Protocol version: {PROTOCOL_VERSION}")
    blockchain_logger.info("=" * 60)

    # ── 1. Node Identity ────────────────────────────────────────
    node._node_id = generate_node_id()
    node._peer_capabilities: Dict[tuple, PeerCapabilities] = {}
    blockchain_logger.info(f"   Node ID: {node._node_id[:16]}...")

    # ── 2. Gossip Protocol ──────────────────────────────────────
    if enable_gossip:
        _attach_gossip(node)

    # ── 3. Merkle Roots in Blocks ───────────────────────────────
    _attach_merkle(node)

    # ── 4. Fee Mempool ──────────────────────────────────────────
    _attach_fee_mempool(node)

    # ── 5. Header-First Sync ────────────────────────────────────
    if enable_header_sync:
        _attach_header_sync(node)

    # ── 6. DHT Peer Discovery ──────────────────────────────────
    if enable_dht:
        _attach_dht(node, dht_port)

    # ── 7. New Message Handlers ─────────────────────────────────
    _attach_message_handlers(node)

    # ── 8. Scale Stats Endpoint ─────────────────────────────────
    node.get_scale_stats = lambda: _get_scale_stats(node)

    # ── 9. Compute Marketplace + Resource Registry ──────────────
    _attach_marketplace(node)

    blockchain_logger.info("🚀 SCALE UPGRADE COMPLETE — node is production-ready")
    blockchain_logger.info("=" * 60)


# ═══════════════════════════════════════════════════════════════
# Gossip Protocol Attachment
# ═══════════════════════════════════════════════════════════════

def _attach_gossip(node):
    """Replace O(n) broadcast with O(k·log n) gossip relay."""
    node._gossip = GossipProtocol(
        node_id=getattr(node, 'node_id', hashlib.sha256(f"{node.host}:{node.port}".encode()).hexdigest()[:16]),
        host=node.host,
        port=node.port + 1000,  # Gossip on separate port (e.g., 6001)
    )

    # Register handlers for gossip messages
    def _on_gossip_block(payload):
        """Handle block received via gossip."""
        try:
            from repryntt.economy.qnode2 import Block
            block = Block.from_dict(payload["block_data"])
            if node.validate_block(block):
                if block.index == len(node.chain):
                    with node.lock:
                        node.chain.append(block)
                        for tx in block.transactions:
                            node._apply_transaction(tx)
                        try:
                            node.save_state()
                        except Exception as e:
                            blockchain_logger.error(f"Save after gossip block failed: {e}")
                    blockchain_logger.info(f"📡 Block {block.index} received via gossip")
        except Exception as e:
            blockchain_logger.warning(f"Gossip block processing error: {e}")

    def _on_gossip_tx(payload):
        """Handle transaction received via gossip."""
        try:
            from repryntt.economy.transaction import Transaction
            tx = Transaction.from_dict(payload["transaction"])
            fee = payload.get("fee", 0)
            if hasattr(node, '_fee_mempool'):
                node._fee_mempool.add_transaction(tx, fee)
            else:
                node.tx_pool.add_transaction(tx)
        except Exception as e:
            blockchain_logger.debug(f"Gossip tx processing error: {e}")

    node._gossip.on_message("block", _on_gossip_block)
    node._gossip.on_message("transaction", _on_gossip_tx)

    # Start gossip in background
    node._gossip.start()

    # Wrap original broadcast_block to also gossip
    _original_broadcast_block = node.broadcast_block

    def _gossip_broadcast_block(block):
        """Broadcast block via both gossip and legacy TCP."""
        # Gossip (O(k·log n) — reaches all nodes efficiently)
        node._gossip.gossip("block", {"block_data": block.to_dict()})
        # Legacy TCP broadcast (for v1 peers that don't speak gossip)
        _original_broadcast_block(block)

    node.broadcast_block = _gossip_broadcast_block

    # Seed gossip with existing peers
    for peer in node.peers:
        node._gossip.add_peer(peer[0], peer[1] + 1000)

    blockchain_logger.info(f"   ✅ Gossip protocol on port {node.port + 1000}")


# ═══════════════════════════════════════════════════════════════
# Merkle Root Integration
# ═══════════════════════════════════════════════════════════════

def _attach_merkle(node):
    """Add Merkle root computation to block creation."""

    # Wrap _create_scheduled_block to add merkle_root
    _original_create_block = node._create_scheduled_block

    def _create_block_with_merkle(pending_txs, pending_completions):
        """Create block with Merkle root in proof_of_power metadata."""
        # Call original
        _original_create_block(pending_txs, pending_completions)

        # Retroactively add merkle_root to the last block
        if node.chain:
            block = node.chain[-1]
            tx_hashes = [tx.tx_hash for tx in block.transactions]
            if tx_hashes:
                merkle_root = compute_merkle_root(tx_hashes)
                block.proof_of_power["merkle_root"] = merkle_root

                # Also store a lightweight header
                header = BlockHeader.from_block(block)
                block.proof_of_power["header"] = header.to_dict()

    node._create_scheduled_block = _create_block_with_merkle

    # Add Merkle proof generation method
    def get_tx_proof(block_index: int, tx_hash: str) -> Optional[dict]:
        """Generate a Merkle proof for a transaction in a block."""
        if block_index >= len(node.chain):
            return None
        block = node.chain[block_index]
        tx_hashes = [tx.tx_hash for tx in block.transactions]
        try:
            tx_idx = tx_hashes.index(tx_hash)
        except ValueError:
            return None

        proof = compute_merkle_proof(tx_hashes, tx_idx)
        merkle_root = compute_merkle_root(tx_hashes)
        return {
            "tx_hash": tx_hash,
            "block_index": block_index,
            "block_hash": block.hash,
            "merkle_proof": proof,
            "merkle_root": merkle_root,
            "timestamp": block.timestamp,
            "confirmations": len(node.chain) - block_index,
        }

    node.get_tx_proof = get_tx_proof
    blockchain_logger.info("   ✅ Merkle roots enabled for new blocks")


# ═══════════════════════════════════════════════════════════════
# Fee Mempool
# ═══════════════════════════════════════════════════════════════

def _attach_fee_mempool(node):
    """Add fee-priority transaction selection to block creation."""
    node._fee_mempool = FeeMempool()

    # Wrap block_generation_loop's tx selection to use fee mempool
    _original_get_txs = node.tx_pool.get_transactions

    def _get_txs_with_fees(max_count=100):
        """Try fee mempool first, fall back to legacy pool."""
        if node._fee_mempool.size() > 0:
            txs, total_fees = node._fee_mempool.select_for_block()
            if txs:
                return txs
        return _original_get_txs(max_count=max_count)

    node.tx_pool.get_transactions = _get_txs_with_fees

    # Start periodic mempool cleanup
    def _mempool_cleanup():
        while True:
            time.sleep(300)
            node._fee_mempool.purge_expired()

    threading.Thread(target=_mempool_cleanup, daemon=True).start()
    blockchain_logger.info("   ✅ Fee-priority mempool active")


# ═══════════════════════════════════════════════════════════════
# Header-First Sync
# ═══════════════════════════════════════════════════════════════

def _attach_header_sync(node):
    """Enable header-first chain synchronization for new nodes."""

    def _get_header(index):
        if index < len(node.chain):
            block = node.chain[index]
            return {
                "index": block.index,
                "previous_hash": block.previous_hash,
                "timestamp": block.timestamp,
                "merkle_root": block.proof_of_power.get("merkle_root", ""),
                "miner_address": block.miner_address,
                "tx_count": len(block.transactions),
                "hash": block.hash,
            }
        return None

    def _add_verified_block(block_dict):
        from repryntt.economy.qnode2 import Block
        block = Block.from_dict(block_dict)
        with node.lock:
            node.chain.append(block)
            for tx in block.transactions:
                node._apply_transaction(tx)

    def _send_to_peer(peer, msg):
        node.broadcast_message(msg)

    node._header_sync = HeaderFirstSync(
        local_height=len(node.chain) - 1,
        get_header_fn=_get_header,
        add_block_fn=_add_verified_block,
        send_message_fn=_send_to_peer,
    )

    blockchain_logger.info("   ✅ Header-first sync enabled")


# ═══════════════════════════════════════════════════════════════
# DHT Peer Discovery
# ═══════════════════════════════════════════════════════════════

def _attach_dht(node, dht_port):
    """Start Kademlia DHT for internet-wide peer discovery."""
    node._dht = KademliaDHT(
        host=node.host,
        port=dht_port,
        node_id=None,  # Auto-generate
    )
    node._dht.start()

    # Register this node's blockchain port in the DHT
    node._dht.store(
        f"repryntt_node:{node.host}:{node.port}",
        json.dumps({
            "host": node.host,
            "port": node.port,
            "chain_height": len(node.chain),
            "version": PROTOCOL_VERSION,
        }),
    )

    # Periodic DHT peer discovery
    def _dht_discover_peers():
        while True:
            time.sleep(120)
            try:
                # Look for other repryntt nodes
                closest = node._dht.find_node(node._dht.node_id)
                for dht_node in closest:
                    # Try to find blockchain port info
                    info = node._dht.find_value(f"repryntt_node:{dht_node.host}:{dht_node.port}")
                    if info:
                        try:
                            peer_info = json.loads(info)
                            peer_host = peer_info.get("host", dht_node.host)
                            peer_port = peer_info.get("port", 5001)
                            if (peer_host, peer_port) not in node.peers:
                                node.connect_peer(peer_host, peer_port)
                        except Exception:
                            pass
            except Exception as e:
                blockchain_logger.debug(f"DHT peer discovery error: {e}")

    threading.Thread(target=_dht_discover_peers, daemon=True).start()

    # Bootstrap DHT from existing peers
    seed_nodes = []
    for peer in node.peers:
        seed_nodes.append((peer[0], dht_port))
    if seed_nodes:
        node._dht.bootstrap(seed_nodes)

    blockchain_logger.info(f"   ✅ DHT peer discovery on UDP port {dht_port}")


# ═══════════════════════════════════════════════════════════════
# New Message Handlers
# ═══════════════════════════════════════════════════════════════

def _attach_message_handlers(node):
    """Extend handle_client to support scale protocol messages."""

    _original_handle_client = node.handle_client

    def _extended_handle_client(client, addr):
        """Extended message handler with scale protocol support."""
        # We need to peek at the message to see if it's a scale message
        # But since handle_client reads the full message, we wrap the dispatch
        _original_handle_client(client, addr)

    # Instead of wrapping handle_client (complex), we add methods the node
    # can call from its existing dispatch. These are registered as node methods.

    def handle_get_headers(message):
        """Handle get_headers request from syncing peer."""
        from_height = message.get("from_height", 0)
        count = min(message.get("count", 2000), 2000)
        headers = []
        for i in range(from_height, min(from_height + count, len(node.chain))):
            block = node.chain[i]
            headers.append({
                "index": block.index,
                "previous_hash": block.previous_hash,
                "timestamp": block.timestamp,
                "merkle_root": block.proof_of_power.get("merkle_root", ""),
                "miner_address": block.miner_address,
                "tx_count": len(block.transactions),
                "hash": block.hash,
            })
        return build_headers_response(headers)

    def handle_get_blocks(message):
        """Handle get_blocks request from syncing peer."""
        from_height = message.get("from_height", 0)
        count = min(message.get("count", 500), 500)
        blocks = []
        for i in range(from_height, min(from_height + count, len(node.chain))):
            blocks.append(node.chain[i].to_dict())
        return build_blocks_response(blocks)

    def handle_get_tx_proof(message):
        """Handle Merkle proof request for a transaction."""
        tx_hash = message.get("tx_hash", "")
        # Search for the transaction in recent blocks
        for block in reversed(node.chain[-1000:]):
            for tx in block.transactions:
                if tx.tx_hash == tx_hash:
                    proof = node.get_tx_proof(block.index, tx_hash)
                    if proof:
                        return {"success": True, "proof": proof}
        return {"success": False, "error": "Transaction not found"}

    def handle_headers_response(message):
        """Handle headers response from peer during sync."""
        if hasattr(node, '_header_sync') and node._header_sync.is_syncing:
            node._header_sync.handle_headers(message.get("headers", []))

    def handle_blocks_response(message):
        """Handle blocks response from peer during sync."""
        if hasattr(node, '_header_sync') and node._header_sync.is_syncing:
            node._header_sync.handle_blocks(message.get("blocks", []))

    def handle_get_mempool_stats(message):
        """Return mempool statistics."""
        if hasattr(node, '_fee_mempool'):
            return {"success": True, "mempool": node._fee_mempool.get_stats()}
        return {"success": True, "mempool": {"size": node.tx_pool.count()}}

    # Register handlers on the node
    node._scale_handlers = {
        "get_headers": handle_get_headers,
        "get_blocks": handle_get_blocks,
        "get_tx_proof": handle_get_tx_proof,
        "headers": handle_headers_response,
        "blocks": handle_blocks_response,
        "get_mempool_stats": handle_get_mempool_stats,
    }

    blockchain_logger.info("   ✅ Scale message handlers registered")


# ═══════════════════════════════════════════════════════════════
# Compute Marketplace + Resource Registry
# ═══════════════════════════════════════════════════════════════

def _attach_marketplace(node):
    """Attach the decentralized compute marketplace to the node."""
    marketplace = ComputeMarketplace()
    marketplace.attach_blockchain(node)
    marketplace.set_identity(
        node_id=getattr(node, '_node_id', 'unknown'),
        address=getattr(node, '_node_id', 'unknown')[:40],  # Use node ID prefix as address for now
    )

    # Attach gossip if available
    if hasattr(node, '_gossip'):
        marketplace.attach_gossip(node._gossip)

    # Create workload router
    router = WorkloadRouter(marketplace.registry)

    # Store on node
    node._marketplace = marketplace
    node._workload_router = router

    # Start marketplace (begins resource announcement + escrow monitoring)
    marketplace.start()

    # Expose convenience methods on node
    node.get_marketplace = lambda: marketplace
    node.get_compute_stats = lambda: marketplace.get_dashboard_data()
    node.browse_compute = lambda **kw: [l.to_dict() for l in marketplace.browse_providers(**kw)]
    node.route_workload = lambda spec: router.route(spec)

    blockchain_logger.info("   ✅ Compute marketplace + resource registry active")


# ═══════════════════════════════════════════════════════════════
# Stats
# ═══════════════════════════════════════════════════════════════

def _get_scale_stats(node) -> dict:
    """Comprehensive scale infrastructure statistics."""
    stats = {
        "protocol_version": PROTOCOL_VERSION,
        "node_id": getattr(node, '_node_id', 'unknown')[:16] + "...",
        "chain_height": len(node.chain),
    }

    if hasattr(node, '_gossip'):
        stats["gossip"] = node._gossip.get_stats()

    if hasattr(node, '_fee_mempool'):
        stats["mempool"] = node._fee_mempool.get_stats()

    if hasattr(node, '_dht'):
        stats["dht"] = node._dht.get_stats()

    if hasattr(node, '_header_sync'):
        stats["header_sync"] = node._header_sync.progress

    if hasattr(node, '_marketplace'):
        stats["marketplace"] = node._marketplace.get_network_stats()
        stats["routing"] = node._workload_router.get_routing_stats()

    stats["peers"] = {
        "tcp_peers": len(node.peers),
        "peer_capabilities": {
            f"{p[0]}:{p[1]}": {"version": c.version, "gossip": c.gossip}
            for p, c in getattr(node, '_peer_capabilities', {}).items()
        },
    }

    return stats
