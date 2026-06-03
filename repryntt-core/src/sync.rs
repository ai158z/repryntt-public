//! Block synchronization — IBD (Initial Block Download) and fork resolution.
//!
//! Matches Python's qnode2.py IBD logic:
//! - Wait for peer discovery, query peers for chain heights
//! - Download blocks in batches of 500 from the best-height peer
//! - Validate genesis hash, hash chain continuity, sequential indices
//! - Refuse forks; v1 does not perform automatic reorgs

use std::net::SocketAddr;
use std::sync::Arc;

use serde_json::Value;

use crate::block::Block;
use crate::chain::{Chain, requires_strict_consensus};
use crate::checkpoint::Checkpoint;
use crate::genesis::EXPECTED_GENESIS_HASH;
use crate::gossip::GossipNode;
use crate::network::Message;
use crate::peer::IBD_BATCH_SIZE;

// ── Constants ────────────────────────────────────────────────────────────────

/// Maximum peers to query for chain height during IBD.
pub const IBD_MAX_PEERS: usize = 8;

/// Delay before starting IBD to allow peer discovery (seconds).
pub const IBD_STARTUP_DELAY_SECS: u64 = 5;

/// Fork resolution check interval (seconds).
pub const FORK_CHECK_INTERVAL_SECS: u64 = 300;

/// Default maximum automatic reorg depth.
pub const DEFAULT_MAX_REORG_DEPTH: u64 = 32;

// ── Sync Error ───────────────────────────────────────────────────────────────

/// Errors during block synchronization.
#[derive(Debug, Clone)]
pub enum SyncError {
    /// No peers available.
    NoPeers,
    /// Peer's genesis hash doesn't match ours.
    GenesisMismatch(String),
    /// Chain validation failed.
    ChainValidation(String),
    /// Network/connection error.
    NetworkError(String),
}

impl std::fmt::Display for SyncError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NoPeers => write!(f, "No peers available for sync"),
            Self::GenesisMismatch(h) => write!(f, "Genesis mismatch: {}", h),
            Self::ChainValidation(msg) => write!(f, "Chain validation: {}", msg),
            Self::NetworkError(msg) => write!(f, "Network: {}", msg),
        }
    }
}

// ── Sync State ───────────────────────────────────────────────────────────────

/// Current synchronization state (for progress reporting).
#[derive(Debug, Clone, PartialEq)]
pub enum SyncState {
    Idle,
    QueryingPeers,
    Downloading { progress_pct: f64 },
    Validating,
    Complete,
    Failed(String),
}

#[derive(Debug, Clone)]
pub enum SyncOutcome {
    UpToDate,
    Append(Vec<Block>),
    Reorg {
        ancestor_height: u64,
        blocks: Vec<Block>,
    },
}

// ── Peer Height ──────────────────────────────────────────────────────────────

/// Chain height info received from a peer.
#[derive(Debug, Clone)]
pub struct PeerHeight {
    pub addr: SocketAddr,
    pub height: u64,
    pub genesis_hash: String,
    /// Peer's tip block hash at `height`. Empty if peer is on an older protocol
    /// that doesn't announce tip hash — callers must treat empty as "unknown"
    /// and fall back to height-only behavior for that peer.
    pub tip_hash: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct BlockHeader {
    pub index: u64,
    pub hash: String,
    pub previous_hash: String,
    pub timestamp: f64,
    pub miner_address: String,
    pub tx_count: u64,
}

// ── Sync Manager ─────────────────────────────────────────────────────────────

/// Orchestrates block synchronization with the P2P network.
pub struct SyncManager {
    gossip: Arc<GossipNode>,
    checkpoint: Option<Checkpoint>,
}

impl SyncManager {
    pub fn new(gossip: Arc<GossipNode>) -> Self {
        Self {
            gossip,
            checkpoint: None,
        }
    }

    pub fn with_checkpoint(mut self, checkpoint: Option<Checkpoint>) -> Self {
        self.checkpoint = checkpoint;
        self
    }

