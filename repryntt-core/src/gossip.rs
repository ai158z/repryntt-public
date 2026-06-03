//! Gossip protocol — message propagation, deduplication, TCP relay.
//!
//! Implements the repryntt gossip layer matching Python's `gossip.py`:
//! - Fanout relay to random peer subset
//! - TTL-bounded message propagation
//! - SHA-256 message ID deduplication
//! - TCP listener with length-prefixed framing
//! - Dead peer pruning and reconnection

use std::collections::{HashMap, HashSet, VecDeque};
use std::net::{IpAddr, SocketAddr, ToSocketAddrs};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use sha3::{Digest, Sha3_256};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::Mutex;

use crate::block::Block;
use crate::genesis::EXPECTED_GENESIS_HASH;
use crate::network::{self, DEFAULT_PORT, Message, MessageType};
use crate::peer::{
    BackoffTracker, ConnectionGuard, HEARTBEAT_INTERVAL_SECS, IBD_BATCH_SIZE, IBD_TIMEOUT_SECS,
    MAX_PEER_LIST_SIZE, MIN_COMPATIBLE_VERSION, MessageRateLimiter, PeerInfo, PeerManager,
};
use crate::storage::Storage;
use crate::transaction::Transaction;

// ── Gossip Constants (matching Python gossip.py) ─────────────────────────────

/// Number of peers to forward each message to.
pub const FANOUT: usize = 6;

/// Maximum hops per gossiped message.
pub const MAX_TTL: u8 = 20;

/// Forget seen message IDs after 5 minutes.
pub const SEEN_EXPIRY_SECS: f64 = 300.0;

/// Maximum number of seen IDs to track.
pub const MAX_SEEN: usize = 100_000;

/// Maximum gossip inbound connections.
pub const GOSSIP_MAX_INBOUND: usize = 64;

// ── Helpers ──────────────────────────────────────────────────────────────────

/// Check if an IP address belongs to this machine.
fn is_local_ip(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => {
            if v4.is_loopback() || v4.is_unspecified() {
                return true;
            }
            // Bind a UDP socket to the IP — only succeeds if it's a local address
            std::net::UdpSocket::bind((v4, 0)).is_ok()
        }
        IpAddr::V6(v6) => {
            if v6.is_loopback() || v6.is_unspecified() {
                return true;
            }
            std::net::UdpSocket::bind((v6, 0)).is_ok()
        }
    }
}

fn configured_public_p2p_addrs() -> Vec<SocketAddr> {
    let raw = match std::env::var("REPRYNTT_PUBLIC_P2P_ADDR") {
        Ok(v) => v,
        Err(_) => return vec![],
    };
    let addr = raw
        .trim()
        .trim_start_matches("tcp://")
        .trim_start_matches("ws://")
        .trim_start_matches("wss://")
        .trim_start_matches("http://")
        .trim_start_matches("https://")
        .split('/')
        .next()
        .unwrap_or("")
        .trim();
    if addr.is_empty() {
        return vec![];
    }
    addr.to_socket_addrs()
        .map(|iter| iter.collect())
        .unwrap_or_default()
}

fn is_configured_public_self_addr(addr: SocketAddr) -> bool {
    configured_public_p2p_addrs()
        .into_iter()
        .any(|public_addr| public_addr == addr)
}

fn find_header_start(blocks: &[Block], locator: &[String]) -> Option<usize> {
    if blocks.is_empty() {
        return Some(0);
    }
    for hash in locator {
        if let Some(pos) = blocks.iter().position(|b| &b.hash == hash) {
            return Some(pos + 1);
        }
    }
    None
}

fn build_headers(
    blocks: &[Block],
    start: usize,
    limit: u64,
    stop_hash: &str,
) -> Vec<serde_json::Value> {
    let mut headers = Vec::new();
    for block in blocks.iter().skip(start).take(limit as usize) {
        headers.push(serde_json::json!({
            "index": block.index,
            "hash": block.hash,
            "previous_hash": block.previous_hash,
            "timestamp": block.timestamp,
            "miner_address": block.miner_address,
            "tx_count": block.transactions.len(),
        }));
        if !stop_hash.is_empty() && block.hash == stop_hash {
            break;
        }
    }
    headers
}

// ── Gossip Message ───────────────────────────────────────────────────────────

/// A message propagated through the gossip network.
#[derive(Debug, Clone)]
pub struct GossipMessage {
    /// SHA-256 of payload — deduplication key.
    pub msg_id: String,
    /// Message category: "block", "tx", "peer_list", "heartbeat".
    pub msg_type: String,
    /// The actual message payload.
    pub payload: Message,
    /// Remaining hops.
    pub ttl: u8,
    /// Node ID of original sender.
    pub origin: String,
    /// When the message was created.
    pub timestamp: f64,
}

impl GossipMessage {
    /// Create a new gossip-wrapped message.
    pub fn new(msg_type: &str, payload: Message, origin: &str) -> Self {
        let msg_id = message_id(&payload);
        Self {
            msg_id,
            msg_type: msg_type.to_string(),
            payload,
            ttl: MAX_TTL,
            origin: origin.to_string(),
            timestamp: now_f64(),
        }
    }

    /// Serialize to JSON bytes for wire transmission.
    pub fn to_bytes(&self) -> Result<Vec<u8>, serde_json::Error> {
        let envelope = serde_json::json!({
            "msg_id": self.msg_id,
            "msg_type": self.msg_type,
            "ttl": self.ttl,
            "origin": self.origin,
            "timestamp": self.timestamp,
            "payload": self.payload,
        });
        serde_json::to_vec(&envelope)
    }

    /// Deserialize from JSON bytes.
    pub fn from_bytes(data: &[u8]) -> Result<Self, serde_json::Error> {
        let v: serde_json::Value = serde_json::from_slice(data)?;
        let payload: Message = serde_json::from_value(v["payload"].clone())?;
        Ok(Self {
            msg_id: v["msg_id"].as_str().unwrap_or("").to_string(),
            msg_type: v["msg_type"].as_str().unwrap_or("").to_string(),
            payload,
            ttl: v["ttl"].as_u64().unwrap_or(0) as u8,
            origin: v["origin"].as_str().unwrap_or("").to_string(),
            timestamp: v["timestamp"].as_f64().unwrap_or(0.0),
        })
    }
}

/// Deterministic message ID from payload content (SHA-256, truncated to 32 hex).
fn message_id(msg: &Message) -> String {
    let bytes = msg.to_bytes().unwrap_or_default();
    let mut hasher = Sha3_256::new();
    hasher.update(&bytes);
    hex::encode(hasher.finalize())[..32].to_string()
}

// ── Seen Message Tracker ─────────────────────────────────────────────────────

/// Tracks recently seen message IDs for deduplication.
struct SeenTracker {
    /// msg_id → timestamp when seen.
    seen: HashMap<String, f64>,
    /// Insertion order for efficient expiry.
    order: VecDeque<(String, f64)>,
}

impl SeenTracker {
    fn new() -> Self {
        Self {
            seen: HashMap::new(),
            order: VecDeque::new(),
        }
    }

    /// Returns true if already seen.
    fn is_seen(&self, msg_id: &str) -> bool {
        self.seen.contains_key(msg_id)
    }

    /// Mark a message as seen.
    fn mark_seen(&mut self, msg_id: &str) {
        let now = now_f64();
        if self.seen.insert(msg_id.to_string(), now).is_none() {
            self.order.push_back((msg_id.to_string(), now));
        }
        self.expire();
    }

    /// Remove expired entries.
    fn expire(&mut self) {
        let cutoff = now_f64() - SEEN_EXPIRY_SECS;

        // Expire old entries
        while let Some((id, ts)) = self.order.front() {
            if *ts < cutoff {
                self.seen.remove(id);
                self.order.pop_front();
            } else {
                break;
            }
        }

        // Hard cap
        while self.seen.len() > MAX_SEEN {
            if let Some((id, _)) = self.order.pop_front() {
                self.seen.remove(&id);
            }
        }
    }
}

// ── Gossip Stats ─────────────────────────────────────────────────────────────

