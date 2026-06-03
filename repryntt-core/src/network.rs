//! Wire protocol — message types, framing, and serialization.
//!
//! All TCP messages use a 4-byte big-endian length prefix followed by a JSON
//! payload.  This matches the Python `struct.pack('!I', len(msg)) + msg`
//! framing used in qnode2.py and gossip.py.
//!
//! ```text
//! [4 bytes: payload length (big-endian u32)][N bytes: JSON payload]
//! ```

use std::collections::BTreeMap;
use std::io;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;

use crate::genesis::{NETWORK_MAGIC_HEX, PROTOCOL_VERSION};

// ── Network Constants ────────────────────────────────────────────────────────

/// Default blockchain P2P port.
pub const DEFAULT_PORT: u16 = 5001;

/// Optional HTTP bootstrap registry default.
///
/// Public release builds intentionally do not ship an official HTTP bootstrap
/// registry. Discovery follows the Bitcoin-style path: configured peers,
/// local node.conf, DNS/static seeds, and peer gossip.
pub const DEFAULT_BOOTSTRAP_URL: &str = "";

/// Hardcoded fallback seed nodes (used only when nothing else is configured).
pub const SEED_NODES: &[(&str, u16)] = &[
    ("seed1.repryntt.ai158z.com", 5001),
    ("seed2.repryntt.ai158z.com", 5001),
    // Static public fallbacks keep non-technical clients online when local DNS
    // briefly fails. DNS seeds remain preferred because IPs can move.
    ("35.208.114.82", 5001),
    ("35.222.170.120", 5001),
];

fn discovery_value_disabled(raw: &str) -> bool {
    matches!(
        raw.trim().to_ascii_lowercase().as_str(),
        "" | "none" | "off" | "disabled"
    )
}

fn bootstrap_url_disabled(raw: &str) -> bool {
    discovery_value_disabled(raw)
}

/// Parse a comma-separated or newline-separated list of `host:port` strings
/// into SocketAddrs. Entries without a port get `default_port` appended.
/// Supports both IP addresses and DNS hostnames (resolved via `ToSocketAddrs`).
fn parse_seed_list(raw: &str, default_port: u16) -> Vec<std::net::SocketAddr> {
    use std::net::ToSocketAddrs;

    raw.split(|c| c == ',' || c == '\n')
        .filter_map(|s| {
            let s = s.trim();
            if s.is_empty() || s.starts_with('#') {
                return None;
            }
            let with_port = if s.contains(':') {
                s.to_string()
            } else {
                format!("{}:{}", s, default_port)
            };
            match with_port.to_socket_addrs() {
                Ok(mut addrs) => addrs.next(),
                Err(_) => {
                    eprintln!("⚠️  Ignoring invalid seed: {} (could not resolve)", s);
                    None
                }
            }
        })
        .collect()
}