    /// Run the Initial Block Download.
    ///
    /// Queries peers, finds the best-height peer, downloads all missing blocks
    /// in batches. Returns new blocks to append (empty if already at tip).
    pub async fn initial_block_download(&self, local_height: u64) -> Result<Vec<Block>, SyncError> {
        let peer_heights = self.query_peer_heights().await;
        if peer_heights.is_empty() {
            return Err(SyncError::NoPeers);
        }

        let best = self.select_best_peer(&peer_heights)?;
        if best.height <= local_height {
            return Ok(vec![]);
        }

        println!(
            "📥 IBD: syncing {} → {} from {} ({} blocks)",
            local_height,
            best.height,
            best.addr,
            best.height - local_height
        );

        let blocks = self
            .download_batched(best.addr, local_height, best.height)
            .await?;

        if !blocks.is_empty() {
            validate_block_sequence(&blocks, local_height)?;
            validate_checkpoint_in_download(
                &blocks,
                local_height,
                best.height,
                self.checkpoint.as_ref(),
            )?;
            println!("✅ IBD: downloaded and validated {} blocks", blocks.len());
        }

        Ok(blocks)
    }

    /// Download blocks missing since our current tip.
    ///
    /// Also verifies the first downloaded block chains from `tip_hash`.
    pub async fn sync_to_tip(
        &self,
        local_height: u64,
        tip_hash: &str,
    ) -> Result<Vec<Block>, SyncError> {
        let peer_heights = self.query_peer_heights().await;
        if peer_heights.is_empty() {
            return Err(SyncError::NoPeers);
        }

        let best = self.select_best_peer(&peer_heights)?;
        if best.height <= local_height {
            return Ok(vec![]);
        }

        let blocks = self
            .download_batched(best.addr, local_height, best.height)
            .await?;

        if !blocks.is_empty() {
            if blocks[0].previous_hash != tip_hash {
                return Err(SyncError::ChainValidation(
                    "Downloaded blocks don't chain from our tip (possible fork)".into(),
                ));
            }
            validate_block_sequence(&blocks, local_height)?;
            validate_checkpoint_in_download(
                &blocks,
                local_height,
                best.height,
                self.checkpoint.as_ref(),
            )?;
        }

        Ok(blocks)
    }

    pub async fn sync_chain(&self, local_chain: &Chain) -> Result<SyncOutcome, SyncError> {
        let local_height = local_chain.height();
        let tip_hash = local_chain.latest_block().hash.clone();
        let peer_heights = self.query_peer_heights().await;
        if peer_heights.is_empty() {
            return Err(SyncError::NoPeers);
        }

        let best = self.select_best_peer(&peer_heights)?;

        // Same-height-different-tip means a fork at our current tip. The peer
        // isn't strictly "ahead" by height, but its chain disagrees with ours,
        // so a height-only check would silently let two producers run parallel
        // chains. Trigger bounded reorg to find the common ancestor and decide.
        //
        // We require the peer's tip_hash to be non-empty (pre-patch peers send
        // empty) — for unknown-tip peers, fall back to height-only behavior.
        if best.height == local_height && !best.tip_hash.is_empty() && best.tip_hash != tip_hash {
            return self
                .resolve_bounded_reorg(local_chain, best.addr, best.height)
                .await;
        }

        if best.height <= local_height {
            return Ok(SyncOutcome::UpToDate);
        }

        let blocks = self
            .download_batched(best.addr, local_height, best.height)
            .await?;

        if blocks.is_empty() {
            return Ok(SyncOutcome::UpToDate);
        }

        if blocks[0].previous_hash == tip_hash {
            validate_block_sequence(&blocks, local_height)?;
            validate_checkpoint_in_download(
                &blocks,
                local_height,
                best.height,
                self.checkpoint.as_ref(),
            )?;
            return Ok(SyncOutcome::Append(blocks));
        }

        self.resolve_bounded_reorg(local_chain, best.addr, best.height)
            .await
    }

