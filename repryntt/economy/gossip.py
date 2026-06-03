"""
Gossip Protocol Layer — Epidemic message propagation for repryntt blockchain.

Replaces O(n²) direct broadcast with O(k·log n) gossip relay.
Each node maintains ~8 peers and forwards messages probabilistically.
Messages propagate through the entire network in O(log n) hops.

Designed for 1M+ node networks.

Architecture:
    Node A creates block → gossips to 6 random peers
    → Each peer gossips to 6 more peers (minus sender)
    → After ~20 hops, 1M nodes have the block (~3 seconds at LAN speed)

Anti-spam:
    - Message deduplication via seen_messages bloom filter
    - TTL (time-to-live) prevents infinite propagation
    - Per-peer rate limiting
"""

import hashlib
import json
import logging
import os
import random
import socket
import struct
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from repryntt.economy.safe_serialize import pack as safe_pack, unpack as safe_unpack

logger = logging.getLogger("gossip")

# ═══════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════

FANOUT = 6              # Number of peers to forward each message to
MAX_TTL = 20            # Maximum hops a message can travel
SEEN_EXPIRY_S = 300     # Forget seen message IDs after 5 minutes
MAX_SEEN = 100_000      # Max seen IDs to track (bloom-filter-like)
HEARTBEAT_INTERVAL = 30 # Seconds between heartbeat gossip
MAX_PEERS = 32          # Maximum peers to maintain
MIN_PEERS = 4           # Try to maintain at least this many peers
PEER_TIMEOUT_S = 120    # Consider peer dead after this silence
GOSSIP_MAX_INBOUND = 64 # Max simultaneous gossip connections
GOSSIP_MAX_PER_IP = 5   # Max simultaneous gossip connections per IP


@dataclass
class PeerInfo:
    """Tracked state for a connected peer."""
    host: str
    port: int
    last_seen: float = field(default_factory=time.time)
    latency_ms: float = 0.0
    messages_relayed: int = 0
    reputation: float = 0.5  # 0.0–1.0
    protocol_version: int = 1
    node_id: str = ""

    @property
    def addr(self) -> Tuple[str, int]:
        return (self.host, self.port)

    def is_alive(self) -> bool:
        return (time.time() - self.last_seen) < PEER_TIMEOUT_S


@dataclass
class GossipMessage:
    """A message propagated through the gossip network."""
    msg_id: str           # SHA256 of payload — deduplication key
    msg_type: str         # "block", "tx", "peer_list", "heartbeat"
    payload: dict         # The actual data
    ttl: int = MAX_TTL    # Remaining hops
    origin: str = ""      # Node ID of original sender
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "msg_id": self.msg_id,
            "msg_type": self.msg_type,
            "payload": self.payload,
            "ttl": self.ttl,
            "origin": self.origin,
            "timestamp": self.timestamp,
        }

    @staticmethod
    def from_dict(d: dict) -> "GossipMessage":
        return GossipMessage(
            msg_id=d["msg_id"],
            msg_type=d["msg_type"],
            payload=d["payload"],
            ttl=d.get("ttl", MAX_TTL),
            origin=d.get("origin", ""),
            timestamp=d.get("timestamp", time.time()),
        )


def _message_id(payload: dict) -> str:
    """Deterministic message ID from payload content."""
    raw = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()[:32]