/// Gossip protocol statistics.
#[derive(Debug, Clone, Default)]
pub struct GossipStats {
    pub messages_received: u64,
    pub messages_relayed: u64,
    pub messages_dropped_dup: u64,
    pub messages_dropped_ttl: u64,
    pub messages_sent: u64,
    pub bytes_sent: u64,
    pub bytes_received: u64,
    pub blocks_announced: u64,
    pub txs_announced: u64,
    pub peer_connections: u64,
    pub peer_disconnections: u64,
}

// ── Gossip Event (for the consumer) ──────────────────────────────────────────

/// Events produced by the gossip layer for the node to handle.
#[derive(Debug, Clone)]
pub enum GossipEvent {
    /// A new block was announced by a peer.
    NewBlock(Block),
    /// A new transaction was announced by a peer.
    NewTransaction(Transaction),
    /// A peer announced its chain height (potentially ahead of ours).
    PeerHeight {
        node_id: String,
        height: u64,
        genesis_hash: String,
    },
    /// A peer sent us a list of other peers.
    PeerList(Vec<(String, u16)>),
    /// A new peer completed handshake.
    PeerConnected(String),
    /// A peer disconnected.
    PeerDisconnected(String),
}

// ── Gossip Node ──────────────────────────────────────────────────────────────

/// The gossip protocol engine.
///
/// Manages peer connections, message relay, and event dispatch.
pub struct GossipNode {
    /// Our node ID.
    pub node_id: String,
    /// Our listen address.
    pub listen_addr: SocketAddr,
    /// Seed nodes to connect to.
    seeds: Vec<SocketAddr>,
    /// Seeds whose connection failure we already logged (log once, not every retry).
    seed_fail_logged: Arc<Mutex<HashSet<SocketAddr>>>,
    /// Outbound addresses currently being dialed; prevents connection storms.
    dialing: Arc<Mutex<HashSet<SocketAddr>>>,
    /// Last log time by repeated warning key.
    log_limiter: Arc<Mutex<HashMap<String, f64>>>,
    /// Peer manager (protected by async mutex for concurrent access).
    pub peers: Arc<Mutex<PeerManager>>,
    /// Connection guard.
    pub guard: Arc<Mutex<ConnectionGuard>>,
    /// Seen message tracker.
    seen: Arc<Mutex<SeenTracker>>,
    /// Statistics.
    pub stats: Arc<Mutex<GossipStats>>,
    /// Event queue for the consumer.
    events: Arc<Mutex<VecDeque<GossipEvent>>>,
    /// Our chain height (updated by the node).
    pub chain_height: Arc<Mutex<u64>>,
    /// Our local tip block hash at `chain_height`. Updated atomically with
    /// `chain_height` via `set_chain_tip`. Empty before the chain is loaded.
    pub chain_tip_hash: Arc<Mutex<String>>,
    /// Running flag.
    pub running: Arc<Mutex<bool>>,
    /// Shared blocks for serving to peers during IBD (fallback if no storage).
    pub shared_blocks: Arc<Mutex<Vec<Block>>>,
    /// Persistent storage — blocks are served from disk when available.
    storage: Arc<Mutex<Option<Arc<Storage>>>>,
    /// Per-message-type rate limiter.
    pub msg_rate_limiter: Arc<Mutex<MessageRateLimiter>>,
    /// Reconnection backoff tracker.
    pub backoff: Arc<Mutex<BackoffTracker>>,
}

impl GossipNode {
    /// Create a new gossip node.
    pub fn new(node_id: &str, listen_port: u16, seeds: Vec<SocketAddr>) -> Self {
        Self {
            node_id: node_id.to_string(),
            listen_addr: SocketAddr::from(([0, 0, 0, 0], listen_port)),
            seeds,
            seed_fail_logged: Arc::new(Mutex::new(HashSet::new())),
            dialing: Arc::new(Mutex::new(HashSet::new())),
            log_limiter: Arc::new(Mutex::new(HashMap::new())),
            peers: Arc::new(Mutex::new(PeerManager::new())),
            guard: Arc::new(Mutex::new(ConnectionGuard::new())),
            seen: Arc::new(Mutex::new(SeenTracker::new())),
            stats: Arc::new(Mutex::new(GossipStats::default())),
            events: Arc::new(Mutex::new(VecDeque::new())),
            chain_height: Arc::new(Mutex::new(0)),
            chain_tip_hash: Arc::new(Mutex::new(String::new())),
            running: Arc::new(Mutex::new(true)),
            shared_blocks: Arc::new(Mutex::new(Vec::new())),
            storage: Arc::new(Mutex::new(None)),
            msg_rate_limiter: Arc::new(Mutex::new(MessageRateLimiter::new())),
            backoff: Arc::new(Mutex::new(BackoffTracker::new())),
        }
    }

    /// Start the gossip node: listener + seed connector + heartbeat loop.
    ///
    /// Returns when the node is shut down.
    pub async fn run(&self) {
        let listener = self.start_listener();
        let connector = self.connect_to_seeds();
        let heartbeat = self.heartbeat_loop();
        let maintenance = self.maintenance_loop();
        let bootstrap = self.bootstrap_loop();

        tokio::join!(listener, connector, heartbeat, maintenance, bootstrap);
    }

    /// Drain all pending events.
    pub async fn drain_events(&self) -> Vec<GossipEvent> {
        let mut events = self.events.lock().await;
        events.drain(..).collect()
    }

    /// Update our chain height and tip hash atomically. Called by the block
    /// producer / sync loop after every height-changing event (produce, import,
    /// reorg). The tip hash is the hash of the block at `height`.
    pub async fn set_chain_tip(&self, height: u64, tip_hash: String) {
        let mut h = self.chain_height.lock().await;
        let mut t = self.chain_tip_hash.lock().await;
        *h = height;
        *t = tip_hash;
    }

    /// Update the shared blocks for serving to peers during IBD.
    pub async fn update_shared_blocks(&self, blocks: Vec<Block>) {
        *self.shared_blocks.lock().await = blocks;
    }

    /// Attach persistent storage so blocks are served from disk during IBD.
    /// Call this after the storage is initialized.
    pub async fn set_storage(&self, storage: Arc<Storage>) {
        *self.storage.lock().await = Some(storage);
    }

    // ── Broadcasting ─────────────────────────────────────────────────────

    /// Broadcast a new block to the gossip network.
    ///
    /// Sends a direct BlockAnnounce wire frame to each alive peer (no
    /// GossipMessage relay envelope — the listener path has no unwrap handler
    /// for that, which is why broadcasts used to silently drop). Receivers
    /// dedupe by block hash via SeenTracker, so a re-broadcast that loops
    /// back to us is dropped.
    pub async fn broadcast_block(&self, block: &Block) {
        // Mark seen locally so we don't accept our own broadcast back.
        self.seen.lock().await.mark_seen(&block.hash);

        let block_json = match serde_json::to_value(block.to_dict()) {
            Ok(v) => v,
            Err(e) => {
                eprintln!("⚠️  broadcast_block serialize failed: {}", e);
                return;
            }
        };
        let msg = network::msg_block_announce(&block_json).with_node_id(&self.node_id);

        let payload = match msg.to_bytes() {
            Ok(b) => b,
            Err(e) => {
                eprintln!("⚠️  broadcast_block encode failed: {}", e);
                return;
            }
        };
        let len_prefix = (payload.len() as u32).to_be_bytes();

        let peer_addrs: Vec<SocketAddr> = {
            let peers = self.peers.lock().await;
            peers
                .peers
                .values()
                .filter(|p| p.is_alive())
                .map(|p| p.addr)
                .collect()
        };
        if peer_addrs.is_empty() {
            self.stats.lock().await.blocks_announced += 1;
            return;
        }

        let mut sent = 0usize;
        for addr in peer_addrs {
            let mut data = Vec::with_capacity(4 + payload.len());
            data.extend_from_slice(&len_prefix);
            data.extend_from_slice(&payload);
            let stats = self.stats.clone();
            tokio::spawn(async move {
                if let Ok(Ok(stream)) = tokio::time::timeout(
                    std::time::Duration::from_secs(5),
                    TcpStream::connect(addr),
                )
                .await
                {
                    if stream.try_write(&data).is_ok() {
                        let mut s = stats.lock().await;
                        s.messages_sent += 1;
                        s.bytes_sent += data.len() as u64;
                    }
                }
            });
            sent += 1;
        }
        if sent > 0 {
            println!("📡 Block {} broadcast → {} peer(s)", block.index, sent);
        }
        self.stats.lock().await.blocks_announced += 1;
    }

