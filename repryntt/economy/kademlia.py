"""
Kademlia-style DHT Peer Discovery for repryntt blockchain.

Replaces UDP broadcast (LAN-only) with a distributed hash table
that scales to millions of nodes across the internet.

Each node has a 256-bit ID. Peers are found by XOR distance — closer
IDs in XOR space are "nearer" in the routing table. This gives
O(log n) lookups in a network of n nodes.

Routing table: 256 k-buckets, one per bit of the address space.
Each bucket holds up to K=20 peers at that XOR distance.

Key operations:
    PING     — Check if a peer is alive
    FIND_NODE — Find K closest peers to a target ID
    STORE    — Store a value at a key
    FIND_VALUE — Retrieve a stored value

Bootstrap: connect to a known seed node, then iteratively
FIND_NODE(self) to populate routing table.
"""

import hashlib
import heapq
import json
import logging
import os
import random
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("dht")

# ── Constants ────────────────────────────────────────────────────
K = 20                      # Bucket size (replication parameter)
ALPHA = 3                   # Parallel lookups
ID_BITS = 256               # Node ID size in bits
REFRESH_INTERVAL = 3600     # Refresh buckets every hour
REPLICATE_INTERVAL = 3600   # Replicate stored values every hour
EXPIRE_TIME = 86400         # Stored values expire after 24h
PING_TIMEOUT = 5            # Seconds to wait for ping response
FIND_TIMEOUT = 10           # Seconds for FIND_NODE round


def xor_distance(id1: bytes, id2: bytes) -> int:
    """XOR distance between two 256-bit node IDs."""
    return int.from_bytes(
        bytes(a ^ b for a, b in zip(id1, id2)), "big"
    )


def bit_length(n: int) -> int:
    """Number of bits needed to represent n (0-indexed bucket)."""
    return n.bit_length() if n > 0 else 0


def generate_node_id(seed: bytes = None) -> bytes:
    """Generate a 256-bit node ID."""
    if seed:
        return hashlib.sha256(seed).digest()
    return hashlib.sha256(os.urandom(32)).digest()


@dataclass
class DHTNode:
    """A node in the DHT network."""
    node_id: bytes              # 32-byte ID
    host: str
    port: int
    last_seen: float = 0.0
    failed_pings: int = 0

    @property
    def id_hex(self) -> str:
        return self.node_id.hex()[:16] + "..."

    def __hash__(self):
        return hash((self.host, self.port))


class KBucket:
    """A single k-bucket in the routing table."""

    def __init__(self, k: int = K):
        self.k = k
        self.nodes: List[DHTNode] = []
        self.last_refreshed: float = time.time()
        self._lock = threading.Lock()

    def add(self, node: DHTNode) -> Optional[DHTNode]:
        """
        Add a node to this bucket.
        
        Returns None if added/updated, or the least-recently-seen
        node if the bucket is full (caller should ping that node).
        """
        with self._lock:
            # Check if node already exists
            for i, existing in enumerate(self.nodes):
                if existing.host == node.host and existing.port == node.port:
                    # Move to end (most recently seen)
                    self.nodes.pop(i)
                    node.last_seen = time.time()
                    self.nodes.append(node)
                    return None

            if len(self.nodes) < self.k:
                node.last_seen = time.time()
                self.nodes.append(node)
                return None
            else:
                # Bucket full — return least-recently-seen for ping check
                return self.nodes[0]

    def remove(self, node: DHTNode):
        """Remove a node from this bucket."""
        with self._lock:
            self.nodes = [n for n in self.nodes if not (n.host == node.host and n.port == node.port)]

    def get_nodes(self) -> List[DHTNode]:
        """Return all nodes in this bucket."""
        with self._lock:
            return list(self.nodes)

    @property
    def size(self) -> int:
        return len(self.nodes)

    @property
    def is_full(self) -> bool:
        return len(self.nodes) >= self.k


