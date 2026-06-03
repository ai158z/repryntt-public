"""
Protocol Versioning & Handshake for repryntt blockchain.

Every peer connection starts with a version handshake.
Nodes announce their protocol version, capabilities, and chain height.
Incompatible versions are rejected; compatible older versions receive
a limited feature set.

Version history:
    1 — Original TCP protocol (implicit, pre-versioning)
    2 — Gossip relay, Merkle blocks, fee mempool, header sync
"""

import hashlib
import json
import logging
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

logger = logging.getLogger("protocol")

# ── Constants ────────────────────────────────────────────────────
# MUST match qnode2.py — single source of truth for the network
PROTOCOL_VERSION = 3
MIN_COMPATIBLE_VERSION = 1
NETWORK_MAGIC = b"RPNT"            # 4-byte network identifier (matches qnode2)
NETWORK_MAGIC_LEN = len(NETWORK_MAGIC)
HANDSHAKE_TIMEOUT = 10            # seconds


@dataclass
class PeerCapabilities:
    """What a peer can do, discovered during handshake."""
    version: int = 1
    gossip: bool = False          # Supports gossip relay
    merkle: bool = False          # Supports Merkle proofs
    headers_first: bool = False   # Supports header-first sync
    light_client: bool = False    # Is running as light client
    chain_height: int = 0         # Peer's current chain height
    node_id: str = ""             # Unique node identifier
    user_agent: str = "repryntt"  # Software identifier
    genesis_hash: str = ""        # Peer's genesis block hash (fork detection)
    services: Set[str] = field(default_factory=set)


def build_version_message(
    chain_height: int,
    node_id: str,
    listen_port: int,
    is_light_client: bool = False,
    genesis_hash: str = "",
) -> bytes:
    """
    Build a version handshake message.

    Format:
        [6 bytes] NETWORK_MAGIC
        [4 bytes] message length (uint32 BE)
        [N bytes] JSON payload
    """
    payload = {
        "type": "version",
        "version": PROTOCOL_VERSION,
        "min_version": MIN_COMPATIBLE_VERSION,
        "timestamp": time.time(),
        "node_id": node_id,
        "chain_height": chain_height,
        "listen_port": listen_port,
        "user_agent": f"repryntt/{PROTOCOL_VERSION}",
        "genesis_hash": genesis_hash,
        "capabilities": {
            "gossip": True,
            "merkle": True,
            "headers_first": True,
            "light_client": is_light_client,
        },
        "services": ["blockchain", "workload", "swarm"] if not is_light_client else ["light"],
    }

    payload_bytes = json.dumps(payload).encode()
    length = struct.pack("!I", len(payload_bytes))
    return NETWORK_MAGIC + length + payload_bytes


def build_verack_message(accepted: bool, reason: str = "") -> bytes:
    """Build a version acknowledgement response."""
    payload = {
        "type": "verack",
        "accepted": accepted,
        "reason": reason,
        "version": PROTOCOL_VERSION,
    }
    payload_bytes = json.dumps(payload).encode()
    length = struct.pack("!I", len(payload_bytes))
    return NETWORK_MAGIC + length + payload_bytes


