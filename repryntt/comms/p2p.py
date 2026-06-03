#!/usr/bin/env python3
"""
SAIGE P2P Mesh Network
═══════════════════════
Decentralized peer-to-peer infrastructure for SAIGE agent swarms.

Architecture:
  Layer 1 — Discovery:  mDNS (LAN auto-discover) + seed nodes (WAN bootstrap)
  Layer 2 — Transport:  WebSocket + msgpack (safe, async, fast)
  Layer 3 — Content:    SHA-256 content-addressed artifact store
  Layer 4 — Gossip:     Artifact catalog sync between peers
  Layer 5 — Missions:   Cross-device swarm mission coordination

Designed for:
  - Low-RAM devices (Jetson Orin Nano, 8GB) → "lite mode"
  - Beefy machines (16-64GB laptops/PCs) → "full mode"
  - NAT-friendly: WebSocket transport works through most firewalls
  - Safe: msgpack serialization (no pickle RCE risk)

Port: 6600 (default)
Protocol: WebSocket + msgpack binary frames

Usage:
  node = SAIGENode(node_name="my-jetson", port=6600)
  await node.start()
  await node.connect_to_peer("ws://192.168.1.50:6600")
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import platform
import re
import secrets
import socket
import struct
import time
import uuid
from dataclasses import dataclass, field, asdict
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import msgpack
import aiohttp
from aiohttp import web, WSMsgType

logger = logging.getLogger("saige.p2p")

# ═══════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════

DEFAULT_PORT = 6600
SERVICE_TYPE = "_saige-p2p._tcp.local."
PROTOCOL_VERSION = 1
MAX_ARTIFACT_SIZE = 10 * 1024 * 1024  # 10 MB max artifact
GOSSIP_INTERVAL = 30  # seconds between catalog gossip rounds
HEARTBEAT_INTERVAL = 15  # seconds between peer heartbeats
PEER_TIMEOUT = 60  # seconds before considering a peer dead
RENDEZVOUS_INTERVAL = 45  # seconds between rendezvous check-ins
RENDEZVOUS_TIMEOUT = 10  # HTTP timeout for rendezvous requests
RENDEZVOUS_MAX_AGE = 300  # seconds before a rendezvous entry expires

# Hardcoded bootstrap/seed nodes — like Bitcoin's DNS seeds.
# Every repryntt node contacts these on first start to find peers.
# Once connected to the mesh, nodes discover each other via DHT/gossip.
BOOTSTRAP_SEEDS = [
    "http://35.208.114.82:6600",  # GCP us-east1 primary bootstrap
]
CATALOG_SYNC_BATCH = 50  # max artifacts per catalog sync message
MAX_PEERS = 50  # maximum simultaneous peer connections
MAX_PEER_LIST_SIZE = 20  # max addresses accepted from a peer list message
MAX_STORE_SIZE_MB = 2048  # max total content store size (2 GB)
MSG_RATE_LIMIT = 100  # max messages per peer per minute

# Valid SHA-256 hash pattern — reject anything else
_VALID_HASH = re.compile(r'^[0-9a-f]{64}$')

# Safe filename pattern — alphanumeric, hyphens, underscores, dots, spaces
_SAFE_FILENAME = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.\- ]{0,200}$')
_SAFE_PROJECT = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.\- ]{0,100}$')

# Message types
MSG_HANDSHAKE = 0x01
MSG_HANDSHAKE_ACK = 0x02
MSG_HEARTBEAT = 0x03
MSG_HEARTBEAT_ACK = 0x04
MSG_CATALOG_SYNC = 0x10
MSG_CATALOG_REQUEST = 0x11
MSG_ARTIFACT_REQUEST = 0x20
MSG_ARTIFACT_RESPONSE = 0x21
MSG_ARTIFACT_ANNOUNCE = 0x22
MSG_MISSION_BROADCAST = 0x30
MSG_MISSION_JOIN = 0x31
MSG_MISSION_RESULT = 0x32
MSG_MISSION_STATUS = 0x33
MSG_AGENT_ANNOUNCE = 0x40
MSG_KNOWLEDGE_QUERY = 0x50
MSG_KNOWLEDGE_RESPONSE = 0x51
MSG_PEER_LIST = 0x60

# ── Compute Economy Messages (P2P ↔ Blockchain bridge) ──
MSG_COMPUTE_ANNOUNCE  = 0x70   # GPU availability announcement
MSG_COMPUTE_REQUEST   = 0x71   # Workload dispatch to network
MSG_COMPUTE_CLAIM     = 0x72   # Miner claims workload
MSG_COMPUTE_RESULT    = 0x73   # Completed work + PoP proof
MSG_COMPUTE_REJECT    = 0x74   # Workload failed/rejected
MSG_BLOCK_ANNOUNCE    = 0x75   # New block mined
MSG_BLOCK_REQUEST     = 0x76   # Request block at height N
MSG_BLOCK_RESPONSE    = 0x77   # Block data response
MSG_ECONOMY_STATUS    = 0x78   # Blockchain state summary


# ═══════════════════════════════════════════════════════
#  DATA STRUCTURES
# ═══════════════════════════════════════════════════════

@dataclass
class PeerInfo:
    """Information about a connected peer."""
    node_id: str
    node_name: str
    address: str  # ws://host:port
    connected_at: float = 0.0
    last_heartbeat: float = 0.0
    agent_count: int = 0
    artifact_count: int = 0
    capabilities: Dict[str, Any] = field(default_factory=dict)
    reputation: float = 1.0
    latency_ms: float = 0.0
    protocol_version: int = PROTOCOL_VERSION
    websocket: Any = None  # aiohttp.WSMsgType reference

    def is_alive(self) -> bool:
        return (time.time() - self.last_heartbeat) < PEER_TIMEOUT

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "node_name": self.node_name,
            "address": self.address,
            "connected_at": self.connected_at,
            "last_heartbeat": self.last_heartbeat,
            "agent_count": self.agent_count,
            "artifact_count": self.artifact_count,
            "capabilities": self.capabilities,
            "reputation": self.reputation,
            "latency_ms": self.latency_ms,
        }


@dataclass
class ArtifactMeta:
    """Metadata for a content-addressed artifact."""
    content_hash: str  # SHA-256 of content
    filename: str
    project: str
    size: int
    created_at: float
    agent_name: str = ""
    agent_id: str = ""
    mission_id: str = ""
    node_id: str = ""  # originating node
    content_type: str = "text"  # text, code, json, binary
    tags: List[str] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ArtifactMeta":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class NetworkMission:
    """A mission that spans multiple SAIGE nodes."""
    mission_id: str
    objective: str
    originator_node: str
    status: str = "recruiting"  # recruiting, active, converging, completed, failed
    required_agents: int = 4
    joined_nodes: Dict[str, Dict] = field(default_factory=dict)  # node_id → {agents: [...], status: ...}
    subtasks: Dict[str, Dict] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    deadline: float = 0.0
    results: Dict[str, str] = field(default_factory=dict)  # node_id → result text


# ═══════════════════════════════════════════════════════
#  CONTENT-ADDRESSED STORE
# ═══════════════════════════════════════════════════════

class ContentStore:
    """
    SHA-256 content-addressed artifact store.
    Similar to IPFS but lightweight — no daemon, no DHT overhead.
    Files stored as: store_dir/{hash[:2]}/{hash[2:4]}/{hash}
    """

    def __init__(self, store_dir: str):
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.catalog: Dict[str, ArtifactMeta] = {}  # hash → metadata
        self._catalog_file = self.store_dir / "catalog.json"
        self._load_catalog()

    def _hash_path(self, content_hash: str) -> Path:
        """Get storage path for a content hash (2-level sharding)."""
        return self.store_dir / content_hash[:2] / content_hash[2:4] / content_hash

    @staticmethod
    def compute_hash(content: bytes) -> str:
        """Compute SHA-256 hash of content."""
        return hashlib.sha256(content).hexdigest()

    def store(self, content: bytes, meta: ArtifactMeta) -> str:
        """
        Store content and metadata. Returns content hash.
        If content already exists (same hash), just updates metadata.
        """
        content_hash = self.compute_hash(content)
        meta.content_hash = content_hash
        meta.size = len(content)

        # Write content if not already stored
        path = self._hash_path(content_hash)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            logger.info(f"📦 Stored artifact: {meta.filename} ({len(content)} bytes) → {content_hash[:12]}")

        # Update catalog
        self.catalog[content_hash] = meta
        self._save_catalog()
        return content_hash

    def retrieve(self, content_hash: str) -> Optional[bytes]:
        """Retrieve content by hash. Returns None if not found."""
        if not _VALID_HASH.match(content_hash):
            logger.warning(f"⚠️ Rejected invalid hash in retrieve: {content_hash[:30]}")
            return None
        path = self._hash_path(content_hash)
        # Verify resolved path is still under store_dir (defense in depth)
        try:
            path.resolve().relative_to(self.store_dir.resolve())
        except ValueError:
            logger.warning(f"⚠️ Path traversal attempt blocked in retrieve: {content_hash[:30]}")
            return None
        if path.exists():
            return path.read_bytes()
        return None

    def has(self, content_hash: str) -> bool:
        """Check if content exists locally."""
        if not _VALID_HASH.match(content_hash):
            return False
        return self._hash_path(content_hash).exists()

    def get_meta(self, content_hash: str) -> Optional[ArtifactMeta]:
        """Get metadata for a hash."""
        return self.catalog.get(content_hash)

    def get_catalog_hashes(self) -> Set[str]:
        """Get all known content hashes."""
        return set(self.catalog.keys())

    def get_catalog_summary(self) -> List[Dict]:
        """Get lightweight catalog for gossip (hashes + timestamps only)."""
        return [
            {"hash": h, "created_at": m.created_at, "size": m.size,
             "filename": m.filename, "project": m.project}
            for h, m in self.catalog.items()
        ]

    def ingest_from_workspace(self, workspace_dir: str, node_id: str,
                               registry: dict = None) -> int:
        """
        Scan a creative_workspace directory and ingest all files into the store.
        Returns number of newly ingested artifacts.
        """
        workspace = Path(workspace_dir)
        if not workspace.is_dir():
            return 0

        count = 0
        for fpath in workspace.rglob("*"):
            if fpath.is_file() and not fpath.name.startswith("."):
                try:
                    content = fpath.read_bytes()
                    if not content:
                        continue

                    content_hash = self.compute_hash(content)
                    if content_hash in self.catalog:
                        continue  # Already have it

                    rel_path = str(fpath.relative_to(workspace))
                    project = rel_path.split(os.sep)[0] if os.sep in rel_path else "_root"
                    ext = fpath.suffix.lstrip(".")

                    # Check artifact registry for agent attribution
                    agent_name = ""
                    agent_id = ""
                    mission_id = ""
                    if registry and rel_path in registry:
                        meta_info = registry[rel_path]
                        agent_name = meta_info.get("agent", "")
                        agent_id = meta_info.get("agent_id", "")
                        mission_id = meta_info.get("mission_id", "")

                    meta = ArtifactMeta(
                        content_hash=content_hash,
                        filename=fpath.name,
                        project=project,
                        size=len(content),
                        created_at=fpath.stat().st_mtime,
                        agent_name=agent_name,
                        agent_id=agent_id,
                        mission_id=mission_id,
                        node_id=node_id,
                        content_type="code" if ext in ("py", "js", "sh", "cpp") else "text",
                        tags=[ext] if ext else [],
                    )
                    self.store(content, meta)
                    count += 1
                except Exception as e:
                    logger.debug(f"Skipping {fpath}: {e}")

        logger.info(f"📦 Ingested {count} new artifacts from workspace")
        return count

    def _load_catalog(self):
        """Load catalog from disk."""
        if self._catalog_file.exists():
            try:
                data = json.loads(self._catalog_file.read_text())
                self.catalog = {
                    h: ArtifactMeta.from_dict(m) for h, m in data.items()
                }
            except Exception as e:
                logger.warning(f"Failed to load catalog: {e}")
                self.catalog = {}

    def _save_catalog(self):
        """Persist catalog to disk."""
        try:
            data = {h: m.to_dict() for h, m in self.catalog.items()}
            self._catalog_file.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning(f"Failed to save catalog: {e}")


# ═══════════════════════════════════════════════════════
#  SAIGE P2P NODE
# ═══════════════════════════════════════════════════════

class SAIGENode:
    """
    A SAIGE P2P mesh node.

    Handles:
    - WebSocket server for incoming peer connections
    - WebSocket client connections to other peers
    - mDNS discovery on LAN (auto-find other SAIGE nodes)
    - Content-addressed artifact storage and sharing
    - Catalog gossip (tell peers what artifacts you have)
    - Cross-device mission coordination
    """

    def __init__(self, node_name: str = None, port: int = DEFAULT_PORT,
                 data_dir: str = None, seed_peers: List[str] = None,
                 enable_mdns: bool = True, machine_id: str = None,
                 auth_token: str = None):
        # Use persistent machine identity if available
        try:
            from repryntt.core.identity.machine_identity import get_identity
            identity = get_identity()
            self.node_id = machine_id or identity["machine_id"][:12]
            self.node_name = node_name or identity["node_name"]
            self.machine_id = identity["machine_id"]  # Full UUID
            self.node_role = identity.get("role", "node")
        except Exception:
            self.node_id = machine_id or str(uuid.uuid4())[:12]
            self.node_name = node_name or f"saige-{platform.node()}"
            self.machine_id = self.node_id
            self.node_role = "node"
        self.port = port
        self.seed_peers = seed_peers or []
        self.enable_mdns = enable_mdns

        # ── Rendezvous (cross-subnet auto-discovery) ──
        self.rendezvous_nodes: List[str] = self._load_rendezvous_nodes()
        self._rendezvous_registry: Dict[str, dict] = {}  # node_id → {address, name, last_seen}

        # ── Security ──
        # Auth token — peers must present this in handshake to be accepted.
        # Auto-generated on first run if not provided, saved to config.
        self.auth_token = auth_token or self._load_or_generate_token()
        # Track which artifact hashes we've actually requested (prevent unsolicited pushes)
        self._pending_artifact_requests: Set[str] = set()
        # Per-peer message rate tracking: {node_id: [timestamp, ...]}
        self._peer_msg_rates: Dict[str, list] = {}
        # Authenticated peer set (node_ids that passed handshake)
        self._authenticated_peers: Set[str] = set()

        # Data directory
        base = Path(data_dir) if data_dir else Path(__file__).parent / "brain" / "p2p"
        base.mkdir(parents=True, exist_ok=True)
        self.data_dir = base

        # Content store
        self.store = ContentStore(str(base / "content_store"))

        # Peer management
        self.peers: Dict[str, PeerInfo] = {}  # node_id → PeerInfo
        self._peer_lock = asyncio.Lock()

        # Network missions
        self.network_missions: Dict[str, NetworkMission] = {}

        # Server
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

        # Background tasks
        self._tasks: List[asyncio.Task] = []
        self._running = False

        # Stats
        self.stats = {
            "messages_sent": 0,
            "messages_received": 0,
            "artifacts_shared": 0,
            "artifacts_received": 0,
            "bytes_transferred": 0,
            "started_at": 0.0,
            "missions_coordinated": 0,
        }

        # mDNS
        self._zeroconf = None
        self._mdns_info = None

        # Daemon reference (set externally)
        self.daemon = None

        # Node state persistence
        self._state_file = base / "node_state.json"
        self._load_state()

        logger.info(f"🌐 SAIGE P2P Node initialized: {self.node_name} ({self.node_id}) [token: {self.auth_token[:8]}...]")

    # ─── Lifecycle ─────────────────────────────────────

    async def start(self):
        """Start the P2P node — WebSocket server, mDNS, background tasks."""
        if self._running:
            return

        self._running = True
        self.stats["started_at"] = time.time()

        # Start WebSocket server
        self._app = web.Application()
        self._app.router.add_get("/ws", self._handle_ws_connection)
        # HTTP endpoints require auth token as query param or header
        self._app.router.add_get("/status", self._handle_http_status)
        self._app.router.add_get("/catalog", self._handle_http_catalog)
        self._app.router.add_get("/artifact/{hash}", self._handle_http_artifact)
        # Rendezvous endpoints (open — no auth, nodes announce themselves)
        self._app.router.add_post("/rendezvous/announce", self._handle_rendezvous_announce)
        self._app.router.add_get("/rendezvous/peers", self._handle_rendezvous_peers)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", self.port, reuse_address=True)
        try:
            await self._site.start()
        except OSError as e:
            logger.warning(f"P2P port {self.port} unavailable: {e} — running without P2P server")
            return
        logger.info(f"🌐 P2P WebSocket server listening on ws://0.0.0.0:{self.port}/ws")

        # Start mDNS discovery
        if self.enable_mdns:
            self._tasks.append(asyncio.create_task(self._start_mdns()))

        # Connect to seed peers
        for peer_addr in self.seed_peers:
            self._tasks.append(asyncio.create_task(self._connect_to_peer(peer_addr)))

        # Connect to previously known peers
        for peer_addr in self._known_peer_addresses:
            if peer_addr not in self.seed_peers:
                self._tasks.append(asyncio.create_task(self._connect_to_peer(peer_addr)))

        # Background gossip + heartbeat + rendezvous
        self._tasks.append(asyncio.create_task(self._gossip_loop()))
        self._tasks.append(asyncio.create_task(self._heartbeat_loop()))
        self._tasks.append(asyncio.create_task(self._ingest_loop()))
        if self.rendezvous_nodes:
            self._tasks.append(asyncio.create_task(self._rendezvous_loop()))
            logger.info(f"🔗 Rendezvous enabled — {len(self.rendezvous_nodes)} tracker(s)")

        logger.info(f"🌐 P2P node started: {self.node_name} @ port {self.port}")

    async def stop(self):
        """Shut down the P2P node."""
        self._running = False

        # Cancel background tasks
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # Close peer connections
        async with self._peer_lock:
            for peer in self.peers.values():
                if peer.websocket and not peer.websocket.closed:
                    await peer.websocket.close()
            self.peers.clear()

        # Stop mDNS
        if self._zeroconf:
            try:
                from zeroconf import Zeroconf
                if self._mdns_info:
                    self._zeroconf.unregister_service(self._mdns_info)
                self._zeroconf.close()
            except Exception:
                pass

        # Stop server
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()

        self._save_state()
        logger.info(f"🌐 P2P node stopped: {self.node_name}")

    # ─── Security ──────────────────────────────────────

    def _load_or_generate_token(self) -> str:
        """Load auth token from p2p_config.json or generate one."""
        config_path = Path(__file__).parent / "p2p_config.json"
        if config_path.exists():
            try:
                with open(config_path) as f:
                    cfg = json.load(f)
                token = cfg.get("auth_token", "")
                if token and len(token) >= 16:
                    return token
            except Exception:
                pass
        # Generate a new token and save it
        token = secrets.token_hex(32)
        try:
            cfg = {}
            if config_path.exists():
                with open(config_path) as f:
                    cfg = json.load(f)
            cfg["auth_token"] = token
            with open(config_path, "w") as f:
                json.dump(cfg, f, indent=2)
            logger.info(f"🔑 Generated new P2P auth token — share this with trusted peers")
        except Exception:
            pass
        return token

    def _check_http_auth(self, request: web.Request) -> bool:
        """Verify HTTP request has valid auth token."""
        # Check query param: ?token=...
        token = request.query.get("token", "")
        if token and hmac.compare_digest(token, self.auth_token):
            return True
        # Check header: Authorization: Bearer <token>
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            if hmac.compare_digest(auth_header[7:], self.auth_token):
                return True
        return False

    def _check_rate_limit(self, sender_id: str) -> bool:
        """Return True if peer is within rate limit, False if exceeded."""
        now = time.time()
        if sender_id not in self._peer_msg_rates:
            self._peer_msg_rates[sender_id] = []
        # Prune old entries (older than 60s)
        self._peer_msg_rates[sender_id] = [
            t for t in self._peer_msg_rates[sender_id] if now - t < 60
        ]
        if len(self._peer_msg_rates[sender_id]) >= MSG_RATE_LIMIT:
            return False
        self._peer_msg_rates[sender_id].append(now)
        return True

    @staticmethod
    def _sanitize_filename(name: str) -> Optional[str]:
        """Sanitize a filename — returns None if unsafe."""
        if not name or not _SAFE_FILENAME.match(name):
            return None
        # Extra: reject any path components
        if '/' in name or '\\' in name or '\x00' in name:
            return None
        return name

    @staticmethod
    def _sanitize_project(name: str) -> Optional[str]:
        """Sanitize a project name — returns None if unsafe."""
        if not name or not _SAFE_PROJECT.match(name):
            return None
        if '/' in name or '\\' in name or '..' in name or '\x00' in name:
            return None
        return name

    @staticmethod
    def _is_safe_peer_address(address: str) -> bool:
        """Check if a peer address is safe to connect to (not internal/loopback)."""
        try:
            # Must be a WebSocket address
            if not address.startswith(("ws://", "wss://")):
                return False
            # Extract host from ws://host:port/path
            host = address.replace("ws://", "").replace("wss://", "").split("/")[0].split(":")[0]
            addr = ip_address(host)
            # Block loopback and link-local (but allow private LAN — that's the point)
            if addr.is_loopback or addr.is_link_local:
                return False
            # Block well-known cloud metadata endpoints
            if str(addr) == "169.254.169.254":
                return False
            return True
        except ValueError:
            # Hostname, not IP — allow it (DNS resolution will handle it)
            return True

    # ─── WebSocket Server ──────────────────────────────

    async def _handle_ws_connection(self, request: web.Request) -> web.WebSocketResponse:
        """Handle incoming WebSocket peer connection."""
        ws = web.WebSocketResponse(max_msg_size=MAX_ARTIFACT_SIZE + 1024)
        await ws.prepare(request)

        peer_addr = f"{request.remote}"
        logger.info(f"🔗 Incoming peer connection from {peer_addr}")

        peer_info = None
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    await self._handle_message(msg.data, ws, peer_addr)
                elif msg.type == WSMsgType.TEXT:
                    # Fallback: JSON text messages
                    try:
                        data = json.loads(msg.data)
                        packed = msgpack.packb(data, use_bin_type=True)
                        await self._handle_message(packed, ws, peer_addr)
                    except Exception:
                        pass
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        except Exception as e:
            logger.warning(f"Peer connection error from {peer_addr}: {e}")
        finally:
            # Remove peer on disconnect
            async with self._peer_lock:
                to_remove = [nid for nid, p in self.peers.items()
                             if p.websocket is ws]
                for nid in to_remove:
                    del self.peers[nid]
                    logger.info(f"🔌 Peer disconnected: {nid}")

        return ws

    async def _handle_http_status(self, request: web.Request) -> web.Response:
        """HTTP status endpoint for health checks and info."""
        if not self._check_http_auth(request):
            return web.json_response({"error": "Unauthorized — add ?token=YOUR_TOKEN"}, status=401)
        return web.json_response(self.get_status())

    async def _handle_http_catalog(self, request: web.Request) -> web.Response:
        """HTTP catalog endpoint — list all artifacts."""
        if not self._check_http_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        return web.json_response({
            "node_id": self.node_id,
            "artifacts": self.store.get_catalog_summary(),
            "total": len(self.store.catalog),
        })

    async def _handle_http_artifact(self, request: web.Request) -> web.Response:
        """HTTP artifact download — get content by hash."""
        if not self._check_http_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        content_hash = request.match_info["hash"]
        if not _VALID_HASH.match(content_hash):
            return web.json_response({"error": "Invalid hash format"}, status=400)
        content = self.store.retrieve(content_hash)
        if content is None:
            return web.json_response({"error": "Not found"}, status=404)
        meta = self.store.get_meta(content_hash)
        return web.Response(
            body=content,
            content_type="application/octet-stream",
            headers={
                "X-Artifact-Hash": content_hash,
                "X-Artifact-Filename": meta.filename if meta else "",
                "X-Artifact-Project": meta.project if meta else "",
            }
        )

    # ─── WebSocket Client ──────────────────────────────

    async def connect_to_peer(self, address: str) -> bool:
        """Public API: connect to a peer by WebSocket address."""
        return await self._connect_to_peer(address)

    async def _connect_to_peer(self, address: str) -> bool:
        """Connect to a peer node via WebSocket."""
        # Normalize address
        if not address.startswith("ws://") and not address.startswith("wss://"):
            address = f"ws://{address}"
        if "/ws" not in address:
            address = address.rstrip("/") + "/ws"

        # Check if already connected
        async with self._peer_lock:
            for p in self.peers.values():
                if p.address == address or address.startswith(p.address.rsplit("/", 1)[0]):
                    return True  # Already connected

        try:
            session = aiohttp.ClientSession()
            ws = await session.ws_connect(address, max_msg_size=MAX_ARTIFACT_SIZE + 1024)

            # Send handshake (includes auth token for peer verification)
            await self._send_msg(ws, MSG_HANDSHAKE, {
                "node_id": self.node_id,
                "machine_id": getattr(self, 'machine_id', self.node_id),
                "node_name": self.node_name,
                "role": getattr(self, 'node_role', 'node'),
                "port": self.port,
                "protocol_version": PROTOCOL_VERSION,
                "agent_count": self._get_agent_count(),
                "artifact_count": len(self.store.catalog),
                "capabilities": self._get_capabilities(),
                "auth_token": self.auth_token,
            })

            logger.info(f"🔗 Connected to peer: {address}")

            # Read messages in background
            asyncio.create_task(self._read_peer_messages(ws, address, session))
            return True

        except Exception as e:
            logger.debug(f"Failed to connect to {address}: {e}")
            return False

    async def _read_peer_messages(self, ws, address: str, session: aiohttp.ClientSession):
        """Background task: read messages from an outbound peer connection."""
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    await self._handle_message(msg.data, ws, address)
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        except Exception as e:
            logger.debug(f"Peer read error ({address}): {e}")
        finally:
            await session.close()
            async with self._peer_lock:
                to_remove = [nid for nid, p in self.peers.items()
                             if p.websocket is ws]
                for nid in to_remove:
                    del self.peers[nid]
                    logger.info(f"🔌 Outbound peer disconnected: {nid}")

    # ─── Message Protocol ──────────────────────────────

    async def _send_msg(self, ws, msg_type: int, payload: dict):
        """Send a msgpack message over WebSocket."""
        try:
            envelope = {"t": msg_type, "from": self.node_id, "ts": time.time(), "d": payload}
            data = msgpack.packb(envelope, use_bin_type=True)
            await ws.send_bytes(data)
            self.stats["messages_sent"] += 1
            self.stats["bytes_transferred"] += len(data)
        except Exception as e:
            logger.debug(f"Send error: {e}")

    async def _broadcast(self, msg_type: int, payload: dict, exclude_node: str = None):
        """Broadcast a message to all connected peers."""
        async with self._peer_lock:
            for nid, peer in list(self.peers.items()):
                if nid == exclude_node:
                    continue
                if peer.websocket and not peer.websocket.closed:
                    await self._send_msg(peer.websocket, msg_type, payload)

    async def _handle_message(self, data: bytes, ws, source_addr: str):
        """Route incoming messages to handlers."""
        try:
            envelope = msgpack.unpackb(data, raw=False)
            msg_type = envelope.get("t", 0)
            sender_id = envelope.get("from", "")
            payload = envelope.get("d", {})
            self.stats["messages_received"] += 1

            # Rate limit check (exempt handshake so new peers can connect)
            if msg_type not in (MSG_HANDSHAKE, MSG_HANDSHAKE_ACK):
                if not self._check_rate_limit(sender_id):
                    logger.warning(f"⚠️ Rate limit exceeded for {sender_id} — dropping message")
                    return
                # Block messages from unauthenticated peers (except handshake)
                if sender_id not in self._authenticated_peers:
                    logger.debug(f"Dropping message from unauthenticated peer {sender_id}")
                    return

            handlers = {
                MSG_HANDSHAKE: self._on_handshake,
                MSG_HANDSHAKE_ACK: self._on_handshake_ack,
                MSG_HEARTBEAT: self._on_heartbeat,
                MSG_HEARTBEAT_ACK: self._on_heartbeat_ack,
                MSG_CATALOG_SYNC: self._on_catalog_sync,
                MSG_CATALOG_REQUEST: self._on_catalog_request,
                MSG_ARTIFACT_REQUEST: self._on_artifact_request,
                MSG_ARTIFACT_RESPONSE: self._on_artifact_response,
                MSG_ARTIFACT_ANNOUNCE: self._on_artifact_announce,
                MSG_MISSION_BROADCAST: self._on_mission_broadcast,
                MSG_MISSION_JOIN: self._on_mission_join,
                MSG_MISSION_RESULT: self._on_mission_result,
                MSG_MISSION_STATUS: self._on_mission_status,
                MSG_AGENT_ANNOUNCE: self._on_agent_announce,
                MSG_KNOWLEDGE_QUERY: self._on_knowledge_query,
                MSG_KNOWLEDGE_RESPONSE: self._on_knowledge_response,
                MSG_PEER_LIST: self._on_peer_list,
            }

            handler = handlers.get(msg_type)
            if handler:
                await handler(sender_id, payload, ws, source_addr)
            else:
                logger.debug(f"Unknown message type: {msg_type}")

        except Exception as e:
            logger.warning(f"Message handling error: {e}")

    # ─── Message Handlers ──────────────────────────────

    async def _on_handshake(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle incoming handshake — register peer and respond."""
        # Verify auth token
        remote_token = payload.get("auth_token", "")
        if not remote_token or not hmac.compare_digest(remote_token, self.auth_token):
            logger.warning(f"🚫 Handshake rejected from {sender_id} — bad auth token")
            try:
                await ws.close()
            except Exception:
                pass
            return
        self._authenticated_peers.add(sender_id)

        # Check peer limit
        if len(self.peers) >= MAX_PEERS:
            logger.warning(f"🚫 Peer limit reached ({MAX_PEERS}), rejecting {sender_id}")
            await ws.close()
            return

        peer = PeerInfo(
            node_id=sender_id,
            node_name=payload.get("node_name", "unknown"),
            address=source_addr,
            connected_at=time.time(),
            last_heartbeat=time.time(),
            agent_count=payload.get("agent_count", 0),
            artifact_count=payload.get("artifact_count", 0),
            capabilities=payload.get("capabilities", {}),
            protocol_version=payload.get("protocol_version", 1),
            websocket=ws,
        )

        async with self._peer_lock:
            self.peers[sender_id] = peer

        logger.info(f"🤝 Peer registered: {peer.node_name} ({sender_id}) "
                     f"— {peer.agent_count} agents, {peer.artifact_count} artifacts")

        # Send handshake ACK (includes our auth token for mutual verification)
        await self._send_msg(ws, MSG_HANDSHAKE_ACK, {
            "node_id": self.node_id,
            "machine_id": getattr(self, 'machine_id', self.node_id),
            "node_name": self.node_name,
            "role": getattr(self, 'node_role', 'node'),
            "port": self.port,
            "agent_count": self._get_agent_count(),
            "artifact_count": len(self.store.catalog),
            "capabilities": self._get_capabilities(),
            "auth_token": self.auth_token,
        })

        # Send our peer list so the new peer can discover others
        peer_addrs = [p.address for p in self.peers.values() if p.node_id != sender_id]
        if peer_addrs:
            await self._send_msg(ws, MSG_PEER_LIST, {"peers": peer_addrs})

        # Trigger catalog sync
        await self._send_catalog(ws)
        self._save_state()

    async def _on_handshake_ack(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle handshake ACK — register peer."""
        # Verify auth token
        remote_token = payload.get("auth_token", "")
        if not remote_token or not hmac.compare_digest(remote_token, self.auth_token):
            logger.warning(f"🚫 Handshake ACK rejected from {sender_id} — bad auth token")
            try:
                await ws.close()
            except Exception:
                pass
            return
        self._authenticated_peers.add(sender_id)

        # Reconstruct proper address from payload
        peer_port = payload.get("port", DEFAULT_PORT)
        host = source_addr.split("//")[-1].split("/")[0].split(":")[0] if "//" in source_addr else source_addr.split(":")[0]
        proper_addr = f"ws://{host}:{peer_port}/ws"

        peer = PeerInfo(
            node_id=sender_id,
            node_name=payload.get("node_name", "unknown"),
            address=proper_addr,
            connected_at=time.time(),
            last_heartbeat=time.time(),
            agent_count=payload.get("agent_count", 0),
            artifact_count=payload.get("artifact_count", 0),
            capabilities=payload.get("capabilities", {}),
            protocol_version=payload.get("protocol_version", 1),
            websocket=ws,
        )

        async with self._peer_lock:
            self.peers[sender_id] = peer

        logger.info(f"🤝 Peer confirmed: {peer.node_name} ({sender_id})")
        self._save_state()

    async def _on_heartbeat(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle heartbeat — update peer liveness."""
        async with self._peer_lock:
            if sender_id in self.peers:
                self.peers[sender_id].last_heartbeat = time.time()
                self.peers[sender_id].agent_count = payload.get("agent_count", 0)
                self.peers[sender_id].artifact_count = payload.get("artifact_count", 0)

        await self._send_msg(ws, MSG_HEARTBEAT_ACK, {
            "agent_count": self._get_agent_count(),
            "artifact_count": len(self.store.catalog),
        })

    async def _on_heartbeat_ack(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle heartbeat ACK."""
        async with self._peer_lock:
            if sender_id in self.peers:
                self.peers[sender_id].last_heartbeat = time.time()
                self.peers[sender_id].agent_count = payload.get("agent_count", 0)
                self.peers[sender_id].artifact_count = payload.get("artifact_count", 0)
                # Calculate latency from round-trip
                self.peers[sender_id].latency_ms = max(0, (time.time() - self.peers[sender_id].last_heartbeat) * 1000)

    async def _on_catalog_sync(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle catalog sync — learn about peer's artifacts, request missing ones."""
        remote_catalog = payload.get("catalog", [])
        my_hashes = self.store.get_catalog_hashes()

        # Find artifacts we don't have
        missing = []
        for item in remote_catalog:
            h = item.get("hash", "")
            if h and h not in my_hashes:
                missing.append(h)

        if missing:
            logger.info(f"📥 Peer {sender_id} has {len(missing)} artifacts we don't — requesting...")
            # Request missing artifacts (batch) — track what we asked for
            for h in missing[:CATALOG_SYNC_BATCH]:
                self._pending_artifact_requests.add(h)
                await self._send_msg(ws, MSG_ARTIFACT_REQUEST, {"hash": h})

    async def _on_catalog_request(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle catalog request — send our catalog."""
        await self._send_catalog(ws)

    async def _on_artifact_request(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle artifact request — send content if we have it."""
        content_hash = payload.get("hash", "")
        content = self.store.retrieve(content_hash)
        meta = self.store.get_meta(content_hash)

        if content and meta:
            await self._send_msg(ws, MSG_ARTIFACT_RESPONSE, {
                "hash": content_hash,
                "content": content,  # msgpack handles bytes natively
                "meta": meta.to_dict(),
            })
            self.stats["artifacts_shared"] += 1
            logger.debug(f"📤 Shared artifact {content_hash[:12]} with {sender_id}")

    async def _on_artifact_response(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle artifact response — store the content we requested."""
        content_hash = payload.get("hash", "")
        content = payload.get("content", b"")
        meta_dict = payload.get("meta", {})

        # SECURITY: Only accept artifacts we actually requested
        if content_hash not in self._pending_artifact_requests:
            logger.warning(f"🚫 Unsolicited artifact {content_hash[:12]} from {sender_id} — rejected")
            return
        self._pending_artifact_requests.discard(content_hash)

        if content and meta_dict:
            # Verify hash
            actual_hash = ContentStore.compute_hash(content if isinstance(content, bytes) else content.encode())
            if actual_hash != content_hash:
                logger.warning(f"⚠️ Hash mismatch for artifact from {sender_id}: expected {content_hash[:12]}, got {actual_hash[:12]}")
                return

            meta = ArtifactMeta.from_dict(meta_dict)
            self.store.store(content if isinstance(content, bytes) else content.encode(), meta)
            self.stats["artifacts_received"] += 1
            logger.info(f"📥 Received artifact from {sender_id}: {meta.filename} ({meta.project})")

            # Also save to creative_workspace for local agents to find
            self._save_to_workspace(meta, content if isinstance(content, bytes) else content.encode())

    async def _on_artifact_announce(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle new artifact announcement — request it if we don't have it."""
        content_hash = payload.get("hash", "")
        if content_hash and not self.store.has(content_hash):
            self._pending_artifact_requests.add(content_hash)
            await self._send_msg(ws, MSG_ARTIFACT_REQUEST, {"hash": content_hash})

    async def _on_mission_broadcast(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle cross-network mission broadcast — a peer wants help."""
        mission_id = payload.get("mission_id", "")
        objective = payload.get("objective", "")
        required = payload.get("required_agents", 4)

        if mission_id in self.network_missions:
            return  # Already know about it

        mission = NetworkMission(
            mission_id=mission_id,
            objective=objective,
            originator_node=sender_id,
            required_agents=required,
        )
        self.network_missions[mission_id] = mission

        logger.info(f"🎯 Network mission received from {sender_id}: {objective[:80]}...")

        # Auto-join if we have available agents
        if self.daemon:
            available = self._get_available_agents(count=2)
            if available:
                await self._send_msg(ws, MSG_MISSION_JOIN, {
                    "mission_id": mission_id,
                    "node_id": self.node_id,
                    "agents": [{"id": a.agent_id, "name": a.display_name or a.name, "role": a.role}
                               for a in available],
                })
                mission.joined_nodes[self.node_id] = {
                    "agents": [a.agent_id for a in available],
                    "status": "joined",
                }

        # Forward to other peers (gossip)
        await self._broadcast(MSG_MISSION_BROADCAST, payload, exclude_node=sender_id)

    async def _on_mission_join(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle mission join — a peer is volunteering agents."""
        mission_id = payload.get("mission_id", "")
        mission = self.network_missions.get(mission_id)
        if not mission:
            return

        joining_node = payload.get("node_id", sender_id)
        agents = payload.get("agents", [])
        mission.joined_nodes[joining_node] = {
            "agents": agents,
            "status": "joined",
        }

        total_agents = sum(len(n.get("agents", [])) for n in mission.joined_nodes.values())
        logger.info(f"🎯 Mission {mission_id[:8]}: {joining_node} joined with {len(agents)} agents "
                     f"({total_agents}/{mission.required_agents} total)")

        # If we're the originator and have enough agents, start the mission
        if mission.originator_node == self.node_id and total_agents >= mission.required_agents:
            mission.status = "active"
            await self._broadcast(MSG_MISSION_STATUS, {
                "mission_id": mission_id,
                "status": "active",
                "subtasks": mission.subtasks,
            })

    async def _on_mission_result(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle mission result from a peer node."""
        mission_id = payload.get("mission_id", "")
        mission = self.network_missions.get(mission_id)
        if not mission:
            return

        result_node = payload.get("node_id", sender_id)
        result_text = payload.get("result", "")
        mission.results[result_node] = result_text

        logger.info(f"🎯 Mission {mission_id[:8]}: result from {result_node} ({len(result_text)} chars)")

        # Check if all nodes have reported
        if len(mission.results) >= len(mission.joined_nodes):
            mission.status = "completed"
            self.stats["missions_coordinated"] += 1
            logger.info(f"🏆 Network mission {mission_id[:8]} completed with {len(mission.results)} node results")

    async def _on_mission_status(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle mission status update."""
        mission_id = payload.get("mission_id", "")
        mission = self.network_missions.get(mission_id)
        if mission:
            mission.status = payload.get("status", mission.status)

    async def _on_agent_announce(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle agent directory announcement from a peer."""
        # Update peer's agent count
        async with self._peer_lock:
            if sender_id in self.peers:
                agents = payload.get("agents", [])
                self.peers[sender_id].agent_count = len(agents)

    async def _on_knowledge_query(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle knowledge query — search local content for the asking peer."""
        query = payload.get("query", "")
        query_id = payload.get("query_id", "")
        if not query:
            return

        # Search our artifacts for relevant content
        results = []
        query_lower = query.lower()
        for h, meta in self.store.catalog.items():
            score = 0
            if query_lower in meta.filename.lower():
                score += 3
            if query_lower in meta.project.lower():
                score += 2
            if any(query_lower in t.lower() for t in meta.tags):
                score += 1
            if meta.description and query_lower in meta.description.lower():
                score += 2
            if score > 0:
                content = self.store.retrieve(h)
                preview = ""
                if content:
                    try:
                        preview = content.decode("utf-8", errors="replace")[:500]
                    except Exception:
                        pass
                results.append({
                    "hash": h,
                    "filename": meta.filename,
                    "project": meta.project,
                    "score": score,
                    "preview": preview,
                    "agent": meta.agent_name,
                })

        results.sort(key=lambda r: r["score"], reverse=True)
        await self._send_msg(ws, MSG_KNOWLEDGE_RESPONSE, {
            "query_id": query_id,
            "query": query,
            "results": results[:10],
            "node_id": self.node_id,
        })

    async def _on_knowledge_response(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle knowledge query response from a peer."""
        # Store in a temporary results dict that callers can poll
        query_id = payload.get("query_id", "")
        if query_id:
            if not hasattr(self, "_knowledge_results"):
                self._knowledge_results = {}
            if query_id not in self._knowledge_results:
                self._knowledge_results[query_id] = []
            self._knowledge_results[query_id].extend(payload.get("results", []))

    async def _on_peer_list(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle peer list — discover new peers through existing ones."""
        peer_addrs = payload.get("peers", [])
        # SECURITY: Cap list size to prevent resource exhaustion
        peer_addrs = peer_addrs[:MAX_PEER_LIST_SIZE]
        for addr in peer_addrs:
            if not isinstance(addr, str) or not addr.startswith("ws"):
                continue
            # SECURITY: Validate address is not a dangerous target (SSRF)
            if not self._is_safe_peer_address(addr):
                logger.debug(f"Skipping unsafe peer address: {addr}")
                continue
            # Check peer count limit
            if len(self.peers) >= MAX_PEERS:
                break
            # Check if we're already connected
            already_connected = False
            async with self._peer_lock:
                for p in self.peers.values():
                    if addr in p.address or p.address in addr:
                        already_connected = True
                        break
            if not already_connected:
                asyncio.create_task(self._connect_to_peer(addr))

    # ─── Catalog & Gossip ──────────────────────────────

    async def _send_catalog(self, ws):
        """Send our catalog summary to a peer."""
        catalog = self.store.get_catalog_summary()
        # Send in batches
        for i in range(0, len(catalog), CATALOG_SYNC_BATCH):
            batch = catalog[i:i + CATALOG_SYNC_BATCH]
            await self._send_msg(ws, MSG_CATALOG_SYNC, {"catalog": batch})

    async def _gossip_loop(self):
        """Periodically sync catalogs with peers."""
        while self._running:
            try:
                await asyncio.sleep(GOSSIP_INTERVAL)
                async with self._peer_lock:
                    alive_peers = [p for p in self.peers.values()
                                   if p.is_alive() and p.websocket and not p.websocket.closed]

                for peer in alive_peers:
                    await self._send_catalog(peer.websocket)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Gossip error: {e}")

    async def _heartbeat_loop(self):
        """Send periodic heartbeats and prune dead peers."""
        while self._running:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)

                # Send heartbeats
                async with self._peer_lock:
                    for peer in list(self.peers.values()):
                        if peer.websocket and not peer.websocket.closed:
                            await self._send_msg(peer.websocket, MSG_HEARTBEAT, {
                                "agent_count": self._get_agent_count(),
                                "artifact_count": len(self.store.catalog),
                            })

                    # Prune dead peers
                    dead = [nid for nid, p in self.peers.items() if not p.is_alive()]
                    for nid in dead:
                        logger.info(f"💀 Pruning dead peer: {self.peers[nid].node_name} ({nid})")
                        ws = self.peers[nid].websocket
                        if ws and not ws.closed:
                            await ws.close()
                        del self.peers[nid]

                self._save_state()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Heartbeat error: {e}")

    async def _ingest_loop(self):
        """Periodically ingest new artifacts from creative_workspace."""
        while self._running:
            try:
                await asyncio.sleep(60)  # Every minute
                workspace = Path(__file__).parent / "brain" / "creative_workspace"
                registry_path = workspace / ".artifact_registry.json"
                registry = {}
                if registry_path.exists():
                    try:
                        registry = json.loads(registry_path.read_text())
                    except Exception:
                        pass
                count = self.store.ingest_from_workspace(str(workspace), self.node_id, registry)
                if count > 0:
                    # Announce new artifacts to peers
                    for h in list(self.store.catalog.keys())[-count:]:
                        await self._broadcast(MSG_ARTIFACT_ANNOUNCE, {"hash": h})

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Ingest error: {e}")

    # ─── Rendezvous (Cross-Subnet Auto-Discovery) ─────

    def _load_rendezvous_nodes(self) -> List[str]:
        """Load rendezvous tracker URLs from config or env.

        Sources (in priority order):
        1. REPRYNTT_RENDEZVOUS env var (comma-separated URLs)
        2. p2p_config.json "rendezvous_nodes" array
        3. Empty list (rendezvous disabled)

        Each entry is the base URL of another repryntt node's P2P port,
        e.g. "http://10.0.0.19:6600" or "http://myserver.example.com:6600"
        """
        # Env var takes priority
        env_val = os.environ.get("REPRYNTT_RENDEZVOUS", "").strip()
        if env_val:
            return [u.strip().rstrip("/") for u in env_val.split(",") if u.strip()]

        # Config file
        config_path = Path(__file__).parent / "p2p_config.json"
        if config_path.exists():
            try:
                with open(config_path) as f:
                    cfg = json.load(f)
                nodes = cfg.get("rendezvous_nodes", [])
                if nodes:
                    return [u.strip().rstrip("/") for u in nodes if u.strip()]
            except Exception:
                pass
        # Fallback: hardcoded bootstrap seeds
        return list(BOOTSTRAP_SEEDS)

    async def _handle_rendezvous_announce(self, request: web.Request) -> web.Response:
        """Accept announcement from a peer node — register it for discovery."""
        try:
            data = await request.json()
            node_id = str(data.get("node_id", "")).strip()[:64]
            address = str(data.get("address", "")).strip()[:256]
            node_name = str(data.get("node_name", "unknown")).strip()[:64]
            port = int(data.get("port", DEFAULT_PORT))

            if not node_id or not address:
                return web.json_response({"error": "Missing node_id or address"}, status=400)

            # Validate address format
            if not address.startswith(("ws://", "wss://")):
                return web.json_response({"error": "address must start with ws://"}, status=400)

            # Validate port range
            if not (1 <= port <= 65535):
                return web.json_response({"error": "invalid port"}, status=400)

            # Don't register ourselves
            if node_id == self.node_id:
                return web.json_response({"status": "self", "peers": len(self._rendezvous_registry)})

            # Cap registry size to prevent memory exhaustion
            if len(self._rendezvous_registry) >= 10000 and node_id not in self._rendezvous_registry:
                self._rendezvous_registry = {
                    nid: info for nid, info in self._rendezvous_registry.items()
                    if time.time() - info["last_seen"] < RENDEZVOUS_MAX_AGE
                }
                if len(self._rendezvous_registry) >= 10000:
                    return web.json_response({"error": "registry full"}, status=503)

            # Store in registry with timestamp (no auth tokens, no IPs)
            self._rendezvous_registry[node_id] = {
                "node_id": node_id,
                "node_name": node_name,
                "address": address,
                "port": port,
                "last_seen": time.time(),
            }
            logger.info(f"🔗 Rendezvous: registered {node_name} ({node_id}) @ {address}")

            # Auto-connect to this peer if not already connected
            if node_id not in self.peers and self._is_safe_peer_address(address):
                asyncio.create_task(self._connect_to_peer(address))

            return web.json_response({
                "status": "registered",
                "your_node_id": node_id,
                "tracker_node_id": self.node_id,
                "peers": len(self._rendezvous_registry),
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def _handle_rendezvous_peers(self, request: web.Request) -> web.Response:
        """Return list of all known nodes from rendezvous registry + live peers."""
        now = time.time()
        # Prune expired entries
        self._rendezvous_registry = {
            nid: info for nid, info in self._rendezvous_registry.items()
            if now - info["last_seen"] < RENDEZVOUS_MAX_AGE
        }
        # Combine registry + live connected peers
        all_nodes = {}
        for nid, info in self._rendezvous_registry.items():
            all_nodes[nid] = {
                "node_id": info["node_id"],
                "node_name": info["node_name"],
                "address": info["address"],
                "port": info.get("port", DEFAULT_PORT),
                "source": "rendezvous",
                "last_seen": info["last_seen"],
            }
        for nid, peer in self.peers.items():
            if peer.is_alive() and nid not in all_nodes:
                all_nodes[nid] = {
                    "node_id": nid,
                    "node_name": peer.node_name,
                    "address": peer.address,
                    "port": DEFAULT_PORT,
                    "source": "connected",
                    "last_seen": peer.last_heartbeat,
                }
        # Include self
        local_ip = self._get_local_ip()
        all_nodes[self.node_id] = {
            "node_id": self.node_id,
            "node_name": self.node_name,
            "address": f"ws://{local_ip}:{self.port}/ws",
            "port": self.port,
            "source": "self",
            "last_seen": now,
        }
        return web.json_response({
            "tracker_node_id": self.node_id,
            "count": len(all_nodes),
            "nodes": list(all_nodes.values()),
        })

    async def _rendezvous_loop(self):
        """Periodically announce to rendezvous trackers and discover new peers."""
        # Initial delay to let server start
        await asyncio.sleep(5)
        local_ip = self._get_local_ip()
        my_address = f"ws://{local_ip}:{self.port}/ws"

        while self._running:
            try:
                for tracker_url in self.rendezvous_nodes:
                    try:
                        async with aiohttp.ClientSession(
                            timeout=aiohttp.ClientTimeout(total=RENDEZVOUS_TIMEOUT)
                        ) as session:
                            # Step 1: Announce ourselves
                            announce_url = f"{tracker_url}/rendezvous/announce"
                            payload = {
                                "node_id": self.node_id,
                                "node_name": self.node_name,
                                "address": my_address,
                                "port": self.port,
                            }
                            async with session.post(announce_url, json=payload) as resp:
                                if resp.status == 200:
                                    result = await resp.json()
                                    logger.debug(
                                        f"🔗 Rendezvous announce to {tracker_url}: "
                                        f"{result.get('peers', 0)} peers known"
                                    )

                            # Step 2: Fetch peer list
                            peers_url = f"{tracker_url}/rendezvous/peers"
                            async with session.get(peers_url) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    nodes = data.get("nodes", [])
                                    for node_info in nodes:
                                        nid = node_info.get("node_id", "")
                                        addr = node_info.get("address", "")
                                        # Skip self and already-connected peers
                                        if nid == self.node_id:
                                            continue
                                        if nid in self.peers and self.peers[nid].is_alive():
                                            continue
                                        if addr and self._is_safe_peer_address(addr):
                                            logger.info(
                                                f"🔗 Rendezvous discovered: "
                                                f"{node_info.get('node_name', '?')} @ {addr}"
                                            )
                                            asyncio.create_task(self._connect_to_peer(addr))
                    except asyncio.TimeoutError:
                        logger.debug(f"🔗 Rendezvous timeout: {tracker_url}")
                    except Exception as e:
                        logger.debug(f"🔗 Rendezvous error ({tracker_url}): {e}")

                await asyncio.sleep(RENDEZVOUS_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Rendezvous loop error: {e}")
                await asyncio.sleep(RENDEZVOUS_INTERVAL)

    # ─── mDNS Discovery ───────────────────────────────

    async def _start_mdns(self):
        """Register this node via mDNS and discover LAN peers."""
        try:
            from zeroconf import Zeroconf, ServiceBrowser, ServiceInfo, ServiceStateChange
            import socket as sock

            # Get local IP
            local_ip = self._get_local_ip()

            # Register our service
            info = ServiceInfo(
                SERVICE_TYPE,
                f"{self.node_name}.{SERVICE_TYPE}",
                addresses=[sock.inet_aton(local_ip)],
                port=self.port,
                properties={
                    b"node_id": self.node_id.encode(),
                    b"version": str(PROTOCOL_VERSION).encode(),
                },
            )

            self._zeroconf = Zeroconf()
            self._zeroconf.register_service(info)
            self._mdns_info = info
            logger.info(f"📡 mDNS registered: {self.node_name} @ {local_ip}:{self.port}")

            # Browse for other SAIGE nodes
            class SAIGEListener:
                def __init__(self, node):
                    self.node = node

                def add_service(self, zc, type_, name):
                    info = zc.get_service_info(type_, name)
                    if info and info.port:
                        for addr_bytes in info.addresses:
                            addr = sock.inet_ntoa(addr_bytes)
                            if addr != local_ip or info.port != self.node.port:
                                peer_url = f"ws://{addr}:{info.port}/ws"
                                logger.info(f"📡 mDNS discovered peer: {peer_url}")
                                asyncio.create_task(self.node._connect_to_peer(peer_url))

                def remove_service(self, zc, type_, name):
                    pass

                def update_service(self, zc, type_, name):
                    pass

            ServiceBrowser(self._zeroconf, SERVICE_TYPE, SAIGEListener(self))
            logger.info(f"📡 mDNS browsing for SAIGE peers on LAN...")

        except ImportError:
            logger.info("📡 mDNS disabled (zeroconf not installed)")
        except Exception as e:
            logger.warning(f"📡 mDNS error: {e}")

    # ─── Public API ────────────────────────────────────

    async def publish_artifact(self, content: bytes, filename: str, project: str,
                                agent_name: str = "", agent_id: str = "",
                                mission_id: str = "", tags: List[str] = None) -> str:
        """
        Publish a new artifact to the network.
        Stores locally and announces to all peers.
        Returns the content hash.
        """
        meta = ArtifactMeta(
            content_hash="",
            filename=filename,
            project=project,
            size=len(content),
            created_at=time.time(),
            agent_name=agent_name,
            agent_id=agent_id,
            mission_id=mission_id,
            node_id=self.node_id,
            content_type=self._guess_content_type(filename),
            tags=tags or [],
        )

        content_hash = self.store.store(content, meta)

        # Announce to all peers
        await self._broadcast(MSG_ARTIFACT_ANNOUNCE, {
            "hash": content_hash,
            "filename": filename,
            "project": project,
            "size": len(content),
            "agent": agent_name,
        })

        return content_hash

    async def search_network(self, query: str, timeout: float = 10.0) -> List[Dict]:
        """
        Search the entire network for knowledge matching a query.
        Asks all peers and collects results.
        """
        query_id = str(uuid.uuid4())[:8]
        if not hasattr(self, "_knowledge_results"):
            self._knowledge_results = {}
        self._knowledge_results[query_id] = []

        # Send query to all peers
        await self._broadcast(MSG_KNOWLEDGE_QUERY, {
            "query": query,
            "query_id": query_id,
        })

        # Also search locally
        local_results = []
        query_lower = query.lower()
        for h, meta in self.store.catalog.items():
            if (query_lower in meta.filename.lower() or
                query_lower in meta.project.lower() or
                query_lower in meta.agent_name.lower()):
                content = self.store.retrieve(h)
                preview = ""
                if content:
                    try:
                        preview = content.decode("utf-8", errors="replace")[:500]
                    except Exception:
                        pass
                local_results.append({
                    "hash": h, "filename": meta.filename, "project": meta.project,
                    "preview": preview, "agent": meta.agent_name,
                    "node_id": self.node_id, "source": "local",
                })

        # Wait for remote results
        await asyncio.sleep(min(timeout, 5.0))
        remote_results = self._knowledge_results.pop(query_id, [])
        for r in remote_results:
            r["source"] = "remote"

        return local_results + remote_results

    async def broadcast_mission(self, objective: str, required_agents: int = 4) -> str:
        """
        Broadcast a mission to the network for cross-device collaboration.
        Returns mission_id.
        """
        mission_id = f"net_{uuid.uuid4().hex[:10]}"
        mission = NetworkMission(
            mission_id=mission_id,
            objective=objective,
            originator_node=self.node_id,
            required_agents=required_agents,
        )
        self.network_missions[mission_id] = mission

        await self._broadcast(MSG_MISSION_BROADCAST, {
            "mission_id": mission_id,
            "objective": objective,
            "required_agents": required_agents,
            "originator": self.node_id,
            "originator_name": self.node_name,
        })

        logger.info(f"🎯 Network mission broadcast: {objective[:80]}... ({mission_id})")
        return mission_id

    def get_status(self) -> dict:
        """Get full node status for API/display."""
        alive_peers = [p for p in self.peers.values() if p.is_alive()]
        return {
            "success": True,
            "node_id": self.node_id,
            "machine_id": getattr(self, 'machine_id', self.node_id),
            "node_name": self.node_name,
            "role": getattr(self, 'node_role', 'node'),
            "port": self.port,
            "running": self._running,
            "uptime_seconds": time.time() - self.stats["started_at"] if self.stats["started_at"] else 0,
            "peers": {
                "connected": len(alive_peers),
                "total_known": len(self.peers),
                "list": [p.to_dict() for p in alive_peers],
            },
            "artifacts": {
                "local_count": len(self.store.catalog),
                "total_size": sum(m.size for m in self.store.catalog.values()),
            },
            "missions": {
                "active": len([m for m in self.network_missions.values() if m.status in ("recruiting", "active")]),
                "completed": len([m for m in self.network_missions.values() if m.status == "completed"]),
                "list": [
                    {
                        "id": m.mission_id, "objective": m.objective[:100],
                        "status": m.status, "nodes": len(m.joined_nodes),
                        "originator": m.originator_node,
                    }
                    for m in self.network_missions.values()
                ],
            },
            "stats": self.stats,
            "network_agents": sum(p.agent_count for p in alive_peers) + self._get_agent_count(),
        }

    # ─── Helpers ───────────────────────────────────────

    def _get_agent_count(self) -> int:
        """Get local agent count from daemon."""
        if self.daemon and hasattr(self.daemon, "agents"):
            return len(self.daemon.agents)
        return 0

    def _get_available_agents(self, count: int = 2) -> list:
        """Get available agents for a network mission."""
        if not self.daemon:
            return []
        available = [a for a in self.daemon.agents.values()
                     if a.status == "active" and not a.active_mission_id]
        return available[:count]

    def _get_capabilities(self) -> dict:
        """Report this node's capabilities (enhanced with compute info by economy bridge)."""
        import shutil
        total_mem = 0
        try:
            from repryntt.hardware_profile import get_profile
            hw = get_profile()
            caps = {
                "platform": hw.platform,
                "arch": hw.arch,
                "hostname": hw.hostname,
                "ram_mb": hw.ram_mb,
                "has_gpu": hw.has_gpu,
                "gpu_backend": hw.gpu_backend,
                "gpu_name": hw.gpu_name,
                "gpu_vram_mb": hw.gpu_vram_mb,
                "can_mine": hw.can_mine,
                "can_train": hw.can_train,
                "disk_free_mb": hw.disk_free_mb,
                "economy_bridge": False,
            }
            return caps
        except Exception:
            pass

        # Fallback if hardware_profile unavailable
        total_mem = 0
        try:
            import psutil
            total_mem = psutil.virtual_memory().total // (1024 * 1024)
        except ImportError:
            from repryntt.platform_utils import get_ram_mb
            total_mem = get_ram_mb()

        from repryntt.platform_utils import has_nvidia_device, has_amd_gpu
        has_gpu = has_nvidia_device() or has_amd_gpu()

        caps = {
            "platform": platform.system(),
            "arch": platform.machine(),
            "hostname": platform.node(),
            "ram_mb": total_mem,
            "has_gpu": has_gpu,
            "has_local_model": os.path.exists(str(Path(__file__).parent / "models")),
            "disk_free_mb": shutil.disk_usage("/").free // (1024 * 1024),
            "economy_bridge": False,  # Set True when bridge is active
        }
        return caps

    def _get_local_ip(self) -> str:
        """Get the local LAN IP address."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def _guess_content_type(self, filename: str) -> str:
        """Guess content type from filename."""
        ext = Path(filename).suffix.lstrip(".").lower()
        if ext in ("py", "js", "ts", "sh", "cpp", "c", "rs", "go", "java"):
            return "code"
        elif ext in ("json", "yaml", "yml", "toml"):
            return "config"
        elif ext in ("md", "txt", "rst"):
            return "text"
        elif ext in ("csv", "tsv"):
            return "data"
        return "text"

    def _save_to_workspace(self, meta: ArtifactMeta, content: bytes):
        """Save a received artifact to the local creative_workspace."""
        try:
            # SECURITY: Sanitize project and filename to prevent path traversal
            safe_project = self._sanitize_project(meta.project)
            safe_filename = self._sanitize_filename(meta.filename)
            if not safe_project or not safe_filename:
                logger.warning(f"🚫 Rejected unsafe artifact path: {meta.project}/{meta.filename}")
                return

            workspace = Path(__file__).parent / "brain" / "creative_workspace"
            target_dir = workspace / safe_project
            target_dir.mkdir(parents=True, exist_ok=True)
            target_file = target_dir / safe_filename

            # SECURITY: Path jail — ensure resolved path stays within workspace
            try:
                target_file.resolve().relative_to(workspace.resolve())
            except ValueError:
                logger.warning(f"🚫 Path escape attempt blocked: {target_file}")
                return

            if not target_file.exists():
                target_file.write_bytes(content)
                logger.info(f"📂 Saved remote artifact to workspace: {safe_project}/{safe_filename}")
        except Exception as e:
            logger.debug(f"Failed to save to workspace: {e}")

    # ─── State Persistence ────────────────────────────

    @property
    def _known_peer_addresses(self) -> List[str]:
        """Get previously known peer addresses."""
        return self._state.get("known_peers", [])

    def _load_state(self):
        """Load persistent node state."""
        self._state = {}
        if self._state_file.exists():
            try:
                self._state = json.loads(self._state_file.read_text())
                # Only restore node_id from state if identity system didn't provide one
                # (identity system is authoritative; state file is fallback)
                if not hasattr(self, 'machine_id') or self.machine_id == self.node_id:
                    stored_id = self._state.get("node_id")
                    if stored_id:
                        self.node_id = stored_id
            except Exception:
                self._state = {}

    def _save_state(self):
        """Save persistent node state."""
        self._state = {
            "node_id": self.node_id,
            "machine_id": getattr(self, 'machine_id', self.node_id),
            "node_name": self.node_name,
            "role": getattr(self, 'node_role', 'node'),
            "port": self.port,
            "known_peers": list(set(
                [p.address for p in self.peers.values() if p.is_alive()] +
                self._state.get("known_peers", [])
            )),
            "last_saved": time.time(),
            "artifact_count": len(self.store.catalog),
        }
        try:
            self._state_file.write_text(json.dumps(self._state, indent=2))
        except Exception as e:
            logger.debug(f"Failed to save state: {e}")


# ═══════════════════════════════════════════════════════
#  STANDALONE RUNNER
# ═══════════════════════════════════════════════════════

async def main():
    """Run a standalone SAIGE P2P node."""
    import argparse

    parser = argparse.ArgumentParser(description="SAIGE P2P Mesh Node")
    parser.add_argument("--name", default=None, help="Node name")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port (default: {DEFAULT_PORT})")
    parser.add_argument("--seed", action="append", default=[], help="Seed peer address (ws://host:port)")
    parser.add_argument("--no-mdns", action="store_true", help="Disable mDNS LAN discovery")
    parser.add_argument("--data-dir", default=None, help="Data directory")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [P2P] %(message)s",
        datefmt="%H:%M:%S",
    )

    node = SAIGENode(
        node_name=args.name,
        port=args.port,
        seed_peers=args.seed,
        enable_mdns=not args.no_mdns,
        data_dir=args.data_dir,
    )

    await node.start()

    # Ingest existing workspace artifacts
    workspace = Path(__file__).parent / "brain" / "creative_workspace"
    if workspace.is_dir():
        registry_path = workspace / ".artifact_registry.json"
        registry = {}
        if registry_path.exists():
            try:
                registry = json.loads(registry_path.read_text())
            except Exception:
                pass
        count = node.store.ingest_from_workspace(str(workspace), node.node_id, registry)
        logger.info(f"📦 Initial ingest: {count} artifacts from creative_workspace")

    local_ip = node._get_local_ip()
    print(f"\n{'='*60}")
    print(f"  SAIGE P2P NODE ACTIVE")
    print(f"  Name:     {node.node_name}")
    print(f"  ID:       {node.node_id}")
    print(f"  Local:    ws://127.0.0.1:{node.port}/ws")
    print(f"  LAN:      ws://{local_ip}:{node.port}/ws")
    print(f"  Status:   http://{local_ip}:{node.port}/status")
    print(f"  Catalog:  http://{local_ip}:{node.port}/catalog")
    print(f"  Artifacts: {len(node.store.catalog)}")
    print(f"{'='*60}\n")

    # Keep running
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        await node.stop()


if __name__ == "__main__":
    asyncio.run(main())