class RoutingTable:
    """
    Kademlia routing table: 256 k-buckets indexed by XOR distance.
    
    Bucket i contains nodes whose XOR distance from us has bit_length == i.
    """

    def __init__(self, own_id: bytes, k: int = K):
        self.own_id = own_id
        self.k = k
        self.buckets = [KBucket(k) for _ in range(ID_BITS + 1)]

    def _bucket_index(self, node_id: bytes) -> int:
        """Which bucket should this node go in?"""
        dist = xor_distance(self.own_id, node_id)
        return bit_length(dist)

    def add_node(self, node: DHTNode) -> Optional[DHTNode]:
        """Add a node; returns eviction candidate if bucket full."""
        if node.node_id == self.own_id:
            return None
        idx = self._bucket_index(node.node_id)
        return self.buckets[idx].add(node)

    def remove_node(self, node: DHTNode):
        """Remove a node from the routing table."""
        idx = self._bucket_index(node.node_id)
        self.buckets[idx].remove(node)

    def find_closest(self, target_id: bytes, count: int = K) -> List[DHTNode]:
        """Find the K closest nodes to a target ID."""
        all_nodes = []
        for bucket in self.buckets:
            all_nodes.extend(bucket.get_nodes())

        # Sort by XOR distance to target
        all_nodes.sort(key=lambda n: xor_distance(n.node_id, target_id))
        return all_nodes[:count]

    @property
    def total_nodes(self) -> int:
        return sum(b.size for b in self.buckets)

    def get_stale_buckets(self) -> List[int]:
        """Return indices of buckets that haven't been refreshed recently."""
        now = time.time()
        stale = []
        for i, bucket in enumerate(self.buckets):
            if bucket.size > 0 and now - bucket.last_refreshed > REFRESH_INTERVAL:
                stale.append(i)
        return stale


