#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║  P2P ↔ BLOCKCHAIN ECONOMY BRIDGE                                    ║
║  Connects the SAIGE P2P mesh network to the Proof-of-Power          ║
║  blockchain, enabling multi-device GPU compute economy.              ║
║                                                                      ║
║  Architecture:                                                       ║
║    Device A (task) ──► P2P Mesh ──► Device B (GPU)                   ║
║    Device B mines  ──► P2P Mesh ──► Device A (result)                ║
║    Blockchain records all transactions and rewards                   ║
║                                                                      ║
║  This bridge:                                                        ║
║    1. Announces GPU compute availability over P2P                    ║
║    2. Relays blockchain workloads to remote miners via P2P           ║
║    3. Returns mining results + PoP proofs over P2P                   ║
║    4. Synchronizes blocks + state across P2P peers                   ║
║    5. Measures real GPU TFLOPS for accurate rewards                  ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import asyncio
import hashlib
import json
import logging
import os
import socket
import struct
import time
import threading
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("p2p_economy_bridge")

# ═══════════════════════════════════════════════════════════════
#  NEW P2P MESSAGE TYPES FOR COMPUTE ECONOMY
#  0x70-0x7F range: Compute / Economy messages
# ═══════════════════════════════════════════════════════════════

MSG_COMPUTE_ANNOUNCE    = 0x70  # "I have GPU compute available"
MSG_COMPUTE_REQUEST     = 0x71  # "Here's a workload to process"
MSG_COMPUTE_CLAIM       = 0x72  # "I'm claiming this workload"
MSG_COMPUTE_RESULT      = 0x73  # "Here's my result + PoP proof"
MSG_COMPUTE_REJECT      = 0x74  # "Workload rejected (invalid/expired)"
MSG_BLOCK_ANNOUNCE      = 0x75  # "New block mined" (blockchain sync)
MSG_BLOCK_REQUEST       = 0x76  # "Send me block at height N"
MSG_BLOCK_RESPONSE      = 0x77  # "Here's the block data"
MSG_ECONOMY_STATUS      = 0x78  # "My blockchain state summary"

# All economy message types for registration
ECONOMY_MESSAGE_TYPES = {
    MSG_COMPUTE_ANNOUNCE, MSG_COMPUTE_REQUEST, MSG_COMPUTE_CLAIM,
    MSG_COMPUTE_RESULT, MSG_COMPUTE_REJECT, MSG_BLOCK_ANNOUNCE,
    MSG_BLOCK_REQUEST, MSG_BLOCK_RESPONSE, MSG_ECONOMY_STATUS,
}


# ═══════════════════════════════════════════════════════════════
#  GPU BENCHMARKING — Real TFLOPS Measurement
# ═══════════════════════════════════════════════════════════════

def measure_gpu_tflops() -> Tuple[float, Dict[str, Any]]:
    """
    Measure actual GPU compute capability in TFLOPS.
    Runs a short matrix multiply benchmark on the available device.
    
    Returns:
        (tflops, device_info_dict)
    """
    device_info = {
        "device_type": "cpu",
        "device_name": "CPU",
        "cuda_available": False,
        "vram_mb": 0,
        "measured_tflops": 0.1,
    }
    
    try:
        import torch
        
        if torch.cuda.is_available():
            device = torch.device('cuda')
            device_info["cuda_available"] = True
            device_info["device_type"] = "cuda"
            device_info["device_name"] = torch.cuda.get_device_name(0)
            props = torch.cuda.get_device_properties(0)
            vram = getattr(props, 'total_memory', 0) or getattr(props, 'total_mem', 0)
            device_info["vram_mb"] = vram // (1024 * 1024) if vram else 0
        else:
            device = torch.device('cpu')
        
        # Benchmark: time a large matrix multiplication
        # FLOPs for matmul(M×K, K×N) = 2*M*N*K
        M, N, K = 1024, 1024, 1024
        flops_per_matmul = 2 * M * N * K  # ~2.1 billion FLOPs
        
        # Warm up
        a = torch.randn(M, K, device=device, dtype=torch.float32)
        b = torch.randn(K, N, device=device, dtype=torch.float32)
        _ = torch.matmul(a, b)
        if device.type == 'cuda':
            torch.cuda.synchronize()
        
        # Benchmark (average over multiple runs)
        num_runs = 5
        if device.type == 'cuda':
            torch.cuda.synchronize()
        
        start = time.perf_counter()
        for _ in range(num_runs):
            _ = torch.matmul(a, b)
        if device.type == 'cuda':
            torch.cuda.synchronize()
        elapsed = time.perf_counter() - start
        
        avg_time = elapsed / num_runs
        tflops = (flops_per_matmul / avg_time) / 1e12
        
        # Clamp to reasonable range
        tflops = max(0.01, min(tflops, 500.0))
        device_info["measured_tflops"] = round(tflops, 3)
        
        # Cleanup
        del a, b
        if device.type == 'cuda':
            torch.cuda.empty_cache()
        
        logger.info(f"⚡ GPU Benchmark: {tflops:.3f} TFLOPS ({device_info['device_name']})")
        return tflops, device_info
        
    except ImportError:
        logger.warning("PyTorch not available — using CPU estimate (0.1 TFLOPS)")
        return 0.1, device_info
    except Exception as e:
        logger.warning(f"GPU benchmark failed: {e} — using fallback estimate")
        return 0.1, device_info


