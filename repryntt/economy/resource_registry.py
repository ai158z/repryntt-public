"""
repryntt.economy.resource_registry — Decentralized Compute Resource Registry

Every node advertises its hardware specs, current load, pricing, and capabilities
to the network via gossip. The registry aggregates all known nodes into a live
view of total network compute, available capacity, and per-node listings.

Gossip message type: "resource_announce"
Broadcast interval: 30 seconds
Stale threshold: 120 seconds (4 missed announcements)
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

ANNOUNCE_INTERVAL = 30       # Seconds between resource announcements
STALE_THRESHOLD = 120        # Seconds before a node is considered offline
PLANCKS_PER_CREDIT = 100_000_000


# ── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class NodeCapabilities:
    """What a node can do, derived from hardware."""
    max_model_params_b: float = 0.0   # Largest model (billions of params) it can run
    can_inference: bool = False
    can_finetune: bool = False
    can_embed: bool = False
    can_transcribe: bool = False
    supported_backends: List[str] = field(default_factory=list)  # ["cuda", "cpu"]


@dataclass
class ResourceListing:
    """A single node's advertised compute resource."""
    # Identity
    node_id: str = ""
    host: str = ""
    port: int = 5001
    address: str = ""                # Blockchain wallet address

    # Hardware
    gpu_name: str = "CPU only"
    gpu_vram_mb: int = 0
    gpu_backend: str = "cpu"
    cpu_cores: int = 1
    ram_mb: int = 0
    platform: str = ""
    arch: str = ""

    # Live metrics
    gpu_load_pct: float = 0.0        # 0-100, current GPU utilization
    cpu_load_pct: float = 0.0        # 0-100, current CPU utilization
    ram_used_pct: float = 0.0        # 0-100, current RAM utilization
    active_workloads: int = 0        # Number of workloads currently processing
    max_concurrent: int = 1          # Max workloads this node can handle

    # Pricing (set by operator)
    price_per_hour_plancks: int = 0  # Plancks per hour of compute
    price_per_inference_plancks: int = 0  # Plancks per single inference request

    # Capabilities
    capabilities: NodeCapabilities = field(default_factory=NodeCapabilities)

    # Availability
    available: bool = True           # Operator can toggle off
    uptime_pct: float = 100.0
    reputation: float = 0.5          # 0.0-1.0

    # Metadata
    last_seen: float = field(default_factory=time.time)
    first_seen: float = field(default_factory=time.time)
    version: str = "2.0.0"

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.last_seen) > STALE_THRESHOLD

    @property
    def free_capacity_pct(self) -> float:
        if self.max_concurrent <= 0:
            return 0.0
        return max(0.0, 100.0 * (1 - self.active_workloads / self.max_concurrent))

    @property
    def price_per_hour_credits(self) -> float:
        return self.price_per_hour_plancks / PLANCKS_PER_CREDIT

    def to_dict(self) -> dict:
        d = asdict(self)
        d["is_stale"] = self.is_stale
        d["free_capacity_pct"] = self.free_capacity_pct
        d["price_per_hour_credits"] = self.price_per_hour_credits
        return d

    @staticmethod
    def from_dict(d: dict) -> "ResourceListing":
        caps_data = d.pop("capabilities", {})
        # Strip computed properties
        d.pop("is_stale", None)
        d.pop("free_capacity_pct", None)
        d.pop("price_per_hour_credits", None)
        caps = NodeCapabilities(**caps_data) if isinstance(caps_data, dict) else NodeCapabilities()
        return ResourceListing(capabilities=caps, **d)