    async fn resolve_bounded_reorg(
        &self,
        local_chain: &Chain,
        peer: SocketAddr,
        peer_height: u64,
    ) -> Result<SyncOutcome, SyncError> {
        let max_depth = std::env::var("REPRYNTT_MAX_REORG_DEPTH")
            .ok()
            .and_then(|v| v.parse::<u64>().ok())
            .unwrap_or(DEFAULT_MAX_REORG_DEPTH);
        let locator = build_block_locator(local_chain);
        let header_limit = peer_height
            .saturating_sub(local_chain.height())
            .saturating_add(max_depth)
            .saturating_add(8)
            .max(64);
        let headers = self.request_headers(peer, &locator, header_limit).await?;
        let first = headers.first().ok_or_else(|| {
            SyncError::ChainValidation("peer returned no headers for fork recovery".into())
        })?;
        let ancestor_hash = first.previous_hash.clone();
        // Common-ancestor search is bounded to the recent in-memory window.
        // A reorg deeper than ~1024 blocks is rejected here AND by the
        // max_depth check below; both safeguards now coincide.
        let ancestor_index = local_chain
            .recent_iter()
            .find(|b| b.hash == ancestor_hash)
            .map(|b| b.index)
            .ok_or_else(|| {
                SyncError::ChainValidation(
                    "no common ancestor found within recent window".into(),
                )
            })?;

        let local_height = local_chain.height();
        let reorg_depth = local_height.saturating_sub(ancestor_index + 1);
        if reorg_depth > max_depth {
            return Err(SyncError::ChainValidation(format!(
                "fork depth {} exceeds REPRYNTT_MAX_REORG_DEPTH={}",
                reorg_depth, max_depth
            )));
        }
        if let Some(cp) = self.checkpoint.as_ref() {
            if cp.height > ancestor_index {
                return Err(SyncError::ChainValidation(format!(
                    "fork would reorg past verified checkpoint at height {}",
                    cp.height
                )));
            }
        }

        let start = ancestor_index + 1;
        let branch = self.download_batched(peer, start, peer_height).await?;
        if branch.is_empty() {
            return Ok(SyncOutcome::UpToDate);
        }
        if branch[0].previous_hash != ancestor_hash {
            return Err(SyncError::ChainValidation(
                "candidate branch does not connect to common ancestor".into(),
            ));
        }
        validate_block_sequence(&branch, start)?;
        validate_checkpoint_in_download(&branch, start, peer_height, self.checkpoint.as_ref())?;

        // Build the candidate chain by replaying common ancestor's prefix
        // from the in-memory window. If the recent window doesn't cover
        // all of [genesis..=ancestor_index], the reorg can't be validated
        // without disk reads — refuse and let the caller resync from peers.
        let oldest_in_recent = local_chain
            .recent
            .front()
            .ok_or_else(|| SyncError::ChainValidation("empty recent window".into()))?;
        if oldest_in_recent.index > 0 {
            // Recent doesn't reach back to genesis — old fork-resolution
            // would require disk reads. Refuse safely; sync caller will
            // retry from peers (or full IBD on a fresh node).
            return Err(SyncError::ChainValidation(
                "fork resolution requires history older than the in-memory recent window".into(),
            ));
        }
        let mut candidate: Vec<Block> = local_chain
            .recent_iter()
            .take_while(|b| b.index <= ancestor_index)
            .cloned()
            .collect();
        candidate.extend(branch);
        if (candidate.len() as u64) <= local_chain.height() {
            return Ok(SyncOutcome::UpToDate);
        }
        let rebuilt = Chain::from_blocks(candidate.clone()).map_err(SyncError::ChainValidation)?;
        rebuilt
            .validate_full()
            .map_err(SyncError::ChainValidation)?;

        Ok(SyncOutcome::Reorg {
            ancestor_height: ancestor_index,
            blocks: candidate,
        })
    }

    /// Check for forks.
    ///
    /// v1 intentionally refuses automatic reorgs. If the peer's next block does
    /// not extend our local tip, sync halts and the operator must recover from
    /// a known-good checkpoint/snapshot.
    pub async fn resolve_forks(
        &self,
        local_height: u64,
        tip_hash: &str,
    ) -> Result<Option<Vec<Block>>, SyncError> {
        let peer_heights = self.query_peer_heights().await;
        if peer_heights.is_empty() {
            return Ok(None);
        }

        let best = match self.select_best_peer(&peer_heights) {
            Ok(p) if p.height > local_height => p,
            _ => return Ok(None),
        };

        let new_blocks = self
            .download_batched(best.addr, local_height, best.height)
            .await?;

        if new_blocks.is_empty() {
            return Ok(None);
        }

        // No fork — blocks chain from our tip
        if new_blocks[0].previous_hash == tip_hash {
            return Ok(None); // Caller should use sync_to_tip instead
        }

        Err(SyncError::ChainValidation(format!(
            "Fork detected at height {}; automatic reorgs are disabled",
            local_height
        )))
    }

    // ── Internal ─────────────────────────────────────────────────────────

