//! Peer management — tracking, scoring, banning, connection guard.
//!
//! Matches the Python `ConnectionGuard` (qnode2.py) and `PeerInfo`
//! (comms/p2p.py) implementations.  Session 6 adds per-message-type
//! rate limiting, explicit bans, safe address validation, backoff tracking,
//! protocol version gating, and connection statistics.

use std::collections::{BTreeMap, HashMap};
use std::net::{IpAddr, SocketAddr};
use std::time::{SystemTime, UNIX_EPOCH};

// ── Connection Limits (Bitcoin-style, matching Python) ───────────────────────

/// Maximum inbound connections.
pub const MAX_INBOUND_CONNECTIONS: usize = 125;

/// No single IP needs more than 3 connections.
pub const MAX_CONNECTIONS_PER_IP: usize = 3;

/// Ban after this many misbehavior points.
pub const BAN_SCORE_THRESHOLD: u32 = 100;

/// 24-hour ban.
pub const BAN_DURATION_SECS: f64 = 86400.0;

/// Drop idle connections after 30 seconds.
pub const SOCKET_TIMEOUT_SECS: u64 = 30;

/// Max messages per IP per 60-second window.
pub const RATE_LIMIT_PER_IP: u32 = 100;

/// Seconds between heartbeat pings.
pub const HEARTBEAT_INTERVAL_SECS: u64 = 30;

/// Peer considered dead after this many seconds of silence.
pub const PEER_TIMEOUT_SECS: f64 = 120.0;

/// Maximum peers to maintain.
pub const MAX_PEERS: usize = 32;

/// Minimum peers — trigger reconnection below this.
pub const MIN_PEERS: usize = 4;

/// IBD: blocks per request batch.
pub const IBD_BATCH_SIZE: u64 = 500;

/// IBD: timeout per batch request.
pub const IBD_TIMEOUT_SECS: u64 = 60;

/// Minimum compatible protocol version — reject peers below this.
pub const MIN_COMPATIBLE_VERSION: u32 = 3;

/// Maximum addresses accepted from a single peer exchange message.
pub const MAX_PEER_LIST_SIZE: usize = 20;

// ── Per-message-type rate limits (messages per 60-second window) ─────────────

/// Handshake: very low limit — peers shouldn't spam handshakes.
pub const RATE_HANDSHAKE: u32 = 5;
/// Ping/Pong: moderate — heartbeats are normal.
pub const RATE_PING: u32 = 30;
/// Block announces: moderate.
pub const RATE_BLOCK_ANNOUNCE: u32 = 30;
/// Tx announces: higher — burst of txs is normal.
pub const RATE_TX_ANNOUNCE: u32 = 60;
/// GetBlocks (IBD): low — IBD is batched.
pub const RATE_GET_BLOCKS: u32 = 10;
/// GetChainHeight: low.
pub const RATE_GET_CHAIN_HEIGHT: u32 = 10;
/// Gossip relay: moderate.
pub const RATE_GOSSIP: u32 = 50;
/// Everything else: moderate default.
pub const RATE_DEFAULT: u32 = 30;

// ── Peer Info ────────────────────────────────────────────────────────────────

/// Information about a connected peer.
#[derive(Debug, Clone)]
pub struct PeerInfo {
    /// Unique node identifier (hex string, derived from public key).
    pub node_id: String,
    /// Display name.
    pub node_name: String,
    /// Network address.
    pub addr: SocketAddr,
    /// When the connection was established.
    pub connected_at: f64,
    /// Last time we received any message from this peer.
    pub last_seen: f64,
    /// Last heartbeat timestamp.
    pub last_heartbeat: f64,
    /// Peer's reported chain height.
    pub chain_height: u64,
    /// Peer's reported tip block hash at `chain_height`.
    /// Empty if peer is on an older protocol that doesn't announce tip hash.
    pub tip_hash: String,
    /// Peer's reported genesis hash (must match ours).
    pub genesis_hash: String,
    /// Peer's compute power.
    pub tflops: f64,
    /// Reputation score: 0.0 (untrusted) to 1.0 (fully trusted).
    pub reputation: f64,
    /// Measured round-trip latency in milliseconds.
    pub latency_ms: f64,
    /// Protocol version reported in handshake.
    pub protocol_version: u32,
    /// Whether this is an outbound connection (we initiated).
    pub outbound: bool,
    /// Number of messages relayed through this peer.
    pub messages_relayed: u64,
    /// Misbehavior score (accumulated).
    pub misbehavior_score: u32,
}