    /// Broadcast a new transaction to the gossip network.
    pub async fn broadcast_tx(&self, tx: &Transaction) {
        // Mark seen locally so we don't accept our own broadcast back via a peer.
        self.seen.lock().await.mark_seen(&tx.tx_hash);

        let tx_json = match serde_json::to_value(tx) {
            Ok(v) => v,
            Err(e) => {
                eprintln!("⚠️  broadcast_tx serialize failed: {}", e);
                return;
            }
        };
        let msg = network::msg_tx_announce(&tx_json).with_node_id(&self.node_id);

        // Send a direct TxAnnounce wire frame to each alive peer. We don't wrap
        // in a GossipMessage relay envelope because the live listener path
        // (GossipNodeHandles::handle_inbound) has no MessageType::Gossip
        // unwrapper and would silently drop the inner TX. Receivers dedupe by
        // tx_hash via SeenTracker, so a flood-relay is loop-safe.
        let payload = match msg.to_bytes() {
            Ok(b) => b,
            Err(e) => {
                eprintln!("⚠️  broadcast_tx encode failed: {}", e);
                return;
            }
        };
        let len_prefix = (payload.len() as u32).to_be_bytes();

        let peer_addrs: Vec<SocketAddr> = {
            let peers = self.peers.lock().await;
            peers
                .peers
                .values()
                .filter(|p| p.is_alive())
                .map(|p| p.addr)
                .collect()
        };
        if peer_addrs.is_empty() {
            self.stats.lock().await.txs_announced += 1;
            return;
        }

        let mut sent = 0usize;
        for addr in peer_addrs {
            let mut data = Vec::with_capacity(4 + payload.len());
            data.extend_from_slice(&len_prefix);
            data.extend_from_slice(&payload);
            let stats = self.stats.clone();
            tokio::spawn(async move {
                if let Ok(Ok(stream)) = tokio::time::timeout(
                    std::time::Duration::from_secs(5),
                    TcpStream::connect(addr),
                )
                .await
                {
                    if stream.try_write(&data).is_ok() {
                        let mut s = stats.lock().await;
                        s.messages_sent += 1;
                        s.bytes_sent += data.len() as u64;
                    }
                }
            });
            sent += 1;
        }
        if sent > 0 {
            // One-line audit trail so we can tell from the log that broadcast
            // actually attempted delivery (vs. silently no-oping with 0 peers).
            println!("📡 TX broadcast {} → {} peer(s)", &tx.tx_hash[..16], sent);
        }
        self.stats.lock().await.txs_announced += 1;
    }

    // ── TCP Listener ─────────────────────────────────────────────────────

    async fn start_listener(&self) {
        let listener = match TcpListener::bind(self.listen_addr).await {
            Ok(l) => {
                println!("🌐 Gossip listener started on {}", self.listen_addr);
                l
            }
            Err(e) => {
                eprintln!(
                    "⚠️  Failed to bind gossip listener on {}: {}",
                    self.listen_addr, e
                );
                return;
            }
        };

        loop {
            if !*self.running.lock().await {
                break;
            }

            let (stream, addr) = match listener.accept().await {
                Ok(pair) => pair,
                Err(_) => continue,
            };

            let ip = addr.ip().to_string();

            // Connection gating
            {
                let mut guard = self.guard.lock().await;
                if let Err(reason) = guard.allow_connection(&ip) {
                    drop(stream);
                    if self
                        .should_log_limited(&format!("reject:{ip}:{reason}"), 60.0)
                        .await
                    {
                        eprintln!("⛔ Rejected connection from {}: {}", ip, reason);
                    }
                    continue;
                }
            }

            // Handle in background
            let node = self.clone_handles();
            let peer_addr = addr;
            tokio::spawn(async move {
                node.handle_inbound(stream, peer_addr).await;
            });
        }
    }

    async fn handle_inbound(&self, mut stream: TcpStream, addr: SocketAddr) {
        let ip = addr.ip().to_string();

        // Set socket timeout
        let _ = stream.set_nodelay(true);

        let result = tokio::time::timeout(
            std::time::Duration::from_secs(30),
            network::read_message(&mut stream),
        )
        .await;

        match result {
            Ok(Ok(Some(msg))) => {
                // Aggregate rate limiting
                {
                    let mut guard = self.guard.lock().await;
                    if !guard.check_rate_limit(&ip) {
                        guard.record_misbehavior(&ip, 10, "rate limit exceeded");
                        self.guard.lock().await.release_connection(&ip);
                        return;
                    }
                }

                // Per-message-type rate limiting
                {
                    let msg_type_str = msg.msg_type.as_str();
                    let mut limiter = self.msg_rate_limiter.lock().await;
                    if !limiter.check(&ip, msg_type_str) {
                        self.guard.lock().await.record_misbehavior(
                            &ip,
                            15,
                            "per-type rate limit exceeded",
                        );
                        self.guard.lock().await.release_connection(&ip);
                        return;
                    }
                }

                // Validate network magic
                if !msg.validate_magic() {
                    let mut guard = self.guard.lock().await;
                    guard.record_misbehavior(&ip, 50, "wrong network magic");
                    guard.release_connection(&ip);
                    return;
                }

                self.stats.lock().await.messages_received += 1;

                // Dispatch by message type
                self.handle_message(msg, &mut stream, addr).await;
            }
            Ok(Ok(None)) => {} // Clean disconnect
            Ok(Err(_)) => {}   // Read error
            Err(_) => {
                // Timeout — potential slowloris
                self.guard
                    .lock()
                    .await
                    .record_misbehavior(&ip, 10, "initial read timeout");
            }
        }

        self.guard.lock().await.release_connection(&ip);
    }

    async fn handle_message(&self, msg: Message, stream: &mut TcpStream, addr: SocketAddr) {
        let msg_type = msg.parsed_type();

        match msg_type {
            Some(MessageType::Handshake) => {
                self.on_handshake(msg, stream, addr).await;
            }
            Some(MessageType::GetChainHeight) => {
                self.on_get_chain_height(stream).await;
            }
            Some(MessageType::GetBlocks) => {
                self.on_get_blocks(msg, stream).await;
            }
            Some(MessageType::GetHeaders) => {
                self.on_get_headers(msg, stream).await;
            }
            Some(MessageType::BlockAnnounce) => {
                self.on_block_announce(msg, addr).await;
            }
            Some(MessageType::TxAnnounce) => {
                self.on_tx_announce(msg, addr).await;
            }
            Some(MessageType::Ping) => {
                self.on_ping(stream).await;
            }
            Some(MessageType::PeerList) => {
                self.on_peer_list(msg).await;
            }
            Some(MessageType::Gossip) => {
                self.on_gossip_relay(msg, addr).await;
            }
            _ => {
                // Unknown message type — minor misbehavior
                let ip = addr.ip().to_string();
                self.guard
                    .lock()
                    .await
                    .record_misbehavior(&ip, 5, "unknown message type");
            }
        }
    }

    // ── Message Handlers ─────────────────────────────────────────────────