/// Resolve seed list.  Priority: env var > node.conf > hardcoded fallback > explicit bootstrap URL.
///
/// **Env vars** (highest priority — first one found wins):
///   `REPRYNTT_SEEDS=10.0.0.19:5001,192.168.1.5:5001`
///   `REPRYNTT_BOOTSTRAP_NODES=10.0.0.19:5001,192.168.1.5:5001`
///   Set either to `none` to explicitly disable all seeds.
///
/// **Config file** (`<data_dir>/node.conf`):
///   ```text
///   # Seed peers — one per line or comma-separated
///   addnode=10.0.0.19:5001
///   addnode=192.168.1.5:5001
///   ```
///
/// **Bootstrap URL** (`REPRYNTT_BOOTSTRAP_URL`):
///   Optional HTTP rendezvous server — only used when explicitly configured.
///
/// **Hardcoded fallback**: `SEED_NODES` DNS names.
pub fn resolve_seeds(p2p_port: u16, data_dir: &std::path::Path) -> Vec<std::net::SocketAddr> {
    // 1. Env vars win — check REPRYNTT_SEEDS first, then REPRYNTT_BOOTSTRAP_NODES
    for var_name in &["REPRYNTT_SEEDS", "REPRYNTT_BOOTSTRAP_NODES"] {
        match std::env::var(var_name) {
            Ok(val) if discovery_value_disabled(&val) => {
                return vec![];
            }
            Ok(val) if !val.trim().is_empty() => {
                let addrs = parse_seed_list(&val, p2p_port);
                if !addrs.is_empty() {
                    return addrs;
                }
                eprintln!(
                    "⚠️  {} configured but no peers resolved; falling back to node.conf/static seeds",
                    var_name
                );
            }
            _ => {}
        }
    }

    // 2. Read node.conf
    let conf_path = data_dir.join("node.conf");
    if conf_path.exists() {
        if let Ok(contents) = std::fs::read_to_string(&conf_path) {
            let addnodes: Vec<String> = contents
                .lines()
                .filter_map(|line| {
                    let line = line.trim();
                    if line.starts_with('#') || line.is_empty() {
                        return None;
                    }
                    // Support "addnode=host:port" and bare "host:port"
                    if let Some(addr) = line.strip_prefix("addnode=") {
                        Some(addr.trim().to_string())
                    } else if line.contains('.') || line.contains(':') {
                        // Looks like an address
                        Some(line.to_string())
                    } else {
                        None
                    }
                })
                .collect();

            if !addnodes.is_empty() {
                let joined = addnodes.join(",");
                let addrs = parse_seed_list(&joined, p2p_port);
                if !addrs.is_empty() {
                    return addrs;
                }
                eprintln!(
                    "⚠️  node.conf seed entries did not resolve; falling back to static seeds"
                );
            }
        }
    }

    // 3. Hardcoded DNS seed fallback
    let joined = SEED_NODES
        .iter()
        .map(|(host, port)| format!("{}:{}", host, port))
        .collect::<Vec<_>>()
        .join(",");
    let dns_seeds = parse_seed_list(&joined, p2p_port);
    if !dns_seeds.is_empty() {
        return dns_seeds;
    }

    // 4. Optional HTTP bootstrap rendezvous server (REPRYNTT_BOOTSTRAP_URL).
    query_bootstrap_url(p2p_port)
}

/// Query the HTTP bootstrap rendezvous server for peer addresses.
///
/// Reads `REPRYNTT_BOOTSTRAP_URL` (e.g. `http://34.x.x.x:6600`).
/// Parse `REPRYNTT_BOOTSTRAP_URL` into a list of bootstrap base URLs.
///
/// Returns an empty Vec when bootstrap is disabled or unset. Accepts a single
/// URL or a comma-separated list, trims whitespace, strips trailing slashes.
/// Supports multiple URLs so a node can query several independent bootstrap
/// servers and union the results — eclipse-attack resistance for free.
fn bootstrap_urls() -> Vec<String> {
    let raw = match std::env::var("REPRYNTT_BOOTSTRAP_URL") {
        Ok(u) if bootstrap_url_disabled(&u) => return vec![],
        Ok(u) if !u.trim().is_empty() => u,
        _ if !DEFAULT_BOOTSTRAP_URL.is_empty() => DEFAULT_BOOTSTRAP_URL.to_string(),
        _ => return vec![],
    };
    raw.split(',')
        .map(|s| s.trim().trim_end_matches('/').to_string())
        .filter(|s| !s.is_empty())
        .collect()
}