impl PeerInfo {
    pub fn new(node_id: String, addr: SocketAddr, outbound: bool) -> Self {
        let now = now_f64();
        Self {
            node_id,
            node_name: String::new(),
            addr,
            connected_at: now,
            last_seen: now,
            last_heartbeat: now,
            chain_height: 0,
            tip_hash: String::new(),
            genesis_hash: String::new(),
            tflops: 0.0,
            reputation: 0.5,
            latency_ms: 0.0,
            protocol_version: 0,
            outbound,
            messages_relayed: 0,
            misbehavior_score: 0,
        }
    }

    /// Peer is alive if last_seen < PEER_TIMEOUT_SECS ago.
    pub fn is_alive(&self) -> bool {
        now_f64() - self.last_seen < PEER_TIMEOUT_SECS
    }

    /// Touch the last_seen timestamp.
    pub fn touch(&mut self) {
        self.last_seen = now_f64();
    }

    /// Apply reputation adjustment.
    pub fn adjust_reputation(&mut self, delta: f64) {
        self.reputation = (self.reputation + delta).clamp(0.0, 1.0);
    }
}

// ── Peer Manager ─────────────────────────────────────────────────────────────

/// Manages the set of connected peers.
pub struct PeerManager {
    /// Active peers indexed by node_id.
    pub peers: BTreeMap<String, PeerInfo>,
}

impl PeerManager {
    pub fn new() -> Self {
        Self {
            peers: BTreeMap::new(),
        }
    }

    /// Register a new peer. Returns false if at capacity.
    pub fn add_peer(&mut self, peer: PeerInfo) -> bool {
        if self.peers.len() >= MAX_PEERS && !self.peers.contains_key(&peer.node_id) {
            return false;
        }
        if let Some(existing) = self.peers.get_mut(&peer.node_id) {
            // Prefer the stable outbound seed address over an inbound ephemeral
            // source port from the same peer. Without this, two seed nodes can
            // overwrite each other with short-lived inbound addresses and keep
            // redialing the real host:port forever.
            if peer.outbound || !existing.outbound {
                existing.addr = peer.addr;
                existing.outbound = peer.outbound;
            }
            existing.chain_height = peer.chain_height;
            if !peer.tip_hash.is_empty() {
                existing.tip_hash = peer.tip_hash;
            }
            existing.genesis_hash = peer.genesis_hash;
            existing.tflops = peer.tflops;
            existing.protocol_version = peer.protocol_version;
            existing.touch();
        } else {
            self.peers.insert(peer.node_id.clone(), peer);
        }
        true
    }

    /// Remove a peer by node_id.
    pub fn remove_peer(&mut self, node_id: &str) -> Option<PeerInfo> {
        self.peers.remove(node_id)
    }

    /// Get a peer reference by node_id.
    pub fn get(&self, node_id: &str) -> Option<&PeerInfo> {
        self.peers.get(node_id)
    }

    /// Get a mutable peer reference.
    pub fn get_mut(&mut self, node_id: &str) -> Option<&mut PeerInfo> {
        self.peers.get_mut(node_id)
    }

    /// Number of connected peers.
    pub fn count(&self) -> usize {
        self.peers.len()
    }

    /// Whether we need more peers.
    pub fn needs_peers(&self) -> bool {
        self.peers.len() < MIN_PEERS
    }

    /// Remove dead peers (no heartbeat within timeout).
    /// Returns the node_ids of removed peers.
    pub fn prune_dead(&mut self) -> Vec<String> {
        let dead: Vec<String> = self
            .peers
            .iter()
            .filter(|(_, p)| !p.is_alive())
            .map(|(id, _)| id.clone())
            .collect();

        for id in &dead {
            self.peers.remove(id);
        }

        dead
    }

    /// Get all peer addresses for peer exchange.
    pub fn peer_addresses(&self) -> Vec<(String, u16)> {
        self.peers
            .values()
            .filter(|p| p.is_alive())
            .map(|p| (p.addr.ip().to_string(), p.addr.port()))
            .collect()
    }