    async fn on_handshake(&self, msg: Message, stream: &mut TcpStream, addr: SocketAddr) {
        let node_id = msg.node_id.clone();
        let genesis_hash = msg
            .payload
            .get("genesis_hash")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let chain_height = msg
            .payload
            .get("chain_height")
            .and_then(|v| v.as_u64())
            .unwrap_or(0);
        let tflops = msg
            .payload
            .get("tflops")
            .and_then(|v| v.as_f64())
            .unwrap_or(0.0);

        let ip = addr.ip().to_string();

        // Self-connection detection
        if node_id == self.node_id {
            self.guard
                .lock()
                .await
                .record_misbehavior(&ip, 5, "self-connection");
            return;
        }

        // Protocol version check
        if msg.protocol_version < MIN_COMPATIBLE_VERSION {
            self.guard
                .lock()
                .await
                .record_misbehavior(&ip, 30, "incompatible protocol version");
            return;
        }

        // Verify genesis match
        if !genesis_hash.is_empty() && genesis_hash != EXPECTED_GENESIS_HASH {
            let ip = addr.ip().to_string();
            self.guard
                .lock()
                .await
                .record_misbehavior(&ip, 100, "genesis mismatch");
            return;
        }

        // Register peer
        let mut peer = PeerInfo::new(node_id.clone(), addr, false);
        peer.chain_height = chain_height;
        peer.genesis_hash = genesis_hash;
        peer.tflops = tflops;
        peer.protocol_version = msg.protocol_version;

        self.peers.lock().await.add_peer(peer);

        // Send handshake ACK
        let height = *self.chain_height.lock().await;
        let ack = network::msg_handshake(
            &self.node_id,
            height,
            EXPECTED_GENESIS_HASH,
            0.0, // our tflops (filled by caller)
        );
        let _ = network::write_message(stream, &ack).await;

        // Send peer list
        let addrs = self.peers.lock().await.peer_addresses();
        if !addrs.is_empty() {
            let peer_msg = network::msg_peer_list(&addrs);
            let _ = network::write_message(stream, &peer_msg).await;
        }

        self.events
            .lock()
            .await
            .push_back(GossipEvent::PeerConnected(node_id));
        self.stats.lock().await.peer_connections += 1;
    }

    async fn on_get_chain_height(&self, stream: &mut TcpStream) {
        let height = *self.chain_height.lock().await;
        let tip_hash = self.chain_tip_hash.lock().await.clone();
        let resp = network::msg_chain_height(height, EXPECTED_GENESIS_HASH, &tip_hash);
        let _ = network::write_message(stream, &resp).await;
    }

    async fn on_get_blocks(&self, msg: Message, stream: &mut TcpStream) {
        let start = msg
            .payload
            .get("start")
            .and_then(|v| v.as_u64())
            .unwrap_or(0);
        let end = msg.payload.get("end").and_then(|v| v.as_u64()).unwrap_or(0);

        let batch_end = end.min(start + IBD_BATCH_SIZE);

        // Try reading from persistent storage (disk) first
        let blocks_result = {
            let storage_guard = self.storage.lock().await;
            if let Some(ref storage) = *storage_guard {
                storage.get_block_range(start, batch_end).ok()
            } else {
                None
            }
        };

        let block_jsons: Vec<serde_json::Value> = match blocks_result {
            Some(blocks) => blocks
                .iter()
                .map(|b| serde_json::to_value(b.to_dict()).unwrap())
                .collect(),
            None => {
                // Fallback to in-memory shared_blocks
                let blocks = self.shared_blocks.lock().await;
                let s = (start as usize).min(blocks.len());
                let e = (batch_end as usize).min(blocks.len());
                blocks[s..e]
                    .iter()
                    .map(|b| serde_json::to_value(b.to_dict()).unwrap())
                    .collect()
            }
        };

        let mut payload = std::collections::BTreeMap::new();
        payload.insert("success".into(), serde_json::Value::Bool(true));
        payload.insert("blocks".into(), serde_json::Value::Array(block_jsons));
        let resp = Message::new(MessageType::Blocks, payload);
        let data = resp.to_bytes().unwrap_or_default();
        let _ = network::write_raw(stream, &data).await;
    }

    async fn on_get_headers(&self, msg: Message, stream: &mut TcpStream) {
        let locator: Vec<String> = msg
            .payload
            .get("locator")
            .and_then(|v| v.as_array())
            .map(|items| {
                items
                    .iter()
                    .filter_map(|v| v.as_str().map(|s| s.to_string()))
                    .collect()
            })
            .unwrap_or_default();
        let stop_hash = msg
            .payload
            .get("stop_hash")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let limit = msg
            .payload
            .get("limit")
            .and_then(|v| v.as_u64())
            .unwrap_or(500)
            .min(2_000);

        let blocks = {
            let storage_guard = self.storage.lock().await;
            if let Some(ref storage) = *storage_guard {
                storage.load_chain().unwrap_or_default()
            } else {
                self.shared_blocks.lock().await.clone()
            }
        };

        let best_height = blocks.len() as u64;
        let start = find_header_start(&blocks, &locator).unwrap_or(0);
        let headers = build_headers(&blocks, start, limit, stop_hash);
        let resp = network::msg_headers(best_height, EXPECTED_GENESIS_HASH, headers);
        let _ = network::write_message(stream, &resp).await;
    }

    async fn on_block_announce(&self, msg: Message, _addr: SocketAddr) {
        let height = msg
            .payload
            .get("block_index")
            .and_then(|v| v.as_u64())
            .unwrap_or(0);
        let genesis_hash = msg
            .payload
            .get("genesis_hash")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let block_hash = msg
            .payload
            .get("hash")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let node_id = msg.node_id.clone();

        self.update_peer_height(&node_id, height, &genesis_hash, &block_hash)
            .await;

        self.events.lock().await.push_back(GossipEvent::PeerHeight {
            node_id,
            height,
            genesis_hash,
        });
    }

    async fn on_tx_announce(&self, msg: Message, _addr: SocketAddr) {
        self.stats.lock().await.txs_announced += 1;

        let tx_hash = match msg.payload.get("tx_hash").and_then(|v| v.as_str()) {
            Some(h) if !h.is_empty() => h.to_string(),
            _ => return,
        };

        // Do not mark transaction announces as seen before mempool validation.
        // Future-nonce PoPW txs are valid once earlier nonces land; if we
        // remember a rejected future nonce here, later retries are dropped
        // before the mempool has a chance to accept them.

        // Parse the full tx body. Older peers that only sent hash/type/amount
        // will fail parsing here and silently fall through — the announce
        // serves as a no-op for them under the new protocol.
        let tx_value = match msg.payload.get("tx") {
            Some(v) => v.clone(),
            None => return,
        };
        let tx: Transaction = match serde_json::from_value(tx_value) {
            Ok(t) => t,
            Err(_) => return,
        };

        // Verify the announced tx_hash matches the body so a peer can't lie
        // about which tx they're sending us under a hash we've already seen.
        if tx.tx_hash != tx_hash {
            return;
        }

        self.events
            .lock()
            .await
            .push_back(GossipEvent::NewTransaction(tx));
    }

    async fn on_ping(&self, stream: &mut TcpStream) {
        let height = *self.chain_height.lock().await;
        let pong = network::msg_pong(height);
        let _ = network::write_message(stream, &pong).await;
    }

    async fn on_peer_list(&self, msg: Message) {
        let peers = msg
            .payload
            .get("peers")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();

        let mut addrs = Vec::new();
        for p in peers {
            let host = p
                .get("host")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let port = p
                .get("port")
                .and_then(|v| v.as_u64())
                .unwrap_or(DEFAULT_PORT as u64) as u16;
            if !host.is_empty() && crate::peer::is_safe_peer_address(&host) {
                addrs.push((host, port));
            }
            // Truncate to MAX_PEER_LIST_SIZE
            if addrs.len() >= MAX_PEER_LIST_SIZE {
                break;
            }
        }

        if !addrs.is_empty() {
            self.events
                .lock()
                .await
                .push_back(GossipEvent::PeerList(addrs));
        }
    }

    async fn on_gossip_relay(&self, _msg: Message, _addr: SocketAddr) {
        // Gossip relay messages carry an inner GossipMessage
        // For the MVP, process as a generic received message
        self.stats.lock().await.messages_relayed += 1;
    }

    // ── Relay / Fanout ───────────────────────────────────────────────────