/// Calls `GET /rendezvous/peers` against every configured bootstrap URL and
/// returns the union of TCP peer addresses they advertise.
///
/// Bootstrap response peers carry `address: "host:port"` for the blockchain
/// protocol. The port advertised by the peer is used directly (no rewriting
/// to `default_port` — different operators may run their P2P on non-default
/// ports). `default_port` is only used as a fallback when a peer's address
/// is missing the port component.
pub fn query_bootstrap_url(default_port: u16) -> Vec<std::net::SocketAddr> {
    use std::net::ToSocketAddrs;
    let urls = bootstrap_urls();
    if urls.is_empty() {
        return vec![];
    }

    let mut seen: std::collections::HashSet<std::net::SocketAddr> =
        std::collections::HashSet::new();
    let mut addrs = Vec::new();

    for url in &urls {
        let endpoint = format!("{}/rendezvous/peers", url);
        eprintln!("🌐 Querying bootstrap: {}", endpoint);

        let resp_str = match ureq::get(&endpoint)
            .timeout(std::time::Duration::from_secs(10))
            .call()
        {
            Ok(r) => match r.into_string() {
                Ok(s) => s,
                Err(e) => {
                    eprintln!("⚠️  Bootstrap response read failed ({}): {}", url, e);
                    continue;
                }
            },
            Err(e) => {
                eprintln!("⚠️  Bootstrap query failed ({}): {}", url, e);
                continue;
            }
        };

        let body: serde_json::Value = match serde_json::from_str(&resp_str) {
            Ok(v) => v,
            Err(e) => {
                eprintln!("⚠️  Bootstrap response parse failed ({}): {}", url, e);
                continue;
            }
        };

        let peers = match body.get("peers").and_then(|p| p.as_array()) {
            Some(arr) => arr,
            None => continue,
        };

        for peer in peers {
            let address = match peer.get("address").and_then(|a| a.as_str()) {
                Some(a) => a,
                None => continue,
            };

            // Strip any legacy URL prefixes a mixed-fleet bootstrap might
            // have stored; blockchain peers should be plain `host:port`.
            let host_port = address
                .trim_start_matches("tcp://")
                .trim_start_matches("ws://")
                .trim_start_matches("wss://")
                .trim_start_matches("http://")
                .trim_start_matches("https://")
                .split('/')
                .next()
                .unwrap_or("")
                .trim();

            if host_port.is_empty() {
                continue;
            }

            // Use the advertised port if present; fall back to default_port
            // when address is bare-host (legacy / partial data).
            let seed = if host_port.contains(':') {
                host_port.to_string()
            } else {
                format!("{}:{}", host_port, default_port)
            };

            if let Ok(resolved) = seed.to_socket_addrs() {
                for addr in resolved {
                    if seen.insert(addr) {
                        addrs.push(addr);
                    }
                }
            }
        }
    }

    if !addrs.is_empty() {
        eprintln!(
            "🌐 Bootstrap union returned {} unique peer(s) across {} server(s)",
            addrs.len(),
            urls.len()
        );
    }
    addrs
}

/// Announce this node's P2P address to the HTTP bootstrap rendezvous server.
///
/// This is discovery-only. The bootstrap server is never trusted for chain
/// validity or transaction acceptance.
pub fn announce_to_bootstrap_url(
    node_id: &str,
    p2p_port: u16,
    chain_height: u64,
    genesis_hash: &str,
) {
    let urls = bootstrap_urls();
    if urls.is_empty() {
        return;
    }

    // `address` is OPTIONAL in the announce. If the operator has set
    // REPRYNTT_PUBLIC_P2P_ADDR (e.g. "seed1.example.com:5001"), we send it
    // explicitly so the bootstrap server lists this exact endpoint. If
    // omitted, the server derives the address from our source IP + the
    // announced `p2p_port`. NAT'd home nodes still announce (so the server
    // can attempt a probe) but the server's TCP probe will fail and they
    // won't actually be listed — which is correct, they can't accept
    // inbound peers anyway.
    let mut payload = serde_json::json!({
        "node_id": node_id,
        "p2p_port": p2p_port,
        "chain_height": chain_height,
        "genesis_hash": genesis_hash,
        "protocol_version": crate::genesis::PROTOCOL_VERSION,
        "version": env!("CARGO_PKG_VERSION"),
    });
    if let Ok(addr) = std::env::var("REPRYNTT_PUBLIC_P2P_ADDR") {
        if !addr.trim().is_empty() {
            payload["address"] = serde_json::Value::String(addr.trim().to_string());
        }
    }
    let body = payload.to_string();

    for url in &urls {
        let endpoint = format!("{}/rendezvous/announce", url);
        match ureq::post(&endpoint)
            .timeout(std::time::Duration::from_secs(10))
            .set("Content-Type", "application/json")
            .send_string(&body)
        {
            Ok(_) => eprintln!("🌐 Announced to bootstrap: {}", endpoint),
            Err(e) => eprintln!("⚠️  Bootstrap announce failed ({}): {}", url, e),
        }
    }
}