def _read_exact(sock, n: int) -> bytes:
    """Read exactly n bytes from socket."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed connection during handshake")
        buf += chunk
    return buf


def parse_version_message(data: bytes) -> Optional[PeerCapabilities]:
    """
    Parse an incoming version handshake message.
    Returns PeerCapabilities or None if invalid.
    """
    magic_len = NETWORK_MAGIC_LEN  # 4 bytes
    header_len = magic_len + 4  # magic + uint32 length
    if len(data) < header_len:
        return None

    magic = data[:magic_len]
    if magic != NETWORK_MAGIC:
        logger.warning("Invalid network magic — wrong network?")
        return None

    msg_len = struct.unpack("!I", data[magic_len:header_len])[0]
    if len(data) < header_len + msg_len:
        return None

    try:
        payload = json.loads(data[header_len : header_len + msg_len].decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    if payload.get("type") != "version":
        return None

    version = payload.get("version", 1)
    if version < MIN_COMPATIBLE_VERSION:
        logger.warning(f"Peer version {version} below minimum {MIN_COMPATIBLE_VERSION}")
        return None

    caps = payload.get("capabilities", {})
    return PeerCapabilities(
        version=version,
        gossip=caps.get("gossip", False),
        merkle=caps.get("merkle", False),
        headers_first=caps.get("headers_first", False),
        light_client=caps.get("light_client", False),
        chain_height=payload.get("chain_height", 0),
        node_id=payload.get("node_id", ""),
        user_agent=payload.get("user_agent", "unknown"),
        genesis_hash=payload.get("genesis_hash", ""),
        services=set(payload.get("services", [])),
    )


def parse_verack_message(data: bytes) -> Optional[dict]:
    """Parse a verack response."""
    magic_len = NETWORK_MAGIC_LEN
    header_len = magic_len + 4
    if len(data) < header_len:
        return None
    magic = data[:magic_len]
    if magic != NETWORK_MAGIC:
        return None
    msg_len = struct.unpack("!I", data[magic_len:header_len])[0]
    if len(data) < header_len + msg_len:
        return None
    try:
        payload = json.loads(data[header_len : header_len + msg_len].decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if payload.get("type") != "verack":
        return None
    return payload


def generate_node_id() -> str:
    """Generate a persistent node ID based on hostname + random salt."""
    id_file = os.path.join(os.path.dirname(__file__), "..", "..", "robot_economy_data", "node_id")
    id_file = os.path.abspath(id_file)
    if os.path.exists(id_file):
        try:
            with open(id_file, "r") as f:
                return f.read().strip()
        except Exception:
            pass

    # Generate fresh 256-bit ID
    raw = os.urandom(32) + os.uname().nodename.encode()
    node_id = hashlib.sha256(raw).hexdigest()

    os.makedirs(os.path.dirname(id_file), exist_ok=True)
    try:
        with open(id_file, "w") as f:
            f.write(node_id)
    except Exception:
        pass

    return node_id


class ProtocolNegotiator:
    """
    Manages the handshake sequence for a new peer connection.

    Usage:
        negotiator = ProtocolNegotiator(chain_height=1234, node_id="abc...")
        # Outbound connection:
        caps = negotiator.initiate(sock)
        # Inbound connection:
        caps = negotiator.respond(sock, raw_version_bytes)
    """

    def __init__(self, chain_height: int, node_id: str, listen_port: int = 5000,
                 is_light_client: bool = False, genesis_hash: str = ""):
        self.chain_height = chain_height
        self.node_id = node_id
        self.listen_port = listen_port
        self.is_light_client = is_light_client
        self.genesis_hash = genesis_hash

    def initiate(self, sock) -> Optional[PeerCapabilities]:
        """Outbound: send our version, wait for verack + their version."""
        try:
            sock.settimeout(HANDSHAKE_TIMEOUT)
            version_msg = build_version_message(
                self.chain_height, self.node_id, self.listen_port, self.is_light_client,
                genesis_hash=self.genesis_hash,
            )
            sock.sendall(version_msg)

            # Read response (verack)
            response = sock.recv(4096)
            verack = parse_verack_message(response)
            if not verack or not verack.get("accepted"):
                reason = verack.get("reason", "unknown") if verack else "no response"
                logger.warning(f"Handshake rejected: {reason}")
                return None

            # Read their version message (may be in same packet or next)
            header_len = NETWORK_MAGIC_LEN + 4
            verack_msg_len = struct.unpack("!I", response[NETWORK_MAGIC_LEN:header_len])[0]
            verack_total = header_len + verack_msg_len
            if len(response) > verack_total:
                remainder = response[verack_total:]
                peer_caps = parse_version_message(remainder)
            else:
                version_data = sock.recv(4096)
                peer_caps = parse_version_message(version_data)

            if peer_caps and peer_caps.node_id == self.node_id:
                logger.debug("Self-connection detected, dropping")
                return None

            return peer_caps

        except Exception as e:
            logger.warning(f"Handshake failed: {e}")
            return None

    def respond(self, sock, raw_data: bytes) -> Optional[PeerCapabilities]:
        """Inbound: parse their version, send verack + our version."""
        try:
            sock.settimeout(HANDSHAKE_TIMEOUT)
            peer_caps = parse_version_message(raw_data)

            if peer_caps is None:
                sock.sendall(build_verack_message(False, "invalid version message"))
                return None

            if peer_caps.version < MIN_COMPATIBLE_VERSION:
                sock.sendall(build_verack_message(False, f"version {peer_caps.version} too old"))
                return None

            if peer_caps.node_id == self.node_id:
                sock.sendall(build_verack_message(False, "self-connection"))
                return None

            # Reject peers on a different chain (fork detection)
            if (peer_caps.genesis_hash and self.genesis_hash
                    and peer_caps.genesis_hash != self.genesis_hash):
                sock.sendall(build_verack_message(False, "genesis mismatch — different chain"))
                logger.warning(
                    f"Rejected peer {peer_caps.node_id[:12]}... — genesis mismatch (fork)"
                )
                return None

            # Accept and respond with our version
            verack = build_verack_message(True)
            our_version = build_version_message(
                self.chain_height, self.node_id, self.listen_port, self.is_light_client,
                genesis_hash=self.genesis_hash,
            )
            sock.sendall(verack + our_version)

            return peer_caps

        except Exception as e:
            logger.warning(f"Handshake response failed: {e}")
            return None