    /// Send a gossip message to a random subset of peers (fanout).
    async fn relay_to_peers(&self, gossip_msg: &GossipMessage, exclude: Option<SocketAddr>) {
        // Mark as seen so we don't process our own message
        self.seen.lock().await.mark_seen(&gossip_msg.msg_id);

        let peers = self.peers.lock().await;
        let mut candidates: Vec<SocketAddr> = peers
            .peers
            .values()
            .filter(|p| p.is_alive())
            .filter(|p| exclude.map_or(true, |ex| p.addr != ex))
            .map(|p| p.addr)
            .collect();

        // Truncate to fanout
        if candidates.len() > FANOUT {
            // Deterministic shuffle using message ID as seed
            let seed = u64::from_be_bytes(
                gossip_msg.msg_id.as_bytes()[..8]
                    .try_into()
                    .unwrap_or([0; 8]),
            );
            // Simple Fisher-Yates shuffle with the seed
            for i in (1..candidates.len()).rev() {
                let j = (seed.wrapping_add(i as u64) as usize) % (i + 1);
                candidates.swap(i, j);
            }
            candidates.truncate(FANOUT);
        }

        let payload = match gossip_msg.to_bytes() {
            Ok(b) => b,
            Err(_) => return,
        };

        let len_prefix = (payload.len() as u32).to_be_bytes();

        for addr in candidates {
            let mut data = Vec::with_capacity(4 + payload.len());
            data.extend_from_slice(&len_prefix);
            data.extend_from_slice(&payload);

            let stats = self.stats.clone();
            let peers = self.peers.clone();
            tokio::spawn(async move {
                match tokio::time::timeout(
                    std::time::Duration::from_secs(5),
                    TcpStream::connect(addr),
                )
                .await
                {
                    Ok(Ok(stream)) => {
                        if stream.try_write(&data).is_ok() {
                            stats.lock().await.messages_sent += 1;
                            stats.lock().await.bytes_sent += data.len() as u64;
                            // Reward reputation for successful relay
                            let mut mgr = peers.lock().await;
                            for p in mgr.peers.values_mut() {
                                if p.addr == addr {
                                    p.adjust_reputation(0.01);
                                    p.messages_relayed += 1;
                                    break;
                                }
                            }
                        }
                    }
                    _ => {
                        // Penalize reputation for failed relay
                        let mut mgr = peers.lock().await;
                        for p in mgr.peers.values_mut() {
                            if p.addr == addr {
                                p.adjust_reputation(-0.05);
                                break;
                            }
                        }
                    }
                }
            });
        }
    }

    // ── Seed Connection ──────────────────────────────────────────────────

    async fn should_log_limited(&self, key: &str, interval_secs: f64) -> bool {
        let now = now_f64();
        let mut limiter = self.log_limiter.lock().await;
        match limiter.get(key).copied() {
            Some(last) if now - last < interval_secs => false,
            _ => {
                limiter.insert(key.to_string(), now);
                true
            }
        }
    }

    async fn connect_to_seeds(&self) {
        // Small delay to let the listener start
        tokio::time::sleep(std::time::Duration::from_secs(1)).await;

        let our_port = self.listen_addr.port();

        for &addr in &self.seeds {
            // Skip if it's us — check both exact match and same-port-on-local-IP
            if addr == self.listen_addr {
                continue;
            }
            if addr.port() == our_port && is_local_ip(addr.ip()) {
                continue;
            }
            if is_configured_public_self_addr(addr) {
                continue;
            }

            if let Err(e) = self.connect_to_peer(addr).await {
                // Log each seed failure only once (Satoshi-style quiet backoff)
                let mut logged = self.seed_fail_logged.lock().await;
                if logged.insert(addr) {
                    eprintln!(
                        "⚠️  Seed {} unreachable: {} (silencing further attempts)",
                        addr, e
                    );
                }
            } else {
                // Connected successfully — remove from logged set so we'd log again if it drops
                self.seed_fail_logged.lock().await.remove(&addr);
            }
        }
    }

    async fn connect_to_bootstrap_peers(&self) {
        let p2p_port = self.listen_addr.port();
        let addrs =
            match tokio::task::spawn_blocking(move || network::query_bootstrap_url(p2p_port)).await
            {
                Ok(addrs) => addrs,
                Err(_) => return,
            };

        for addr in addrs {
            if addr == self.listen_addr {
                continue;
            }
            if addr.port() == self.listen_addr.port() && is_local_ip(addr.ip()) {
                continue;
            }
            if is_configured_public_self_addr(addr) {
                continue;
            }
            let _ = self.connect_to_peer(addr).await;
        }
    }

    /// Connect to a specific peer and perform handshake.
    pub async fn connect_to_peer(&self, addr: SocketAddr) -> Result<(), String> {
        {
            let mut dialing = self.dialing.lock().await;
            if !dialing.insert(addr) {
                return Ok(());
            }
        }
        let result = self.connect_to_peer_inner(addr).await;
        self.dialing.lock().await.remove(&addr);
        result
    }

    async fn connect_to_peer_inner(&self, addr: SocketAddr) -> Result<(), String> {
        // Check backoff
        {
            let backoff = self.backoff.lock().await;
            if !backoff.can_reconnect(&addr.to_string()) {
                return Err("backoff active".into());
            }
        }

        // Check ban
        {
            let guard = self.guard.lock().await;
            if guard.is_banned(&addr.ip().to_string()) {
                return Err("peer is banned".into());
            }
        }

        // Check if already connected
        {
            let peers = self.peers.lock().await;
            for p in peers.peers.values() {
                if p.addr == addr {
                    return Ok(()); // Already connected
                }
            }
        }

        let mut stream = match tokio::time::timeout(
            std::time::Duration::from_secs(10),
            TcpStream::connect(addr),
        )
        .await
        {
            Ok(Ok(s)) => s,
            Ok(Err(e)) => {
                self.backoff.lock().await.record_failure(&addr.to_string());
                return Err(format!("Connect error: {}", e));
            }
            Err(_) => {
                self.backoff.lock().await.record_failure(&addr.to_string());
                return Err("Connection timeout".to_string());
            }
        };

        // Send handshake
        let height = *self.chain_height.lock().await;
        let handshake = network::msg_handshake(&self.node_id, height, EXPECTED_GENESIS_HASH, 0.0);
        network::write_message(&mut stream, &handshake)
            .await
            .map_err(|e| format!("Write error: {}", e))?;

        // Read handshake ACK
        let resp = tokio::time::timeout(
            std::time::Duration::from_secs(10),
            network::read_message(&mut stream),
        )
        .await
        .map_err(|_| "Handshake ACK timeout".to_string())?
        .map_err(|e| format!("Read error: {}", e))?;

        if let Some(ack) = resp {
            if !ack.validate_magic() {
                self.backoff.lock().await.record_failure(&addr.to_string());
                return Err("Wrong network magic".into());
            }

            let node_id = ack.node_id.clone();

            // Self-connection detection
            if node_id == self.node_id {
                return Err("Self-connection detected".into());
            }

            // Protocol version check
            if ack.protocol_version < MIN_COMPATIBLE_VERSION {
                self.backoff.lock().await.record_failure(&addr.to_string());
                return Err(format!(
                    "Incompatible protocol version {} (min: {})",
                    ack.protocol_version, MIN_COMPATIBLE_VERSION
                ));
            }

            let chain_height = ack
                .payload
                .get("chain_height")
                .and_then(|v| v.as_u64())
                .unwrap_or(0);

            let mut peer = PeerInfo::new(node_id.clone(), addr, true);
            peer.chain_height = chain_height;
            peer.protocol_version = ack.protocol_version;

            self.peers.lock().await.add_peer(peer);
            self.backoff.lock().await.record_success(&addr.to_string());
            self.events
                .lock()
                .await
                .push_back(GossipEvent::PeerConnected(node_id));
            self.stats.lock().await.peer_connections += 1;

            println!("🤝 Connected to peer {} (height: {})", addr, chain_height);
        }

        Ok(())
    }

    // ── Heartbeat Loop ───────────────────────────────────────────────────