    /// Query up to IBD_MAX_PEERS for their chain height.
    pub async fn query_peer_heights(&self) -> Vec<PeerHeight> {
        let addrs: Vec<SocketAddr> = {
            let peers = self.gossip.peers.lock().await;
            peers
                .peers
                .values()
                .filter(|p| p.is_alive())
                .take(IBD_MAX_PEERS)
                .map(|p| p.addr)
                .collect()
        };

        let mut results = Vec::new();
        for addr in addrs {
            if let Ok((height, genesis_hash, tip_hash)) =
                self.gossip.request_chain_height(addr).await
            {
                results.push(PeerHeight {
                    addr,
                    height,
                    genesis_hash,
                    tip_hash,
                });
            }
        }
        results
    }

    /// Select the best peer: highest height with matching genesis.
    fn select_best_peer(&self, peers: &[PeerHeight]) -> Result<PeerHeight, SyncError> {
        peers
            .iter()
            .filter(|p| p.genesis_hash == EXPECTED_GENESIS_HASH)
            .max_by_key(|p| p.height)
            .cloned()
            .ok_or_else(|| {
                SyncError::GenesisMismatch(
                    peers
                        .first()
                        .map(|p| p.genesis_hash.clone())
                        .unwrap_or_default(),
                )
            })
    }

    pub async fn request_headers(
        &self,
        peer: SocketAddr,
        locator: &[String],
        limit: u64,
    ) -> Result<Vec<BlockHeader>, SyncError> {
        let msg = self
            .gossip
            .request_headers(peer, locator, None, limit)
            .await
            .map_err(SyncError::NetworkError)?;
        parse_headers_response(&msg)
    }