@dataclass
class NetworkStats:
    """Aggregated view of the entire compute network."""
    total_nodes: int = 0
    active_nodes: int = 0       # Non-stale
    total_gpu_vram_mb: int = 0
    available_gpu_vram_mb: int = 0
    total_cpu_cores: int = 0
    total_ram_mb: int = 0
    active_workloads: int = 0
    total_capacity_slots: int = 0
    available_capacity_slots: int = 0
    avg_price_per_hour_credits: float = 0.0
    min_price_per_hour_credits: float = 0.0
    max_price_per_hour_credits: float = 0.0
    gpu_breakdown: Dict[str, int] = field(default_factory=dict)  # gpu_name → count

    def to_dict(self) -> dict:
        return asdict(self)


# ── Resource Registry ────────────────────────────────────────────────────────

class ResourceRegistry:
    """
    Maintains a live registry of all compute resources on the network.

    Each node runs a ResourceRegistry that:
    1. Builds a ResourceListing from local hardware + operator config
    2. Gossips it to the network every ANNOUNCE_INTERVAL seconds
    3. Listens for other nodes' announcements and stores them
    4. Provides queries: list nodes, filter by capability, aggregate stats
    """

    def __init__(self, node_id: str = "", address: str = ""):
        self.node_id = node_id
        self.address = address
        self._nodes: Dict[str, ResourceListing] = {}  # node_id → listing
        self._lock = threading.Lock()
        self._local_listing: Optional[ResourceListing] = None
        self._gossip = None      # Set by attach_gossip()
        self._blockchain = None  # Set by attach_blockchain()
        self._announce_thread: Optional[threading.Thread] = None
        self._running = False

        # Operator config (loaded from env or set programmatically)
        self._operator_price_hour = int(os.environ.get(
            "REPRYNTT_COMPUTE_PRICE_HOUR",
            str(int(1.5 * PLANCKS_PER_CREDIT))  # Default 1.5 CR/hour
        ))
        self._operator_price_inference = int(os.environ.get(
            "REPRYNTT_COMPUTE_PRICE_INFERENCE",
            str(int(0.01 * PLANCKS_PER_CREDIT))  # Default 0.01 CR per request
        ))
        self._operator_max_concurrent = int(os.environ.get(
            "REPRYNTT_MAX_CONCURRENT_WORKLOADS", "2"
        ))
        self._operator_available = os.environ.get(
            "REPRYNTT_COMPUTE_AVAILABLE", "1"
        ) == "1"

    # ── Setup ────────────────────────────────────────────────────────────

    def attach_gossip(self, gossip) -> None:
        """Attach gossip protocol for broadcasting/receiving announcements."""
        self._gossip = gossip
        gossip.on_message("resource_announce", self._handle_announcement)
        logger.info("ResourceRegistry: gossip attached")

    def attach_blockchain(self, blockchain) -> None:
        """Attach blockchain node for reputation/balance lookups."""
        self._blockchain = blockchain

    def build_local_listing(self) -> ResourceListing:
        """Build our own resource listing from hardware detection."""
        try:
            from repryntt.hardware_profile import get_profile
            hw = get_profile()
        except Exception:
            hw = None

        # Derive capabilities
        caps = NodeCapabilities()
        if hw:
            if hw.has_gpu:
                caps.can_inference = True
                caps.can_embed = True
                caps.supported_backends.append(hw.gpu_backend)
                # Estimate max model size from VRAM
                # Rule of thumb: ~2GB VRAM per 1B params (quantized)
                caps.max_model_params_b = round(hw.gpu_vram_mb / 2048, 1)
                if hw.can_train:
                    caps.can_finetune = True
            else:
                caps.can_inference = hw.can_run_local_llm
                caps.supported_backends.append("cpu")
                caps.max_model_params_b = round(hw.ram_mb / 4096, 1)  # CPU needs more RAM

        # Get live load metrics
        gpu_load = 0.0
        cpu_load = 0.0
        ram_used = 0.0
        try:
            import psutil
            cpu_load = psutil.cpu_percent(interval=0.1)
            mem = psutil.virtual_memory()
            ram_used = mem.percent
        except ImportError:
            pass

        # Count active workloads from blockchain contract
        active = 0
        if self._blockchain and hasattr(self._blockchain, 'contract'):
            claimed = self._blockchain.contract.claimed_workloads
            if self.address:
                active = sum(1 for v in claimed.values()
                             if v.get("miner") == self.address)

        rep = 0.5
        if self._blockchain and self.address in (self._blockchain.reputation or {}):
            rep = self._blockchain.reputation[self.address]

        listing = ResourceListing(
            node_id=self.node_id,
            host=self._blockchain.host if self._blockchain else "0.0.0.0",
            port=self._blockchain.port if self._blockchain else 5001,
            address=self.address,
            gpu_name=hw.gpu_name if hw else "Unknown",
            gpu_vram_mb=hw.gpu_vram_mb if hw else 0,
            gpu_backend=hw.gpu_backend if hw else "cpu",
            cpu_cores=os.cpu_count() or 1,
            ram_mb=hw.ram_mb if hw else 0,
            platform=hw.platform if hw else "",
            arch=hw.arch if hw else "",
            gpu_load_pct=gpu_load,
            cpu_load_pct=cpu_load,
            ram_used_pct=ram_used,
            active_workloads=active,
            max_concurrent=self._operator_max_concurrent,
            price_per_hour_plancks=self._operator_price_hour,
            price_per_inference_plancks=self._operator_price_inference,
            capabilities=caps,
            available=self._operator_available,
            uptime_pct=100.0,
            reputation=rep,
            last_seen=time.time(),
            first_seen=self._local_listing.first_seen if self._local_listing else time.time(),
        )

        self._local_listing = listing
        # Also store in our own registry
        with self._lock:
            self._nodes[self.node_id] = listing

        return listing

    # ── Gossip ───────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start periodic resource announcement."""
        if self._running:
            return
        self._running = True
        self._announce_thread = threading.Thread(
            target=self._announce_loop, daemon=True, name="resource-announce"
        )
        self._announce_thread.start()
        logger.info(f"ResourceRegistry started (announce every {ANNOUNCE_INTERVAL}s)")

    def stop(self) -> None:
        self._running = False

    def _announce_loop(self) -> None:
        while self._running:
            try:
                listing = self.build_local_listing()
                if self._gossip:
                    self._gossip.gossip("resource_announce", listing.to_dict())
                self._prune_stale()
            except Exception as e:
                logger.warning(f"Resource announce error: {e}")
            time.sleep(ANNOUNCE_INTERVAL)

    def _handle_announcement(self, msg) -> None:
        """Handle incoming resource_announce gossip message."""
        try:
            payload = msg.payload if hasattr(msg, "payload") else msg
            if isinstance(payload, dict):
                listing = ResourceListing.from_dict(payload.copy())
                if listing.node_id and listing.node_id != self.node_id:
                    listing.last_seen = time.time()
                    with self._lock:
                        self._nodes[listing.node_id] = listing
        except Exception as e:
            logger.debug(f"Bad resource announcement: {e}")

    def _prune_stale(self) -> None:
        """Remove nodes that haven't announced in STALE_THRESHOLD seconds."""
        now = time.time()
        with self._lock:
            stale = [nid for nid, n in self._nodes.items()
                     if (now - n.last_seen) > STALE_THRESHOLD * 3
                     and nid != self.node_id]
            for nid in stale:
                del self._nodes[nid]

    # ── Queries ──────────────────────────────────────────────────────────

    def list_nodes(self, include_stale: bool = False) -> List[ResourceListing]:
        """Get all known nodes, optionally including stale ones."""
        with self._lock:
            nodes = list(self._nodes.values())
        if not include_stale:
            nodes = [n for n in nodes if not n.is_stale]
        return sorted(nodes, key=lambda n: n.reputation, reverse=True)

    def list_available(self) -> List[ResourceListing]:
        """Get nodes that are available and have free capacity."""
        return [
            n for n in self.list_nodes()
            if n.available and n.active_workloads < n.max_concurrent
        ]

    def find_nodes_for_workload(
        self,
        min_vram_mb: int = 0,
        min_model_params_b: float = 0.0,
        need_gpu: bool = False,
        max_price_per_hour_plancks: int = 0,
    ) -> List[ResourceListing]:
        """Find available nodes matching workload requirements."""
        candidates = self.list_available()
        results = []
        for n in candidates:
            if need_gpu and n.gpu_vram_mb == 0:
                continue
            if min_vram_mb > 0 and n.gpu_vram_mb < min_vram_mb:
                continue
            if min_model_params_b > 0 and n.capabilities.max_model_params_b < min_model_params_b:
                continue
            if max_price_per_hour_plancks > 0 and n.price_per_hour_plancks > max_price_per_hour_plancks:
                continue
            results.append(n)
        # Score: cheaper + higher reputation + more free capacity = better
        results.sort(key=lambda n: (
            -n.reputation,
            n.price_per_hour_plancks,
            -n.free_capacity_pct,
        ))
        return results

    def get_network_stats(self) -> NetworkStats:
        """Aggregate statistics across the entire network."""
        nodes = self.list_nodes(include_stale=False)
        if not nodes:
            return NetworkStats()

        gpu_breakdown: Dict[str, int] = {}
        total_vram = 0
        avail_vram = 0
        total_cores = 0
        total_ram = 0
        active_wl = 0
        total_cap = 0
        avail_cap = 0
        prices = []

        for n in nodes:
            total_vram += n.gpu_vram_mb
            if n.available and n.active_workloads < n.max_concurrent:
                avail_vram += n.gpu_vram_mb
            total_cores += n.cpu_cores
            total_ram += n.ram_mb
            active_wl += n.active_workloads
            total_cap += n.max_concurrent
            avail_cap += max(0, n.max_concurrent - n.active_workloads)

            if n.gpu_name and n.gpu_name != "CPU only":
                gpu_breakdown[n.gpu_name] = gpu_breakdown.get(n.gpu_name, 0) + 1

            if n.price_per_hour_plancks > 0:
                prices.append(n.price_per_hour_credits)

        return NetworkStats(
            total_nodes=len(nodes),
            active_nodes=len([n for n in nodes if n.available]),
            total_gpu_vram_mb=total_vram,
            available_gpu_vram_mb=avail_vram,
            total_cpu_cores=total_cores,
            total_ram_mb=total_ram,
            active_workloads=active_wl,
            total_capacity_slots=total_cap,
            available_capacity_slots=avail_cap,
            avg_price_per_hour_credits=sum(prices) / len(prices) if prices else 0.0,
            min_price_per_hour_credits=min(prices) if prices else 0.0,
            max_price_per_hour_credits=max(prices) if prices else 0.0,
            gpu_breakdown=gpu_breakdown,
        )

    def get_node(self, node_id: str) -> Optional[ResourceListing]:
        with self._lock:
            return self._nodes.get(node_id)

    # ── Operator Controls ────────────────────────────────────────────────

    def set_price(self, price_per_hour_credits: float, price_per_inference_credits: float = 0.01) -> None:
        """Operator sets their compute pricing."""
        self._operator_price_hour = int(price_per_hour_credits * PLANCKS_PER_CREDIT)
        self._operator_price_inference = int(price_per_inference_credits * PLANCKS_PER_CREDIT)
        logger.info(f"Compute price set: {price_per_hour_credits} CR/hr, "
                     f"{price_per_inference_credits} CR/inference")

    def set_available(self, available: bool) -> None:
        """Operator toggles compute availability."""
        self._operator_available = available
        logger.info(f"Compute availability: {'ON' if available else 'OFF'}")

    def set_max_concurrent(self, max_concurrent: int) -> None:
        """Operator sets max concurrent workloads."""
        self._operator_max_concurrent = max(1, max_concurrent)