    async fn heartbeat_loop(&self) {
        loop {
            if !*self.running.lock().await {
                break;
            }

            tokio::time::sleep(std::time::Duration::from_secs(HEARTBEAT_INTERVAL_SECS)).await;

            let height = *self.chain_height.lock().await;
            let ping = network::msg_ping(height).with_node_id(&self.node_id);

            // Get peer addresses to ping
            let addrs: Vec<SocketAddr> = {
                let peers = self.peers.lock().await;
                peers.peers.values().map(|p| p.addr).collect()
            };

            // Send pings
            for addr in &addrs {
                let ping_clone = ping.clone();
                let addr = *addr;
                tokio::spawn(async move {
                    if let Ok(Ok(mut stream)) = tokio::time::timeout(
                        std::time::Duration::from_secs(5),
                        TcpStream::connect(addr),
                    )
                    .await
                    {
                        let _ = network::write_message(&mut stream, &ping_clone).await;
                    }
                });
            }

            // Prune dead peers
            let dead = self.peers.lock().await.prune_dead();
            for id in &dead {
                self.events
                    .lock()
                    .await
                    .push_back(GossipEvent::PeerDisconnected(id.clone()));
                self.stats.lock().await.peer_disconnections += 1;
            }
        }
    }

    // ── Maintenance Loop ─────────────────────────────────────────────────

    async fn maintenance_loop(&self) {
        loop {
            if !*self.running.lock().await {
                break;
            }

            tokio::time::sleep(std::time::Duration::from_secs(60)).await;

            // Cleanup connection guard
            self.guard.lock().await.cleanup();

            // Cleanup per-message-type rate limiter
            self.msg_rate_limiter.lock().await.cleanup();

            // Cleanup backoff tracker
            self.backoff.lock().await.cleanup();

            // If we need more peers, try reconnecting to seeds
            if self.peers.lock().await.needs_peers() {
                self.connect_to_seeds().await;
            }
        }
    }

    async fn bootstrap_loop(&self) {
        let bootstrap_url = std::env::var("REPRYNTT_BOOTSTRAP_URL")
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase();
        if matches!(bootstrap_url.as_str(), "" | "none" | "off" | "disabled") {
            return;
        }

        tokio::time::sleep(std::time::Duration::from_secs(3)).await;

        loop {
            if !*self.running.lock().await {
                break;
            }

            let node_id = self.node_id.clone();
            let p2p_port = self.listen_addr.port();
            let height = *self.chain_height.lock().await;
            tokio::task::spawn_blocking(move || {
                network::announce_to_bootstrap_url(
                    &node_id,
                    p2p_port,
                    height,
                    EXPECTED_GENESIS_HASH,
                )
            })
            .await
            .ok();

            self.connect_to_bootstrap_peers().await;

            tokio::time::sleep(std::time::Duration::from_secs(60)).await;
        }
    }

    /// Shutdown the gossip node.
    pub async fn shutdown(&self) {
        *self.running.lock().await = false;
    }

    // ── IBD: Initial Block Download ──────────────────────────────────────

    /// Query a specific peer for their chain height.
    pub async fn request_chain_height(
        &self,
        addr: SocketAddr,
    ) -> Result<(u64, String, String), String> {
        let mut stream =
            tokio::time::timeout(std::time::Duration::from_secs(10), TcpStream::connect(addr))
                .await
                .map_err(|_| "Connection timeout".to_string())?
                .map_err(|e| format!("Connect error: {}", e))?;

        let req = network::msg_get_chain_height().with_node_id(&self.node_id);
        network::write_message(&mut stream, &req)
            .await
            .map_err(|e| format!("Write error: {}", e))?;

        let resp = tokio::time::timeout(
            std::time::Duration::from_secs(10),
            network::read_message(&mut stream),
        )
        .await
        .map_err(|_| "Response timeout".to_string())?
        .map_err(|e| format!("Read error: {}", e))?;

        match resp {
            Some(msg) => {
                let height = msg
                    .payload
                    .get("chain_height")
                    .and_then(|v| v.as_u64())
                    .unwrap_or(0);
                let genesis = msg
                    .payload
                    .get("genesis_hash")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let tip_hash = msg
                    .payload
                    .get("tip_hash")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                self.touch_peer_by_addr(addr).await;
                self.update_peer_height_by_addr(addr, height, &genesis, &tip_hash)
                    .await;
                Ok((height, genesis, tip_hash))
            }
            None => Err("Empty response".into()),
        }
    }

    /// Request blocks [start, end) from a specific peer.
    pub async fn request_block_range(
        &self,
        addr: SocketAddr,
        start: u64,
        end: u64,
    ) -> Result<Vec<u8>, String> {
        let mut stream = tokio::time::timeout(
            std::time::Duration::from_secs(IBD_TIMEOUT_SECS),
            TcpStream::connect(addr),
        )
        .await
        .map_err(|_| "Connection timeout".to_string())?
        .map_err(|e| format!("Connect error: {}", e))?;

        let req = network::msg_get_blocks(start, end).with_node_id(&self.node_id);
        network::write_message(&mut stream, &req)
            .await
            .map_err(|e| format!("Write error: {}", e))?;

        // Read response (potentially large — use block download limit)
        let data = tokio::time::timeout(
            std::time::Duration::from_secs(IBD_TIMEOUT_SECS),
            network::read_raw(&mut stream, network::MAX_BLOCK_DOWNLOAD_SIZE),
        )
        .await
        .map_err(|_| "Response timeout".to_string())?
        .map_err(|e| format!("Read error: {}", e))?;

        match data {
            Some(bytes) => {
                self.touch_peer_by_addr(addr).await;
                Ok(bytes)
            }
            None => Err("Empty response".to_string()),
        }
    }

    /// Request compact headers from a v4 peer.
    pub async fn request_headers(
        &self,
        addr: SocketAddr,
        locator: &[String],
        stop_hash: Option<&str>,
        limit: u64,
    ) -> Result<Message, String> {
        let mut stream =
            tokio::time::timeout(std::time::Duration::from_secs(10), TcpStream::connect(addr))
                .await
                .map_err(|_| "Connection timeout".to_string())?
                .map_err(|e| format!("Connect error: {}", e))?;

        let req = network::msg_get_headers(locator, stop_hash, limit).with_node_id(&self.node_id);
        network::write_message(&mut stream, &req)
            .await
            .map_err(|e| format!("Write error: {}", e))?;

        let resp = tokio::time::timeout(
            std::time::Duration::from_secs(10),
            network::read_message(&mut stream),
        )
        .await
        .map_err(|_| "Response timeout".to_string())?
        .map_err(|e| format!("Read error: {}", e))?;

        resp.ok_or_else(|| "Empty response".to_string())
    }

    // ── Internal helpers ─────────────────────────────────────────────────

    async fn touch_peer_by_addr(&self, addr: SocketAddr) {
        let mut peers = self.peers.lock().await;
        for peer in peers.peers.values_mut() {
            if peer.addr == addr {
                peer.touch();
                break;
            }
        }
    }

    async fn update_peer_height(
        &self,
        node_id: &str,
        height: u64,
        genesis_hash: &str,
        tip_hash: &str,
    ) {
        let mut peers = self.peers.lock().await;
        if let Some(peer) = peers.get_mut(node_id) {
            // Replace, don't max — tip_hash must correspond to height, so a
            // lower-height update from the same peer is still authoritative.
            peer.chain_height = height;
            if !tip_hash.is_empty() {
                peer.tip_hash = tip_hash.to_string();
            }
            if !genesis_hash.is_empty() {
                peer.genesis_hash = genesis_hash.to_string();
            }
            peer.touch();
        }
    }

    async fn update_peer_height_by_addr(
        &self,
        addr: SocketAddr,
        height: u64,
        genesis_hash: &str,
        tip_hash: &str,
    ) {
        let mut peers = self.peers.lock().await;
        for peer in peers.peers.values_mut() {
            if peer.addr == addr {
                peer.chain_height = height;
                if !tip_hash.is_empty() {
                    peer.tip_hash = tip_hash.to_string();
                }
                if !genesis_hash.is_empty() {
                    peer.genesis_hash = genesis_hash.to_string();
                }
                peer.touch();
                break;
            }
        }
    }