# ═══════════════════════════════════════════════════════════════
#  COMPUTE PEER TRACKER
# ═══════════════════════════════════════════════════════════════

@dataclass
class ComputePeer:
    """A peer that has announced GPU compute availability."""
    node_id: str
    wallet_address: str
    tflops: float
    device_type: str        # 'cuda' or 'cpu'
    device_name: str
    vram_mb: int
    available: bool = True  # Currently accepting workloads
    active_workloads: int = 0
    max_concurrent: int = 1
    last_announce: float = 0.0
    reputation: float = 1.0
    total_completed: int = 0
    total_failed: int = 0
    
    def is_fresh(self) -> bool:
        """Check if the compute announcement is still valid (< 5 min old)."""
        return (time.time() - self.last_announce) < 300
    
    def is_available(self) -> bool:
        """Check if peer can accept a new workload."""
        return (self.available 
                and self.is_fresh() 
                and self.active_workloads < self.max_concurrent
                and self.reputation > 0.3)


@dataclass
class PendingWorkload:
    """A workload that's been dispatched to the P2P network."""
    workload_id: str
    workload_key: str
    submitter_node: str
    submitter_wallet: str
    workload_data: Any
    workload_type: str      # 'inference', 'training', 'analysis'
    claimed_by: Optional[str] = None  # node_id of miner
    result: Any = None
    status: str = "pending"  # pending, claimed, completed, failed, expired
    created_at: float = field(default_factory=time.time)
    claimed_at: float = 0.0
    completed_at: float = 0.0
    claim_timeout: float = 120.0  # 2 minutes to complete after claiming
    fee_plancks: int = 1000000    # 0.01 CR default
    
    def is_expired(self) -> bool:
        if self.status == "claimed" and self.claimed_at > 0:
            return (time.time() - self.claimed_at) > self.claim_timeout
        if self.status == "pending":
            return (time.time() - self.created_at) > 300  # 5 min to claim
        return False


# ═══════════════════════════════════════════════════════════════
#  P2P ECONOMY BRIDGE
# ═══════════════════════════════════════════════════════════════