/// Maximum message payload: 4 MB.
pub const MAX_MESSAGE_SIZE: u32 = 4 * 1024 * 1024;

/// Maximum block download batch: 256 MB.
pub const MAX_BLOCK_DOWNLOAD_SIZE: u32 = 256 * 1024 * 1024;

// ── Message Types ────────────────────────────────────────────────────────────

/// All message types exchanged between blockchain peers.
///
/// Matches the Python message type constants from qnode2.py and comms/p2p.py.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MessageType {
    // Handshake & heartbeat
    Handshake,
    HandshakeAck,
    Ping,
    Pong,

    // Chain sync
    GetChainHeight,
    ChainHeight,
    GetHeaders,
    Headers,
    GetBlocks,
    Blocks,

    // Block propagation
    BlockAnnounce,
    BlockRequest,
    BlockResponse,

    // Transaction propagation
    TxAnnounce,
    TxRequest,
    TxResponse,

    // Peer exchange
    GetPeers,
    PeerList,

    // Gossip relay
    Gossip,

    // Compute economy (0x70-0x7F in Python)
    ComputeAnnounce,
    ComputeRequest,
    ComputeClaim,
    ComputeResult,
    ComputeReject,

    // Economy status
    EconomyStatus,
}

impl MessageType {
    /// Convert to the string used in JSON `"type"` field.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Handshake => "handshake",
            Self::HandshakeAck => "handshake_ack",
            Self::Ping => "ping",
            Self::Pong => "pong",
            Self::GetChainHeight => "get_chain_height",
            Self::ChainHeight => "chain_height",
            Self::GetHeaders => "get_headers",
            Self::Headers => "headers",
            Self::GetBlocks => "get_blocks",
            Self::Blocks => "blocks",
            Self::BlockAnnounce => "block_announce",
            Self::BlockRequest => "block_request",
            Self::BlockResponse => "block_response",
            Self::TxAnnounce => "tx_announce",
            Self::TxRequest => "tx_request",
            Self::TxResponse => "tx_response",
            Self::GetPeers => "get_peers",
            Self::PeerList => "peer_list",
            Self::Gossip => "gossip",
            Self::ComputeAnnounce => "compute_announce",
            Self::ComputeRequest => "compute_request",
            Self::ComputeClaim => "compute_claim",
            Self::ComputeResult => "compute_result",
            Self::ComputeReject => "compute_reject",
            Self::EconomyStatus => "economy_status",
        }
    }

    /// Parse from the JSON `"type"` string.
    pub fn from_str(s: &str) -> Option<Self> {
        match s {
            "handshake" => Some(Self::Handshake),
            "handshake_ack" => Some(Self::HandshakeAck),
            "ping" => Some(Self::Ping),
            "pong" => Some(Self::Pong),
            "get_chain_height" => Some(Self::GetChainHeight),
            "chain_height" => Some(Self::ChainHeight),
            "get_headers" => Some(Self::GetHeaders),
            "headers" => Some(Self::Headers),
            "get_blocks" => Some(Self::GetBlocks),
            "blocks" => Some(Self::Blocks),
            "block_announce" => Some(Self::BlockAnnounce),
            "block_request" => Some(Self::BlockRequest),
            "block_response" => Some(Self::BlockResponse),
            "tx_announce" => Some(Self::TxAnnounce),
            "tx_request" => Some(Self::TxRequest),
            "tx_response" => Some(Self::TxResponse),
            "get_peers" => Some(Self::GetPeers),
            "peer_list" => Some(Self::PeerList),
            "gossip" => Some(Self::Gossip),
            "compute_announce" => Some(Self::ComputeAnnounce),
            "compute_request" => Some(Self::ComputeRequest),
            "compute_claim" => Some(Self::ComputeClaim),
            "compute_result" => Some(Self::ComputeResult),
            "compute_reject" => Some(Self::ComputeReject),
            "economy_status" => Some(Self::EconomyStatus),
            _ => None,
        }
    }
}