class GossipProtocol:
    """
    Gossip-based message propagation layer.

    Usage:
        gp = GossipProtocol(node_id="abc123", port=5001)
        gp.on_message("block", handle_new_block)
        gp.on_message("tx", handle_new_tx)
        gp.start()

        # To broadcast:
        gp.gossip("block", block_dict)
    """

    def __init__(
        self,
        node_id: str,
        host: str = "0.0.0.0",
        port: int = 5001,
        fanout: int = FANOUT,
    ):
        self.node_id = node_id
        self.host = host
        self.port = port
        self.fanout = fanout

        # Peer management
        self.peers: Dict[Tuple[str, int], PeerInfo] = {}
        self._peer_lock = threading.Lock()

        # Message deduplication
        self._seen: Dict[str, float] = {}  # msg_id → timestamp
        self._seen_lock = threading.Lock()

        # Message handlers: msg_type → callback(payload)
        self._handlers: Dict[str, List[Callable]] = defaultdict(list)

        # Stats
        self.stats = {
            "messages_sent": 0,
            "messages_received": 0,
            "messages_relayed": 0,
            "messages_dropped_dup": 0,
            "messages_dropped_ttl": 0,
        }

        self._running = False

    # ─── Public API ──────────────────────────────────

    def on_message(self, msg_type: str, callback: Callable):
        """Register a handler for a message type."""
        self._handlers[msg_type].append(callback)

    def gossip(self, msg_type: str, payload: dict):
        """Broadcast a message to the gossip network."""
        msg_id = _message_id(payload)
        msg = GossipMessage(
            msg_id=msg_id,
            msg_type=msg_type,
            payload=payload,
            ttl=MAX_TTL,
            origin=self.node_id,
        )
        self._mark_seen(msg_id)
        self._relay(msg, exclude=None)
        self.stats["messages_sent"] += 1

    def add_peer(self, host: str, port: int, node_id: str = ""):
        """Add a peer to the gossip network."""
        addr = (host, port)
        with self._peer_lock:
            if addr not in self.peers and len(self.peers) < MAX_PEERS:
                self.peers[addr] = PeerInfo(
                    host=host, port=port, node_id=node_id
                )
                logger.info(f"Added gossip peer: {host}:{port}")

    def remove_peer(self, host: str, port: int):
        """Remove a peer."""
        addr = (host, port)
        with self._peer_lock:
            self.peers.pop(addr, None)

    def get_peers(self) -> List[PeerInfo]:
        """Get list of active peers."""
        with self._peer_lock:
            return [p for p in self.peers.values() if p.is_alive()]

    def start(self):
        """Start the gossip protocol (listener + background threads)."""
        self._running = True
        threading.Thread(target=self._listen, daemon=True).start()
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        threading.Thread(target=self._cleanup_loop, daemon=True).start()
        logger.info(f"Gossip protocol started: {self.host}:{self.port} (fanout={self.fanout})")

    def stop(self):
        """Stop the gossip protocol."""
        self._running = False

    # ─── Network Layer ───────────────────────────────

    def _listen(self):
        """TCP listener for incoming gossip messages."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.settimeout(2.0)
        server.bind((self.host, self.port))
        server.listen(64)
        logger.info(f"Gossip listener on {self.host}:{self.port}")

        # Per-IP connection tracking for gossip
        _gossip_conns: Dict[str, int] = {}
        _gossip_total = 0
        _gossip_lock = threading.Lock()

        while self._running:
            try:
                client, addr = server.accept()
                ip = addr[0]

                # Connection gating
                with _gossip_lock:
                    if _gossip_total >= GOSSIP_MAX_INBOUND:
                        client.close()
                        continue
                    if _gossip_conns.get(ip, 0) >= GOSSIP_MAX_PER_IP:
                        client.close()
                        continue
                    _gossip_conns[ip] = _gossip_conns.get(ip, 0) + 1
                    _gossip_total += 1

                def _tracked_handle(cl, ad, _lock=_gossip_lock, _conns=_gossip_conns):
                    try:
                        self._handle_incoming(cl, ad)
                    finally:
                        with _lock:
                            _ip = ad[0]
                            _conns[_ip] = max(0, _conns.get(_ip, 1) - 1)
                            if _conns[_ip] == 0:
                                _conns.pop(_ip, None)
                            nonlocal _gossip_total
                            _gossip_total = max(0, _gossip_total - 1)

                threading.Thread(
                    target=_tracked_handle, args=(client, addr), daemon=True
                ).start()
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    logger.error(f"Gossip listen error: {e}")

    def _handle_incoming(self, client: socket.socket, addr: tuple):
        """Handle an incoming gossip message."""
        try:
            client.settimeout(10)
            # Read length-prefixed message
            length_bytes = client.recv(4)
            if len(length_bytes) < 4:
                return
            length = struct.unpack("!I", length_bytes)[0]
            if length > 4 * 1024 * 1024:  # 4 MB max
                return

            data = b""
            while len(data) < length:
                chunk = client.recv(min(length - len(data), 65536))
                if not chunk:
                    break
                data += chunk

            msg_dict = safe_unpack(data)
            if not isinstance(msg_dict, dict) or "msg_id" not in msg_dict:
                return

            msg = GossipMessage.from_dict(msg_dict)
            self._process_incoming(msg, sender_addr=addr)
            self.stats["messages_received"] += 1

        except Exception as e:
            logger.debug(f"Gossip recv error from {addr}: {e}")
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _process_incoming(self, msg: GossipMessage, sender_addr: tuple):
        """Process a received gossip message."""
        # Dedup check
        if self._is_seen(msg.msg_id):
            self.stats["messages_dropped_dup"] += 1
            return

        # TTL check
        if msg.ttl <= 0:
            self.stats["messages_dropped_ttl"] += 1
            return

        # Mark seen
        self._mark_seen(msg.msg_id)

        # Update peer last_seen
        with self._peer_lock:
            peer = self.peers.get(sender_addr)
            if peer:
                peer.last_seen = time.time()
                peer.messages_relayed += 1

        # Dispatch to handlers
        for handler in self._handlers.get(msg.msg_type, []):
            try:
                handler(msg.payload)
            except Exception as e:
                logger.error(f"Gossip handler error ({msg.msg_type}): {e}")

        # Relay to other peers (decrement TTL)
        msg.ttl -= 1
        if msg.ttl > 0:
            self._relay(msg, exclude=sender_addr)
            self.stats["messages_relayed"] += 1

    def _relay(self, msg: GossipMessage, exclude: Optional[tuple] = None):
        """Forward message to a random subset of peers."""
        with self._peer_lock:
            candidates = [
                p for p in self.peers.values()
                if p.is_alive() and p.addr != exclude
            ]

        # Select random subset (fanout)
        targets = random.sample(candidates, min(self.fanout, len(candidates)))

        for peer in targets:
            threading.Thread(
                target=self._send_to_peer, args=(peer, msg), daemon=True
            ).start()

    def _send_to_peer(self, peer: PeerInfo, msg: GossipMessage):
        """Send a gossip message to a specific peer."""
        try:
            data = safe_pack(msg.to_dict())
            size_prefix = struct.pack("!I", len(data))
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect(peer.addr)
                s.sendall(size_prefix + data)
        except Exception as e:
            logger.debug(f"Failed to gossip to {peer.host}:{peer.port}: {e}")
            with self._peer_lock:
                peer.reputation = max(0.0, peer.reputation - 0.05)

    # ─── Heartbeat & Peer Exchange ───────────────────

    def _heartbeat_loop(self):
        """Periodically announce presence and exchange peer lists."""
        while self._running:
            try:
                time.sleep(HEARTBEAT_INTERVAL)
                self._send_heartbeat()
                self._prune_dead_peers()
                self._request_more_peers()
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")

    def _send_heartbeat(self):
        """Gossip a heartbeat with our peer list (peer exchange)."""
        with self._peer_lock:
            known_peers = [
                {"host": p.host, "port": p.port, "node_id": p.node_id}
                for p in self.peers.values() if p.is_alive()
            ]
        payload = {
            "node_id": self.node_id,
            "host": self.host,
            "port": self.port,
            "peers": known_peers[:20],  # Share up to 20 peers
            "timestamp": time.time(),
        }
        self.gossip("heartbeat", payload)

    def _prune_dead_peers(self):
        """Remove peers that haven't been seen recently."""
        with self._peer_lock:
            dead = [addr for addr, p in self.peers.items() if not p.is_alive()]
            for addr in dead:
                del self.peers[addr]
                logger.info(f"Pruned dead peer: {addr[0]}:{addr[1]}")

    def _request_more_peers(self):
        """If we have too few peers, try to get more from heartbeats."""
        # Peer exchange happens via heartbeat payloads — when we receive
        # a heartbeat, we add any new peers from its peer list.
        # This is handled in the heartbeat handler registered by the node.
        pass

    # ─── Deduplication ────────────────────────────────

    def _mark_seen(self, msg_id: str):
        with self._seen_lock:
            self._seen[msg_id] = time.time()

    def _is_seen(self, msg_id: str) -> bool:
        with self._seen_lock:
            return msg_id in self._seen

    def _cleanup_loop(self):
        """Periodically clean up expired seen message IDs."""
        while self._running:
            time.sleep(60)
            now = time.time()
            with self._seen_lock:
                expired = [
                    mid for mid, ts in self._seen.items()
                    if now - ts > SEEN_EXPIRY_S
                ]
                for mid in expired:
                    del self._seen[mid]
                # Hard cap
                if len(self._seen) > MAX_SEEN:
                    oldest = sorted(self._seen.items(), key=lambda x: x[1])
                    for mid, _ in oldest[: len(self._seen) - MAX_SEEN]:
                        del self._seen[mid]

    # ─── Stats ────────────────────────────────────────

    def get_stats(self) -> dict:
        return {
            **self.stats,
            "peers_active": len(self.get_peers()),
            "peers_total": len(self.peers),
            "seen_cache_size": len(self._seen),
        }