class P2PEconomyBridge:
    """
    Bridges the SAIGE P2P mesh network with the Proof-of-Power blockchain.
    
    This class:
    - Registers compute message handlers on the P2P node
    - Announces this device's GPU capabilities to the mesh
    - Dispatches blockchain workloads to remote GPU peers
    - Receives mining results and submits them to the local blockchain
    - Synchronizes blocks across the P2P mesh
    
    Usage:
        bridge = P2PEconomyBridge(p2p_node, blockchain_node, economy_manager)
        await bridge.start()
    """
    
    def __init__(self, p2p_node, blockchain_node=None, economy_manager=None):
        """
        Args:
            p2p_node: SAIGENode instance (from saige_p2p.py)
            blockchain_node: ProofOfPowerBlockchain instance (from qnode2.py)  
            economy_manager: RobotEconomyManager instance
        """
        self.p2p = p2p_node
        self.blockchain = blockchain_node
        self.economy = economy_manager
        
        # Compute peers discovered on the network
        self.compute_peers: Dict[str, ComputePeer] = {}
        
        # Workloads dispatched to the network
        self.pending_workloads: Dict[str, PendingWorkload] = {}
        
        # Results received from remote miners
        self.completed_results: Dict[str, Dict] = {}
        
        # Our own compute capabilities
        self.local_tflops = 0.1
        self.local_device_info = {}
        self.local_wallet_address = ""
        
        # Configuration
        self.announce_interval = 60      # Announce compute every 60s
        self.cleanup_interval = 30       # Cleanup expired workloads every 30s
        self.block_sync_interval = 69    # Sync blocks at block interval
        self.accept_remote_work = True   # Whether to mine for remote peers
        self.max_remote_workloads = 2    # Max concurrent remote workloads
        self._active_remote_mining = 0
        
        # Background tasks
        self._tasks: List[asyncio.Task] = []
        self._running = False
        
        # Locks
        self._compute_lock = asyncio.Lock()
        self._workload_lock = asyncio.Lock()
        
        # Stats
        self.stats = {
            "workloads_dispatched": 0,
            "workloads_received": 0,
            "workloads_completed": 0,
            "workloads_failed": 0,
            "blocks_synced": 0,
            "compute_peers_seen": 0,
            "tokens_earned_remote": 0.0,
            "tokens_paid_remote": 0.0,
        }
    
    # ─── Initialization ──────────────────────────────────────
    
    async def start(self):
        """Start the economy bridge — register handlers, benchmark GPU, begin announcing."""
        if self._running:
            return
        self._running = True
        
        # Benchmark GPU
        self.local_tflops, self.local_device_info = measure_gpu_tflops()
        
        # Get wallet address
        self.local_wallet_address = self._get_wallet_address()
        
        # Register P2P message handlers
        self._register_handlers()
        
        # Enhance the P2P node's capability reporting
        self._enhance_capabilities()
        
        # Start background loops
        self._tasks.append(asyncio.create_task(self._announce_loop()))
        self._tasks.append(asyncio.create_task(self._cleanup_loop()))
        if self.blockchain:
            self._tasks.append(asyncio.create_task(self._block_sync_loop()))
        
        logger.info(
            f"🌐 P2P Economy Bridge ACTIVE | "
            f"{self.local_tflops:.2f} TFLOPS | "
            f"Wallet: {self.local_wallet_address[:16]}... | "
            f"Accept remote work: {self.accept_remote_work}"
        )
    
    async def stop(self):
        """Stop all bridge background tasks."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        logger.info("🌐 P2P Economy Bridge stopped")
    
    def _get_wallet_address(self) -> str:
        """Get the local miner/AI wallet address."""
        # Try economy manager first
        if self.economy:
            try:
                brain = getattr(self.economy, 'brain_system', None)
                if brain:
                    wallet = getattr(brain, 'personality_brain', {}).get('ai_wallet', '')
                    if wallet:
                        return wallet
            except Exception:
                pass
        
        # Try blockchain node
        if self.blockchain:
            # Look for the AI wallet in balances (highest balance that's not SYSTEM/DAO)
            system_addrs = {'SYSTEM', 'DAO', 'FAUCET', 'STAKE_POOL', 'burn',
                            '0000000000000000000000000000000000000000'}
            try:
                candidates = {k: v for k, v in self.blockchain.balances.items()
                              if k not in system_addrs and v > 0}
                if candidates:
                    return max(candidates, key=candidates.get)
            except Exception:
                pass
        
        # Fallback: generate deterministic address from node ID
        return hashlib.sha1(self.p2p.node_id.encode()).hexdigest()
    
    def _register_handlers(self):
        """Register economy message handlers on the P2P node."""
        # The handlers dict in saige_p2p.py's _handle_message is rebuilt each call,
        # so we inject our handlers by monkey-patching the _handle_message method.
        original_handler = self.p2p._handle_message
        bridge = self
        
        async def extended_handle_message(data, ws, source_addr):
            """Extended message handler that includes economy messages."""
            import msgpack
            try:
                envelope = msgpack.unpackb(data, raw=False)
                msg_type = envelope.get("t", 0)
                
                # Check if this is an economy message
                if msg_type in ECONOMY_MESSAGE_TYPES:
                    sender_id = envelope.get("from", "")
                    payload = envelope.get("d", {})
                    
                    # Must be authenticated (same check as P2P)
                    if sender_id not in bridge.p2p._authenticated_peers:
                        if msg_type not in (0x01, 0x02):  # handshake exempt
                            logger.debug(f"Dropping economy message from unauthenticated {sender_id}")
                            return
                    
                    economy_handlers = {
                        MSG_COMPUTE_ANNOUNCE: bridge._on_compute_announce,
                        MSG_COMPUTE_REQUEST: bridge._on_compute_request,
                        MSG_COMPUTE_CLAIM:   bridge._on_compute_claim,
                        MSG_COMPUTE_RESULT:  bridge._on_compute_result,
                        MSG_COMPUTE_REJECT:  bridge._on_compute_reject,
                        MSG_BLOCK_ANNOUNCE:  bridge._on_block_announce,
                        MSG_BLOCK_REQUEST:   bridge._on_block_request,
                        MSG_BLOCK_RESPONSE:  bridge._on_block_response,
                        MSG_ECONOMY_STATUS:  bridge._on_economy_status,
                    }
                    handler = economy_handlers.get(msg_type)
                    if handler:
                        await handler(sender_id, payload, ws, source_addr)
                    return
            except Exception:
                pass
            
            # Fall through to original handler for non-economy messages
            await original_handler(data, ws, source_addr)
        
        self.p2p._handle_message = extended_handle_message
        logger.info("📡 Economy message handlers registered on P2P node")
    
    def _enhance_capabilities(self):
        """Enhance the P2P node's capability dict with compute/economy info."""
        original_get_caps = self.p2p._get_capabilities
        bridge = self
        
        def enhanced_capabilities() -> dict:
            caps = original_get_caps()
            caps.update({
                "gpu_tflops": bridge.local_tflops,
                "gpu_device": bridge.local_device_info.get("device_name", "unknown"),
                "gpu_vram_mb": bridge.local_device_info.get("vram_mb", 0),
                "mining_available": bridge.accept_remote_work,
                "wallet_address": bridge.local_wallet_address,
                "economy_bridge": True,  # Flag: this node supports compute economy
            })
            return caps
        
        self.p2p._get_capabilities = enhanced_capabilities
    
    # ─── Background Loops ────────────────────────────────────
    
    async def _announce_loop(self):
        """Periodically announce compute availability to the mesh."""
        while self._running:
            try:
                await self._broadcast_compute_announce()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Compute announce error: {e}")
            await asyncio.sleep(self.announce_interval)
    
    async def _cleanup_loop(self):
        """Periodically clean up expired workloads and stale peers."""
        while self._running:
            try:
                await self._cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Cleanup error: {e}")
            await asyncio.sleep(self.cleanup_interval)
    
    async def _block_sync_loop(self):
        """Periodically share new blocks with P2P peers."""
        last_height = 0
        if self.blockchain:
            last_height = len(self.blockchain.chain)
        
        while self._running:
            try:
                if self.blockchain:
                    current_height = len(self.blockchain.chain)
                    if current_height > last_height:
                        # Announce new blocks
                        for i in range(last_height, current_height):
                            await self._announce_block(i)
                        last_height = current_height
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Block sync error: {e}")
            await asyncio.sleep(self.block_sync_interval)
    
    # ─── Outbound: Announcements ─────────────────────────────
    
    async def _broadcast_compute_announce(self):
        """Announce this device's compute availability to all peers."""
        payload = {
            "wallet_address": self.local_wallet_address,
            "tflops": self.local_tflops,
            "device_type": self.local_device_info.get("device_type", "cpu"),
            "device_name": self.local_device_info.get("device_name", "Unknown"),
            "vram_mb": self.local_device_info.get("vram_mb", 0),
            "available": self.accept_remote_work and (
                self._active_remote_mining < self.max_remote_workloads
            ),
            "max_concurrent": self.max_remote_workloads,
            "active_workloads": self._active_remote_mining,
            "reputation": self._get_local_reputation(),
            "blockchain_height": len(self.blockchain.chain) if self.blockchain else 0,
        }
        await self.p2p._broadcast(MSG_COMPUTE_ANNOUNCE, payload)
        logger.debug(f"📡 Broadcast compute announce: {self.local_tflops:.2f} TFLOPS, "
                     f"available={payload['available']}")
    
    async def _announce_block(self, block_index: int):
        """Announce a new block to P2P peers."""
        if not self.blockchain or block_index >= len(self.blockchain.chain):
            return
        
        block = self.blockchain.chain[block_index]
        block_dict = block.to_dict() if hasattr(block, 'to_dict') else {}
        
        payload = {
            "block_index": block_index,
            "block_hash": block_dict.get("hash", ""),
            "prev_hash": block_dict.get("previous_hash", ""),
            "timestamp": block_dict.get("timestamp", 0),
            "tx_count": len(block_dict.get("transactions", [])),
            "total_supply": sum(self.blockchain.balances.values()) / 1e8 if self.blockchain else 0,
        }
        await self.p2p._broadcast(MSG_BLOCK_ANNOUNCE, payload)
        self.stats["blocks_synced"] += 1
    
    # ─── Outbound: Workload Dispatch ─────────────────────────
    
    async def dispatch_workload(self, workload_key: str, workload_data: Any,
                                 workload_type: str = "inference",
                                 submitter_wallet: str = None,
                                 fee_plancks: int = 1000000) -> Optional[str]:
        """
        Dispatch a workload to the P2P network for remote mining.
        
        This is called by the blockchain node or economy manager when a workload 
        is submitted but no local miner is available (or for load balancing).
        
        Args:
            workload_key: SHA3-512 hash identifying the workload
            workload_data: The actual workload (prompt, task data, etc.)
            workload_type: 'inference', 'training', or 'analysis'
            submitter_wallet: Wallet that submitted/pays for the workload
            fee_plancks: Fee in plancks for the workload
            
        Returns:
            workload_id if dispatched, None if no compute peers available
        """
        # Find available compute peers 
        available_peers = [
            p for p in self.compute_peers.values()
            if p.is_available() and p.node_id != self.p2p.node_id
        ]
        
        if not available_peers:
            logger.debug("No remote compute peers available for workload dispatch")
            return None
        
        # Sort by: highest TFLOPS × reputation (best miner first)
        available_peers.sort(key=lambda p: p.tflops * p.reputation, reverse=True)
        
        workload_id = str(uuid.uuid4())[:12]
        
        # Serialize workload data for transmission
        if isinstance(workload_data, dict):
            serialized_data = workload_data
        elif isinstance(workload_data, str):
            serialized_data = {"prompt": workload_data, "type": workload_type}
        else:
            serialized_data = {"raw": str(workload_data), "type": workload_type}
        
        pending = PendingWorkload(
            workload_id=workload_id,
            workload_key=workload_key,
            submitter_node=self.p2p.node_id,
            submitter_wallet=submitter_wallet or self.local_wallet_address,
            workload_data=serialized_data,
            workload_type=workload_type,
            fee_plancks=fee_plancks,
        )
        
        async with self._workload_lock:
            self.pending_workloads[workload_id] = pending
        
        # Broadcast workload request to the network
        payload = {
            "workload_id": workload_id,
            "workload_key": workload_key,
            "workload_type": workload_type,
            "workload_data": serialized_data,
            "submitter_node": self.p2p.node_id,
            "submitter_wallet": submitter_wallet or self.local_wallet_address,
            "fee_plancks": fee_plancks,
            "required_tflops": 0.1,  # minimum TFLOPS required
            "deadline": time.time() + 300,  # 5 min deadline
        }
        
        await self.p2p._broadcast(MSG_COMPUTE_REQUEST, payload)
        
        self.stats["workloads_dispatched"] += 1
        logger.info(f"📤 Workload dispatched to P2P: {workload_id} ({workload_type}) — "
                    f"{len(available_peers)} compute peers available")
        
        return workload_id
    
    async def get_workload_result(self, workload_id: str, timeout: float = 180.0) -> Optional[Dict]:
        """
        Wait for a remote workload result.
        
        Args:
            workload_id: The workload ID returned by dispatch_workload()
            timeout: Max seconds to wait
            
        Returns:
            Result dict or None if timed out
        """
        deadline = time.time() + timeout
        while time.time() < deadline and self._running:
            if workload_id in self.completed_results:
                return self.completed_results.pop(workload_id)
            
            async with self._workload_lock:
                wl = self.pending_workloads.get(workload_id)
                if wl and wl.status == "failed":
                    return {"success": False, "error": "Workload failed on remote miner"}
            
            await asyncio.sleep(0.5)
        
        return None
    
    # ─── Inbound: Message Handlers ───────────────────────────
    
    async def _on_compute_announce(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle a compute availability announcement from a peer."""
        wallet = payload.get("wallet_address", "")
        tflops = payload.get("tflops", 0.1)
        
        async with self._compute_lock:
            if sender_id in self.compute_peers:
                # Update existing
                peer = self.compute_peers[sender_id]
                peer.tflops = tflops
                peer.available = payload.get("available", True)
                peer.active_workloads = payload.get("active_workloads", 0)
                peer.last_announce = time.time()
                peer.device_name = payload.get("device_name", peer.device_name)
                peer.vram_mb = payload.get("vram_mb", peer.vram_mb)
            else:
                # New compute peer
                self.compute_peers[sender_id] = ComputePeer(
                    node_id=sender_id,
                    wallet_address=wallet,
                    tflops=tflops,
                    device_type=payload.get("device_type", "cpu"),
                    device_name=payload.get("device_name", "Unknown"),
                    vram_mb=payload.get("vram_mb", 0),
                    available=payload.get("available", True),
                    max_concurrent=payload.get("max_concurrent", 1),
                    active_workloads=payload.get("active_workloads", 0),
                    last_announce=time.time(),
                    reputation=payload.get("reputation", 1.0),
                )
                self.stats["compute_peers_seen"] += 1
                logger.info(f"⚡ New compute peer: {sender_id} — "
                           f"{tflops:.2f} TFLOPS ({payload.get('device_name', 'Unknown')})")
    
    async def _on_compute_request(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle a workload request from a remote peer — mine it if we can."""
        if not self.accept_remote_work:
            return
        
        if self._active_remote_mining >= self.max_remote_workloads:
            logger.debug(f"Skipping workload from {sender_id} — at capacity")
            return
        
        workload_id = payload.get("workload_id", "")
        workload_key = payload.get("workload_key", "")
        workload_data = payload.get("workload_data", {})
        workload_type = payload.get("workload_type", "inference")
        deadline = payload.get("deadline", 0)
        
        # Check deadline
        if deadline and time.time() > deadline:
            logger.debug(f"Skipping expired workload {workload_id}")
            return
        
        # Claim the workload
        claim_payload = {
            "workload_id": workload_id,
            "miner_node": self.p2p.node_id,
            "miner_wallet": self.local_wallet_address,
            "tflops": self.local_tflops,
        }
        
        # Send claim to submitter
        peer_info = self.p2p.peers.get(sender_id)
        if peer_info and peer_info.websocket:
            await self.p2p._send_msg(peer_info.websocket, MSG_COMPUTE_CLAIM, claim_payload)
        
        self._active_remote_mining += 1
        self.stats["workloads_received"] += 1
        logger.info(f"⛏️  Mining remote workload {workload_id} from {sender_id} ({workload_type})")
        
        # Process the workload in background
        asyncio.create_task(self._mine_remote_workload(
            sender_id, workload_id, workload_key, workload_data, workload_type, ws
        ))
    
    async def _mine_remote_workload(self, sender_id: str, workload_id: str,
                                     workload_key: str, workload_data: dict,
                                     workload_type: str, ws):
        """Actually perform AI inference for a remote peer's workload."""
        start_time = time.time()
        try:
            # Extract prompt from workload
            prompt = workload_data.get("prompt", "")
            if not prompt:
                prompt = json.dumps(workload_data) if isinstance(workload_data, dict) else str(workload_data)
            
            max_tokens = workload_data.get("max_tokens", 512)
            temperature = workload_data.get("temperature", 0.7)
            
            # Call local LLM (same path as local miner)
            result = await self._call_local_llm(prompt, max_tokens, temperature)
            
            computation_time = time.time() - start_time
            
            if result is None:
                raise RuntimeError("LLM inference failed")
            
            # Generate PoP proof
            pop_proof = self._generate_pop_proof(
                workload_key, workload_data, result,
                self.local_wallet_address, computation_time
            )
            
            # Send result back to submitter
            result_payload = {
                "workload_id": workload_id,
                "workload_key": workload_key,
                "miner_node": self.p2p.node_id,
                "miner_wallet": self.local_wallet_address,
                "result": result,
                "computation_time": computation_time,
                "tflops": self.local_tflops,
                "pop_proof": pop_proof,
                "device_type": self.local_device_info.get("device_type", "cpu"),
            }
            
            # Send directly to the submitter peer
            peer_info = self.p2p.peers.get(sender_id)
            if peer_info and peer_info.websocket:
                await self.p2p._send_msg(peer_info.websocket, MSG_COMPUTE_RESULT, result_payload)
            else:
                # Submitter disconnected — broadcast result
                await self.p2p._broadcast(MSG_COMPUTE_RESULT, result_payload)
            
            self.stats["workloads_completed"] += 1
            logger.info(f"✅ Remote workload {workload_id} completed in {computation_time:.1f}s")
            
        except Exception as e:
            logger.error(f"❌ Remote workload {workload_id} failed: {e}")
            self.stats["workloads_failed"] += 1
            
            # Notify submitter of failure
            reject_payload = {
                "workload_id": workload_id,
                "reason": str(e),
                "miner_node": self.p2p.node_id,
            }
            peer_info = self.p2p.peers.get(sender_id)
            if peer_info and peer_info.websocket:
                await self.p2p._send_msg(peer_info.websocket, MSG_COMPUTE_REJECT, reject_payload)
        
        finally:
            self._active_remote_mining = max(0, self._active_remote_mining - 1)
    
    async def _on_compute_claim(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle a workload claim from a remote miner."""
        workload_id = payload.get("workload_id", "")
        miner_wallet = payload.get("miner_wallet", "")
        
        async with self._workload_lock:
            wl = self.pending_workloads.get(workload_id)
            if not wl:
                return
            
            if wl.status != "pending":
                # Already claimed by someone else
                return
            
            wl.status = "claimed"
            wl.claimed_by = sender_id
            wl.claimed_at = time.time()
        
        logger.info(f"🤝 Workload {workload_id} claimed by {sender_id} "
                    f"({payload.get('tflops', '?')} TFLOPS)")
    
    async def _on_compute_result(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle a completed workload result from a remote miner."""
        workload_id = payload.get("workload_id", "")
        workload_key = payload.get("workload_key", "")
        miner_wallet = payload.get("miner_wallet", "")
        result = payload.get("result")
        computation_time = payload.get("computation_time", 0)
        pop_proof = payload.get("pop_proof", {})
        
        async with self._workload_lock:
            wl = self.pending_workloads.get(workload_id)
            if not wl:
                logger.warning(f"Received result for unknown workload {workload_id}")
                return
            
            if wl.status == "completed":
                return  # Already got a result
        
        # Verify the PoP proof
        proof_valid = self._verify_pop_proof(pop_proof, wl.workload_data, result, miner_wallet)
        
        if not proof_valid:
            logger.warning(f"❌ Invalid PoP proof from {sender_id} for workload {workload_id}")
            # Penalize reputation
            async with self._compute_lock:
                if sender_id in self.compute_peers:
                    self.compute_peers[sender_id].reputation *= 0.8
                    self.compute_peers[sender_id].total_failed += 1
            return
        
        # Valid result — update state
        async with self._workload_lock:
            wl.status = "completed"
            wl.result = result
            wl.completed_at = time.time()
        
        # Store result for retrieval
        self.completed_results[workload_id] = {
            "success": True,
            "result": result,
            "miner_node": sender_id,
            "miner_wallet": miner_wallet,
            "computation_time": computation_time,
            "workload_key": workload_key,
        }
        
        # Submit the completed work to the local blockchain for reward minting
        if self.blockchain:
            try:
                reward_result = self.blockchain.process_ai_workload_with_pop(
                    workload_key=workload_key,
                    workload_data=wl.workload_data,
                    computation_result=result,
                    miner_address=miner_wallet,
                    computation_time=computation_time,
                    workload_type=wl.workload_type,
                )
                if reward_result.get("success"):
                    reward_cr = reward_result.get("reward", 0)
                    self.stats["tokens_paid_remote"] += reward_cr
                    logger.info(f"💰 Remote miner {miner_wallet[:16]}... rewarded "
                               f"{reward_cr:.4f} CR for workload {workload_id}")
            except Exception as e:
                logger.error(f"Failed to submit remote work to blockchain: {e}")
        
        # Update compute peer stats
        async with self._compute_lock:
            if sender_id in self.compute_peers:
                self.compute_peers[sender_id].total_completed += 1
                self.compute_peers[sender_id].reputation = min(
                    1.0, self.compute_peers[sender_id].reputation + 0.05
                )
        
        self.stats["workloads_completed"] += 1
        logger.info(f"✅ Remote result received for {workload_id} from {sender_id} "
                    f"({computation_time:.1f}s)")
    
    async def _on_compute_reject(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle a workload rejection/failure from a remote miner."""
        workload_id = payload.get("workload_id", "")
        reason = payload.get("reason", "unknown")
        
        async with self._workload_lock:
            wl = self.pending_workloads.get(workload_id)
            if wl and wl.status != "completed":
                wl.status = "failed"
        
        logger.warning(f"⚠️ Workload {workload_id} rejected by {sender_id}: {reason}")
        
        # Reduce miner reputation
        async with self._compute_lock:
            if sender_id in self.compute_peers:
                self.compute_peers[sender_id].reputation *= 0.9
                self.compute_peers[sender_id].total_failed += 1
    
    async def _on_block_announce(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle a new block announcement from a peer."""
        if not self.blockchain:
            return
        
        remote_height = payload.get("block_index", 0)
        local_height = len(self.blockchain.chain) - 1
        
        if remote_height > local_height:
            # Peer has blocks we don't — request them
            for i in range(local_height + 1, remote_height + 1):
                peer_info = self.p2p.peers.get(sender_id)
                if peer_info and peer_info.websocket:
                    await self.p2p._send_msg(peer_info.websocket, MSG_BLOCK_REQUEST, {
                        "block_index": i,
                        "requesting_node": self.p2p.node_id,
                    })
            logger.info(f"📥 Requesting blocks {local_height+1}–{remote_height} from {sender_id}")
    
    async def _on_block_request(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle a block data request from a peer."""
        if not self.blockchain:
            return
        
        block_index = payload.get("block_index", 0)
        if 0 <= block_index < len(self.blockchain.chain):
            block = self.blockchain.chain[block_index]
            block_dict = block.to_dict() if hasattr(block, 'to_dict') else {}
            
            peer_info = self.p2p.peers.get(sender_id)
            if peer_info and peer_info.websocket:
                await self.p2p._send_msg(peer_info.websocket, MSG_BLOCK_RESPONSE, {
                    "block_index": block_index,
                    "block_data": block_dict,
                })
    
    async def _on_block_response(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle a block data response — integrate into local chain."""
        if not self.blockchain:
            return
        
        block_data = payload.get("block_data", {})
        block_index = payload.get("block_index", 0)
        
        if not block_data:
            return
        
        # Import block into blockchain (thread-safe)
        try:
            # Use the blockchain's existing block import mechanism
            if hasattr(self.blockchain, 'receive_block'):
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    lambda: self.blockchain.receive_block(block_data)
                )
                self.stats["blocks_synced"] += 1
                logger.info(f"📦 Block {block_index} synced from {sender_id}")
        except Exception as e:
            logger.warning(f"Failed to import block {block_index}: {e}")
    
    async def _on_economy_status(self, sender_id: str, payload: dict, ws, source_addr: str):
        """Handle economy status from a peer."""
        # Just log — useful for debugging and monitoring
        remote_height = payload.get("blockchain_height", 0)
        remote_supply = payload.get("total_supply", 0)
        logger.debug(f"📊 Economy status from {sender_id}: "
                    f"height={remote_height}, supply={remote_supply:.2f} CR")
    
    # ─── Local LLM Interface ────────────────────────────────
    
    async def _call_local_llm(self, prompt: str, max_tokens: int = 512,
                               temperature: float = 0.7) -> Optional[str]:
        """Call the local llama.cpp server for AI inference."""
        import aiohttp
        
        llm_url = os.environ.get("SAIGE_LLM_URL", "http://localhost:8080/v1/chat/completions")
        
        payload = {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": min(max_tokens, 2048),
            "temperature": temperature,
            "stream": False,
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(llm_url, json=payload, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    else:
                        logger.error(f"LLM returned status {resp.status}")
                        return None
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return None
    
    # ─── PoP Proof Generation/Verification ───────────────────
    
    def _generate_pop_proof(self, workload_key: str, workload_data: Any,
                             result: Any, miner_address: str,
                             computation_time: float) -> dict:
        """Generate a Proof of Power proof for completed work."""
        workload_str = json.dumps(workload_data, sort_keys=True, default=str) if isinstance(workload_data, dict) else str(workload_data)
        result_str = json.dumps(result, sort_keys=True, default=str) if isinstance(result, dict) else str(result)
        
        workload_hash = hashlib.sha3_512(workload_str.encode()).hexdigest()
        result_hash = hashlib.sha3_512(result_str.encode()).hexdigest()
        
        proof = {
            "workload_key": workload_key,
            "workload_hash": workload_hash,
            "result_hash": result_hash,
            "miner_address": miner_address,
            "timestamp": time.time(),
            "computation_time": computation_time,
            "verification_method": "deterministic",
            "metadata": {
                "result_size": len(result_str),
                "workload_size": len(workload_str),
                "tflops": self.local_tflops,
                "device": self.local_device_info.get("device_name", "unknown"),
            },
        }
        
        # Tamper-proof hash of entire proof
        proof_str = json.dumps(proof, sort_keys=True, default=str)
        proof["proof_hash"] = hashlib.sha3_512(proof_str.encode()).hexdigest()
        
        return proof
    
    def _verify_pop_proof(self, proof: dict, workload_data: Any,
                           result: Any, expected_miner: str) -> bool:
        """Verify a PoP proof from a remote miner."""
        if not proof:
            return False
        
        try:
            # Verify proof hash integrity
            proof_copy = {k: v for k, v in proof.items() if k != "proof_hash"}
            proof_str = json.dumps(proof_copy, sort_keys=True, default=str)
            expected_hash = hashlib.sha3_512(proof_str.encode()).hexdigest()
            
            if proof.get("proof_hash") != expected_hash:
                logger.warning("PoP proof hash mismatch — tampered!")
                return False
            
            # Verify workload hash
            workload_str = json.dumps(workload_data, sort_keys=True, default=str) if isinstance(workload_data, dict) else str(workload_data)
            workload_hash = hashlib.sha3_512(workload_str.encode()).hexdigest()
            
            if proof.get("workload_hash") != workload_hash:
                logger.warning("PoP workload hash mismatch")
                return False
            
            # Verify result hash
            result_str = json.dumps(result, sort_keys=True, default=str) if isinstance(result, dict) else str(result)
            result_hash = hashlib.sha3_512(result_str.encode()).hexdigest()
            
            if proof.get("result_hash") != result_hash:
                logger.warning("PoP result hash mismatch")
                return False
            
            # Verify miner
            if expected_miner and proof.get("miner_address") != expected_miner:
                logger.warning("PoP miner address mismatch")
                return False
            
            # Verify timestamp (not in future, not older than 10 min)
            ts = proof.get("timestamp", 0)
            now = time.time()
            if ts > now + 60:
                logger.warning("PoP timestamp in the future")
                return False
            if ts < now - 600:
                logger.warning("PoP timestamp too old (>10 min)")
                return False
            
            return True
            
        except Exception as e:
            logger.warning(f"PoP verification error: {e}")
            return False
    
    # ─── Helpers ─────────────────────────────────────────────
    
    def _get_local_reputation(self) -> float:
        """Get this node's reputation from the blockchain."""
        if self.blockchain and self.local_wallet_address:
            return self.blockchain.reputation.get(self.local_wallet_address, 1.0)
        return 1.0
    
    async def _cleanup_expired(self):
        """Clean up expired workloads and stale compute peers."""
        now = time.time()
        
        # Expire old workloads
        async with self._workload_lock:
            expired = [wid for wid, wl in self.pending_workloads.items()
                       if wl.is_expired()]
            for wid in expired:
                self.pending_workloads[wid].status = "expired"
                logger.debug(f"Workload {wid} expired")
        
        # Remove stale compute peers (no announce for > 10 min)
        async with self._compute_lock:
            stale = [nid for nid, cp in self.compute_peers.items()
                     if (now - cp.last_announce) > 600]
            for nid in stale:
                del self.compute_peers[nid]
                logger.debug(f"Removed stale compute peer {nid}")
    
    def get_network_compute_summary(self) -> Dict[str, Any]:
        """Get a summary of the compute network state."""
        active_peers = [p for p in self.compute_peers.values() if p.is_fresh()]
        total_tflops = sum(p.tflops for p in active_peers)
        
        return {
            "local_tflops": self.local_tflops,
            "local_device": self.local_device_info.get("device_name", "unknown"),
            "local_wallet": self.local_wallet_address,
            "compute_peers": len(active_peers),
            "total_network_tflops": round(total_tflops + self.local_tflops, 3),
            "available_miners": len([p for p in active_peers if p.is_available()]),
            "pending_workloads": len([w for w in self.pending_workloads.values()
                                      if w.status in ("pending", "claimed")]),
            "blockchain_height": len(self.blockchain.chain) if self.blockchain else 0,
            "stats": self.stats.copy(),
            "peers": [
                {
                    "node_id": p.node_id,
                    "tflops": p.tflops,
                    "device": p.device_name,
                    "available": p.is_available(),
                    "reputation": round(p.reputation, 2),
                    "completed": p.total_completed,
                }
                for p in active_peers
            ],
        }
