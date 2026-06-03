#!/usr/bin/env python3
"""
Robot Economy Manager - Integration Layer for AI Autonomous Framework
Coordinates the entire Reprynt 2040 robot economy system within SAIGE's autonomous framework
"""

import asyncio
import os
import sys
import json
import time
import threading
import logging
import socket
import struct
from datetime import datetime
from typing import Dict, List, Any, Optional
from pathlib import Path

# Robot economy components
from repryntt.economy.qnode2 import ProofOfPowerBlockchain as Blockchain
from repryntt.economy.workload_submitter import WorkloadSubmitter
# SelfEvolvingAI is DEPRECATED and imports torch (~400MB RSS).
# Lazy-loaded in _start_ais() to avoid bloating every process.
from repryntt.economy.smartcontracts import WorkloadContract
from repryntt.economy.dao import PlanetaryDAO
from repryntt.economy.wallet import Wallet
from repryntt.economy.transaction import Transaction
from repryntt.economy.safe_serialize import pack as safe_pack, unpack as safe_unpack

# Database imports
from repryntt.database import init_database, get_db_session
try:
    from repryntt.database.models import Wallet as WalletModel
except Exception:
    WalletModel = None  # DB models unavailable — queries will use fallback path


def get_ai_wallet_address(manager) -> str:
    """Get the AI's system wallet address from the manager's personality brain data."""
    # Check personality_brain dict
    pb = getattr(manager, 'personality_brain', None)
    if pb and isinstance(pb, dict) and 'ai_wallet' in pb:
        addr = pb['ai_wallet']
        if addr and not all(c == '0' for c in addr):
            return addr
    # Check cached address
    cached = getattr(manager, '_ai_wallet_address', None)
    if cached and not all(c == '0' for c in cached):
        return cached
    # No fallback — let the caller try the next tier.
    # Returning zeros would poison the PoPW wallet resolution.
    return ""


class BlockchainNodeClient:
    """
    Lightweight JSON-RPC client that connects to the running Rust blockchain node.
    Exposes the same API surface as ProofOfPowerBlockchain so the manager
    can use it interchangeably for read and write operations.
    """

    def __init__(self, host: str, port: int):
        self.host = host if host != '0.0.0.0' else '127.0.0.1'
        self.port = port
        self.lock = threading.Lock()
        self.logger = logging.getLogger(f"{__name__}.NodeClient")
        # Cached state — refreshed from Rust JSON-RPC explorer endpoints.
        self._balances: Dict[str, int] = {}
        self._stakes: Dict[str, int] = {}
        self._reputation: Dict[str, float] = {}
        self._chain_length = 0
        self._latest_block: Dict[str, Any] = {}
        self._difficulty = 1
        self._network_tflops = 0.0
        self._faucet_used_wallets: set = set()
        self._pending_workloads = 0
        self._peers = 0
        self._is_client = True  # Flag to distinguish from real Blockchain
        # Sync on init
        self.refresh()

    def _rpc(self, method: str, params: Optional[dict] = None, timeout: float = 10.0) -> dict:
        """Send a JSON-RPC request to the Rust node."""
        from repryntt.economy.rust_chain_client import rpc_call

        resp = rpc_call(method, params or {}, host=self.host, port=self.port, timeout=timeout)
        if "error" in resp:
            return {"success": False, "error": resp["error"]}
        return {"success": True, **resp}

    def refresh(self):
        """Refresh cached state from the running node."""
        info = self._rpc("get_chain_info")
        if not info.get("success"):
            self.logger.warning(f"Failed to refresh node state: {info.get('error')}")
            return

        latest = self._rpc("get_latest_block")
        richlist = self._rpc("get_richlist", {"limit": 500, "offset": 0})
        network = self._rpc("get_network_stats")
        mempool = self._rpc("get_mempool_txs")

        self._chain_length = int(info.get("height", 0))
        self._latest_block = latest.get("block", latest) if latest.get("success") else {}
        self._balances = {}
        self._stakes = {}
        if richlist.get("success"):
            for entry in richlist.get("richlist", []):
                address = entry.get("address")
                if address:
                    self._balances[address] = int(entry.get("balance_plancks", 0))
                    self._stakes[address] = int(entry.get("stake_plancks", 0))
        if network.get("success"):
            self._peers = int(network.get("peer_count", network.get("peers", 0)) or 0)
            self._network_tflops = float(network.get("network_tflops", 0.0) or 0.0)
        if mempool.get("success"):
            self._pending_workloads = len(mempool.get("pending_transactions", []))

    # --- Properties that match Blockchain API surface ---

    @property
    def balances(self):
        return self._balances

    @property
    def stakes(self):
        return self._stakes

    @property
    def reputation(self):
        return self._reputation

    @property
    def chain(self):
        """Return a list-like object whose len() gives chain length."""
        return [None] * self._chain_length

    @property
    def difficulty(self):
        return self._difficulty

    @property
    def faucet_used_wallets(self):
        return self._faucet_used_wallets

    @property
    def node_compute_shares(self):
        return {"total": self._network_tflops}

    @property
    def peers(self):
        return [None] * self._peers

    @property
    def contract(self):
        return _ClientContractProxy(self._pending_workloads)

    @property
    def tx_pool(self):
        return _ClientTxPoolProxy()

    def get_latest_block(self):
        self.refresh()
        return _ClientBlock(self._latest_block)

    def get_network_stats(self):
        resp = self._rpc("get_network_stats")
        if resp.get("success"):
            return resp
        return {}

    def get_leaderboard(self, top_n=20):
        resp = self._rpc("get_leaderboard", {"top_n": top_n})
        if resp.get("success"):
            return resp.get("leaderboard", [])
        return []

    def save_state(self):
        """No-op — state is saved by the real node."""
        pass

    # --- Write operations via signed Rust RPC ---

    def credit_address(
        self,
        address: str,
        amount_plancks: int,
        purpose: str = "robot_economy_credit",
        metadata: Optional[dict] = None,
    ) -> dict:
        from repryntt.economy.rust_chain_client import submit_node_signed_workload_credit

        resp = submit_node_signed_workload_credit(
            to_address=address,
            amount_plancks=amount_plancks,
            purpose=purpose,
            metadata=metadata or {},
            host=self.host,
            port=self.port,
        )
        if "error" in resp:
            return {"success": False, "error": resp["error"]}
        self.refresh()
        balance = self.get_balance(address)
        return {
            "success": True,
            "tx_hash": resp.get("tx_hash"),
            "address": address,
            "amount_plancks": amount_plancks,
            "balance_credits": balance.get("balance_credits", 0),
        }

    def transfer(self, from_address: str, to_address: str, amount_plancks: int) -> dict:
        return {
            "success": False,
            "error": "Rust transfers require a locally held signing key; use RobotEconomyManager.transfer_credits",
        }

    def faucet_claim(self, address: str, amount_credits: float = 10.0) -> dict:
        amount_plancks = int(amount_credits * 100000000)
        return self.credit_address(
            address,
            amount_plancks,
            purpose="faucet_claim",
            metadata={"amount_credits": amount_credits, "source": "faucet"},
        )

    def get_balance(self, address: str) -> dict:
        resp = self._rpc("get_balance", {"address": address})
        if not resp.get("success"):
            return resp
        return {
            "success": True,
            "address": address,
            "balance_plancks": int(resp.get("balance_plancks", 0)),
            "balance_credits": float(resp.get("balance_cr", 0.0)),
            "stake_plancks": int(resp.get("stake_plancks", 0)),
            "stake_credits": float(resp.get("stake_cr", 0.0)),
            "nonce": int(resp.get("nonce", 0)),
        }


class _ClientBlock:
    """Minimal block representation from node client."""
    def __init__(self, data: dict):
        self.index = data.get("index", 0)
        self.hash = data.get("hash", "0" * 64)
        self.timestamp = data.get("timestamp", 0)
        self.miner_address = data.get("miner", "SYSTEM")


class _ClientContractProxy:
    """Minimal proxy for contract queries."""
    def __init__(self, pending_count):
        self.valid_keys = [None] * pending_count
        self.workloads = {}