// ── Message Envelope ─────────────────────────────────────────────────────────

/// A network message with type, payload, and network metadata.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Message {
    /// Message type identifier.
    #[serde(rename = "type")]
    pub msg_type: String,
    /// Network magic (must match `NETWORK_MAGIC_HEX`).
    pub network_magic: String,
    /// Sender node ID.
    #[serde(default)]
    pub node_id: String,
    /// Protocol version.
    #[serde(default)]
    pub protocol_version: u32,
    /// Message-specific payload fields (flattened into the same object).
    #[serde(flatten)]
    pub payload: BTreeMap<String, Value>,
}

impl Message {
    /// Create a new message with the given type and payload.
    pub fn new(msg_type: MessageType, payload: BTreeMap<String, Value>) -> Self {
        Self {
            msg_type: msg_type.as_str().to_string(),
            network_magic: NETWORK_MAGIC_HEX.to_string(),
            node_id: String::new(),
            protocol_version: PROTOCOL_VERSION,
            payload,
        }
    }

    /// Create a new message with a sender node ID.
    pub fn with_node_id(mut self, node_id: &str) -> Self {
        self.node_id = node_id.to_string();
        self
    }

    /// Parse the message type.
    pub fn parsed_type(&self) -> Option<MessageType> {
        MessageType::from_str(&self.msg_type)
    }

    /// Validate that this message has the correct network magic.
    pub fn validate_magic(&self) -> bool {
        self.network_magic == NETWORK_MAGIC_HEX
    }

    /// Serialize to JSON bytes.
    pub fn to_bytes(&self) -> Result<Vec<u8>, serde_json::Error> {
        serde_json::to_vec(self)
    }

    /// Deserialize from JSON bytes.
    pub fn from_bytes(data: &[u8]) -> Result<Self, serde_json::Error> {
        serde_json::from_slice(data)
    }
}

// ── Wire Protocol: Length-Prefixed Framing ────────────────────────────────────

/// Write a length-prefixed message to a TCP stream.
///
/// Format: `[4 bytes: payload length (big-endian u32)][N bytes: JSON payload]`
pub async fn write_message(stream: &mut TcpStream, msg: &Message) -> io::Result<()> {
    let payload = msg
        .to_bytes()
        .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;

    if payload.len() as u64 > MAX_MESSAGE_SIZE as u64 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("Message too large: {} bytes", payload.len()),
        ));
    }

    let len_prefix = (payload.len() as u32).to_be_bytes();
    stream.write_all(&len_prefix).await?;
    stream.write_all(&payload).await?;
    stream.flush().await?;
    Ok(())
}

/// Read a length-prefixed message from a TCP stream.
///
/// Returns `None` if the stream is closed cleanly.
pub async fn read_message(stream: &mut TcpStream) -> io::Result<Option<Message>> {
    read_message_with_limit(stream, MAX_MESSAGE_SIZE).await
}