class KademliaDHT:
    """
    Full Kademlia DHT implementation for peer discovery.

    Usage:
        dht = KademliaDHT(host="0.0.0.0", port=5100)
        dht.start()
        dht.bootstrap([("seed1.repryntt.network", 5100)])
        
        # Find peers near a service key
        peers = dht.find_node(target_id)
        
        # Store/retrieve data
        dht.store(key, value)
        value = dht.find_value(key)
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 5100,
                 node_id: bytes = None):
        self.host = host
        self.port = port
        self.node_id = node_id or generate_node_id()
        self.table = RoutingTable(self.node_id)

        # Local data store (key → (value, timestamp))
        self._store: Dict[str, Tuple[str, float]] = {}
        self._store_lock = threading.Lock()

        # Pending RPC responses
        self._pending: Dict[str, threading.Event] = {}
        self._responses: Dict[str, dict] = {}

        self._running = False
        self._server_thread = None
        self._maintenance_thread = None

    def start(self):
        """Start the DHT node."""
        self._running = True
        self._server_thread = threading.Thread(target=self._listen, daemon=True)
        self._server_thread.start()
        self._maintenance_thread = threading.Thread(target=self._maintenance_loop, daemon=True)
        self._maintenance_thread.start()
        logger.info(f"DHT started on {self.host}:{self.port} id={self.node_id.hex()[:16]}...")

    def stop(self):
        """Stop the DHT node."""
        self._running = False
        logger.info("DHT stopped")

    def bootstrap(self, seed_nodes: List[Tuple[str, int]]):
        """Bootstrap into the network by contacting seed nodes."""
        for host, port in seed_nodes:
            node = DHTNode(
                node_id=generate_node_id(f"{host}:{port}".encode()),
                host=host,
                port=port,
            )
            self.table.add_node(node)
            self._send_ping(node)

        # Iterative FIND_NODE(self) to populate routing table
        self.find_node(self.node_id)
        logger.info(
            f"Bootstrap complete: {self.table.total_nodes} peers in routing table"
        )

    # ── Core RPCs ───────────────────────────────────────────────

    def find_node(self, target_id: bytes) -> List[DHTNode]:
        """
        Iterative FIND_NODE: find the K closest nodes to target_id.
        
        Algorithm:
            1. Pick ALPHA closest nodes from routing table
            2. Send FIND_NODE to each in parallel
            3. Add results to routing table
            4. Repeat with new closest nodes
            5. Stop when no closer nodes found
        """
        closest = self.table.find_closest(target_id, K)
        if not closest:
            return []

        queried: Set[tuple] = set()
        best = list(closest)

        for _round in range(10):  # Max 10 rounds
            # Pick ALPHA unqueried nodes from best
            to_query = [
                n for n in best
                if (n.host, n.port) not in queried
            ][:ALPHA]

            if not to_query:
                break

            new_nodes = []
            for node in to_query:
                queried.add((node.host, node.port))
                result = self._rpc_find_node(node, target_id)
                if result:
                    for n in result:
                        self.table.add_node(n)
                        new_nodes.append(n)

            # Merge and re-sort
            all_nodes = {(n.host, n.port): n for n in best + new_nodes}
            best = sorted(
                all_nodes.values(),
                key=lambda n: xor_distance(n.node_id, target_id),
            )[:K]

        return best

    def store(self, key: str, value: str):
        """Store a key-value pair at the K closest nodes."""
        key_hash = hashlib.sha256(key.encode()).digest()
        targets = self.find_node(key_hash)

        for node in targets[:K]:
            self._rpc_store(node, key, value)

        # Also store locally
        with self._store_lock:
            self._store[key] = (value, time.time())

    def find_value(self, key: str) -> Optional[str]:
        """Look up a value by key."""
        # Check local store first
        with self._store_lock:
            if key in self._store:
                val, ts = self._store[key]
                if time.time() - ts < EXPIRE_TIME:
                    return val

        key_hash = hashlib.sha256(key.encode()).digest()
        closest = self.table.find_closest(key_hash, K)

        for node in closest:
            result = self._rpc_find_value(node, key)
            if result is not None:
                return result

        return None

    # ── RPC Implementation ──────────────────────────────────────

    def _send_rpc(self, node: DHTNode, msg: dict, timeout: float = FIND_TIMEOUT) -> Optional[dict]:
        """Send an RPC message and wait for response."""
        rpc_id = os.urandom(16).hex()
        msg["rpc_id"] = rpc_id
        msg["sender_id"] = self.node_id.hex()
        msg["sender_host"] = self.host
        msg["sender_port"] = self.port

        event = threading.Event()
        self._pending[rpc_id] = event

        try:
            data = json.dumps(msg).encode()
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.sendto(data, (node.host, node.port))

            if event.wait(timeout):
                return self._responses.pop(rpc_id, None)
            return None
        except Exception as e:
            logger.debug(f"RPC to {node.host}:{node.port} failed: {e}")
            return None
        finally:
            self._pending.pop(rpc_id, None)

    def _send_ping(self, node: DHTNode) -> bool:
        """Ping a node, return True if alive."""
        result = self._send_rpc(node, {"type": "dht_ping"}, timeout=PING_TIMEOUT)
        if result and result.get("type") == "dht_pong":
            node.last_seen = time.time()
            node.failed_pings = 0
            return True
        node.failed_pings += 1
        return False

    def _rpc_find_node(self, node: DHTNode, target_id: bytes) -> Optional[List[DHTNode]]:
        """Send FIND_NODE RPC."""
        result = self._send_rpc(node, {
            "type": "dht_find_node",
            "target_id": target_id.hex(),
        })
        if not result or result.get("type") != "dht_found_nodes":
            return None

        nodes = []
        for n in result.get("nodes", []):
            try:
                nodes.append(DHTNode(
                    node_id=bytes.fromhex(n["node_id"]),
                    host=n["host"],
                    port=n["port"],
                ))
            except Exception:
                continue
        return nodes

    def _rpc_store(self, node: DHTNode, key: str, value: str):
        """Send STORE RPC."""
        self._send_rpc(node, {
            "type": "dht_store",
            "key": key,
            "value": value,
        }, timeout=5)

    def _rpc_find_value(self, node: DHTNode, key: str) -> Optional[str]:
        """Send FIND_VALUE RPC."""
        result = self._send_rpc(node, {
            "type": "dht_find_value",
            "key": key,
        })
        if result and result.get("type") == "dht_value":
            return result.get("value")
        return None

    # ── Server ──────────────────────────────────────────────────

    def _listen(self):
        """UDP listener for incoming DHT messages."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.settimeout(1.0)

        while self._running:
            try:
                data, addr = sock.recvfrom(65536)
                msg = json.loads(data.decode())
                threading.Thread(
                    target=self._handle_message,
                    args=(msg, addr),
                    daemon=True,
                ).start()
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    logger.debug(f"DHT listener error: {e}")

        sock.close()

    def _handle_message(self, msg: dict, addr: tuple):
        """Route incoming DHT message to handler."""
        msg_type = msg.get("type", "")

        # Update routing table with sender
        sender_id = msg.get("sender_id")
        sender_host = msg.get("sender_host", addr[0])
        sender_port = msg.get("sender_port", addr[1])
        if sender_id:
            try:
                node = DHTNode(
                    node_id=bytes.fromhex(sender_id),
                    host=sender_host,
                    port=sender_port,
                    last_seen=time.time(),
                )
                self.table.add_node(node)
            except Exception:
                pass

        # Check if this is a response to a pending RPC
        rpc_id = msg.get("rpc_id")
        if rpc_id and rpc_id in self._pending:
            self._responses[rpc_id] = msg
            self._pending[rpc_id].set()
            return

        # Handle RPC requests
        if msg_type == "dht_ping":
            self._respond(addr, {"type": "dht_pong", "rpc_id": rpc_id})

        elif msg_type == "dht_find_node":
            target_hex = msg.get("target_id", "")
            try:
                target_id = bytes.fromhex(target_hex)
            except ValueError:
                return
            closest = self.table.find_closest(target_id, K)
            self._respond(addr, {
                "type": "dht_found_nodes",
                "rpc_id": rpc_id,
                "nodes": [
                    {"node_id": n.node_id.hex(), "host": n.host, "port": n.port}
                    for n in closest
                ],
            })

        elif msg_type == "dht_store":
            key = msg.get("key", "")
            value = msg.get("value", "")
            if key and len(value) < 65536:  # 64KB value limit
                with self._store_lock:
                    self._store[key] = (value, time.time())
                self._respond(addr, {"type": "dht_stored", "rpc_id": rpc_id})

        elif msg_type == "dht_find_value":
            key = msg.get("key", "")
            with self._store_lock:
                entry = self._store.get(key)
            if entry and time.time() - entry[1] < EXPIRE_TIME:
                self._respond(addr, {
                    "type": "dht_value",
                    "rpc_id": rpc_id,
                    "key": key,
                    "value": entry[0],
                })
            else:
                # Don't have it — return closest nodes
                key_hash = hashlib.sha256(key.encode()).digest()
                closest = self.table.find_closest(key_hash, K)
                self._respond(addr, {
                    "type": "dht_found_nodes",
                    "rpc_id": rpc_id,
                    "nodes": [
                        {"node_id": n.node_id.hex(), "host": n.host, "port": n.port}
                        for n in closest
                    ],
                })

    def _respond(self, addr: tuple, msg: dict):
        """Send a UDP response."""
        msg["sender_id"] = self.node_id.hex()
        msg["sender_host"] = self.host
        msg["sender_port"] = self.port
        try:
            data = json.dumps(msg).encode()
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.sendto(data, addr)
        except Exception as e:
            logger.debug(f"DHT respond error: {e}")

    # ── Maintenance ─────────────────────────────────────────────

    def _maintenance_loop(self):
        """Periodic maintenance: refresh buckets, expire data."""
        while self._running:
            try:
                time.sleep(60)

                # Refresh stale buckets
                for idx in self.table.get_stale_buckets():
                    random_id = os.urandom(32)
                    self.find_node(random_id)
                    self.table.buckets[idx].last_refreshed = time.time()

                # Expire old stored values
                now = time.time()
                with self._store_lock:
                    expired_keys = [
                        k for k, (v, ts) in self._store.items()
                        if now - ts > EXPIRE_TIME
                    ]
                    for k in expired_keys:
                        del self._store[k]

                # Ping oldest node in each non-empty bucket
                for bucket in self.table.buckets:
                    nodes = bucket.get_nodes()
                    if nodes:
                        oldest = nodes[0]
                        if now - oldest.last_seen > REFRESH_INTERVAL:
                            if not self._send_ping(oldest):
                                if oldest.failed_pings >= 3:
                                    bucket.remove(oldest)

            except Exception as e:
                logger.debug(f"DHT maintenance error: {e}")

    # ── Stats ───────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Return DHT network statistics."""
        non_empty = sum(1 for b in self.table.buckets if b.size > 0)
        return {
            "node_id": self.node_id.hex()[:16] + "...",
            "total_peers": self.table.total_nodes,
            "non_empty_buckets": non_empty,
            "stored_values": len(self._store),
            "host": self.host,
            "port": self.port,
        }