    /// Get peers sorted by reputation (best first), useful for choosing
    /// sync partners.
    pub fn by_reputation(&self) -> Vec<&PeerInfo> {
        let mut peers: Vec<&PeerInfo> = self.peers.values().filter(|p| p.is_alive()).collect();
        peers.sort_by(|a, b| {
            b.reputation
                .partial_cmp(&a.reputation)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        peers
    }

    /// Get the peer with the highest chain height (best sync source).
    pub fn best_height_peer(&self) -> Option<&PeerInfo> {
        self.peers
            .values()
            .filter(|p| p.is_alive())
            .max_by_key(|p| p.chain_height)
    }
}

impl Default for PeerManager {
    fn default() -> Self {
        Self::new()
    }
}

// ── Connection Guard ─────────────────────────────────────────────────────────

/// Per-IP connection tracking, rate limiting, and ban management.
///
/// Matches Python's `ConnectionGuard` from qnode2.py.
pub struct ConnectionGuard {
    /// Active connections per IP.
    active: HashMap<String, usize>,
    /// Ban list: IP → (ban_until_timestamp, reason).
    bans: HashMap<String, (f64, String)>,
    /// Misbehavior scores per IP.
    misbehavior: HashMap<String, u32>,
    /// Rate tracking: IP → { second → count }.
    msg_rates: HashMap<String, HashMap<u64, u32>>,
    /// Total inbound connections.
    total_inbound: usize,
}

impl ConnectionGuard {
    pub fn new() -> Self {
        Self {
            active: HashMap::new(),
            bans: HashMap::new(),
            misbehavior: HashMap::new(),
            msg_rates: HashMap::new(),
            total_inbound: 0,
        }
    }

    /// Check if an inbound connection from `ip` should be accepted.
    /// Returns `Ok(())` or `Err(reason)`.
    pub fn allow_connection(&mut self, ip: &str) -> Result<(), String> {
        // Localhost always allowed
        if is_localhost(ip) {
            self.active
                .entry(ip.to_string())
                .and_modify(|c| *c += 1)
                .or_insert(1);
            self.total_inbound += 1;
            return Ok(());
        }

        // Check ban list
        if let Some((until, reason)) = self.bans.get(ip) {
            if now_f64() < *until {
                return Err(format!("banned: {}", reason));
            }
            // Ban expired — remove it
            self.bans.remove(&ip.to_string());
        }

        // Global limit
        if self.total_inbound >= MAX_INBOUND_CONNECTIONS {
            return Err("max inbound connections reached".into());
        }

        // Per-IP limit
        let count = self.active.get(ip).copied().unwrap_or(0);
        if count >= MAX_CONNECTIONS_PER_IP {
            return Err(format!("per-IP limit ({})", MAX_CONNECTIONS_PER_IP));
        }

        self.active
            .entry(ip.to_string())
            .and_modify(|c| *c += 1)
            .or_insert(1);
        self.total_inbound += 1;
        Ok(())
    }

    /// Release a connection slot when a connection closes.
    pub fn release_connection(&mut self, ip: &str) {
        if let Some(count) = self.active.get_mut(ip) {
            if *count <= 1 {
                self.active.remove(ip);
            } else {
                *count -= 1;
            }
        }
        self.total_inbound = self.total_inbound.saturating_sub(1);
    }

    /// Check rate limit. Returns `true` if the request is allowed.
    pub fn check_rate_limit(&mut self, ip: &str) -> bool {
        if is_localhost(ip) {
            return true;
        }

        let now_sec = now_secs();
        let bucket = self.msg_rates.entry(ip.to_string()).or_default();

        *bucket.entry(now_sec).or_insert(0) += 1;

        // Sum messages in the last 60 seconds
        let cutoff = now_sec.saturating_sub(60);
        bucket.retain(|&sec, _| sec > cutoff);
        let total: u32 = bucket.values().sum();

        total <= RATE_LIMIT_PER_IP
    }

    /// Record misbehavior. Auto-bans if score exceeds threshold.
    pub fn record_misbehavior(&mut self, ip: &str, points: u32, reason: &str) {
        if is_localhost(ip) {
            return;
        }

        let score = self.misbehavior.entry(ip.to_string()).or_insert(0);
        *score += points;

        if *score >= BAN_SCORE_THRESHOLD {
            let ban_reason = if reason.is_empty() {
                format!("misbehavior score={}", score)
            } else {
                reason.to_string()
            };
            self.bans
                .insert(ip.to_string(), (now_f64() + BAN_DURATION_SECS, ban_reason));
        }
    }

    /// Check if an IP is currently banned.
    pub fn is_banned(&self, ip: &str) -> bool {
        match self.bans.get(ip) {
            Some((until, _)) => now_f64() < *until,
            None => false,
        }
    }

    /// Total active inbound connections.
    pub fn total_inbound(&self) -> usize {
        self.total_inbound
    }

    /// Cleanup: remove expired bans and stale rate data.
    pub fn cleanup(&mut self) {
        let now = now_f64();
        self.bans.retain(|_, (until, _)| now < *until);

        let cutoff = now_secs().saturating_sub(120);
        self.msg_rates.retain(|_, bucket| {
            bucket.retain(|&sec, _| sec > cutoff);
            !bucket.is_empty()
        });
    }

    /// Explicitly ban an IP for the given duration (in seconds).
    pub fn ban(&mut self, ip: &str, duration_secs: f64, reason: &str) {
        if is_localhost(ip) {
            return;
        }
        self.bans.insert(
            ip.to_string(),
            (now_f64() + duration_secs, reason.to_string()),
        );
    }

    /// Get the current misbehavior score for an IP (0 if not tracked).
    pub fn misbehavior_score(&self, ip: &str) -> u32 {
        self.misbehavior.get(ip).copied().unwrap_or(0)
    }

    /// Snapshot of current connection guard state.
    pub fn get_stats(&self) -> ConnectionStats {
        ConnectionStats {
            total_inbound: self.total_inbound,
            unique_ips: self.active.len(),
            banned_ips: self.bans.len(),
            tracked_rate_ips: self.msg_rates.len(),
        }
    }
}

impl Default for ConnectionGuard {
    fn default() -> Self {
        Self::new()
    }
}

// ── Connection Statistics ────────────────────────────────────────────────────

/// Snapshot of connection guard state.
#[derive(Debug, Clone)]
pub struct ConnectionStats {
    pub total_inbound: usize,
    pub unique_ips: usize,
    pub banned_ips: usize,
    pub tracked_rate_ips: usize,
}

// ── Per-Message-Type Rate Limiter ────────────────────────────────────────────

/// Tracks per-IP, per-message-type rates within a 60-second window.
pub struct MessageRateLimiter {
    /// ip → (msg_type_key → { second → count })
    buckets: HashMap<String, HashMap<String, HashMap<u64, u32>>>,
}

impl MessageRateLimiter {
    pub fn new() -> Self {
        Self {
            buckets: HashMap::new(),
        }
    }

    /// Check if a message of the given type from `ip` is allowed.
    /// Returns `true` if within the per-type rate limit.
    pub fn check(&mut self, ip: &str, msg_type: &str) -> bool {
        if is_localhost(ip) {
            return true;
        }

        let limit = match msg_type {
            "handshake" | "handshake_ack" => RATE_HANDSHAKE,
            "ping" | "pong" => RATE_PING,
            "block_announce" => RATE_BLOCK_ANNOUNCE,
            "tx_announce" => RATE_TX_ANNOUNCE,
            "get_blocks" | "blocks" => RATE_GET_BLOCKS,
            "get_chain_height" | "chain_height" => RATE_GET_CHAIN_HEIGHT,
            "gossip" => RATE_GOSSIP,
            _ => RATE_DEFAULT,
        };

        let now_sec = now_secs();
        let cutoff = now_sec.saturating_sub(60);

        let type_map = self.buckets.entry(ip.to_string()).or_default();
        let bucket = type_map.entry(msg_type.to_string()).or_default();

        *bucket.entry(now_sec).or_insert(0) += 1;
        bucket.retain(|&sec, _| sec > cutoff);

        let total: u32 = bucket.values().sum();
        total <= limit
    }

    /// Remove stale entries older than 2 minutes.
    pub fn cleanup(&mut self) {
        let cutoff = now_secs().saturating_sub(120);
        self.buckets.retain(|_, type_map| {
            type_map.retain(|_, bucket| {
                bucket.retain(|&sec, _| sec > cutoff);
                !bucket.is_empty()
            });
            !type_map.is_empty()
        });
    }
}

impl Default for MessageRateLimiter {
    fn default() -> Self {
        Self::new()
    }
}

// ── Backoff Tracker (reconnection with exponential backoff) ──────────────────

/// Tracks reconnection attempts with exponential backoff per peer address.
pub struct BackoffTracker {
    /// addr_string → (attempt_count, next_allowed_timestamp)
    entries: HashMap<String, (u32, f64)>,
    /// Maximum backoff in seconds (cap at ~17 minutes).
    pub max_backoff_secs: f64,
}

impl BackoffTracker {
    pub fn new() -> Self {
        Self {
            entries: HashMap::new(),
            max_backoff_secs: 1024.0,
        }
    }

    /// Check if a reconnection to `addr` is currently allowed.
    pub fn can_reconnect(&self, addr: &str) -> bool {
        match self.entries.get(addr) {
            Some(&(_, next_allowed)) => now_f64() >= next_allowed,
            None => true,
        }
    }

    /// Record a failed connection attempt — increases backoff.
    pub fn record_failure(&mut self, addr: &str) {
        let (attempts, _) = self.entries.entry(addr.to_string()).or_insert((0, 0.0));
        *attempts += 1;
        let delay = (2.0_f64.powi(*attempts as i32)).min(self.max_backoff_secs);
        self.entries.get_mut(addr).unwrap().1 = now_f64() + delay;
    }

    /// Record a successful connection — reset backoff.
    pub fn record_success(&mut self, addr: &str) {
        self.entries.remove(addr);
    }

    /// Get the current attempt count for an address.
    pub fn attempts(&self, addr: &str) -> u32 {
        self.entries.get(addr).map(|&(a, _)| a).unwrap_or(0)
    }

    /// Remove entries that have been backed off for over an hour (stale).
    pub fn cleanup(&mut self) {
        let cutoff = now_f64() - 3600.0;
        self.entries.retain(|_, &mut (_, next)| next > cutoff);
    }
}

impl Default for BackoffTracker {
    fn default() -> Self {
        Self::new()
    }
}

// ── Safe Peer Address Validation ─────────────────────────────────────────────

/// Check whether a peer address is safe to connect to.
///
/// Rejects loopback, link-local, cloud metadata, and unspecified addresses.
/// Matches Python's `_is_safe_peer_address()` from comms/p2p.py.
pub fn is_safe_peer_address(host: &str) -> bool {
    // Try parsing as IP address directly
    if let Ok(ip) = host.parse::<IpAddr>() {
        return is_safe_ip(&ip);
    }

    // Reject known dangerous hostnames
    let lower = host.to_lowercase();
    if lower == "localhost" || lower.ends_with(".local") {
        return false;
    }

    // AWS / cloud metadata endpoint
    if lower == "169.254.169.254" || lower == "metadata.google.internal" {
        return false;
    }

    true
}

/// Check an `IpAddr` for safety.
fn is_safe_ip(ip: &IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => {
            // Reject loopback 127.0.0.0/8
            if v4.is_loopback() {
                return false;
            }
            // Reject unspecified 0.0.0.0
            if v4.is_unspecified() {
                return false;
            }
            // Reject link-local 169.254.0.0/16
            if v4.is_link_local() {
                return false;
            }
            // Reject broadcast 255.255.255.255
            if v4.is_broadcast() {
                return false;
            }
            true
        }
        IpAddr::V6(v6) => {
            // Reject loopback ::1
            if v6.is_loopback() {
                return false;
            }
            // Reject unspecified ::
            if v6.is_unspecified() {
                return false;
            }
            true
        }
    }
}

// ── Helpers ──────────────────────────────────────────────────────────────────

fn is_localhost(ip: &str) -> bool {
    matches!(ip, "127.0.0.1" | "::1" | "localhost")
}

fn now_f64() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn addr(ip: &str, port: u16) -> SocketAddr {
        format!("{}:{}", ip, port).parse().unwrap()
    }