/// Read a length-prefixed message with a custom size limit.
pub async fn read_message_with_limit(
    stream: &mut TcpStream,
    max_size: u32,
) -> io::Result<Option<Message>> {
    // Read 4-byte length prefix
    let mut len_buf = [0u8; 4];
    match stream.read_exact(&mut len_buf).await {
        Ok(_) => {}
        Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(e) => return Err(e),
    }

    let length = u32::from_be_bytes(len_buf);
    if length > max_size {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("Message too large: {} > {} bytes", length, max_size),
        ));
    }

    // Read payload
    let mut payload = vec![0u8; length as usize];
    stream.read_exact(&mut payload).await?;

    let msg =
        Message::from_bytes(&payload).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;

    Ok(Some(msg))
}

/// Write raw length-prefixed bytes (for block download responses).
pub async fn write_raw(stream: &mut TcpStream, data: &[u8]) -> io::Result<()> {
    let len_prefix = (data.len() as u32).to_be_bytes();
    stream.write_all(&len_prefix).await?;
    stream.write_all(data).await?;
    stream.flush().await?;
    Ok(())
}

/// Read raw length-prefixed bytes with a custom size limit.
pub async fn read_raw(stream: &mut TcpStream, max_size: u32) -> io::Result<Option<Vec<u8>>> {
    let mut len_buf = [0u8; 4];
    match stream.read_exact(&mut len_buf).await {
        Ok(_) => {}
        Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(e) => return Err(e),
    }

    let length = u32::from_be_bytes(len_buf);
    if length > max_size {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("Payload too large: {} > {} bytes", length, max_size),
        ));
    }

    let mut data = vec![0u8; length as usize];
    stream.read_exact(&mut data).await?;
    Ok(Some(data))
}

// ── Helper: build common messages ────────────────────────────────────────────

/// Build a `get_chain_height` request.
pub fn msg_get_chain_height() -> Message {
    Message::new(MessageType::GetChainHeight, BTreeMap::new())
}

/// Build a `chain_height` response.
///
/// `tip_hash` is the hash of the block at `height`. Older peers that haven't
/// been patched may send an empty string; receivers must treat empty tip_hash
/// as "unknown" and fall back to height-only behavior for that peer.
pub fn msg_chain_height(height: u64, genesis_hash: &str, tip_hash: &str) -> Message {
    let mut payload = BTreeMap::new();
    payload.insert("success".into(), Value::Bool(true));
    payload.insert("chain_height".into(), Value::Number(height.into()));
    payload.insert(
        "genesis_hash".into(),
        Value::String(genesis_hash.to_string()),
    );
    payload.insert("tip_hash".into(), Value::String(tip_hash.to_string()));
    Message::new(MessageType::ChainHeight, payload)
}

/// Build a `get_blocks` request for a range `[start, end)`.
pub fn msg_get_blocks(start: u64, end: u64) -> Message {
    let mut payload = BTreeMap::new();
    payload.insert("start".into(), Value::Number(start.into()));
    payload.insert("end".into(), Value::Number(end.into()));
    Message::new(MessageType::GetBlocks, payload)
}

/// Build a `get_headers` request using a Bitcoin-style block locator.
pub fn msg_get_headers(locator: &[String], stop_hash: Option<&str>, limit: u64) -> Message {
    let mut payload = BTreeMap::new();
    payload.insert(
        "locator".into(),
        Value::Array(locator.iter().map(|h| Value::String(h.clone())).collect()),
    );
    if let Some(stop) = stop_hash {
        payload.insert("stop_hash".into(), Value::String(stop.to_string()));
    }
    payload.insert("limit".into(), Value::Number(limit.into()));
    Message::new(MessageType::GetHeaders, payload)
}

/// Build a `headers` response.
pub fn msg_headers(best_height: u64, genesis_hash: &str, headers: Vec<Value>) -> Message {
    let mut payload = BTreeMap::new();
    payload.insert("success".into(), Value::Bool(true));
    payload.insert("best_height".into(), Value::Number(best_height.into()));
    payload.insert(
        "genesis_hash".into(),
        Value::String(genesis_hash.to_string()),
    );
    payload.insert("headers".into(), Value::Array(headers));
    Message::new(MessageType::Headers, payload)
}