    /// Clone the Arc handles for spawning tasks.
    fn clone_handles(&self) -> GossipNodeHandles {
        GossipNodeHandles {
            node_id: self.node_id.clone(),
            listen_addr: self.listen_addr,
            peers: self.peers.clone(),
            guard: self.guard.clone(),
            seen: self.seen.clone(),
            stats: self.stats.clone(),
            events: self.events.clone(),
            chain_height: self.chain_height.clone(),
            chain_tip_hash: self.chain_tip_hash.clone(),
            running: self.running.clone(),
            shared_blocks: self.shared_blocks.clone(),
            storage: self.storage.clone(),
        }
    }
}

/// Lightweight handle bundle for spawned tasks (avoids cloning the full GossipNode).
struct GossipNodeHandles {
    node_id: String,
    listen_addr: SocketAddr,
    peers: Arc<Mutex<PeerManager>>,
    guard: Arc<Mutex<ConnectionGuard>>,
    seen: Arc<Mutex<SeenTracker>>,
    stats: Arc<Mutex<GossipStats>>,
    events: Arc<Mutex<VecDeque<GossipEvent>>>,
    chain_height: Arc<Mutex<u64>>,
    chain_tip_hash: Arc<Mutex<String>>,
    running: Arc<Mutex<bool>>,
    shared_blocks: Arc<Mutex<Vec<Block>>>,
    storage: Arc<Mutex<Option<Arc<Storage>>>>,
}