    #[test]
    fn test_peer_info_alive() {
        let mut peer = PeerInfo::new("node1".into(), addr("10.0.0.1", 5001), false);
        assert!(peer.is_alive());

        // Simulate timeout
        peer.last_seen = now_f64() - PEER_TIMEOUT_SECS - 1.0;
        assert!(!peer.is_alive());
    }

    #[test]
    fn test_peer_reputation() {
        let mut peer = PeerInfo::new("node1".into(), addr("10.0.0.1", 5001), false);
        assert_eq!(peer.reputation, 0.5);

        peer.adjust_reputation(0.3);
        assert!((peer.reputation - 0.8).abs() < f64::EPSILON);

        peer.adjust_reputation(0.5); // Clamped to 1.0
        assert!((peer.reputation - 1.0).abs() < f64::EPSILON);

        peer.adjust_reputation(-1.5); // Clamped to 0.0
        assert!((peer.reputation - 0.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_peer_manager_add_remove() {
        let mut mgr = PeerManager::new();
        let peer = PeerInfo::new("node1".into(), addr("10.0.0.1", 5001), true);
        assert!(mgr.add_peer(peer));
        assert_eq!(mgr.count(), 1);
        assert!(mgr.get("node1").is_some());

        mgr.remove_peer("node1");
        assert_eq!(mgr.count(), 0);
    }

    #[test]
    fn test_peer_manager_capacity() {
        let mut mgr = PeerManager::new();
        for i in 0..MAX_PEERS {
            let peer = PeerInfo::new(
                format!("node_{}", i),
                addr("10.0.0.1", 5001 + i as u16),
                true,
            );
            assert!(mgr.add_peer(peer));
        }
        assert_eq!(mgr.count(), MAX_PEERS);

        // One more should fail
        let extra = PeerInfo::new("overflow".into(), addr("10.0.0.99", 5001), true);
        assert!(!mgr.add_peer(extra));
    }

    #[test]
    fn test_peer_manager_prune_dead() {
        let mut mgr = PeerManager::new();

        // Add a live peer
        let live = PeerInfo::new("live".into(), addr("10.0.0.1", 5001), true);
        mgr.add_peer(live);

        // Add a dead peer
        let mut dead = PeerInfo::new("dead".into(), addr("10.0.0.2", 5001), true);
        dead.last_seen = now_f64() - PEER_TIMEOUT_SECS - 10.0;
        mgr.add_peer(dead);

        assert_eq!(mgr.count(), 2);

        let pruned = mgr.prune_dead();
        assert_eq!(pruned, vec!["dead"]);
        assert_eq!(mgr.count(), 1);
    }

    #[test]
    fn test_peer_manager_best_height() {
        let mut mgr = PeerManager::new();

        let mut p1 = PeerInfo::new("n1".into(), addr("10.0.0.1", 5001), true);
        p1.chain_height = 50;
        mgr.add_peer(p1);

        let mut p2 = PeerInfo::new("n2".into(), addr("10.0.0.2", 5001), true);
        p2.chain_height = 200;
        mgr.add_peer(p2);

        let best = mgr.best_height_peer().unwrap();
        assert_eq!(best.node_id, "n2");
        assert_eq!(best.chain_height, 200);
    }

    #[test]
    fn test_connection_guard_allow() {
        let mut guard = ConnectionGuard::new();
        assert!(guard.allow_connection("10.0.0.1").is_ok());
        assert_eq!(guard.total_inbound(), 1);
    }

    #[test]
    fn test_connection_guard_per_ip_limit() {
        let mut guard = ConnectionGuard::new();
        for _ in 0..MAX_CONNECTIONS_PER_IP {
            guard.allow_connection("10.0.0.1").unwrap();
        }
        // Next one should fail
        assert!(guard.allow_connection("10.0.0.1").is_err());
    }

    #[test]
    fn test_connection_guard_localhost_bypass() {
        let mut guard = ConnectionGuard::new();
        // Localhost bypasses per-IP limits
        for _ in 0..10 {
            guard.allow_connection("127.0.0.1").unwrap();
        }
    }

    #[test]
    fn test_connection_guard_release() {
        let mut guard = ConnectionGuard::new();
        guard.allow_connection("10.0.0.1").unwrap();
        assert_eq!(guard.total_inbound(), 1);

        guard.release_connection("10.0.0.1");
        assert_eq!(guard.total_inbound(), 0);
    }

    #[test]
    fn test_connection_guard_ban() {
        let mut guard = ConnectionGuard::new();
        assert!(!guard.is_banned("10.0.0.1"));

        // Accumulate misbehavior to trigger ban
        guard.record_misbehavior("10.0.0.1", BAN_SCORE_THRESHOLD, "test ban");
        assert!(guard.is_banned("10.0.0.1"));

        // Banned IP should be rejected
        assert!(guard.allow_connection("10.0.0.1").is_err());
    }

    #[test]
    fn test_connection_guard_rate_limit() {
        let mut guard = ConnectionGuard::new();

        // Should allow up to RATE_LIMIT
        for _ in 0..RATE_LIMIT_PER_IP {
            assert!(guard.check_rate_limit("10.0.0.1"));
        }

        // Next one should be denied
        assert!(!guard.check_rate_limit("10.0.0.1"));

        // Localhost is exempt
        for _ in 0..200 {
            assert!(guard.check_rate_limit("127.0.0.1"));
        }
    }

    #[test]
    fn test_connection_guard_misbehavior_localhost_immune() {
        let mut guard = ConnectionGuard::new();
        guard.record_misbehavior("127.0.0.1", 9999, "test");
        assert!(!guard.is_banned("127.0.0.1"));
    }

    #[test]
    fn test_connection_guard_explicit_ban() {
        let mut guard = ConnectionGuard::new();
        assert!(!guard.is_banned("10.0.0.50"));

        guard.ban("10.0.0.50", 3600.0, "manual ban");
        assert!(guard.is_banned("10.0.0.50"));
        assert!(guard.allow_connection("10.0.0.50").is_err());

        // Localhost immune to explicit ban
        guard.ban("127.0.0.1", 3600.0, "should not work");
        assert!(!guard.is_banned("127.0.0.1"));
    }

    #[test]
    fn test_connection_guard_get_stats() {
        let mut guard = ConnectionGuard::new();
        guard.allow_connection("10.0.0.1").unwrap();
        guard.allow_connection("10.0.0.2").unwrap();
        guard.ban("10.0.0.99", 3600.0, "evil");

        let stats = guard.get_stats();
        assert_eq!(stats.total_inbound, 2);
        assert_eq!(stats.unique_ips, 2);
        assert_eq!(stats.banned_ips, 1);
    }

    #[test]
    fn test_connection_guard_misbehavior_score() {
        let mut guard = ConnectionGuard::new();
        assert_eq!(guard.misbehavior_score("10.0.0.1"), 0);

        guard.record_misbehavior("10.0.0.1", 25, "test");
        assert_eq!(guard.misbehavior_score("10.0.0.1"), 25);

        guard.record_misbehavior("10.0.0.1", 30, "test2");
        assert_eq!(guard.misbehavior_score("10.0.0.1"), 55);
    }

    // ── Safe Address Validation Tests ────────────────────────────────────

    #[test]
    fn test_safe_peer_address_normal() {
        assert!(is_safe_peer_address("10.0.0.1"));
        assert!(is_safe_peer_address("192.168.1.1"));
        assert!(is_safe_peer_address("8.8.8.8"));
        assert!(is_safe_peer_address("node.example.com"));
    }

    #[test]
    fn test_safe_peer_address_rejects_loopback() {
        assert!(!is_safe_peer_address("127.0.0.1"));
        assert!(!is_safe_peer_address("127.0.0.2"));
        assert!(!is_safe_peer_address("localhost"));
        assert!(!is_safe_peer_address("::1"));
    }

    #[test]
    fn test_safe_peer_address_rejects_link_local() {
        assert!(!is_safe_peer_address("169.254.1.1"));
        assert!(!is_safe_peer_address("169.254.169.254")); // cloud metadata
    }

    #[test]
    fn test_safe_peer_address_rejects_unspecified() {
        assert!(!is_safe_peer_address("0.0.0.0"));
        assert!(!is_safe_peer_address("::"));
    }

    #[test]
    fn test_safe_peer_address_rejects_broadcast() {
        assert!(!is_safe_peer_address("255.255.255.255"));
    }

    #[test]
    fn test_safe_peer_address_rejects_local_hostnames() {
        assert!(!is_safe_peer_address("mybox.local"));
    }

    // ── Message Rate Limiter Tests ───────────────────────────────────────

    #[test]
    fn test_message_rate_limiter_allows_within_limit() {
        let mut limiter = MessageRateLimiter::new();
        for _ in 0..RATE_PING {
            assert!(limiter.check("10.0.0.1", "ping"));
        }
    }

    #[test]
    fn test_message_rate_limiter_blocks_over_limit() {
        let mut limiter = MessageRateLimiter::new();
        for _ in 0..RATE_HANDSHAKE {
            assert!(limiter.check("10.0.0.1", "handshake"));
        }
        // Next one exceeds
        assert!(!limiter.check("10.0.0.1", "handshake"));
    }

    #[test]
    fn test_message_rate_limiter_independent_types() {
        let mut limiter = MessageRateLimiter::new();
        // Fill up handshake limit
        for _ in 0..RATE_HANDSHAKE {
            limiter.check("10.0.0.1", "handshake");
        }
        assert!(!limiter.check("10.0.0.1", "handshake"));

        // Ping should still be allowed (different type)
        assert!(limiter.check("10.0.0.1", "ping"));
    }

    #[test]
    fn test_message_rate_limiter_independent_ips() {
        let mut limiter = MessageRateLimiter::new();
        for _ in 0..RATE_HANDSHAKE {
            limiter.check("10.0.0.1", "handshake");
        }
        assert!(!limiter.check("10.0.0.1", "handshake"));

        // Different IP should still be allowed
        assert!(limiter.check("10.0.0.2", "handshake"));
    }

    #[test]
    fn test_message_rate_limiter_localhost_exempt() {
        let mut limiter = MessageRateLimiter::new();
        // Should never be rate-limited
        for _ in 0..200 {
            assert!(limiter.check("127.0.0.1", "handshake"));
        }
    }

    // ── Backoff Tracker Tests ────────────────────────────────────────────

    #[test]
    fn test_backoff_new_address_allowed() {
        let tracker = BackoffTracker::new();
        assert!(tracker.can_reconnect("10.0.0.1:5001"));
        assert_eq!(tracker.attempts("10.0.0.1:5001"), 0);
    }

    #[test]
    fn test_backoff_after_failure() {
        let mut tracker = BackoffTracker::new();
        tracker.record_failure("10.0.0.1:5001");
        assert_eq!(tracker.attempts("10.0.0.1:5001"), 1);
        // Immediately after failure, backoff is at least 2s so can_reconnect = false
        assert!(!tracker.can_reconnect("10.0.0.1:5001"));
    }

    #[test]
    fn test_backoff_reset_on_success() {
        let mut tracker = BackoffTracker::new();
        tracker.record_failure("10.0.0.1:5001");
        tracker.record_failure("10.0.0.1:5001");
        assert_eq!(tracker.attempts("10.0.0.1:5001"), 2);

        tracker.record_success("10.0.0.1:5001");
        assert_eq!(tracker.attempts("10.0.0.1:5001"), 0);
        assert!(tracker.can_reconnect("10.0.0.1:5001"));
    }

    #[test]
    fn test_backoff_exponential() {
        let mut tracker = BackoffTracker::new();
        tracker.record_failure("peer1");
        assert_eq!(tracker.attempts("peer1"), 1); // delay = 2^1 = 2s

        tracker.record_failure("peer1");
        assert_eq!(tracker.attempts("peer1"), 2); // delay = 2^2 = 4s

        tracker.record_failure("peer1");
        assert_eq!(tracker.attempts("peer1"), 3); // delay = 2^3 = 8s
    }

    #[test]
    fn test_backoff_max_cap() {
        let mut tracker = BackoffTracker::new();
        tracker.max_backoff_secs = 16.0;
        for _ in 0..20 {
            tracker.record_failure("peer1");
        }
        // Even after 20 failures, backoff shouldn't exceed max
        let (_, next_allowed) = tracker.entries.get("peer1").unwrap();
        let max_expected = now_f64() + 16.0 + 1.0; // +1s tolerance
        assert!(*next_allowed <= max_expected);
    }

    #[test]
    fn test_peer_addresses() {
        let mut mgr = PeerManager::new();
        let p = PeerInfo::new("n1".into(), addr("10.0.0.5", 5001), true);
        mgr.add_peer(p);
        let addrs = mgr.peer_addresses();
        assert_eq!(addrs.len(), 1);
        assert_eq!(addrs[0], ("10.0.0.5".to_string(), 5001));
    }
}