/// Build a `block_announce` message carrying the full serialized block.
///
/// Embeds the entire block body (not just header fields) so receivers can
/// validate and append/reorg without a follow-up `get_blocks` round trip.
/// This is how block propagation actually converges the network after a
/// production event — every peer learns about the new block immediately
/// instead of waiting for the next periodic height poll. Receivers should
/// de-duplicate by hash before relaying to break gossip loops.
pub fn msg_block_announce(block: &serde_json::Value) -> Message {
    let mut payload = BTreeMap::new();
    let index = block.get("index").and_then(|v| v.as_u64()).unwrap_or(0);
    let hash = block
        .get("hash")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let prev_hash = block
        .get("previous_hash")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    payload.insert("block_index".into(), Value::Number(index.into()));
    payload.insert("hash".into(), Value::String(hash));
    payload.insert("previous_hash".into(), Value::String(prev_hash));
    payload.insert("block".into(), block.clone());
    Message::new(MessageType::BlockAnnounce, payload)
}

/// Build a `tx_announce` message carrying the full serialized transaction.
///
/// We embed the entire tx body (not just hash/type/amount) so receivers can
/// validate and add it to their mempool without a follow-up `tx_request`.
/// For a small trusted network this trades a little bandwidth for protocol
/// simplicity. Receivers should de-duplicate by `tx_hash` before relaying.
pub fn msg_tx_announce(tx: &serde_json::Value) -> Message {
    let mut payload = BTreeMap::new();
    let tx_hash = tx
        .get("tx_hash")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    payload.insert("tx_hash".into(), Value::String(tx_hash));
    payload.insert("tx".into(), tx.clone());
    Message::new(MessageType::TxAnnounce, payload)
}

/// Build a handshake message.
pub fn msg_handshake(node_id: &str, chain_height: u64, genesis_hash: &str, tflops: f64) -> Message {
    let mut payload = BTreeMap::new();
    payload.insert("chain_height".into(), Value::Number(chain_height.into()));
    payload.insert(
        "genesis_hash".into(),
        Value::String(genesis_hash.to_string()),
    );
    if let Some(n) = serde_json::Number::from_f64(tflops) {
        payload.insert("tflops".into(), Value::Number(n));
    }
    Message::new(MessageType::Handshake, payload).with_node_id(node_id)
}

/// Build a ping message.
pub fn msg_ping(chain_height: u64) -> Message {
    let mut payload = BTreeMap::new();
    payload.insert("chain_height".into(), Value::Number(chain_height.into()));
    Message::new(MessageType::Ping, payload)
}

/// Build a pong response.
pub fn msg_pong(chain_height: u64) -> Message {
    let mut payload = BTreeMap::new();
    payload.insert("chain_height".into(), Value::Number(chain_height.into()));
    Message::new(MessageType::Pong, payload)
}