class _ClientTxPoolProxy:
    """Minimal proxy for tx pool."""
    def add_transaction(self, tx, balances, require_signature=False):
        return False, "Cannot add transactions via client — use node directly"

class RobotEconomyManager:
    """Central coordinator for the robot economy ecosystem"""
    _instance = None

    def __new__(cls, brain_system=None, use_database=True):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, brain_system=None, use_database=True):
        # Guard: only initialize once for the singleton
        if hasattr(self, '_initialized') and self._initialized:
            # Update brain_system reference if provided, but don't reset state
            if brain_system is not None:
                self.brain_system = brain_system
            return
        self._initialized = True
        self.brain_system = brain_system
        self.use_database = use_database
        self.logger = logging.getLogger(__name__)

        # Network configuration for peer-to-peer blockchain (configurable via env)
        _bootstrap_env = os.environ.get('REPRYNTT_BOOTSTRAP_NODES', os.environ.get('SAIGE_BOOTSTRAP_NODES', ''))
        if _bootstrap_env:
            self.bootstrap_nodes = [n.strip() for n in _bootstrap_env.split(',') if n.strip()]
        else:
            self.bootstrap_nodes = []  # Standalone mode — no external peers
        self.max_peers = 8  # Maximum number of peer connections
        self.network_synced = False

        # Initialize database if enabled
        if self.use_database:
            if not init_database():
                self.logger.error("❌ Database initialization failed - falling back to JSON storage")
                self.use_database = False

        # Economy state
        self.is_running = False
        self.nodes = {}
        self.submitters = {}
        self.ais = {}
        
        # P2P Economy Bridge
        self.p2p_node = None        # Set externally by evolution loop / daemon
        self._p2p_loop = None       # asyncio event loop for P2P mesh
        self.economy_bridge = None  # P2PEconomyBridge instance
        
        # Wallet private keys for signing transactions (address -> private_key_bytes)
        self.wallet_keys: Dict[str, bytes] = {}

        # Configuration
        self.config = {
            'node_host': os.environ.get(
                'REPRYNTT_RUST_RPC_HOST',
                os.environ.get('REPRYNTT_NODE_HOST', os.environ.get('SAIGE_NODE_HOST', '127.0.0.1'))
            ),
            'node_port': int(os.environ.get('REPRYNTT_RUST_RPC_PORT', os.environ.get('REPRYNTT_NODE_PORT', '9332'))),
            'max_nodes': 3,
            'max_submitters': int(os.environ.get('REPRYNTT_SUBMITTERS_PER_MACHINE', os.environ.get('SAIGE_SUBMITTERS_PER_MACHINE', '0'))),
            'max_ais': int(os.environ.get('REPRYNTT_AIS_PER_MACHINE', os.environ.get('SAIGE_AIS_PER_MACHINE', '0'))),
            'storage_path': 'robot_economy_data',
            'log_path': 'logs/robot_economy'
        }

        # Create directories
        os.makedirs(self.config['storage_path'], exist_ok=True)
        os.makedirs(self.config['log_path'], exist_ok=True)

        # Economy metrics
        self.metrics = {
            'total_blocks': 0,
            'pending_workloads': 0,
            'completed_workloads': 0,
            'total_rewards': 0,
            'network_tflops': 0
        }

        # Thread management
        self.threads = []
        self.processes = []

        self.logger.info("🤖 Robot Economy Manager initialized")

    def start_economy(self) -> Dict[str, Any]:
        """Start the complete robot economy ecosystem"""
        try:
            if self.is_running:
                return {"success": False, "error": "Economy already running"}

            self.logger.info("🚀 Starting Robot Economy ecosystem...")

            # Start blockchain node (with error handling)
            try:
                self._start_blockchain_node()
            except Exception as e:
                self.logger.error(f"Blockchain node failed to start: {e}")
                self.logger.warning("Continuing with economy startup despite blockchain failure")

            # Always try to start other components, even if blockchain fails
            try:
                # Start workload submitters (OPTIONAL - only if not using blockchain AI routing)
                # When SAIGE routes AI calls through blockchain, we don't need dummy submitters
                start_dummy_submitters = os.environ.get("REPRYNTT_DUMMY_SUBMITTERS", os.environ.get("SAIGE_DUMMY_SUBMITTERS", "0")).strip() in ("1", "true", "yes", "y")
                if start_dummy_submitters:
                    self._start_submitters()
                    self.logger.info("📤 Dummy workload submitters started (for testing)")
                else:
                    self.logger.info("📤 Dummy workload submitters DISABLED - using real AI workloads from brain_system")
            except Exception as e:
                self.logger.error(f"Failed to start submitters: {e}")

            # DISABLED: Legacy AI processors (PyTorch) - miners now do real AI work via llama.cpp
            # try:
            #     self._start_ais()
            # except Exception as e:
            #     self.logger.error(f"Failed to start AI processors: {e}")
            self.logger.info("🧠 Legacy AI processors DISABLED - miners handle AI inference via llama.cpp")

            try:
                # Start monitoring thread
                self._start_monitoring()
            except Exception as e:
                self.logger.error(f"Failed to start monitoring: {e}")

            # Mark as running even if some components failed
            self.is_running = True
            
            # Bootstrap economy: Fund submitters so they can pay fees
            time.sleep(2)  # Wait for nodes to stabilize
            self._bootstrap_economy()
            
            # Start P2P Economy Bridge if P2P node is available
            try:
                self._start_economy_bridge()
            except Exception as e:
                self.logger.warning(f"P2P Economy Bridge not started: {e}")
            
            self.logger.info("✅ Robot Economy ecosystem started successfully")

            return {
                "success": True,
                "nodes": len(self.nodes),
                "submitters": len(self.submitters),
                "ais": len(self.ais)
            }

        except Exception as e:
            self.logger.error(f"Failed to start economy: {e}")
            self.stop_economy()
            return {"success": False, "error": str(e)}

    def stop_economy(self) -> Dict[str, Any]:
        """Stop the entire robot economy ecosystem"""
        try:
            self.logger.info("🛑 Stopping Robot Economy ecosystem...")

            self.is_running = False

            # Stop all threads
            for thread in self.threads:
                if thread.is_alive():
                    thread.join(timeout=5)

            # Terminate all processes
            for process in self.processes:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)

            # Clear collections
            self.nodes.clear()
            self.submitters.clear()
            self.ais.clear()

            self.logger.info("✅ Robot Economy ecosystem stopped")
            return {"success": True}

        except Exception as e:
            self.logger.error(f"Error stopping economy: {e}")
            return {"success": False, "error": str(e)}

    def _bootstrap_economy(self):
        """Verify the economy is ready — no pre-mine.

        Satoshi-style: genesis block is a zero-value marker.  All coins
        enter circulation through coinbase block rewards earned by mining.
        The first ~100 blocks allow stakeless mining so the chain can
        bootstrap itself from nothing.
        """
        main_node = self.nodes.get('main')
        if not main_node:
            return
        wallet_addr = self._get_ai_brain_wallet()
        if wallet_addr:
            self.logger.info(
                f"✅ Economy ready — node wallet {wallet_addr[:16]}... "
                f"will earn CR through mining (no pre-mine)"
            )
        else:
            self.logger.warning(
                "⚠️ No node wallet — create one to earn mining rewards"
            )

    def _start_economy_bridge(self):
        """Start the P2P Economy Bridge to connect the mesh network to the blockchain."""
        if not self.p2p_node:
            self.logger.info("📡 P2P node not set — economy bridge will start when P2P connects")
            return
        
        main_node = self.nodes.get('main')
        if not main_node:
            self.logger.warning("⚠️ No blockchain node — economy bridge cannot start")
            return
        
        try:
            from repryntt.economy.p2p_economy_bridge import P2PEconomyBridge
            
            self.economy_bridge = P2PEconomyBridge(
                p2p_node=self.p2p_node,
                blockchain_node=main_node,
                economy_manager=self,
            )
            
            # Start the bridge in the P2P event loop
            loop = self._p2p_loop
            if loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(self.economy_bridge.start(), loop)
            else:
                # If no loop yet, store bridge — it will be started when loop is available
                self.logger.info("📡 Economy bridge created — will start with P2P event loop")
            
            self.logger.info("🌐 P2P Economy Bridge initialized — multi-device mining ENABLED")
        except Exception as e:
            self.logger.error(f"Failed to start economy bridge: {e}")
    
    def connect_p2p_node(self, p2p_node, p2p_loop=None):
        """
        Connect a P2P mesh node to the economy manager.
        Called by the evolution loop or daemon after P2P node is initialized.
        
        Args:
            p2p_node: SAIGENode instance from saige_p2p.py
            p2p_loop: The asyncio event loop the P2P node runs in
        """
        self.p2p_node = p2p_node
        self._p2p_loop = p2p_loop
        self.logger.info(f"📡 P2P node connected to economy: {getattr(p2p_node, 'node_name', 'unknown')}")
        
        # If economy is already running, start the bridge now
        if self.is_running and self.economy_bridge is None:
            try:
                self._start_economy_bridge()
            except Exception as e:
                self.logger.warning(f"Failed to start economy bridge after P2P connect: {e}")
    
    async def dispatch_workload_to_network(self, workload_key: str, workload_data, 
                                            workload_type: str = "inference",
                                            fee_plancks: int = 1000000) -> Optional[str]:
        """
        Dispatch a workload to remote GPU peers via the P2P mesh.
        Falls back to local mining if no remote peers are available.
        
        This is the primary API for brain_system to request remote compute.
        
        Returns:
            workload_id if dispatched to remote peer, None if local-only
        """
        if not self.economy_bridge:
            return None
        
        return await self.economy_bridge.dispatch_workload(
            workload_key=workload_key,
            workload_data=workload_data,
            workload_type=workload_type,
            fee_plancks=fee_plancks,
        )
    
    def get_compute_network_status(self) -> Dict[str, Any]:
        """Get the compute network status including remote GPU peers."""
        if self.economy_bridge:
            return self.economy_bridge.get_network_compute_summary()
        return {
            "local_tflops": 0,
            "compute_peers": 0,
            "total_network_tflops": 0,
            "economy_bridge": False,
        }

    def register_wallet_key(self, address: str, mnemonic_phrase: str) -> bool:
        """Register wallet private key for transaction signing"""
        try:
            from repryntt.economy.crypto_utils import crypto_utils
            private_key, public_key = crypto_utils.derive_private_key_from_mnemonic(mnemonic_phrase)
            if private_key:
                self.wallet_keys[address] = private_key
                self.logger.info(f"🔐 Registered signing key for wallet {address[:16]}...")
                return True
            return False
        except Exception as e:
            self.logger.warning(f"Failed to register wallet key: {e}")
            return False

    def _get_ai_brain_wallet(self) -> Optional[str]:
        """Get the node operator's wallet address.

        Priority:
          1. Canonical node wallet (~/.repryntt/wallet/node_wallet.json)
          2. brain_system personality_brain['ai_wallet']
          3. machine1.json wallet in the wallets/ directory
          4. ~/.repryntt/brain/ava_brain.json → ai_wallet
          5. None (caller decides fallback — never returns 0x000)
        """
        try:
            # Canonical node wallet — the Satoshi way (one wallet per node)
            try:
                from repryntt.economy.node_wallet import get_node_wallet
                nw = get_node_wallet()
                if nw:
                    self.logger.info(f"🔐 Using canonical node wallet: {nw.address[:16]}...")
                    return nw.address
            except Exception:
                pass

            # Try to get from brain system if available
            if self.brain_system and hasattr(self.brain_system, 'personality_brain'):
                ai_wallet = self.brain_system.personality_brain.get('ai_wallet')
                ai_mnemonic = self.brain_system.personality_brain.get('ai_wallet_mnemonic')
                if ai_wallet:
                    # Register the private key if we have the mnemonic
                    if ai_mnemonic and ai_wallet not in self.wallet_keys:
                        self.register_wallet_key(ai_wallet, ai_mnemonic)
                    return ai_wallet

            # Preferred fallback: operator's machine1.json wallet
            wallets_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'wallets')
            machine1_file = os.path.join(wallets_dir, 'machine1.json')
            if os.path.exists(machine1_file):
                with open(machine1_file, 'r') as f:
                    wallet_data = json.load(f)
                    if 'address' in wallet_data:
                        self.logger.info(f"🔐 Using operator wallet (machine1): {wallet_data['address'][:16]}...")
                        return wallet_data['address']

            # Fallback: try to read from brain file in ~/.repryntt/
            from repryntt.paths import brain_dir as _brain_dir
            brain_file = str(_brain_dir() / "ava_brain.json")
            if os.path.exists(brain_file):
                with open(brain_file, 'r') as f:
                    brain_data = json.load(f)
                    if 'ai_wallet' in brain_data:
                        ai_wallet = brain_data['ai_wallet']
                        ai_mnemonic = brain_data.get('ai_wallet_mnemonic')
                        if ai_mnemonic and ai_wallet not in self.wallet_keys:
                            self.register_wallet_key(ai_wallet, ai_mnemonic)
                        return ai_wallet
        except Exception as e:
            self.logger.warning(f"Could not get AI brain wallet: {e}")
        
        return None

    # ------------------------------------------------------------------
    # Node Operator Startup Bonus
    # ------------------------------------------------------------------

    # Pre-mine removed — all coins enter circulation through coinbase
    # block rewards, earned by mining.  No special treatment for any node.

    def _start_blockchain_node(self):
        """Start the main blockchain node with robust error handling and verification"""
        try:
            # CHECK IF BLOCKCHAIN NODE IS ALREADY RUNNING IN THIS PROCESS
            if 'main' in self.nodes and self.nodes['main'] is not None:
                self.logger.info("✅ Blockchain node already running - skipping startup")
                return

            # CHECK IF BLOCKCHAIN NODE IS ALREADY RUNNING ON THE NETWORK
            import socket as _sock
            sock = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            try:
                host = self.config['node_host'] if self.config['node_host'] != '0.0.0.0' else '127.0.0.1'
                result = sock.connect_ex((host, self.config['node_port']))
                sock.close()
                if result == 0:
                    self.logger.info(f"✅ Blockchain node already running on {host}:{self.config['node_port']} - connecting as light client")
                    client = BlockchainNodeClient(host, self.config['node_port'])
                    self.nodes['main'] = client
                    self.logger.info(f"✅ Light client connected — chain length: {client._chain_length}, accounts: {len(client._balances)}")
                    return
            except Exception:
                sock.close()
            if os.environ.get("REPRYNTT_ENABLE_LEGACY_PY_CHAIN", "").lower() not in {"1", "true", "yes"}:
                self.logger.warning(
                    "⚠️ Rust blockchain RPC is not reachable on %s:%s; robot economy will wait for repryntt-chain.service",
                    self.config['node_host'],
                    self.config['node_port'],
                )
                return

            self.logger.info("🏗️ Starting blockchain node...")

            # Create blockchain instance
            blockchain = Blockchain(
                host=self.config['node_host'],
                port=self.config['node_port']
            )

            # Store reference
            self.nodes['main'] = blockchain

            # Start server thread with error handling
            server_thread = threading.Thread(
                target=self._run_blockchain_server,
                args=(blockchain,),
                daemon=True,
                name="BlockchainNode"
            )
            server_thread.start()
            self.threads.append(server_thread)

            # Verify server actually started and is listening
            if self._wait_for_blockchain_ready(blockchain):
                # Update config with actual port
                self.config['node_port'] = blockchain.port
                self.logger.info(f"✅ Blockchain node ready on {self.config['node_host']}:{self.config['node_port']}")

                # Connect to peer network and synchronize blockchain
                self._connect_to_peer_network(blockchain)
                self._synchronize_blockchain(blockchain)

                # Small additional delay to ensure full initialization
                time.sleep(1)
            else:
                self.logger.warning("⚠️ Blockchain node thread started but server not responding - continuing anyway")
                time.sleep(2)  # Give it more time even if not responding
                # Don't raise exception - allow economy to continue without blockchain networking

        except Exception as e:
            self.logger.error(f"❌ Blockchain node startup failed: {e}")
            # Don't raise - allow economy to continue without blockchain
            self.logger.warning("⚠️ Continuing without blockchain node - some features may be limited")

    def _run_blockchain_server(self, blockchain):
        """Run blockchain server with proper error handling"""
        try:
            blockchain.start_server()
        except Exception as e:
            self.logger.error(f"❌ Blockchain server thread crashed: {e}")
            # Remove failed node from nodes dict
            if 'main' in self.nodes and self.nodes['main'] == blockchain:
                del self.nodes['main']

    def _wait_for_blockchain_ready(self, blockchain, timeout=10):
        """Wait for blockchain node to be ready and responding"""
        import socket

        for i in range(timeout):
            try:
                # Try to connect to the blockchain port
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(1)
                    result = s.connect_ex((blockchain.host, blockchain.port))
                    if result == 0:
                        return True
            except:
                pass

            time.sleep(1)

        return False

    def _connect_to_peer_network(self, blockchain):
        """Connect to known bootstrap nodes to join the peer-to-peer network"""
        try:
            self.logger.info("🌐 Connecting to SAIGE peer-to-peer network...")

            connected_peers = 0

            # Try to connect to bootstrap nodes
            for peer_addr in self.bootstrap_nodes:
                try:
                    if ":" in peer_addr:
                        host, port_str = peer_addr.split(":")
                        port = int(port_str)
                    else:
                        host = peer_addr
                        port = 5001  # Default port

                    # Skip connecting to ourselves
                    if host == blockchain.host and port == blockchain.port:
                        continue

                    # Attempt to connect to peer
                    if blockchain.connect_peer(host, port):
                        connected_peers += 1
                        self.logger.info(f"✅ Connected to peer: {host}:{port}")

                        # Limit connections to avoid overwhelming the network
                        if connected_peers >= self.max_peers:
                            break
                    else:
                        self.logger.debug(f"Could not connect to peer: {host}:{port}")

                except Exception as e:
                    self.logger.debug(f"Failed to connect to bootstrap peer {peer_addr}: {e}")

            if connected_peers > 0:
                self.logger.info(f"🌐 Connected to {connected_peers} peers in SAIGE network")
            else:
                self.logger.info("🌐 No peers available - running as solo node (this is normal for first device)")

        except Exception as e:
            self.logger.error(f"❌ Failed to connect to peer network: {e}")

    def _synchronize_blockchain(self, blockchain):
        """Download and synchronize blockchain from network peers"""
        import socket
        # DEPRECATED — replaced by safe_serialize
        # import pickle
        from repryntt.economy.safe_serialize import pack as safe_pack, unpack as safe_unpack
        import struct

        try:
            if not blockchain.peers:
                self.logger.info("📚 No peers available for blockchain synchronization - starting with genesis block")
                self.network_synced = True  # Consider synced if no peers (first node)
                return

            self.logger.info("📚 Synchronizing blockchain from network peers...")

            # Request blockchain from a random peer
            peer = blockchain.peers[0] if blockchain.peers else None
            if not peer:
                self.logger.warning("No peers available for synchronization")
                return

            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(30)  # Longer timeout for blockchain sync
                    s.connect(peer)

                    # Request full blockchain
                    message = {"type": "get_blockchain"}
                    serialized = safe_pack(message)
                    s.sendall(struct.pack('!I', len(serialized)))
                    s.sendall(serialized)

                    # Receive blockchain data
                    response_size = s.recv(4)
                    if len(response_size) != 4:
                        raise Exception("Invalid response size")

                    response_size = struct.unpack('!I', response_size)[0]
                    response_data = s.recv(response_size)
                    if len(response_data) != response_size:
                        raise Exception("Incomplete blockchain data")

                    network_blockchain = safe_unpack(response_data)

                    if network_blockchain.get("success"):
                        remote_chain = network_blockchain["chain"]
                        remote_balances = network_blockchain["balances"]

                        # Validate and replace our chain if network chain is longer/valid
                        if len(remote_chain) > len(blockchain.chain):
                            # Verify the remote chain
                            if self._validate_network_chain(remote_chain):
                                blockchain.chain = remote_chain
                                blockchain.balances = remote_balances.copy()
                                blockchain.save_state()

                                self.logger.info(f"✅ Synchronized blockchain: {len(remote_chain)} blocks from network")
                                self.network_synced = True
                            else:
                                self.logger.warning("❌ Network blockchain validation failed - keeping local chain")
                        else:
                            self.logger.info("📚 Local blockchain is up to date or longer than network")
                            self.network_synced = True
                    else:
                        self.logger.warning(f"❌ Failed to get blockchain from peer: {network_blockchain.get('error')}")

            except Exception as e:
                self.logger.warning(f"❌ Blockchain synchronization failed: {e}")

        except Exception as e:
            self.logger.error(f"❌ Blockchain synchronization error: {e}")

    def _validate_network_chain(self, chain):
        """Validate a blockchain received from the network"""
        try:
            if not chain or len(chain) == 0:
                return False

            # Check genesis block
            if chain[0].index != 0:
                return False

            # Validate chain continuity and hashes
            for i in range(1, len(chain)):
                current = chain[i]
                previous = chain[i-1]

                # Check index continuity
                if current.index != previous.index + 1:
                    return False

                # Check hash linkage
                if current.previous_hash != previous.hash:
                    return False

                # Verify current block hash
                if current.hash != current.calculate_hash():
                    return False

            return True

        except Exception as e:
            self.logger.error(f"Chain validation error: {e}")
            return False

    def _start_submitters(self):
        """Start workload submitters"""
        try:
            self.logger.info("📤 Starting workload submitters...")

            for i in range(self.config['max_submitters']):
                submitter_id = f"submitter_{i+1}"
                machine_id = f"machine_{i+1}"

                # Create submitter instance
                submitter = WorkloadSubmitter(
                    host=self.config['node_host'],
                    port=self.config['node_port'],
                    machine_id=machine_id
                )

                self.submitters[submitter_id] = submitter

                # Start submitter in thread
                thread = threading.Thread(
                    target=self._run_submitter,
                    args=(submitter,),
                    daemon=True,
                    name=f"Submitter-{submitter_id}"
                )
                thread.start()
                self.threads.append(thread)

                self.logger.info(f"✅ Workload submitter {submitter_id} started")

        except Exception as e:
            self.logger.error(f"Failed to start submitters: {e}")
            raise

    def _start_ais(self):
        """Start AI processors"""
        try:
            self.logger.info("🧠 Starting AI processors...")

            for i in range(self.config['max_ais']):
                ai_id = f"ai_{i+1}"

                # Create AI instance (lazy import — deprecated module loads torch)
                from repryntt.economy.seai import SelfEvolvingAI
                ai = SelfEvolvingAI()

                self.ais[ai_id] = ai

                # Start AI in thread
                thread = threading.Thread(
                    target=self._run_ai,
                    args=(ai,),
                    daemon=True,
                    name=f"AI-{ai_id}"
                )
                thread.start()
                self.threads.append(thread)

                self.logger.info(f"✅ AI processor {ai_id} started")

        except Exception as e:
            self.logger.error(f"Failed to start AIs: {e}")
            raise

    def _run_submitter(self, submitter: WorkloadSubmitter):
        """Run a workload submitter"""
        try:
            submission_count = 0
            while self.is_running:
                try:
                    self.logger.info(f"📤 Submitter attempting workload submission #{submission_count + 1}")
                    result = submitter.submit_workload(
                        node_host=self.config['node_host'],
                        node_port=self.config['node_port']
                    )
                    if result:
                        submission_count += 1
                        self.logger.info(f"✅ Workload submission #{submission_count} successful")
                    else:
                        self.logger.warning(f"⚠️ Workload submission failed (returned False)")
                except Exception as e:
                    self.logger.error(f"❌ Submitter iteration error: {e}", exc_info=True)
                
                time.sleep(30)  # Submit every 30 seconds
        except Exception as e:
            self.logger.error(f"❌ Submitter thread error: {e}", exc_info=True)

    def _run_ai(self, ai):
        """Run an AI processor"""
        try:
            ai.run(
                node_host=self.config['node_host'],
                node_port=self.config['node_port']
            )
        except Exception as e:
            self.logger.error(f"AI processor error: {e}")

    def _start_monitoring(self):
        """Start economy monitoring thread"""
        monitor_thread = threading.Thread(
            target=self._monitor_economy,
            daemon=True,
            name="EconomyMonitor"
        )
        monitor_thread.start()
        self.threads.append(monitor_thread)

    def _monitor_economy(self):
        """Monitor economy health and metrics"""
        while self.is_running:
            try:
                # Check if blockchain node is still alive
                main_node = self.nodes.get('main')
                if main_node and not getattr(main_node, '_is_client', False):
                    # In-process node: check if the server thread is still alive
                    server_thread_alive = False
                    for thread in self.threads:
                        if thread.name == "BlockchainNode" and thread.is_alive():
                            server_thread_alive = True
                            break
                    
                    if not server_thread_alive:
                        self.logger.warning("⚠️ Blockchain node thread died, restarting...")
                        if 'main' in self.nodes:
                            del self.nodes['main']
                        try:
                            self._start_blockchain_node()
                            self.logger.info("✅ Blockchain node restarted successfully")
                        except Exception as restart_error:
                            self.logger.error(f"❌ Failed to restart blockchain node: {restart_error}")
                elif main_node and getattr(main_node, '_is_client', False):
                    # Light-client mode: check if remote node is still reachable
                    try:
                        main_node.refresh()
                    except Exception:
                        self.logger.warning("⚠️ Lost connection to blockchain node, reconnecting...")
                        try:
                            self._start_blockchain_node()
                        except Exception as e:
                            self.logger.error(f"❌ Reconnection failed: {e}")
                elif not main_node:
                    # No main node at all, try to start one
                    self.logger.warning("⚠️ No blockchain node found, starting one...")
                    try:
                        self._start_blockchain_node()
                        self.logger.info("✅ Blockchain node started successfully")
                    except Exception as start_error:
                        self.logger.error(f"❌ Failed to start blockchain node: {start_error}")

                # Update metrics
                main_node = self.nodes.get('main')
                if main_node:
                    if getattr(main_node, '_is_client', False):
                        main_node.refresh()
                    self.metrics['total_blocks'] = len(main_node.chain)
                    self.metrics['pending_workloads'] = len(main_node.contract.valid_keys)
                    self.metrics['network_tflops'] = sum(main_node.node_compute_shares.values())
                    self.metrics['total_rewards'] = sum(v for v in main_node.balances.values() if isinstance(v, (int, float))) / 100000000

                    # Calculate completed workloads
                    completed = 0
                    for key, workload in main_node.contract.workloads.items():
                        if workload['status'] == 'completed':
                            completed += 1
                    self.metrics['completed_workloads'] = completed

                # Log status
                self.logger.info(f"📊 Economy Status: {self.metrics}")

                # Store in brain if available
                if self.brain_system:
                    self.brain_system.store_episodic_memory(
                        conversation_id="robot_economy_monitoring",
                        user_input="Economy monitoring update",
                        ai_response=f"Economy metrics: {self.metrics}",
                        tool_calls=[],
                        outcome="monitoring"
                    )

                time.sleep(60)  # Update every minute

            except Exception as e:
                self.logger.error(f"Monitoring error: {e}")
                time.sleep(30)

    def get_status(self) -> Dict[str, Any]:
        """Get current economy status.

        If the manager didn't start the economy itself, probe the live
        health-check endpoint and blockchain TCP port to discover
        externally-launched services.
        """
        # ── Live discovery: detect externally-started node + miners ──
        if not self.is_running and not self.nodes:
            self._discover_running_services()

        status = {
            "running": self.is_running,
            "phase": "testnet",  # Until real external workloads exist
            "metrics": self.metrics,
            "nodes": len(self.nodes),
            "submitters": len(self.submitters),
            "ais": len(self.ais),
        }

        # Enrich with live blockchain data when a node is available
        main_node = self.nodes.get('main')
        if main_node:
            try:
                is_client = getattr(main_node, '_is_client', False)
                if is_client:
                    status["block_height"] = getattr(main_node, '_chain_length', 0)
                else:
                    net = main_node.get_network_stats()
                    status["block_height"] = net.get("block_height", len(main_node.chain))
                    status["total_supply_cr"] = net.get("total_supply_cr", 0)
                    status["max_supply_cr"] = net.get("max_supply_cr", 0)
                    status["supply_pct"] = net.get("supply_pct", 0)
                    status["active_wallets"] = net.get("active_wallets", 0)
                    status["total_staked_cr"] = net.get("total_staked_cr", 0)
                    status["peers"] = net.get("peers", 0)

                    # Operator wallet balance
                    op_addr = self._get_ai_brain_wallet()
                    if op_addr:
                        bal = main_node.balances.get(op_addr, 0)
                        status["operator_wallet"] = op_addr[:16] + "..."
                        status["operator_balance_cr"] = bal / 100000000

                    # Pending transactions
                    status["pending_txs"] = main_node.tx_pool.size()
            except Exception as e:
                self.logger.debug(f"Status enrichment failed: {e}")

        return status

    def _discover_running_services(self):
        """Probe the network for an already-running blockchain node and miners.

        Called lazily from get_status() when the manager didn't start the
        economy itself.  Connects as a light client if the node responds.
        """
        import socket as _sock
        import urllib.request
        import json as _json

        host = self.config['node_host']
        if host == '0.0.0.0':
            host = '127.0.0.1'
        port = self.config['node_port']

        # 1) Try TCP connect to the blockchain node
        try:
            sock = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((host, port))
            sock.close()
            if result == 0:
                # Node is listening — connect as light client
                try:
                    client = BlockchainNodeClient(host, port)
                    self.nodes['main'] = client
                    self.is_running = True
                    self.logger.info(
                        f"🔍 Discovered running node on {host}:{port} "
                        f"(chain={client._chain_length})")
                except Exception as e:
                    self.logger.debug(f"Light client connect failed: {e}")
        except Exception:
            try:
                sock.close()
            except Exception:
                pass

        # 2) Try the HTTP health endpoint for richer metrics
        health_port = port + 1000  # qnode2 convention
        try:
            url = f"http://{host}:{health_port}/health"
            if not url.startswith(("http://", "https://")):
                raise ValueError("Invalid URL scheme")
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=2) as resp:  # noqa: S310
                data = _json.loads(resp.read().decode())
            self.metrics['total_blocks'] = data.get('chain_length', 0)
            self.metrics['network_tflops'] = 0  # health endpoint doesn't expose this
            self.metrics['pending_workloads'] = data.get('pending_transactions', 0)
            peers = data.get('peer_count', 0)
            if data.get('status') == 'healthy':
                self.is_running = True
            self.logger.info(
                f"🔍 Health check: blocks={data.get('chain_length')}, "
                f"peers={peers}, pending={data.get('pending_transactions')}")
        except Exception:
            pass

    def submit_custom_workload(self, workload_data: Dict[str, Any]) -> Dict[str, Any]:
        """Submit a custom workload to the economy"""
        try:
            if not self.is_running:
                return {"success": False, "error": "Economy not running"}

            main_node = self.nodes.get('main')
            if not main_node:
                return {"success": False, "error": "No main node available"}

            # Create a submitter instance for this workload
            submitter = WorkloadSubmitter(
                host=self.config['node_host'],
                port=self.config['node_port'],
                machine_id=f"custom_{int(time.time())}"
            )

            # Submit the workload
            result = submitter.submit_workload(
                node_host=self.config['node_host'],
                node_port=self.config['node_port'],
                purpose=workload_data.get('purpose', 'Custom AI workload')
            )

            # Store workload data in swarm
            if result.get('success'):
                submitter.store_in_swarm(
                    workload_data.get('data', {}),
                    submitter.generate_key()
                )

            return result

        except Exception as e:
            self.logger.error(f"Custom workload submission error: {e}")
            return {"success": False, "error": str(e)}

    def get_wallet_balance(self, address: str) -> Dict[str, Any]:
        """Get wallet balance for an address"""
        try:
            if self.use_database:
                # Use database for persistence
                with get_db_session() as session:
                    wallet = session.query(WalletModel).filter(WalletModel.address == address).first()
                    if wallet:
                        balance_plancks = wallet.balance_plancks
                        balance_credits = balance_plancks / 100000000  # Convert to Credits
                        return {
                            "success": True,
                            "address": address,
                            "balance_plancks": balance_plancks,
                            "balance_credits": balance_credits,
                            "wallet_type": wallet.wallet_type,
                            "last_updated": wallet.last_updated.isoformat()
                        }
                    else:
                        # Wallet doesn't exist in database
                        return {
                            "success": True,
                            "address": address,
                            "balance_plancks": 0,
                            "balance_credits": 0.0,
                            "wallet_type": "unknown"
                        }
            else:
                # Fallback to node balances
                main_node = self.nodes.get('main')
                if not main_node:
                    return {"success": False, "error": "No main node available - balance check requires local blockchain node or database"}

                # For light-client mode, query the node directly for fresh balance
                if getattr(main_node, '_is_client', False):
                    resp = main_node.get_balance(address)
                    if resp.get("success"):
                        return {
                            "success": True,
                            "address": address,
                            "balance_plancks": int(resp["balance_credits"] * 100000000),
                            "balance_credits": resp["balance_credits"],
                            "stake_credits": resp.get("stake_credits", 0),
                            "reputation": resp.get("reputation", 0.5),
                        }

                balance_plancks = main_node.balances.get(address, 0)
                balance_credits = balance_plancks / 100000000  # Convert to Credits

                return {
                    "success": True,
                    "address": address,
                    "balance_plancks": balance_plancks,
                    "balance_credits": balance_credits
                }

        except Exception as e:
            self.logger.error(f"Balance check error: {e}")
            return {"success": False, "error": str(e)}

    def get_all_wallet_balances(self) -> Dict[str, Any]:
        """Get all wallet balances in the system - like a blockchain explorer"""
        try:
            main_node = self.nodes.get('main')
            if not main_node:
                return {"success": False, "error": "No main node available - balance explorer requires local blockchain node"}

            wallets = []
            total_supply = 0
            
            for address, balance_plancks in main_node.balances.items():
                balance_credits = balance_plancks / 100000000
                total_supply += balance_credits
                wallets.append({
                    "address": address,
                    "balance_credits": round(balance_credits, 8),
                    "balance_plancks": balance_plancks
                })
            
            # Sort by balance descending
            wallets.sort(key=lambda x: x['balance_credits'], reverse=True)

            return {
                "success": True,
                "total_wallets": len(wallets),
                "total_supply_credits": round(total_supply, 8),
                "wallets": wallets
            }

        except Exception as e:
            self.logger.error(f"Get all balances error: {e}")
            return {"success": False, "error": str(e)}

    def transfer_credits(self, from_address: str, to_address: str, amount_credits: float) -> Dict[str, Any]:
        """
        Transfer credits between wallets.
        Used for AI operation charges, workload payments, etc.
        Creates proper transaction objects for blockchain transparency.
        """
        try:
            main_node = self.nodes.get('main')
            if not main_node:
                # No local blockchain node - this process doesn't run the blockchain
                # Return success to allow operations to continue
                self.logger.debug(f"Transfer credits: {from_address} -> {to_address} ({amount_credits} CR) - no local node, operation logged only")
                return {
                    "success": True,
                    "message": "Transfer recorded (blockchain operations handled by evolution loop process)",
                    "from_address": from_address,
                    "to_address": to_address,
                    "amount_credits": amount_credits
                }

            amount_plancks = int(amount_credits * 100000000)

            # Light-client mode: delegate transfer to running node
            if getattr(main_node, '_is_client', False):
                if from_address not in self.wallet_keys:
                    return {
                        "success": False,
                        "error": "Wallet signing key required for on-chain debit",
                        "from_address": from_address,
                    }
                from repryntt.economy.rust_chain_client import submit_signed_transfer
                recipient = to_address or "burn"
                resp = submit_signed_transfer(
                    from_address=from_address,
                    private_key=self.wallet_keys[from_address],
                    to_address=recipient,
                    amount_plancks=amount_plancks,
                    metadata={"purpose": "robot_economy_transfer", "to": recipient},
                    host=main_node.host,
                    port=main_node.port,
                )
                if "error" not in resp:
                    balance_resp = main_node.get_balance(from_address)
                    return {
                        "success": True,
                        "from_address": from_address,
                        "to_address": recipient,
                        "amount_transferred": amount_credits,
                        "sender_new_balance": balance_resp.get("balance_credits", 0),
                        "recipient_new_balance": 0,
                        "tx_hash": resp.get("tx_hash"),
                        "message": f"Transferred {amount_credits:.8f} Credits"
                    }
                return {"success": False, "error": resp.get("error", "Transfer failed")}

            # Import transaction utilities
            from repryntt.economy.transaction import Transaction, create_fee_transaction

            # Create transaction object for blockchain record
            if to_address == "burn":
                # Burn transaction (AI call charges)
                tx = create_fee_transaction(from_address, amount_plancks, metadata={
                    "purpose": "AI operation charge",
                    "burned": True
                })
            else:
                # Regular transfer
                tx = Transaction(
                    from_address=from_address,
                    to_address=to_address,
                    amount=amount_plancks,
                    tx_type="transfer",
                    metadata={"via": "robot_economy_manager"}
                )

            # Sign transaction if we have the private key
            if from_address in self.wallet_keys:
                try:
                    private_key = self.wallet_keys[from_address]
                    # Derive public key from private key
                    from cryptography.hazmat.primitives.asymmetric import ed25519
                    from cryptography.hazmat.primitives import serialization
                    private_key_obj = ed25519.Ed25519PrivateKey.from_private_bytes(private_key)
                    public_key_obj = private_key_obj.public_key()
                    public_key = public_key_obj.public_bytes(
                        encoding=serialization.Encoding.Raw,
                        format=serialization.PublicFormat.Raw
                    )
                    tx.public_key = public_key
                    tx.sign(private_key)
                except Exception as e:
                    self.logger.warning(f"Failed to sign transaction: {e}")

            with main_node.lock:
                # Check if sender has sufficient balance (check database first if enabled, then in-memory)
                if self.use_database:
                    # Use database balance as source of truth
                    balance_result = self.get_wallet_balance(from_address)
                    sender_balance = int(balance_result.get('balance_plancks', 0))
                    
                    # Sync in-memory to match database
                    main_node.balances[from_address] = sender_balance
                else:
                    # Use in-memory balance
                    sender_balance = main_node.balances.get(from_address, 0)
                
                if sender_balance < amount_plancks:
                    return {
                        "success": False,
                        "error": f"Insufficient balance. Has {sender_balance/100000000:.8f} CR, needs {amount_credits:.8f} CR"
                    }

                # Add transaction to pool for next block
                # Don't require signature for system transactions that don't have keys
                require_signature = from_address in self.wallet_keys
                success, msg = main_node.tx_pool.add_transaction(tx, main_node.balances, require_signature=require_signature)
                if not success:
                    self.logger.warning(f"Transaction pool rejected: {msg}")

                # Deduct from sender
                main_node.balances[from_address] = sender_balance - amount_plancks

                # Add to recipient (if specified, otherwise it's a burn/destruction)
                if to_address and to_address != "burn":
                    main_node.balances[to_address] = main_node.balances.get(to_address, 0) + amount_plancks

                main_node.save_state()  # Persist to JSON immediately

            sender_new_balance = main_node.balances.get(from_address, 0) / 100000000
            recipient_new_balance = main_node.balances.get(to_address, 0) / 100000000 if to_address and to_address != "burn" else 0

            # Sync to database if enabled
            if self.use_database:
                try:
                    self._sync_wallet_to_database(from_address, main_node.balances.get(from_address, 0))
                    if to_address and to_address != "burn":
                        self._sync_wallet_to_database(to_address, main_node.balances.get(to_address, 0))
                except Exception as e:
                    self.logger.warning(f"Failed to sync wallet balances to database: {e}")

            self.logger.info(f"💸 Transfer: {amount_credits:.8f} CR from {from_address[:16]}... to {to_address[:16] if to_address else 'burn'}... (sender balance: {sender_new_balance:.8f} CR)")

            return {
                "success": True,
                "from_address": from_address,
                "to_address": to_address,
                "amount_transferred": amount_credits,
                "sender_new_balance": sender_new_balance,
                "recipient_new_balance": recipient_new_balance,
                "message": f"Transferred {amount_credits:.8f} Credits"
            }

        except Exception as e:
            self.logger.error(f"Transfer error: {e}")
            return {"success": False, "error": str(e)}

    def add_credits(self, address: str, amount_credits: float, reason: str = "robot_economy_credit") -> Dict[str, Any]:
        """Credit a wallet through the signed local Rust system-credit path."""
        try:
            main_node = self.nodes.get('main')
            if not main_node:
                return {"success": False, "error": "No main node available"}
            amount_plancks = int(amount_credits * 100000000)
            if amount_plancks <= 0:
                return {"success": False, "error": "Amount must be positive"}

            if getattr(main_node, '_is_client', False):
                resp = main_node.credit_address(
                    address,
                    amount_plancks,
                    purpose="robot_economy_credit",
                    metadata={"reason": reason, "amount_credits": amount_credits},
                )
                if resp.get("success") and self.use_database:
                    self._sync_wallet_to_database(address, int(resp.get("balance_credits", 0) * 100000000))
                return resp

            with main_node.lock:
                main_node.balances[address] = main_node.balances.get(address, 0) + amount_plancks
                main_node.save_state()
            if self.use_database:
                self._sync_wallet_to_database(address, main_node.balances.get(address, 0))
            return {"success": True, "address": address, "amount_added": amount_credits}
        except Exception as e:
            self.logger.error(f"Add credits error: {e}")
            return {"success": False, "error": str(e)}

    def deduct_credits(self, address: str, amount_credits: float, reason: str = "robot_economy_debit") -> Dict[str, Any]:
        """Deduct credits by submitting a signed transfer to the burn address."""
        result = self.transfer_credits(address, "burn", amount_credits)
        if result.get("success"):
            result["reason"] = reason
        return result

    def charge_ai_operation(self, ai_wallet: str, amount_credits: float, operation_type: str = "ai_call") -> Dict[str, Any]:
        """
        Charge credits for AI operations (calls, tool usage, etc.)
        Credits are deducted from AI wallet and effectively burned (no recipient).
        """
        return self.transfer_credits(ai_wallet, "burn", amount_credits)

    def reward_consciousness_operation(self, ai_wallet: str, operation_type: str, operation_details: Dict[str, Any]) -> Dict[str, Any]:
        """
        Reward AI for consciousness operations.
        Different operations have different reward structures.
        """
        try:
            main_node = self.nodes.get('main')
            if not main_node:
                # No local blockchain node - this process doesn't run the blockchain
                # Return success to allow operations to continue
                reward_amount = 0.001  # Default reward
                self.logger.debug(f"Reward consciousness: {ai_wallet} +{reward_amount} CR for {operation_type} - no local node, operation logged only")
                return {
                    "success": True,
                    "message": f"Reward recorded: {reward_amount} CR for {operation_type} (blockchain operations handled by evolution loop process)",
                    "ai_wallet": ai_wallet,
                    "operation_type": operation_type,
                    "reward_amount": reward_amount
                }

            # Define reward amounts for different consciousness operations
            reward_rates = {
                'consciousness_meta_decision': 0.001,
                'consciousness_attention_allocation': 0.006,
                'consciousness_goal_operations': 0.010,
                'consciousness_brain_context': 0.005,
                'consciousness_brain_query': 0.005,
                'consciousness_subsystem_coordination': 0.003,
                'consciousness_cycle_complete': 0.034
            }

            reward_amount = reward_rates.get(operation_type, 0.001)  # Default reward

            # Use proper reward transaction to reward the AI
            return self.reward_ai_for_task(ai_wallet, reward_amount, operation_type)

        except Exception as e:
            self.logger.error(f"Error rewarding consciousness operation {operation_type}: {e}")
            return {"success": False, "error": str(e)}

    def faucet(self, address: str, amount_credits: float = 10.0) -> Dict[str, Any]:
        """
        Faucet - distribute initial credits to bootstrap wallets.
        Like Bitcoin's testnet faucet, this allows new wallets to get started.
        One-time use per wallet with 1000 CR maximum.
        """
        try:
            main_node = self.nodes.get('main')
            if not main_node:
                return {"success": False, "error": "No main node available - faucet requires local blockchain node"}

            is_client = getattr(main_node, '_is_client', False)

            # Light-client mode: delegate entirely to the running node
            if is_client:
                result = main_node.faucet_claim(address, amount_credits)
                if result.get("success") and self.use_database:
                    try:
                        balance_resp = main_node.get_balance(address)
                        if balance_resp.get("success"):
                            self._sync_wallet_to_database(address, int(balance_resp["balance_credits"] * 100000000))
                    except Exception:
                        pass
                return result

            # In-process mode: direct balance mutation
            if not hasattr(main_node, 'faucet_used_wallets'):
                main_node.faucet_used_wallets = set()
            
            if address in main_node.faucet_used_wallets:
                return {
                    "success": False, 
                    "error": f"Wallet {address[:16]}... has already used faucet (one-time use only)"
                }

            # Limit faucet amount (anti-abuse)
            max_faucet = 1000.0  # Max 1000 Credits per faucet request
            if amount_credits > max_faucet:
                amount_credits = max_faucet

            amount_plancks = int(amount_credits * 100000000)

            with main_node.lock:
                faucet_tx = Transaction(
                    from_address='FAUCET',
                    to_address=address,
                    amount=amount_plancks,
                    tx_type='faucet',
                    metadata={'purpose': 'initial_funding', 'fee': 0}
                )
                
                success, message = main_node.tx_pool.add_transaction(
                    faucet_tx, 
                    main_node.balances,
                    require_signature=False
                )
                
                if not success:
                    return {"success": False, "error": f"Failed to add faucet transaction: {message}"}
                
                main_node.balances[address] = main_node.balances.get(address, 0) + amount_plancks
                main_node.faucet_used_wallets.add(address)
                main_node.save_state()

            new_balance = main_node.balances.get(address, 0) / 100000000

            if self.use_database:
                try:
                    self._sync_wallet_to_database(address, main_node.balances.get(address, 0))
                except Exception as e:
                    self.logger.warning(f"Failed to sync wallet balance to database: {e}")

            self.logger.info(f"💰 Faucet: Sent {amount_credits:.8f} CR to {address[:16]}... (new balance: {new_balance:.8f} CR)")

            return {
                "success": True,
                "address": address,
                "amount_sent": amount_credits,
                "new_balance": new_balance,
                "message": f"Sent {amount_credits:.8f} Credits to wallet"
            }

        except Exception as e:
            self.logger.error(f"Faucet error: {e}")
            return {"success": False, "error": str(e)}

    def reward_ai_for_task(self, address: str, amount_credits: float, task_description: str = "task_completion") -> Dict[str, Any]:
        """
        Reward AI wallet for completing tasks using proper reward transactions.
        Unlike faucet (one-time bootstrap), this creates ongoing reward transactions.
        
        Args:
            address: AI wallet address to reward
            amount_credits: Amount in Credits to reward
            task_description: Description of what earned the reward
        
        Returns:
            Dict with success status and details
        """
        try:
            main_node = self.nodes.get('main')
            if not main_node:
                return {"success": False, "error": "No main node available"}

            # Convert credits to plancks
            amount_plancks = int(amount_credits * 100000000)

            if getattr(main_node, '_is_client', False):
                result = main_node.credit_address(
                    address,
                    amount_plancks,
                    purpose="ai_task_completion",
                    metadata={
                        "task": task_description,
                        "reward_type": "ai_task_completion",
                        "timestamp": time.time(),
                    },
                )
                if result.get("success"):
                    result.update({
                        "address": address,
                        "amount_rewarded": amount_credits,
                        "task": task_description,
                        "message": f"Rewarded {amount_credits:.8f} Credits for {task_description}",
                    })
                return result

            with main_node.lock:
                # Import the reward transaction creator
                from repryntt.economy.transaction import create_reward_transaction
                
                # Create proper reward transaction (SYSTEM mints new coins as reward)
                reward_tx = create_reward_transaction(
                    miner_address=address,
                    amount=amount_plancks,
                    metadata={
                        'purpose': task_description,
                        'reward_type': 'ai_task_completion',
                        'timestamp': time.time()
                    }
                )
                
                # Add transaction to pool (will be included in next block)
                success, message = main_node.tx_pool.add_transaction(
                    reward_tx, 
                    main_node.balances,
                    require_signature=False  # Reward transactions don't need signature (system mints)
                )
                
                if not success:
                    return {"success": False, "error": f"Failed to add reward transaction: {message}"}
                
                # Update balance immediately (transaction will be confirmed when block is mined)
                main_node.balances[address] = main_node.balances.get(address, 0) + amount_plancks
                
                main_node.save_state()  # Persist to JSON immediately

            new_balance = main_node.balances.get(address, 0) / 100000000

            # Sync to database if enabled
            if self.use_database:
                try:
                    self._sync_wallet_to_database(address, main_node.balances.get(address, 0))
                except Exception as e:
                    self.logger.warning(f"Failed to sync wallet balance to database: {e}")

            self.logger.info(f"🎁 Reward: Sent {amount_credits:.8f} CR to {address[:16]}... for {task_description} (new balance: {new_balance:.8f} CR)")

            return {
                "success": True,
                "address": address,
                "amount_rewarded": amount_credits,
                "new_balance": new_balance,
                "task": task_description,
                "message": f"Rewarded {amount_credits:.8f} Credits for {task_description}"
            }

        except Exception as e:
            self.logger.error(f"Reward error: {e}")
            return {"success": False, "error": str(e)}

    def fund_all_submitters(self, amount_credits: float = 10.0) -> Dict[str, Any]:
        """Fund all active submitters with credits so they can submit workloads"""
        try:
            results = []
            for submitter_id, submitter in self.submitters.items():
                result = self.faucet(submitter.address, amount_credits)
                results.append({
                    "submitter_id": submitter_id,
                    "address": submitter.address,
                    "success": result.get("success", False),
                    "new_balance": result.get("new_balance", 0)
                })
            
            return {
                "success": True,
                "funded_count": len(results),
                "results": results
            }

        except Exception as e:
            self.logger.error(f"Fund submitters error: {e}")
            return {"success": False, "error": str(e)}

    def get_blockchain_info(self) -> Dict[str, Any]:
        """Get blockchain information"""
        try:
            main_node = self.nodes.get('main')
            if not main_node:
                return {"success": False, "error": "No main node available"}

            latest_block = main_node.get_latest_block()

            return {
                "success": True,
                "chain_length": len(main_node.chain),
                "latest_block": {
                    "index": latest_block.index,
                    "hash": latest_block.hash[:16] + "...",
                    "timestamp": latest_block.timestamp,
                    "miner": latest_block.miner_address[:16] + "..."
                },
                "difficulty": main_node.difficulty,
                "total_accounts": len(main_node.balances),
                "network_tflops": sum(main_node.node_compute_shares.values())
            }

        except Exception as e:
            self.logger.error(f"Blockchain info error: {e}")
            return {"success": False, "error": str(e)}

    def allocate_dao_funds(self, machine_address: str, amount_credits: float, purpose: str) -> Dict[str, Any]:
        """Allocate DAO funds for a specific purpose"""
        try:
            main_node = self.nodes.get('main')
            if not main_node:
                return {"success": False, "error": "No main node available"}

            amount_plancks = int(amount_credits * 100000000)  # Convert to Plancks
            result = main_node.dao.allocate_tokens(
                machine_address=machine_address,
                amount_plancks=amount_plancks,
                purpose=purpose,
                balances=main_node.balances
            )

            return result

        except Exception as e:
            self.logger.error(f"DAO allocation error: {e}")
            return {"success": False, "error": str(e)}

    def create_wallet(self, wallet_type: str = "user") -> Dict[str, Any]:
        """Create a new quantum-safe wallet"""
        try:
            # Generate wallet using workload submitter (includes crypto generation)
            submitter = WorkloadSubmitter(
                host=self.config['node_host'],
                port=self.config['node_port']
            )

            address = submitter.address
            key_phrase = submitter.key_phrase

            # Store in database if available
            if self.use_database:
                with get_db_session() as session:
                    # Check if wallet already exists
                    existing_wallet = session.query(WalletModel).filter(WalletModel.address == address).first()
                    if existing_wallet:
                        return {
                            "success": False,
                            "error": "Wallet already exists",
                            "address": address
                        }

                    # Create new wallet record
                    wallet_record = WalletModel(
                        address=address,
                        balance_plancks=0,
                        wallet_type=wallet_type,
                        metadata={"created_via": "robot_economy_manager"}
                    )
                    session.add(wallet_record)
                    session.commit()

                    self.logger.info(f"✅ Wallet created in database: {address[:16]}...")

            return {
                "success": True,
                "address": address,
                "key_phrase": key_phrase,
                "wallet_type": wallet_type,
                "message": "Wallet created successfully. SAVE THE KEY PHRASE SECURELY!"
            }

        except Exception as e:
            self.logger.error(f"Wallet creation error: {e}")
            return {"success": False, "error": str(e)}

    def recover_wallet(self, key_phrase: str) -> Dict[str, Any]:
        """Recover wallet from key phrase"""
        try:
            wallet = Wallet()
            address = wallet.recover_wallet(key_phrase)

            if address:
                return {
                    "success": True,
                    "address": address,
                    "key_phrase": key_phrase
                }
            else:
                return {"success": False, "error": "Invalid key phrase"}

        except Exception as e:
            self.logger.error(f"Wallet recovery error: {e}")
            return {"success": False, "error": str(e)}

    def _sync_wallet_to_database(self, address: str, balance_plancks: int):
        """Sync a wallet's balance from in-memory to database"""
        if not self.use_database:
            return

        try:
            with get_db_session() as session:
                wallet = session.query(WalletModel).filter(WalletModel.address == address).first()
                
                if wallet:
                    # Update existing wallet
                    wallet.balance_plancks = balance_plancks
                    wallet.last_updated = datetime.utcnow()
                else:
                    # Create new wallet in database
                    wallet = WalletModel(
                        address=address,
                        balance_plancks=balance_plancks,
                        wallet_type="ai" if address.startswith("b1c5") else "user"
                    )
                    session.add(wallet)
                
                session.commit()
        except Exception as e:
            self.logger.warning(f"Database wallet sync failed for {address[:16]}...: {e}")

    def report_consciousness_operation(self, operation_type: str, details: dict, value: float = 0.0) -> Dict[str, Any]:
        """
        Report a consciousness operation for tokenization and economic processing.
        This centralizes all consciousness economic logic in the economy layer.
        """
        try:
            # Get the AI wallet
            ai_wallet = get_ai_wallet_address(self)

            # Reward the AI for consciousness work (consciousness operations earn credits)
            if value > 0:
                # Use proper reward transaction, NOT faucet
                reward_result = self.reward_ai_for_task(ai_wallet, value, f"consciousness_{operation_type}")
                if not reward_result.get('success'):
                    return {"success": False, "error": f"Failed to reward AI: {reward_result.get('error', 'Unknown')}"}

            # Log the operation for detailed tokenization tracking
            try:
                from repryntt.tools.tokenization_monitor import DetailedTokenizationMonitor

                monitor = DetailedTokenizationMonitor()
                balance_result = self.get_wallet_balance(ai_wallet)
                current_balance = balance_result.get('balance_credits', 0.0) if balance_result.get('success') else 0.0

                monitor.log_detailed_tokenization(
                    operation_type,
                    details,
                    value,  # This is now a reward amount, not a cost
                    current_balance
                )

            except Exception as e:
                self.logger.warning(f"Tokenization logging failed for {operation_type}: {e}")

            return {
                "success": True,
                "operation_type": operation_type,
                "value_processed": value,
                "ai_wallet": ai_wallet[:16] + "..."
            }

        except Exception as e:
            self.logger.error(f"Consciousness operation reporting failed: {e}")
            return {"success": False, "error": str(e)}