    /// Download blocks [start, end) in IBD_BATCH_SIZE batches.
    async fn download_batched(
        &self,
        peer: SocketAddr,
        start: u64,
        end: u64,
    ) -> Result<Vec<Block>, SyncError> {
        let mut all_blocks = Vec::new();
        let mut cursor = start;

        while cursor < end {
            let batch_end = (cursor + IBD_BATCH_SIZE).min(end);

            let raw = self
                .gossip
                .request_block_range(peer, cursor, batch_end)
                .await
                .map_err(SyncError::NetworkError)?;

            let batch = parse_blocks_response(&raw)?;
            if batch.is_empty() {
                break;
            }

            let count = batch.len() as u64;
            all_blocks.extend(batch);
            cursor += count;

            // Progress logging for multi-batch downloads
            if end - start > IBD_BATCH_SIZE {
                let total = end - start;
                let done = cursor - start;
                println!(
                    "  📦 {}/{} blocks ({:.0}%)",
                    done,
                    total,
                    done as f64 / total as f64 * 100.0
                );
            }
        }

        Ok(all_blocks)
    }
}

/// Build a Bitcoin-style block locator: exponentially-spaced block hashes
/// from tip back toward genesis. With the bounded recent window we draw
/// the locator from `chain.recent` (last ~1024 blocks) and always append
/// the canonical genesis hash at the end.
pub fn build_block_locator(chain: &Chain) -> Vec<String> {
    let mut locator = Vec::new();
    let recent_len = chain.recent.len();
    if recent_len == 0 {
        return locator;
    }

    let mut index = recent_len - 1;
    let mut step = 1usize;
    loop {
        locator.push(chain.recent[index].hash.clone());
        if index == 0 {
            break;
        }
        index = index.saturating_sub(step);
        if locator.len() > 10 {
            step = (step * 2).min(1024);
        }
    }

    let genesis_hash = chain.genesis.hash.clone();
    if locator.last() != Some(&genesis_hash) {
        locator.push(genesis_hash);
    }
    locator
}

// ── Validation ───────────────────────────────────────────────────────────────

/// Validate a sequence of downloaded blocks.
///
/// Checks sequential indices, prev_hash linkage, and strict-era hashes.
/// Pre-activation history still tolerates legacy JSON float roundtrip hashes;
/// strict-era blocks must rehash exactly.
pub fn validate_block_sequence(blocks: &[Block], expected_start: u64) -> Result<(), SyncError> {
    for (i, block) in blocks.iter().enumerate() {
        let expected_index = expected_start + i as u64;
        if block.index != expected_index {
            return Err(SyncError::ChainValidation(format!(
                "Index gap: expected {}, got {}",
                expected_index, block.index
            )));
        }

        if i > 0 && block.previous_hash != blocks[i - 1].hash {
            return Err(SyncError::ChainValidation(format!(
                "Block {} prev_hash doesn't link to block {}",
                block.index,
                blocks[i - 1].index
            )));
        }

        if requires_strict_consensus(block.index) {
            Chain::validate_block_hash(block).map_err(SyncError::ChainValidation)?;
        }
    }
    Ok(())
}

pub fn validate_checkpoint_in_download(
    blocks: &[Block],
    start_index: u64,
    peer_height: u64,
    checkpoint: Option<&Checkpoint>,
) -> Result<(), SyncError> {
    let Some(cp) = checkpoint else {
        return Ok(());
    };
    if cp.height == 0 {
        return Err(SyncError::ChainValidation(
            "checkpoint height must be >= 1".into(),
        ));
    }
    if cp.height <= start_index || cp.height > peer_height {
        return Ok(());
    }

    let checkpoint_index = cp.height - 1;
    let Some(offset) = checkpoint_index.checked_sub(start_index) else {
        return Ok(());
    };
    let Some(block) = blocks.get(offset as usize) else {
        return Err(SyncError::ChainValidation(format!(
            "peer did not serve checkpoint block at height {}",
            cp.height
        )));
    };
    if block.hash != cp.hash {
        return Err(SyncError::ChainValidation(format!(
            "peer checkpoint mismatch at height {}: {} != {}",
            cp.height, block.hash, cp.hash
        )));
    }
    Ok(())
}

pub fn parse_headers_response(msg: &Message) -> Result<Vec<BlockHeader>, SyncError> {
    if msg.parsed_type() != Some(crate::network::MessageType::Headers) {
        return Err(SyncError::NetworkError("expected headers response".into()));
    }
    let genesis = msg
        .payload
        .get("genesis_hash")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if genesis != EXPECTED_GENESIS_HASH {
        return Err(SyncError::GenesisMismatch(genesis.to_string()));
    }

    let headers = msg
        .payload
        .get("headers")
        .and_then(|v| v.as_array())
        .ok_or_else(|| SyncError::NetworkError("headers response missing headers".into()))?;

    headers
        .iter()
        .map(parse_header_value)
        .collect::<Result<Vec<_>, _>>()
}

fn parse_header_value(value: &Value) -> Result<BlockHeader, SyncError> {
    Ok(BlockHeader {
        index: value
            .get("index")
            .and_then(|v| v.as_u64())
            .ok_or_else(|| SyncError::NetworkError("header missing index".into()))?,
        hash: value
            .get("hash")
            .and_then(|v| v.as_str())
            .ok_or_else(|| SyncError::NetworkError("header missing hash".into()))?
            .to_string(),
        previous_hash: value
            .get("previous_hash")
            .and_then(|v| v.as_str())
            .ok_or_else(|| SyncError::NetworkError("header missing previous_hash".into()))?
            .to_string(),
        timestamp: value
            .get("timestamp")
            .and_then(|v| v.as_f64())
            .ok_or_else(|| SyncError::NetworkError("header missing timestamp".into()))?,
        miner_address: value
            .get("miner_address")
            .and_then(|v| v.as_str())
            .ok_or_else(|| SyncError::NetworkError("header missing miner_address".into()))?
            .to_string(),
        tx_count: value
            .get("tx_count")
            .and_then(|v| v.as_u64())
            .ok_or_else(|| SyncError::NetworkError("header missing tx_count".into()))?,
    })
}

/// Parse raw bytes from a blocks response into Block objects.
pub fn parse_blocks_response(data: &[u8]) -> Result<Vec<Block>, SyncError> {
    let msg = Message::from_bytes(data)
        .map_err(|e| SyncError::NetworkError(format!("JSON parse: {}", e)))?;

    let arr = msg
        .payload
        .get("blocks")
        .and_then(|v| v.as_array())
        .ok_or_else(|| SyncError::NetworkError("Missing 'blocks' array".into()))?;

    let mut blocks = Vec::with_capacity(arr.len());
    for (i, val) in arr.iter().enumerate() {
        let map: std::collections::BTreeMap<String, serde_json::Value> =
            serde_json::from_value(val.clone())
                .map_err(|e| SyncError::NetworkError(format!("Block {}: {}", i, e)))?;
        let block = Block::from_dict(&map)
            .ok_or_else(|| SyncError::NetworkError(format!("Block {} from_dict failed", i)))?;
        blocks.push(block);
    }

    Ok(blocks)
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::chain::Chain;
    use crate::checkpoint::Checkpoint;
    use crate::crypto;
    use crate::genesis::{self, EXPECTED_GENESIS_HASH};
    use crate::gossip::GossipNode;
    use crate::network::{self, MessageType};
    use crate::peer::PeerInfo;
    use crate::producer::{BlockProducer, NodeConfig};
    use serde_json::Value;
    use std::collections::BTreeMap;
    use std::sync::Arc;
    use tokio::net::TcpListener;

    /// Produce a test chain of genesis + n blocks.
    fn make_test_blocks(n: usize) -> Vec<Block> {
        let config = NodeConfig {
            address: genesis::GENESIS_CREATOR.to_string(),
            measured_tflops: 5.0,
            compute_share: 1.0,
            tflops: 5.0,
            mining_enabled: true,
        };
        let mut producer = BlockProducer::new(config);
        producer.ibd_complete = true;
        for _ in 0..n {
            producer.try_produce_block();
        }
        producer.chain.recent_as_vec()
    }

    fn signed_checkpoint(height: u64, hash: &str) -> Checkpoint {
        let (sk, pk) = crypto::generate_keypair();
        let address = crypto::address_from_pubkey(&pk);
        let message = Checkpoint::expected_message(crate::transaction::CHAIN_ID, height, hash);
        Checkpoint {
            chain_id: crate::transaction::CHAIN_ID.to_string(),
            height,
            hash: hash.to_string(),
            signer_address: address,
            public_key: hex::encode(pk),
            signature: hex::encode(crypto::sign(message.as_bytes(), &sk)),
            message,
        }
    }

    /// Serialize blocks into a wire-format response (JSON bytes).
    fn serialize_blocks_response(blocks: &[Block]) -> Vec<u8> {
        let block_jsons: Vec<Value> = blocks
            .iter()
            .map(|b| serde_json::to_value(b.to_dict()).unwrap())
            .collect();
        let mut payload = BTreeMap::new();
        payload.insert("success".into(), Value::Bool(true));
        payload.insert("blocks".into(), Value::Array(block_jsons));
        let msg = Message::new(MessageType::Blocks, payload);
        msg.to_bytes().unwrap()
    }

    // ── validate_block_sequence ──────────────────────────────────────────

    #[test]
    fn test_validate_sequence_valid() {
        let blocks = make_test_blocks(5);
        validate_block_sequence(&blocks[1..], 1).unwrap();
    }

    #[test]
    fn test_validate_sequence_index_gap() {
        let blocks = make_test_blocks(3);
        let mut slice = blocks[1..].to_vec();
        slice[1] = Block {
            index: 99,
            ..slice[1].clone()
        };
        let err = validate_block_sequence(&slice, 1).unwrap_err();
        assert!(err.to_string().contains("Index gap"));
    }

    #[test]
    fn test_validate_sequence_hash_mismatch() {
        // Hash recomputation no longer done (aarch64 float precision),
        // but tampering still caught via prev_hash chain break
        let blocks = make_test_blocks(3);
        let mut slice = blocks[1..].to_vec();
        slice[0].hash = "tampered".into();
        // This causes block[1].prev_hash != blocks[0].hash (which is now "tampered")
        // The NEXT block's prev_hash won't match
        let err = validate_block_sequence(&slice, 1).unwrap_err();
        assert!(err.to_string().contains("prev_hash"));
    }

    #[test]
    fn test_validate_sequence_broken_link() {
        let blocks = make_test_blocks(4);
        let mut slice = blocks[1..].to_vec();
        // Tamper block[2]'s prev_hash — directly caught by link check
        slice[1].previous_hash = "broken".into();
        let err = validate_block_sequence(&slice, 1).unwrap_err();
        assert!(err.to_string().contains("prev_hash"));
    }

    #[test]
    fn test_validate_checkpoint_in_download_rejects_mismatch() {
        let blocks = make_test_blocks(4);
        let cp = signed_checkpoint(3, "not-the-canonical-hash");
        let err = validate_checkpoint_in_download(&blocks[1..], 1, blocks.len() as u64, Some(&cp))
            .unwrap_err();
        assert!(err.to_string().contains("checkpoint mismatch"));
    }

    #[test]
    fn test_validate_checkpoint_in_download_accepts_matching_block() {
        let blocks = make_test_blocks(4);
        let cp = signed_checkpoint(3, &blocks[2].hash);
        validate_checkpoint_in_download(&blocks[1..], 1, blocks.len() as u64, Some(&cp)).unwrap();
    }

    // ── parse_blocks_response ────────────────────────────────────────────

    #[test]
    fn test_parse_blocks_response_valid() {
        let blocks = make_test_blocks(3);
        let data = serialize_blocks_response(&blocks);
        let parsed = parse_blocks_response(&data).unwrap();
        assert_eq!(parsed.len(), blocks.len());
        for (a, b) in parsed.iter().zip(blocks.iter()) {
            assert_eq!(a.hash, b.hash);
        }
    }

    #[test]
    fn test_parse_blocks_response_empty() {
        let data = serialize_blocks_response(&[]);
        assert!(parse_blocks_response(&data).unwrap().is_empty());
    }

    // ── Chain::from_blocks ───────────────────────────────────────────────

    #[test]
    fn test_chain_from_blocks() {
        let blocks = make_test_blocks(5);
        let chain = Chain::from_blocks(blocks).unwrap();
        assert_eq!(chain.height(), 6);
        chain.validate_full().unwrap();
        let bal = chain
            .balances
            .get(genesis::GENESIS_CREATOR)
            .copied()
            .unwrap_or(0);
        assert!(bal > 0, "Genesis creator should have balance from mining");
    }

    #[test]
    fn test_chain_from_blocks_genesis_mismatch() {
        let mut blocks = make_test_blocks(1);
        blocks[0].hash = "wrong_genesis".into();
        assert!(Chain::from_blocks(blocks).is_err());
    }

    // ── select_best_peer ─────────────────────────────────────────────────

    #[test]
    fn test_select_best_peer() {
        let gossip = Arc::new(GossipNode::new("test", 0, vec![]));
        let sync = SyncManager::new(gossip);

        let peers = vec![
            PeerHeight {
                addr: "10.0.0.1:5001".parse().unwrap(),
                height: 50,
                genesis_hash: EXPECTED_GENESIS_HASH.to_string(),
                tip_hash: String::new(),
            },
            PeerHeight {
                addr: "10.0.0.2:5001".parse().unwrap(),
                height: 200,
                genesis_hash: EXPECTED_GENESIS_HASH.to_string(),
                tip_hash: String::new(),
            },
            PeerHeight {
                addr: "10.0.0.3:5001".parse().unwrap(),
                height: 999,
                genesis_hash: "wrong_genesis".to_string(),
                tip_hash: String::new(),
            },
        ];

        let best = sync.select_best_peer(&peers).unwrap();
        assert_eq!(best.height, 200); // 999 filtered out: wrong genesis
    }

    #[test]
    fn test_select_best_peer_all_wrong_genesis() {
        let gossip = Arc::new(GossipNode::new("test", 0, vec![]));
        let sync = SyncManager::new(gossip);

        let peers = vec![PeerHeight {
            addr: "10.0.0.1:5001".parse().unwrap(),
            height: 100,
            genesis_hash: "wrong".into(),
            tip_hash: String::new(),
        }];

        assert!(matches!(
            sync.select_best_peer(&peers),
            Err(SyncError::GenesisMismatch(_))
        ));
    }

    // ── TCP integration tests ────────────────────────────────────────────

    #[tokio::test]
    async fn test_ibd_full_flow() {
        let all_blocks = make_test_blocks(5); // genesis + 5 = height 6
        let target_height = all_blocks.len() as u64;
        let server_blocks = all_blocks.clone();

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        let server = tokio::spawn(async move {
            // Connection 1: chain height query
            let (mut stream, _) = listener.accept().await.unwrap();
            let msg = network::read_message(&mut stream).await.unwrap().unwrap();
            assert_eq!(msg.parsed_type(), Some(MessageType::GetChainHeight));
            let resp = network::msg_chain_height(target_height, EXPECTED_GENESIS_HASH, "");
            network::write_message(&mut stream, &resp).await.unwrap();
            drop(stream);

            // Connection 2: block download
            let (mut stream, _) = listener.accept().await.unwrap();
            let msg = network::read_message(&mut stream).await.unwrap().unwrap();
            assert_eq!(msg.parsed_type(), Some(MessageType::GetBlocks));
            let start = msg
                .payload
                .get("start")
                .and_then(|v| v.as_u64())
                .unwrap_or(0) as usize;
            let end = msg.payload.get("end").and_then(|v| v.as_u64()).unwrap_or(0) as usize;

            let data =
                serialize_blocks_response(&server_blocks[start..end.min(server_blocks.len())]);
            network::write_raw(&mut stream, &data).await.unwrap();
        });

        let gossip = Arc::new(GossipNode::new("ibd_node", 0, vec![]));
        {
            let peer = PeerInfo::new("mock_peer".into(), addr, true);
            gossip.peers.lock().await.add_peer(peer);
        }

        let sync_mgr = SyncManager::new(gossip);
        let new_blocks = sync_mgr.initial_block_download(1).await.unwrap();

        server.await.unwrap();

        // Should have 5 new blocks (indices 1–5)
        assert_eq!(new_blocks.len(), 5);
        assert_eq!(new_blocks[0].index, 1);
        assert_eq!(new_blocks[4].index, 5);

        // Verify they form a valid chain when appended
        let mut chain = Chain::new();
        chain.add_blocks(new_blocks).unwrap();
        assert_eq!(chain.height(), target_height);
        chain.validate_full().unwrap();
    }

    #[tokio::test]
    async fn test_sync_to_tip() {
        let all_blocks = make_test_blocks(5);
        let target_height = all_blocks.len() as u64;
        let server_blocks = all_blocks.clone();
        let local_height = 3u64;
        let local_tip_hash = all_blocks[2].hash.clone();

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        let server = tokio::spawn(async move {
            // Height query
            let (mut stream, _) = listener.accept().await.unwrap();
            let _msg = network::read_message(&mut stream).await.unwrap().unwrap();
            let resp = network::msg_chain_height(target_height, EXPECTED_GENESIS_HASH, "");
            network::write_message(&mut stream, &resp).await.unwrap();
            drop(stream);

            // Block download
            let (mut stream, _) = listener.accept().await.unwrap();
            let msg = network::read_message(&mut stream).await.unwrap().unwrap();
            let start = msg
                .payload
                .get("start")
                .and_then(|v| v.as_u64())
                .unwrap_or(0) as usize;
            let end = msg.payload.get("end").and_then(|v| v.as_u64()).unwrap_or(0) as usize;

            let data =
                serialize_blocks_response(&server_blocks[start..end.min(server_blocks.len())]);
            network::write_raw(&mut stream, &data).await.unwrap();
        });

        let gossip = Arc::new(GossipNode::new("sync_node", 0, vec![]));
        gossip
            .peers
            .lock()
            .await
            .add_peer(PeerInfo::new("mock".into(), addr, true));

        let sync_mgr = SyncManager::new(gossip);
        let new_blocks = sync_mgr
            .sync_to_tip(local_height, &local_tip_hash)
            .await
            .unwrap();

        server.await.unwrap();

        assert_eq!(new_blocks.len(), 3); // blocks 3, 4, 5
        assert_eq!(new_blocks[0].index, 3);
        assert_eq!(new_blocks[0].previous_hash, local_tip_hash);
    }

    #[tokio::test]
    async fn test_ibd_no_peers() {
        let gossip = Arc::new(GossipNode::new("lonely", 0, vec![]));
        let sync_mgr = SyncManager::new(gossip);
        assert!(matches!(
            sync_mgr.initial_block_download(1).await,
            Err(SyncError::NoPeers)
        ));
    }

    #[tokio::test]
    async fn test_ibd_already_at_tip() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let _msg = network::read_message(&mut stream).await.unwrap();
            let resp = network::msg_chain_height(5, EXPECTED_GENESIS_HASH, "");
            network::write_message(&mut stream, &resp).await.unwrap();
        });

        let gossip = Arc::new(GossipNode::new("synced", 0, vec![]));
        gossip
            .peers
            .lock()
            .await
            .add_peer(PeerInfo::new("mock".into(), addr, true));

        let sync_mgr = SyncManager::new(gossip);
        let blocks = sync_mgr.initial_block_download(5).await.unwrap();

        server.await.unwrap();
        assert!(blocks.is_empty());
    }
}