/// Build a peer list message.
pub fn msg_peer_list(peers: &[(String, u16)]) -> Message {
    let list: Vec<Value> = peers
        .iter()
        .map(|(host, port)| {
            let mut m = serde_json::Map::new();
            m.insert("host".into(), Value::String(host.clone()));
            m.insert("port".into(), Value::Number((*port).into()));
            Value::Object(m)
        })
        .collect();
    let mut payload = BTreeMap::new();
    payload.insert("peers".into(), Value::Array(list));
    Message::new(MessageType::PeerList, payload)
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_message_roundtrip() {
        let msg = msg_handshake("node_abc", 42, "deadbeef", 5.4);
        let bytes = msg.to_bytes().unwrap();
        let decoded = Message::from_bytes(&bytes).unwrap();
        assert_eq!(decoded.msg_type, "handshake");
        assert_eq!(decoded.node_id, "node_abc");
        assert!(decoded.validate_magic());
        assert_eq!(decoded.protocol_version, PROTOCOL_VERSION);
        assert_eq!(
            decoded.payload.get("chain_height").unwrap().as_u64(),
            Some(42)
        );
    }

    #[test]
    fn test_message_types_roundtrip() {
        for msg_type in &[
            MessageType::Handshake,
            MessageType::BlockAnnounce,
            MessageType::GetBlocks,
            MessageType::TxAnnounce,
            MessageType::Gossip,
            MessageType::ComputeResult,
        ] {
            let s = msg_type.as_str();
            assert_eq!(MessageType::from_str(s), Some(*msg_type));
        }
    }

    #[test]
    fn test_invalid_magic() {
        let mut msg = msg_ping(10);
        msg.network_magic = "DEADBEEF".into();
        assert!(!msg.validate_magic());
    }

    #[test]
    fn test_chain_height_message() {
        let msg = msg_chain_height(1000, "abc123", "deadbeef");
        let bytes = msg.to_bytes().unwrap();
        let decoded = Message::from_bytes(&bytes).unwrap();
        assert_eq!(
            decoded.payload.get("chain_height").unwrap().as_u64(),
            Some(1000)
        );
        assert_eq!(
            decoded.payload.get("genesis_hash").unwrap().as_str(),
            Some("abc123")
        );
    }

    #[test]
    fn test_block_announce_message() {
        let block = serde_json::json!({
            "index": 5,
            "hash": "blockhash",
            "previous_hash": "prevhash",
            "transactions": [{}, {}, {}]
        });
        let msg = msg_block_announce(&block);
        let bytes = msg.to_bytes().unwrap();
        let decoded = Message::from_bytes(&bytes).unwrap();
        assert_eq!(decoded.parsed_type(), Some(MessageType::BlockAnnounce));
        assert_eq!(
            decoded.payload.get("block_index").unwrap().as_u64(),
            Some(5)
        );
        assert_eq!(
            decoded.payload.get("hash").unwrap().as_str(),
            Some("blockhash")
        );
        // The full block body should be embedded too.
        assert!(decoded.payload.get("block").is_some());
    }

    #[test]
    fn test_peer_list_message() {
        let peers = vec![("10.0.0.1".into(), 5001), ("10.0.0.2".into(), 5002)];
        let msg = msg_peer_list(&peers);
        let bytes = msg.to_bytes().unwrap();
        let decoded = Message::from_bytes(&bytes).unwrap();
        let list = decoded.payload.get("peers").unwrap().as_array().unwrap();
        assert_eq!(list.len(), 2);
        assert_eq!(list[0]["host"].as_str(), Some("10.0.0.1"));
        assert_eq!(list[0]["port"].as_u64(), Some(5001));
    }

    #[test]
    fn test_public_release_has_no_default_http_bootstrap() {
        assert!(DEFAULT_BOOTSTRAP_URL.is_empty());
        assert!(bootstrap_url_disabled(""));
        assert!(bootstrap_url_disabled("none"));
        assert!(bootstrap_url_disabled("off"));
        assert!(bootstrap_url_disabled("disabled"));
        assert_eq!(SEED_NODES[0], ("seed1.repryntt.ai158z.com", 5001));
        assert_eq!(SEED_NODES[1], ("seed2.repryntt.ai158z.com", 5001));
    }

    #[tokio::test]
    async fn test_wire_framing_roundtrip() {
        // Create a TCP pair
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        let msg = msg_handshake("test_node", 100, "genesis_hash_hex", 3.14);

        let write_handle = tokio::spawn(async move {
            let mut stream = TcpStream::connect(addr).await.unwrap();
            write_message(&mut stream, &msg).await.unwrap();
        });

        let (mut stream, _) = listener.accept().await.unwrap();
        let received = read_message(&mut stream).await.unwrap().unwrap();

        write_handle.await.unwrap();

        assert_eq!(received.msg_type, "handshake");
        assert_eq!(received.node_id, "test_node");
        assert_eq!(
            received.payload.get("chain_height").unwrap().as_u64(),
            Some(100)
        );
    }
}