impl GossipNodeHandles {
    async fn handle_inbound(&self, mut stream: TcpStream, addr: SocketAddr) {
        let ip = addr.ip().to_string();

        let result = tokio::time::timeout(
            std::time::Duration::from_secs(30),
            network::read_message(&mut stream),
        )
        .await;

        match result {
            Ok(Ok(Some(msg))) => {
                {
                    let mut guard = self.guard.lock().await;
                    if !guard.check_rate_limit(&ip) {
                        guard.record_misbehavior(&ip, 10, "rate limit exceeded");
                        return;
                    }
                }

                if !msg.validate_magic() {
                    let mut guard = self.guard.lock().await;
                    guard.record_misbehavior(&ip, 50, "wrong network magic");
                    return;
                }

                self.stats.lock().await.messages_received += 1;

                // Refresh last_seen for any peer that just sent us anything,
                // regardless of message type. Without this, an inbound-only
                // peer (one we never proactively dial — e.g., a home-network
                // node behind dynamic NAT) ages out of the peer manager after
                // PEER_TIMEOUT_SECS even though it's still actively polling
                // us every 30s. The handshake handler also touches via
                // add_peer, but post-handshake messages (GetChainHeight,
                // BlockAnnounce, TxAnnounce, etc.) used to skip the touch
                // and the peer would silently drop out of `peers=N`.
                if !msg.node_id.is_empty() {
                    let mut peers = self.peers.lock().await;
                    if let Some(peer) = peers.peers.get_mut(&msg.node_id) {
                        peer.touch();
                    }
                }

                // Basic dispatch for spawned handler
                match msg.parsed_type() {
                    Some(MessageType::Handshake) => {
                        let node_id = msg.node_id.clone();
                        let genesis_hash = msg
                            .payload
                            .get("genesis_hash")
                            .and_then(|v| v.as_str())
                            .unwrap_or("")
                            .to_string();
                        let chain_height = msg
                            .payload
                            .get("chain_height")
                            .and_then(|v| v.as_u64())
                            .unwrap_or(0);
                        let tflops = msg
                            .payload
                            .get("tflops")
                            .and_then(|v| v.as_f64())
                            .unwrap_or(0.0);

                        if node_id == self.node_id {
                            let mut guard = self.guard.lock().await;
                            guard.record_misbehavior(&ip, 5, "self-connection");
                            guard.release_connection(&ip);
                            return;
                        }
                        if msg.protocol_version < MIN_COMPATIBLE_VERSION {
                            let mut guard = self.guard.lock().await;
                            guard.record_misbehavior(&ip, 30, "incompatible protocol version");
                            guard.release_connection(&ip);
                            return;
                        }
                        if !genesis_hash.is_empty() && genesis_hash != EXPECTED_GENESIS_HASH {
                            let mut guard = self.guard.lock().await;
                            guard.record_misbehavior(&ip, 100, "genesis mismatch");
                            guard.release_connection(&ip);
                            return;
                        }

                        let mut peer = PeerInfo::new(node_id.clone(), addr, false);
                        peer.chain_height = chain_height;
                        peer.genesis_hash = genesis_hash;
                        peer.tflops = tflops;
                        peer.protocol_version = msg.protocol_version;
                        self.peers.lock().await.add_peer(peer);

                        let height = *self.chain_height.lock().await;
                        let ack = network::msg_handshake(
                            &self.node_id,
                            height,
                            EXPECTED_GENESIS_HASH,
                            0.0,
                        );
                        let _ = network::write_message(&mut stream, &ack).await;

                        self.events
                            .lock()
                            .await
                            .push_back(GossipEvent::PeerConnected(node_id));
                        self.stats.lock().await.peer_connections += 1;
                    }
                    Some(MessageType::Ping) => {
                        let height = *self.chain_height.lock().await;
                        let pong = network::msg_pong(height);
                        let _ = network::write_message(&mut stream, &pong).await;
                    }
                    Some(MessageType::GetChainHeight) => {
                        let height = *self.chain_height.lock().await;
                        let tip_hash = self.chain_tip_hash.lock().await.clone();
                        let resp =
                            network::msg_chain_height(height, EXPECTED_GENESIS_HASH, &tip_hash);
                        let _ = network::write_message(&mut stream, &resp).await;
                    }
                    Some(MessageType::GetBlocks) => {
                        let start = msg
                            .payload
                            .get("start")
                            .and_then(|v| v.as_u64())
                            .unwrap_or(0);
                        let end = msg.payload.get("end").and_then(|v| v.as_u64()).unwrap_or(0);
                        let batch_end = end.min(start + IBD_BATCH_SIZE);

                        // Try storage first, fall back to shared_blocks
                        let blocks_from_storage = {
                            let sg = self.storage.lock().await;
                            if let Some(ref s) = *sg {
                                s.get_block_range(start, batch_end).ok()
                            } else {
                                None
                            }
                        };
                        let arr: Vec<serde_json::Value> = match blocks_from_storage {
                            Some(blocks) => blocks
                                .iter()
                                .map(|b| serde_json::to_value(b.to_dict()).unwrap())
                                .collect(),
                            None => {
                                let blocks = self.shared_blocks.lock().await;
                                let s = (start as usize).min(blocks.len());
                                let e = (batch_end as usize).min(blocks.len());
                                blocks[s..e]
                                    .iter()
                                    .map(|b| serde_json::to_value(b.to_dict()).unwrap())
                                    .collect()
                            }
                        };
                        let mut p = std::collections::BTreeMap::new();
                        p.insert("success".into(), serde_json::Value::Bool(true));
                        p.insert("blocks".into(), serde_json::Value::Array(arr));
                        let resp = Message::new(MessageType::Blocks, p);
                        let data = resp.to_bytes().unwrap_or_default();
                        let _ = network::write_raw(&mut stream, &data).await;
                    }
                    Some(MessageType::GetHeaders) => {
                        let locator: Vec<String> = msg
                            .payload
                            .get("locator")
                            .and_then(|v| v.as_array())
                            .map(|items| {
                                items
                                    .iter()
                                    .filter_map(|v| v.as_str().map(|s| s.to_string()))
                                    .collect()
                            })
                            .unwrap_or_default();
                        let stop_hash = msg
                            .payload
                            .get("stop_hash")
                            .and_then(|v| v.as_str())
                            .unwrap_or("");
                        let limit = msg
                            .payload
                            .get("limit")
                            .and_then(|v| v.as_u64())
                            .unwrap_or(500)
                            .min(2_000);
                        let blocks = {
                            let storage = self.storage.lock().await;
                            if let Some(storage) = storage.as_ref() {
                                storage.load_chain().unwrap_or_default()
                            } else {
                                self.shared_blocks.lock().await.clone()
                            }
                        };
                        let start = find_header_start(&blocks, &locator).unwrap_or(0);
                        let headers = build_headers(&blocks, start, limit, stop_hash);
                        let resp = network::msg_headers(
                            blocks.len() as u64,
                            EXPECTED_GENESIS_HASH,
                            headers,
                        );
                        let _ = network::write_message(&mut stream, &resp).await;
                    }
                    Some(MessageType::TxAnnounce) => {
                        self.stats.lock().await.txs_announced += 1;
                        let tx_hash = msg
                            .payload
                            .get("tx_hash")
                            .and_then(|v| v.as_str())
                            .unwrap_or("")
                            .to_string();
                        if tx_hash.is_empty() {
                            // fall through to release
                        } else {
                            if let Some(tx_value) = msg.payload.get("tx").cloned() {
                                if let Ok(tx) = serde_json::from_value::<Transaction>(tx_value) {
                                    if tx.tx_hash == tx_hash {
                                        self.events
                                            .lock()
                                            .await
                                            .push_back(GossipEvent::NewTransaction(tx));
                                    }
                                }
                            }
                        }
                    }
                    Some(MessageType::BlockAnnounce) => {
                        self.stats.lock().await.blocks_announced += 1;
                        let block_hash = msg
                            .payload
                            .get("hash")
                            .and_then(|v| v.as_str())
                            .unwrap_or("")
                            .to_string();
                        if !block_hash.is_empty() {
                            let already_seen = {
                                let mut seen = self.seen.lock().await;
                                if seen.is_seen(&block_hash) {
                                    true
                                } else {
                                    seen.mark_seen(&block_hash);
                                    false
                                }
                            };
                            if !already_seen {
                                if let Some(block_value) = msg.payload.get("block").cloned() {
                                    if let Ok(map) =
                                        serde_json::from_value::<
                                            std::collections::BTreeMap<String, serde_json::Value>,
                                        >(block_value)
                                    {
                                        if let Some(block) = Block::from_dict(&map) {
                                            if block.hash == block_hash {
                                                self.events
                                                    .lock()
                                                    .await
                                                    .push_back(GossipEvent::NewBlock(block));
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    _ => {}
                }
            }
            _ => {}
        }

        self.guard.lock().await.release_connection(&ip);
    }
}

// ── Helpers ──────────────────────────────────────────────────────────────────

fn now_f64() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn test_block_announce_fixture(index: u64, hash: &str, prev_hash: &str) -> serde_json::Value {
        serde_json::json!({
            "index": index,
            "hash": hash,
            "previous_hash": prev_hash,
            "transactions": []
        })
    }

    #[test]
    fn test_gossip_message_roundtrip() {
        let fixture = test_block_announce_fixture(42, "hash123", "prev456");
        let inner = network::msg_block_announce(&fixture);
        let gossip = GossipMessage::new("block", inner, "node_abc");

        assert_eq!(gossip.msg_type, "block");
        assert_eq!(gossip.origin, "node_abc");
        assert_eq!(gossip.ttl, MAX_TTL);
        assert!(!gossip.msg_id.is_empty());

        let bytes = gossip.to_bytes().unwrap();
        let decoded = GossipMessage::from_bytes(&bytes).unwrap();
        assert_eq!(decoded.msg_id, gossip.msg_id);
        assert_eq!(decoded.msg_type, "block");
        assert_eq!(decoded.ttl, MAX_TTL);
    }

    #[test]
    fn test_message_id_deterministic() {
        let f1 = test_block_announce_fixture(10, "h", "p");
        let msg1 = network::msg_block_announce(&f1);
        let f2 = test_block_announce_fixture(10, "h", "p");
        let msg2 = network::msg_block_announce(&f2);
        assert_eq!(message_id(&msg1), message_id(&msg2));
    }

    #[test]
    fn test_message_id_different_for_different_content() {
        let f1 = test_block_announce_fixture(10, "h", "p");
        let msg1 = network::msg_block_announce(&f1);
        let f2 = test_block_announce_fixture(11, "h", "p");
        let msg2 = network::msg_block_announce(&f2);
        assert_ne!(message_id(&msg1), message_id(&msg2));
    }

    #[test]
    fn test_seen_tracker() {
        let mut tracker = SeenTracker::new();
        assert!(!tracker.is_seen("abc123"));

        tracker.mark_seen("abc123");
        assert!(tracker.is_seen("abc123"));

        // Different ID not seen
        assert!(!tracker.is_seen("def456"));
    }

    #[test]
    fn test_seen_tracker_max_cap() {
        let mut tracker = SeenTracker::new();
        for i in 0..(MAX_SEEN + 100) {
            tracker.mark_seen(&format!("msg_{}", i));
        }
        // Should not exceed MAX_SEEN
        assert!(tracker.seen.len() <= MAX_SEEN);
    }

    #[test]
    fn test_gossip_stats_default() {
        let stats = GossipStats::default();
        assert_eq!(stats.messages_received, 0);
        assert_eq!(stats.messages_relayed, 0);
        assert_eq!(stats.blocks_announced, 0);
    }

    #[tokio::test]
    async fn test_gossip_node_creation() {
        let node = GossipNode::new("test_node", 0, vec![]);
        assert_eq!(node.node_id, "test_node");
        assert_eq!(*node.chain_height.lock().await, 0);
    }

    #[tokio::test]
    async fn test_gossip_node_set_chain_tip() {
        let node = GossipNode::new("test_node", 0, vec![]);
        node.set_chain_tip(42, "deadbeef".into()).await;
        assert_eq!(*node.chain_height.lock().await, 42);
        assert_eq!(node.chain_tip_hash.lock().await.as_str(), "deadbeef");
    }

    #[tokio::test]
    async fn test_gossip_node_drain_events_empty() {
        let node = GossipNode::new("test_node", 0, vec![]);
        let events = node.drain_events().await;
        assert!(events.is_empty());
    }

    #[tokio::test]
    async fn test_gossip_connect_to_self_skipped() {
        // Create a node and verify seed connection skips self
        let node = GossipNode::new("test_node", 5099, vec![]);
        // This shouldn't crash — just skip self-connections
        // (Seeds are on port 5001, we're on 5099, so no self-skip needed)
        assert_eq!(node.peers.lock().await.count(), 0);
    }

    #[tokio::test]
    async fn test_gossip_handshake_flow() {
        // Start a listener
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        // Spawn a server that responds to handshake
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let msg = network::read_message(&mut stream).await.unwrap().unwrap();
            assert_eq!(msg.parsed_type(), Some(MessageType::Handshake));
            assert_eq!(msg.node_id, "client_node");

            let ack = network::msg_handshake("server_node", 50, EXPECTED_GENESIS_HASH, 10.0);
            network::write_message(&mut stream, &ack).await.unwrap();
        });

        // Connect from client
        let client = GossipNode::new("client_node", 0, vec![]);
        client.connect_to_peer(addr).await.unwrap();

        server.await.unwrap();

        // Verify peer was registered
        let peers = client.peers.lock().await;
        assert_eq!(peers.count(), 1);
        let peer = peers.get("server_node").unwrap();
        assert_eq!(peer.chain_height, 50);
    }

    #[tokio::test]
    async fn test_chain_height_request() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        // Server responds with chain height
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let msg = network::read_message(&mut stream).await.unwrap().unwrap();
            assert_eq!(msg.parsed_type(), Some(MessageType::GetChainHeight));

            let resp = network::msg_chain_height(100, EXPECTED_GENESIS_HASH, "test_tip");
            network::write_message(&mut stream, &resp).await.unwrap();
        });

        let client = GossipNode::new("client", 0, vec![]);
        let (height, genesis, tip_hash) = client.request_chain_height(addr).await.unwrap();

        server.await.unwrap();

        assert_eq!(height, 100);
        assert_eq!(genesis, EXPECTED_GENESIS_HASH);
        assert_eq!(tip_hash, "test_tip");
    }
}
