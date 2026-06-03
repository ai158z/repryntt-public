//! P2P Economy Bridge — connects the compute mesh to the blockchain.
//!
//! Ports Python's `p2p_economy_bridge.py`.  This module provides:
//!
//! - **Compute peer discovery**: track GPU-capable peers, honour 5-min TTL,
//!   reputation scoring.
//! - **Workload dispatch**: accept a workload, select the best remote miner,
//!   route via P2P, timeout handling.
//! - **Remote mining handler**: receive a workload request, generate PoP proof.
//! - **PoP verification + reward minting**: validate proof, credit miner.
//! - **Block gossip**: announce new blocks, request missing blocks, integrate.
//! - **Economy status / network compute summary**.
//!
//! The module is data-structure-first (no live sockets) so that all logic is
//! testable without a running TCP stack.

use std::collections::{BTreeMap, HashMap};

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::chain::Chain;
use crate::network::MessageType;
use crate::pop::{DeviceInfo, ProofOfPower, calculate_reward};

// ── Constants ────────────────────────────────────────────────────────────────

/// How long a compute announce is fresh (seconds).
pub const ANNOUNCE_TTL_SECS: f64 = 300.0;

/// How long before a stale peer is garbage-collected (seconds).
pub const STALE_PEER_SECS: f64 = 600.0;

/// Default claim timeout (seconds): miner must finish within this.
pub const CLAIM_TIMEOUT_SECS: f64 = 120.0;

/// Default pending timeout: workload must be claimed within this.
pub const PENDING_TIMEOUT_SECS: f64 = 300.0;

/// Reputation floor: below this, a peer is not eligible for work.
pub const MIN_REPUTATION: f64 = 0.3;

/// Default fee in plancks (0.01 CR).
pub const DEFAULT_FEE_PLANCKS: i64 = 1_000_000;

/// Maximum concurrent remote workloads.
pub const MAX_CONCURRENT_REMOTE: usize = 2;

// ── Compute Peer ─────────────────────────────────────────────────────────────

/// A remote peer that has announced GPU compute availability.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ComputePeer {
    pub node_id: String,
    pub wallet_address: String,
    pub tflops: f64,
    pub device_type: String,
    pub device_name: String,
    pub vram_mb: u64,
    pub available: bool,
    pub active_workloads: usize,
    pub max_concurrent: usize,
    pub last_announce: f64,
    pub reputation: f64,
    pub total_completed: u64,
    pub total_failed: u64,
}

impl ComputePeer {
    pub fn new(node_id: &str, wallet: &str, tflops: f64) -> Self {
        Self {
            node_id: node_id.to_string(),
            wallet_address: wallet.to_string(),
            tflops,
            device_type: "cpu".into(),
            device_name: "Unknown".into(),
            vram_mb: 0,
            available: true,
            active_workloads: 0,
            max_concurrent: 1,
            last_announce: 0.0,
            reputation: 1.0,
            total_completed: 0,
            total_failed: 0,
        }
    }

    /// Is the last announce within the TTL?
    pub fn is_fresh(&self, now: f64) -> bool {
        (now - self.last_announce) < ANNOUNCE_TTL_SECS
    }

    /// Can this peer accept a new workload right now?
    pub fn is_available(&self, now: f64) -> bool {
        self.available
            && self.is_fresh(now)
            && self.active_workloads < self.max_concurrent
            && self.reputation > MIN_REPUTATION
    }

    /// Scoring metric: higher is better for miner selection.
    pub fn score(&self) -> f64 {
        self.tflops * self.reputation
    }

    /// Penalise: multiply reputation by `factor` (e.g. 0.8 for 20% cut).
    pub fn penalise(&mut self, factor: f64) {
        self.reputation = (self.reputation * factor).clamp(0.0, 1.0);
        self.total_failed += 1;
    }

    /// Reward a successful completion: +0.05 reputation (capped at 1.0).
    pub fn reward_success(&mut self) {
        self.reputation = (self.reputation + 0.05).min(1.0);
        self.total_completed += 1;
    }

    pub fn to_dict(&self) -> Value {
        serde_json::json!({
            "node_id": self.node_id,
            "wallet_address": self.wallet_address,
            "tflops": self.tflops,
            "device_type": self.device_type,
            "device_name": self.device_name,
            "vram_mb": self.vram_mb,
            "available": self.available,
            "active_workloads": self.active_workloads,
            "max_concurrent": self.max_concurrent,
            "last_announce": self.last_announce,
            "reputation": self.reputation,
            "total_completed": self.total_completed,
            "total_failed": self.total_failed,
        })
    }
}

// ── Pending Workload ─────────────────────────────────────────────────────────

/// Workload lifecycle: Pending → Claimed → Completed | Failed | Expired.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WorkloadStatus {
    Pending,
    Claimed,
    Completed,
    Failed,
    Expired,
}

/// A workload submitted to the P2P network for remote mining.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PendingWorkload {
    pub workload_id: String,
    pub workload_key: String,
    pub submitter_node: String,
    pub submitter_wallet: String,
    pub workload_data: Value,
    pub workload_type: String,
    pub status: WorkloadStatus,
    pub claimed_by: Option<String>,
    pub result: Option<Value>,
    pub created_at: f64,
    pub claimed_at: f64,
    pub completed_at: f64,
    pub claim_timeout: f64,
    pub fee_plancks: i64,
}

impl PendingWorkload {
    pub fn new(
        workload_id: &str,
        workload_key: &str,
        submitter_node: &str,
        submitter_wallet: &str,
        workload_data: Value,
        workload_type: &str,
        fee_plancks: i64,
        now: f64,
    ) -> Self {
        Self {
            workload_id: workload_id.to_string(),
            workload_key: workload_key.to_string(),
            submitter_node: submitter_node.to_string(),
            submitter_wallet: submitter_wallet.to_string(),
            workload_data,
            workload_type: workload_type.to_string(),
            status: WorkloadStatus::Pending,
            claimed_by: None,
            result: None,
            created_at: now,
            claimed_at: 0.0,
            completed_at: 0.0,
            claim_timeout: CLAIM_TIMEOUT_SECS,
            fee_plancks,
        }
    }

    /// Has this workload exceeded its timeout?
    pub fn is_expired(&self, now: f64) -> bool {
        match self.status {
            WorkloadStatus::Claimed if self.claimed_at > 0.0 => {
                (now - self.claimed_at) > self.claim_timeout
            }
            WorkloadStatus::Pending => (now - self.created_at) > PENDING_TIMEOUT_SECS,
            _ => false,
        }
    }
}

// ── Completed Result ─────────────────────────────────────────────────────────

/// A verified result from a remote miner, ready for the submitter to consume.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CompletedResult {
    pub workload_id: String,
    pub workload_key: String,
    pub miner_node: String,
    pub miner_wallet: String,
    pub result: Value,
    pub computation_time: f64,
    pub reward_plancks: i64,
}

// ── Economy Bridge Stats ─────────────────────────────────────────────────────

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct BridgeStats {
    pub workloads_dispatched: u64,
    pub workloads_received: u64,
    pub workloads_completed: u64,
    pub workloads_failed: u64,
    pub blocks_synced: u64,
    pub compute_peers_seen: u64,
    pub plancks_earned_remote: i64,
    pub plancks_paid_remote: i64,
}

// ── Outbound Message ─────────────────────────────────────────────────────────

/// An outbound message produced by the bridge, to be sent by the network layer.
#[derive(Debug, Clone)]
pub struct OutboundMessage {
    /// `None` = broadcast to all peers, `Some(id)` = unicast.
    pub target: Option<String>,
    pub msg_type: MessageType,
    pub payload: Value,
}

// ── Economy Bridge (data-structure core) ─────────────────────────────────────

/// The P2P Economy Bridge: stateful logic without live IO.
///
/// Callers feed inbound messages via `handle_*()` methods and collect
/// outbound messages from the returned `Vec<OutboundMessage>`.  This
/// keeps the bridge fully testable without sockets.
pub struct EconomyBridge {
    // Identity
    pub local_node_id: String,
    pub local_wallet: String,
    pub local_tflops: f64,
    pub local_device: DeviceInfo,

    // Peer tracking
    pub compute_peers: HashMap<String, ComputePeer>,

    // Workload tracking
    pub pending_workloads: HashMap<String, PendingWorkload>,
    pub completed_results: HashMap<String, CompletedResult>,

    // Configuration
    pub accept_remote_work: bool,
    pub max_remote_workloads: usize,
    pub active_remote_mining: usize,

    // Stats
    pub stats: BridgeStats,
}

impl EconomyBridge {
    pub fn new(node_id: &str, wallet: &str, device: DeviceInfo) -> Self {
        let tflops = device.effective_tflops();
        Self {
            local_node_id: node_id.to_string(),
            local_wallet: wallet.to_string(),
            local_tflops: tflops,
            local_device: device,
            compute_peers: HashMap::new(),
            pending_workloads: HashMap::new(),
            completed_results: HashMap::new(),
            accept_remote_work: true,
            max_remote_workloads: MAX_CONCURRENT_REMOTE,
            active_remote_mining: 0,
            stats: BridgeStats::default(),
        }
    }

    // ── Compute announce (inbound) ──────────────────────────────────────────

    /// Handle a compute announce from a remote peer.
    pub fn handle_compute_announce(&mut self, sender_id: &str, payload: &Value, now: f64) {
        let wallet = payload["wallet_address"].as_str().unwrap_or("").to_string();
        let tflops = payload["tflops"].as_f64().unwrap_or(0.1);

        if let Some(peer) = self.compute_peers.get_mut(sender_id) {
            peer.tflops = tflops;
            peer.available = payload["available"].as_bool().unwrap_or(true);
            peer.active_workloads = payload["active_workloads"].as_u64().unwrap_or(0) as usize;
            peer.last_announce = now;
            peer.device_name = payload["device_name"]
                .as_str()
                .unwrap_or(&peer.device_name)
                .to_string();
            peer.vram_mb = payload["vram_mb"].as_u64().unwrap_or(peer.vram_mb);
        } else {
            let peer = ComputePeer {
                node_id: sender_id.to_string(),
                wallet_address: wallet,
                tflops,
                device_type: payload["device_type"].as_str().unwrap_or("cpu").to_string(),
                device_name: payload["device_name"]
                    .as_str()
                    .unwrap_or("Unknown")
                    .to_string(),
                vram_mb: payload["vram_mb"].as_u64().unwrap_or(0),
                available: payload["available"].as_bool().unwrap_or(true),
                max_concurrent: payload["max_concurrent"].as_u64().unwrap_or(1) as usize,
                active_workloads: payload["active_workloads"].as_u64().unwrap_or(0) as usize,
                last_announce: now,
                reputation: payload["reputation"]
                    .as_f64()
                    .unwrap_or(1.0)
                    .clamp(0.0, 1.0),
                total_completed: 0,
                total_failed: 0,
            };
            self.compute_peers.insert(sender_id.to_string(), peer);
            self.stats.compute_peers_seen += 1;
        }
    }

    // ── Compute announce (outbound) ─────────────────────────────────────────

    /// Build a broadcast announce advertising our compute availability.
    pub fn build_compute_announce(&self, chain_height: u64, now: f64) -> OutboundMessage {
        let available =
            self.accept_remote_work && self.active_remote_mining < self.max_remote_workloads;
        let payload = serde_json::json!({
            "wallet_address": self.local_wallet,
            "tflops": self.local_tflops,
            "device_type": self.local_device.device_type,
            "device_name": self.local_device.device_name,
            "vram_mb": (self.local_device.memory_gb * 1024.0) as u64,
            "available": available,
            "max_concurrent": self.max_remote_workloads,
            "active_workloads": self.active_remote_mining,
            "reputation": 1.0,
            "blockchain_height": chain_height,
            "timestamp": now,
        });
        OutboundMessage {
            target: None,
            msg_type: MessageType::ComputeAnnounce,
            payload,
        }
    }

    // ── Workload dispatch (outbound) ────────────────────────────────────────

    /// Dispatch a workload to the P2P network.
    ///
    /// Returns `Some(workload_id)` if there are available miners, else `None`.
    pub fn dispatch_workload(
        &mut self,
        workload_key: &str,
        workload_data: Value,
        workload_type: &str,
        submitter_wallet: Option<&str>,
        fee_plancks: i64,
        now: f64,
    ) -> Option<(String, OutboundMessage)> {
        // Collect available peers (exclude ourselves)
        let mut available: Vec<&ComputePeer> = self
            .compute_peers
            .values()
            .filter(|p| p.is_available(now) && p.node_id != self.local_node_id)
            .collect();

        if available.is_empty() {
            return None;
        }

        // Sort by score descending (best miner first)
        available.sort_by(|a, b| {
            b.score()
                .partial_cmp(&a.score())
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        let workload_id = format!("wl_{:016x}", {
            use std::hash::{Hash, Hasher};
            let mut h = std::collections::hash_map::DefaultHasher::new();
            workload_key.hash(&mut h);
            now.to_bits().hash(&mut h);
            h.finish()
        });

        let wallet = submitter_wallet.unwrap_or(&self.local_wallet).to_string();

        let pending = PendingWorkload::new(
            &workload_id,
            workload_key,
            &self.local_node_id,
            &wallet,
            workload_data.clone(),
            workload_type,
            fee_plancks,
            now,
        );
        self.pending_workloads.insert(workload_id.clone(), pending);
        self.stats.workloads_dispatched += 1;

        let payload = serde_json::json!({
            "workload_id": workload_id,
            "workload_key": workload_key,
            "workload_type": workload_type,
            "workload_data": workload_data,
            "submitter_node": self.local_node_id,
            "submitter_wallet": wallet,
            "fee_plancks": fee_plancks,
            "required_tflops": 0.1,
            "deadline": now + PENDING_TIMEOUT_SECS,
        });

        let msg = OutboundMessage {
            target: None,
            msg_type: MessageType::ComputeRequest,
            payload,
        };

        Some((workload_id, msg))
    }

    // ── Compute request (inbound — we are a miner) ─────────────────────────

    /// Handle a workload request from a remote submitter.
    ///
    /// Returns `Some(OutboundMessage)` (a claim) if we accept the work.
    pub fn handle_compute_request(
        &mut self,
        sender_id: &str,
        payload: &Value,
        now: f64,
    ) -> Option<OutboundMessage> {
        if !self.accept_remote_work {
            return None;
        }
        if self.active_remote_mining >= self.max_remote_workloads {
            return None;
        }

        let deadline = payload["deadline"].as_f64().unwrap_or(0.0);
        if deadline > 0.0 && now > deadline {
            return None; // expired
        }

        let workload_id = payload["workload_id"].as_str().unwrap_or("");
        if workload_id.is_empty() {
            return None;
        }

        self.active_remote_mining += 1;
        self.stats.workloads_received += 1;

        let claim_payload = serde_json::json!({
            "workload_id": workload_id,
            "miner_node": self.local_node_id,
            "miner_wallet": self.local_wallet,
            "tflops": self.local_tflops,
        });

        Some(OutboundMessage {
            target: Some(sender_id.to_string()),
            msg_type: MessageType::ComputeClaim,
            payload: claim_payload,
        })
    }

    // ── Compute claim (inbound — we are the submitter) ──────────────────────

    /// A miner has claimed our workload.
    pub fn handle_compute_claim(&mut self, sender_id: &str, payload: &Value, now: f64) -> bool {
        let workload_id = payload["workload_id"].as_str().unwrap_or("");
        let wl = match self.pending_workloads.get_mut(workload_id) {
            Some(w) => w,
            None => return false,
        };
        if wl.status != WorkloadStatus::Pending {
            return false; // already claimed
        }

        wl.status = WorkloadStatus::Claimed;
        wl.claimed_by = Some(sender_id.to_string());
        wl.claimed_at = now;
        true
    }

    // ── Compute result (create — we are the miner) ──────────────────────────

    /// Build a result message after completing a remote workload.
    ///
    /// The caller is responsible for actually running the LLM; this method
    /// packages the result + PoP proof.
    pub fn build_compute_result(
        &mut self,
        submitter_id: &str,
        workload_id: &str,
        workload_key: &str,
        workload_data: &str,
        result: &str,
        computation_time: f64,
        hw_attestation: &str,
    ) -> OutboundMessage {
        let proof = ProofOfPower::generate(
            workload_key,
            workload_data,
            result,
            &self.local_wallet,
            computation_time,
            "deterministic",
            &self.local_device,
            hw_attestation,
        );

        self.active_remote_mining = self.active_remote_mining.saturating_sub(1);
        self.stats.workloads_completed += 1;

        let payload = serde_json::json!({
            "workload_id": workload_id,
            "workload_key": workload_key,
            "miner_node": self.local_node_id,
            "miner_wallet": self.local_wallet,
            "result": result,
            "computation_time": computation_time,
            "tflops": self.local_tflops,
            "pop_proof": proof.to_dict(),
            "device_type": self.local_device.device_type,
        });

        OutboundMessage {
            target: Some(submitter_id.to_string()),
            msg_type: MessageType::ComputeResult,
            payload,
        }
    }

    // ── Compute result (inbound — we are the submitter) ─────────────────────

    /// Handle a completed result from a remote miner.  Verify PoP, credit
    /// the miner's balance on the chain, and store the result.
    ///
    /// Returns `Ok(reward_plancks)` on success or `Err(reason)`.
    pub fn handle_compute_result(
        &mut self,
        sender_id: &str,
        payload: &Value,
        chain: &mut Chain,
        now: f64,
    ) -> Result<i64, String> {
        let workload_id = payload["workload_id"].as_str().unwrap_or("").to_string();
        let workload_key = payload["workload_key"].as_str().unwrap_or("").to_string();
        let miner_wallet = payload["miner_wallet"].as_str().unwrap_or("").to_string();
        let result_str = payload["result"].as_str().unwrap_or("").to_string();
        let computation_time = payload["computation_time"].as_f64().unwrap_or(0.0);
        let tflops = payload["tflops"].as_f64().unwrap_or(0.0);

        // Look up the pending workload
        let wl = self
            .pending_workloads
            .get(&workload_id)
            .ok_or_else(|| format!("Unknown workload {}", workload_id))?;
        if wl.status == WorkloadStatus::Completed {
            return Err("Already completed".into());
        }

        // Extract workload_data as string for PoP verification
        let workload_data_str = if wl.workload_data.is_string() {
            wl.workload_data.as_str().unwrap_or("").to_string()
        } else {
            serde_json::to_string(&wl.workload_data).unwrap_or_default()
        };

        // Reconstruct + verify PoP proof
        let pop_val = &payload["pop_proof"];
        let proof = reconstruct_proof(pop_val)?;
        let (valid, quality, reason) = proof.verify(&workload_data_str, &result_str, &miner_wallet);

        if !valid {
            // Penalise miner
            if let Some(peer) = self.compute_peers.get_mut(sender_id) {
                peer.penalise(0.8);
            }
            return Err(format!("Invalid PoP proof: {}", reason));
        }

        // Calculate reward
        let reward = calculate_reward(tflops, quality, chain.height());

        // Credit miner
        *chain.balances.entry(miner_wallet.clone()).or_insert(0) += reward;

        // Update workload state
        if let Some(wl) = self.pending_workloads.get_mut(&workload_id) {
            wl.status = WorkloadStatus::Completed;
            wl.result = Some(Value::String(result_str.clone()));
            wl.completed_at = now;
        }

        // Store completed result
        self.completed_results.insert(
            workload_id.clone(),
            CompletedResult {
                workload_id: workload_id.clone(),
                workload_key,
                miner_node: sender_id.to_string(),
                miner_wallet: miner_wallet.clone(),
                result: Value::String(result_str),
                computation_time,
                reward_plancks: reward,
            },
        );

        // Update peer stats
        if let Some(peer) = self.compute_peers.get_mut(sender_id) {
            peer.reward_success();
        }

        self.stats.plancks_paid_remote += reward;
        Ok(reward)
    }

    // ── Compute reject (inbound) ────────────────────────────────────────────

    /// Handle a workload rejection/failure from a remote miner.
    pub fn handle_compute_reject(&mut self, sender_id: &str, payload: &Value) {
        let workload_id = payload["workload_id"].as_str().unwrap_or("");
        if let Some(wl) = self.pending_workloads.get_mut(workload_id) {
            if wl.status != WorkloadStatus::Completed {
                wl.status = WorkloadStatus::Failed;
            }
        }
        self.stats.workloads_failed += 1;
        if let Some(peer) = self.compute_peers.get_mut(sender_id) {
            peer.penalise(0.9);
        }
    }

    // ── Block announce / request / response ─────────────────────────────────

    /// Handle a block announce from a peer.
    ///
    /// Returns a vec of block-request messages for blocks we're missing.
    pub fn handle_block_announce(
        &self,
        sender_id: &str,
        payload: &Value,
        local_height: u64,
    ) -> Vec<OutboundMessage> {
        let remote_height = payload["block_index"].as_u64().unwrap_or(0);
        if remote_height <= local_height {
            return vec![];
        }
        let mut msgs = Vec::new();
        for i in (local_height + 1)..=remote_height {
            msgs.push(OutboundMessage {
                target: Some(sender_id.to_string()),
                msg_type: MessageType::BlockRequest,
                payload: serde_json::json!({
                    "block_index": i,
                    "requesting_node": self.local_node_id,
                }),
            });
        }
        msgs
    }

    /// Build a block-announce broadcast for a newly mined block.
    pub fn build_block_announce(
        &mut self,
        block_index: u64,
        block_hash: &str,
        prev_hash: &str,
        timestamp: f64,
        tx_count: usize,
    ) -> OutboundMessage {
        self.stats.blocks_synced += 1;
        OutboundMessage {
            target: None,
            msg_type: MessageType::BlockAnnounce,
            payload: serde_json::json!({
                "block_index": block_index,
                "block_hash": block_hash,
                "prev_hash": prev_hash,
                "timestamp": timestamp,
                "tx_count": tx_count,
            }),
        }
    }

    /// Build a block-response for a peer that requested a specific block.
    pub fn build_block_response(
        &self,
        requester: &str,
        block_index: u64,
        block_data: Value,
    ) -> OutboundMessage {
        OutboundMessage {
            target: Some(requester.to_string()),
            msg_type: MessageType::BlockResponse,
            payload: serde_json::json!({
                "block_index": block_index,
                "block_data": block_data,
            }),
        }
    }

    // ── Economy status ──────────────────────────────────────────────────────

    /// Build an economy-status broadcast.
    pub fn build_economy_status(
        &self,
        chain_height: u64,
        total_supply_plancks: i64,
    ) -> OutboundMessage {
        OutboundMessage {
            target: None,
            msg_type: MessageType::EconomyStatus,
            payload: serde_json::json!({
                "node_id": self.local_node_id,
                "blockchain_height": chain_height,
                "total_supply_plancks": total_supply_plancks,
                "compute_peers": self.compute_peers.len(),
                "tflops": self.local_tflops,
                "wallet": self.local_wallet,
            }),
        }
    }

    // ── Cleanup ─────────────────────────────────────────────────────────────

    /// Expire stale workloads and prune old compute peers.
    ///
    /// Returns `(expired_workloads, pruned_peers)`.
    pub fn cleanup(&mut self, now: f64) -> (usize, usize) {
        // Expire workloads
        let expired: Vec<String> = self
            .pending_workloads
            .iter()
            .filter(|(_, wl)| wl.is_expired(now))
            .map(|(id, _)| id.clone())
            .collect();
        for id in &expired {
            if let Some(wl) = self.pending_workloads.get_mut(id) {
                wl.status = WorkloadStatus::Expired;
            }
        }

        // Prune stale peers
        let stale: Vec<String> = self
            .compute_peers
            .iter()
            .filter(|(_, cp)| (now - cp.last_announce) > STALE_PEER_SECS)
            .map(|(id, _)| id.clone())
            .collect();
        for id in &stale {
            self.compute_peers.remove(id);
        }

        (expired.len(), stale.len())
    }

    // ── Network compute summary (matches Python exactly) ────────────────────

    /// Get a JSON summary of the compute network state.
    pub fn network_compute_summary(&self, chain_height: u64, now: f64) -> Value {
        let active_peers: Vec<&ComputePeer> = self
            .compute_peers
            .values()
            .filter(|p| p.is_fresh(now))
            .collect();
        let total_tflops: f64 =
            active_peers.iter().map(|p| p.tflops).sum::<f64>() + self.local_tflops;
        let available_miners = active_peers.iter().filter(|p| p.is_available(now)).count();
        let pending_count = self
            .pending_workloads
            .values()
            .filter(|w| matches!(w.status, WorkloadStatus::Pending | WorkloadStatus::Claimed))
            .count();

        serde_json::json!({
            "local_tflops": self.local_tflops,
            "local_device": self.local_device.device_name,
            "local_wallet": self.local_wallet,
            "compute_peers": active_peers.len(),
            "total_network_tflops": (total_tflops * 1000.0).round() / 1000.0,
            "available_miners": available_miners,
            "pending_workloads": pending_count,
            "blockchain_height": chain_height,
            "stats": serde_json::to_value(&self.stats).unwrap_or(Value::Null),
            "peers": active_peers.iter().map(|p| serde_json::json!({
                "node_id": p.node_id,
                "tflops": p.tflops,
                "device": p.device_name,
                "available": p.is_available(now),
                "reputation": (p.reputation * 100.0).round() / 100.0,
                "completed": p.total_completed,
            })).collect::<Vec<_>>(),
        })
    }

    // ── Message router ──────────────────────────────────────────────────────

    /// Route an inbound economy message.  Returns any outbound messages to send.
    ///
    /// This is the main dispatch point — the network layer calls this for any
    /// `MessageType` in the compute-economy range.
    pub fn route_message(
        &mut self,
        msg_type: MessageType,
        sender_id: &str,
        payload: &Value,
        chain: &mut Chain,
        now: f64,
    ) -> Vec<OutboundMessage> {
        match msg_type {
            MessageType::ComputeAnnounce => {
                self.handle_compute_announce(sender_id, payload, now);
                vec![]
            }
            MessageType::ComputeRequest => {
                match self.handle_compute_request(sender_id, payload, now) {
                    Some(msg) => vec![msg],
                    None => vec![],
                }
            }
            MessageType::ComputeClaim => {
                self.handle_compute_claim(sender_id, payload, now);
                vec![]
            }
            MessageType::ComputeResult => {
                // result verification + reward minting
                let _ = self.handle_compute_result(sender_id, payload, chain, now);
                vec![]
            }
            MessageType::ComputeReject => {
                self.handle_compute_reject(sender_id, payload);
                vec![]
            }
            MessageType::BlockAnnounce => {
                self.handle_block_announce(sender_id, payload, chain.height())
            }
            _ => vec![], // EconomyStatus, BlockRequest, BlockResponse handled at network layer
        }
    }
}

// ── Helper: reconstruct ProofOfPower from JSON ──────────────────────────────

fn reconstruct_proof(val: &Value) -> Result<ProofOfPower, String> {
    let s = |key: &str| -> String { val[key].as_str().unwrap_or("").to_string() };
    let f = |key: &str| -> f64 { val[key].as_f64().unwrap_or(0.0) };

    let proof_hash = s("proof_hash");
    if proof_hash.is_empty() {
        return Err("Missing proof_hash".into());
    }

    Ok(ProofOfPower {
        proof_hash,
        workload_key: s("workload_key"),
        data_hash: s("data_hash"),
        result_hash: s("result_hash"),
        miner_address: s("miner_address"),
        computation_time: f("computation_time"),
        method: s("method"),
        timestamp: f("timestamp"),
        version: s("version"),
        device_type: s("device_type"),
        gpu_backend: s("gpu_backend"),
        tflops_measured: f("tflops_measured"),
        hw_attestation_hash: s("hw_attestation_hash"),
        challenge_response: val["challenge_response"].as_str().map(|s| s.to_string()),
    })
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pop::DeviceInfo;

    fn test_device() -> DeviceInfo {
        DeviceInfo {
            device_type: "cuda".into(),
            device_name: "Orin".into(),
            tflops_fp16: 5.4,
            tflops_fp32: 1.3,
            memory_gb: 7.4,
            benchmark_time_s: 0.5,
        }
    }

    fn make_bridge(id: &str, wallet: &str) -> EconomyBridge {
        EconomyBridge::new(id, wallet, test_device())
    }

    // ── ComputePeer ─────────────────────────────────────────────────────

    #[test]
    fn test_compute_peer_new() {
        let cp = ComputePeer::new("node1", "wallet1", 5.4);
        assert_eq!(cp.node_id, "node1");
        assert_eq!(cp.tflops, 5.4);
        assert_eq!(cp.reputation, 1.0);
        assert!(cp.available);
    }

    #[test]
    fn test_compute_peer_freshness() {
        let mut cp = ComputePeer::new("n1", "w1", 1.0);
        cp.last_announce = 1000.0;
        assert!(cp.is_fresh(1100.0)); // 100s < 300s TTL
        assert!(!cp.is_fresh(1400.0)); // 400s > 300s TTL
    }

    #[test]
    fn test_compute_peer_availability() {
        let mut cp = ComputePeer::new("n1", "w1", 1.0);
        cp.last_announce = 1000.0;

        assert!(cp.is_available(1100.0));

        // At capacity
        cp.active_workloads = 1;
        assert!(!cp.is_available(1100.0));

        // Fresh + under capacity but low reputation
        cp.active_workloads = 0;
        cp.reputation = 0.2;
        assert!(!cp.is_available(1100.0));
    }

    #[test]
    fn test_compute_peer_score() {
        let mut cp = ComputePeer::new("n1", "w1", 10.0);
        cp.reputation = 0.8;
        assert!((cp.score() - 8.0).abs() < 1e-9);
    }

    #[test]
    fn test_compute_peer_penalise_and_reward() {
        let mut cp = ComputePeer::new("n1", "w1", 1.0);
        cp.reputation = 1.0;
        cp.penalise(0.8);
        assert!((cp.reputation - 0.8).abs() < 1e-9);
        assert_eq!(cp.total_failed, 1);

        cp.reward_success();
        assert!((cp.reputation - 0.85).abs() < 1e-9);
        assert_eq!(cp.total_completed, 1);
    }

    #[test]
    fn test_compute_peer_to_dict() {
        let cp = ComputePeer::new("n1", "w1", 5.4);
        let d = cp.to_dict();
        assert_eq!(d["node_id"], "n1");
        assert_eq!(d["tflops"], 5.4);
    }

    // ── PendingWorkload ─────────────────────────────────────────────────

    #[test]
    fn test_workload_new() {
        let wl = PendingWorkload::new(
            "wl1",
            "key1",
            "sub1",
            "wal1",
            serde_json::json!({"prompt": "hello"}),
            "inference",
            1000000,
            100.0,
        );
        assert_eq!(wl.workload_id, "wl1");
        assert_eq!(wl.status, WorkloadStatus::Pending);
        assert!(wl.claimed_by.is_none());
    }

    #[test]
    fn test_workload_pending_expiry() {
        let wl = PendingWorkload::new(
            "wl1",
            "k1",
            "s1",
            "w1",
            Value::Null,
            "inference",
            100,
            1000.0,
        );
        // Not expired at 1200s (200s < 300s pending timeout)
        assert!(!wl.is_expired(1200.0));
        // Expired at 1400s (400s > 300s pending timeout)
        assert!(wl.is_expired(1400.0));
    }

    #[test]
    fn test_workload_claimed_expiry() {
        let mut wl = PendingWorkload::new(
            "wl1",
            "k1",
            "s1",
            "w1",
            Value::Null,
            "inference",
            100,
            1000.0,
        );
        wl.status = WorkloadStatus::Claimed;
        wl.claimed_at = 1010.0;
        // Not expired at 1100s (90s < 120s claim timeout)
        assert!(!wl.is_expired(1100.0));
        // Expired at 1200s (190s > 120s claim timeout)
        assert!(wl.is_expired(1200.0));
    }

    // ── EconomyBridge: Compute Announce ─────────────────────────────────

    #[test]
    fn test_handle_compute_announce_new_peer() {
        let mut bridge = make_bridge("local", "wallet_local");
        let payload = serde_json::json!({
            "wallet_address": "wallet_remote",
            "tflops": 10.5,
            "device_type": "cuda",
            "device_name": "RTX4090",
            "vram_mb": 24576,
            "available": true,
            "max_concurrent": 2,
            "active_workloads": 0,
            "reputation": 0.95,
        });

        bridge.handle_compute_announce("remote1", &payload, 1000.0);

        assert_eq!(bridge.compute_peers.len(), 1);
        let peer = &bridge.compute_peers["remote1"];
        assert_eq!(peer.tflops, 10.5);
        assert_eq!(peer.device_name, "RTX4090");
        assert_eq!(peer.vram_mb, 24576);
        assert_eq!(peer.max_concurrent, 2);
        assert_eq!(bridge.stats.compute_peers_seen, 1);
    }

    #[test]
    fn test_handle_compute_announce_update_existing() {
        let mut bridge = make_bridge("local", "wl");
        let p1 = serde_json::json!({
            "wallet_address": "w1",
            "tflops": 5.0,
            "device_type": "cuda",
            "device_name": "GTX1080",
            "available": true,
        });
        bridge.handle_compute_announce("r1", &p1, 1000.0);

        let p2 = serde_json::json!({
            "wallet_address": "w1",
            "tflops": 6.0,
            "device_name": "RTX3090",
            "available": false,
        });
        bridge.handle_compute_announce("r1", &p2, 1050.0);

        assert_eq!(bridge.compute_peers.len(), 1);
        let peer = &bridge.compute_peers["r1"];
        assert_eq!(peer.tflops, 6.0);
        assert_eq!(peer.device_name, "RTX3090");
        assert!(!peer.available);
        assert_eq!(peer.last_announce, 1050.0);
        // Only 1 new peer was seen
        assert_eq!(bridge.stats.compute_peers_seen, 1);
    }

    #[test]
    fn test_build_compute_announce() {
        let bridge = make_bridge("n1", "w1");
        let msg = bridge.build_compute_announce(42, 1000.0);
        assert!(msg.target.is_none()); // broadcast
        assert_eq!(msg.msg_type, MessageType::ComputeAnnounce);
        assert_eq!(msg.payload["wallet_address"], "w1");
        assert_eq!(msg.payload["blockchain_height"], 42);
    }

    // ── EconomyBridge: Workload Dispatch ────────────────────────────────

    #[test]
    fn test_dispatch_no_peers() {
        let mut bridge = make_bridge("local", "wl");
        let result = bridge.dispatch_workload(
            "key1",
            serde_json::json!({"prompt": "hi"}),
            "inference",
            None,
            DEFAULT_FEE_PLANCKS,
            1000.0,
        );
        assert!(result.is_none());
    }

    #[test]
    fn test_dispatch_with_available_peer() {
        let mut bridge = make_bridge("local", "wl");

        // Add a remote compute peer
        let announce = serde_json::json!({
            "wallet_address": "w_remote",
            "tflops": 10.0,
            "device_type": "cuda",
            "device_name": "GPU",
            "available": true,
            "max_concurrent": 2,
            "active_workloads": 0,
        });
        bridge.handle_compute_announce("remote1", &announce, 1000.0);

        let result = bridge.dispatch_workload(
            "key1",
            serde_json::json!({"prompt": "test"}),
            "inference",
            None,
            DEFAULT_FEE_PLANCKS,
            1050.0,
        );
        assert!(result.is_some());
        let (wl_id, msg) = result.unwrap();
        assert!(!wl_id.is_empty());
        assert!(msg.target.is_none()); // broadcast request
        assert_eq!(msg.msg_type, MessageType::ComputeRequest);
        assert_eq!(msg.payload["workload_key"], "key1");
        assert_eq!(bridge.stats.workloads_dispatched, 1);
        assert!(bridge.pending_workloads.contains_key(&wl_id));
    }

    #[test]
    fn test_dispatch_excludes_self() {
        let mut bridge = make_bridge("local", "wl");
        // Add ourselves as a compute peer — should be excluded
        let announce = serde_json::json!({
            "wallet_address": "wl",
            "tflops": 10.0,
            "available": true,
        });
        bridge.handle_compute_announce("local", &announce, 1000.0);

        let result = bridge.dispatch_workload("key1", Value::Null, "inference", None, 100, 1050.0);
        assert!(result.is_none()); // no *remote* peers
    }

    // ── EconomyBridge: Compute Request ──────────────────────────────────

    #[test]
    fn test_handle_compute_request_accept() {
        let mut bridge = make_bridge("miner", "wm");
        let payload = serde_json::json!({
            "workload_id": "wl_001",
            "workload_key": "k1",
            "workload_type": "inference",
            "workload_data": {"prompt": "test"},
            "deadline": 2000.0,
        });

        let claim = bridge.handle_compute_request("submitter", &payload, 1000.0);
        assert!(claim.is_some());
        let msg = claim.unwrap();
        assert_eq!(msg.target.as_deref(), Some("submitter"));
        assert_eq!(msg.msg_type, MessageType::ComputeClaim);
        assert_eq!(msg.payload["workload_id"], "wl_001");
        assert_eq!(bridge.active_remote_mining, 1);
        assert_eq!(bridge.stats.workloads_received, 1);
    }

    #[test]
    fn test_handle_compute_request_reject_work_disabled() {
        let mut bridge = make_bridge("m", "w");
        bridge.accept_remote_work = false;
        let payload = serde_json::json!({"workload_id": "wl1", "deadline": 2000.0});
        assert!(
            bridge
                .handle_compute_request("s1", &payload, 1000.0)
                .is_none()
        );
    }

    #[test]
    fn test_handle_compute_request_reject_at_capacity() {
        let mut bridge = make_bridge("m", "w");
        bridge.active_remote_mining = MAX_CONCURRENT_REMOTE;
        let payload = serde_json::json!({"workload_id": "wl1", "deadline": 2000.0});
        assert!(
            bridge
                .handle_compute_request("s1", &payload, 1000.0)
                .is_none()
        );
    }

    #[test]
    fn test_handle_compute_request_reject_expired() {
        let mut bridge = make_bridge("m", "w");
        let payload = serde_json::json!({"workload_id": "wl1", "deadline": 500.0});
        // now (1000) > deadline (500) → expired
        assert!(
            bridge
                .handle_compute_request("s1", &payload, 1000.0)
                .is_none()
        );
    }

    // ── EconomyBridge: Claim ────────────────────────────────────────────

    #[test]
    fn test_handle_compute_claim() {
        let mut bridge = make_bridge("submitter", "ws");
        // Create a pending workload
        let ann = serde_json::json!({"wallet_address":"wr","tflops":10.0,"available":true});
        bridge.handle_compute_announce("miner1", &ann, 1000.0);
        let (wl_id, _) = bridge
            .dispatch_workload("k1", Value::Null, "inference", None, 100, 1010.0)
            .unwrap();

        let claim = serde_json::json!({
            "workload_id": wl_id,
            "miner_node": "miner1",
            "miner_wallet": "wr",
            "tflops": 10.0,
        });

        assert!(bridge.handle_compute_claim("miner1", &claim, 1020.0));
        assert_eq!(
            bridge.pending_workloads[&wl_id].status,
            WorkloadStatus::Claimed
        );
        assert_eq!(
            bridge.pending_workloads[&wl_id].claimed_by.as_deref(),
            Some("miner1")
        );
    }

    #[test]
    fn test_handle_compute_claim_unknown_workload() {
        let mut bridge = make_bridge("s", "w");
        let claim = serde_json::json!({"workload_id": "nonexistent"});
        assert!(!bridge.handle_compute_claim("m1", &claim, 1000.0));
    }

    // ── EconomyBridge: Compute Result (full cycle) ──────────────────────

    #[test]
    fn test_build_compute_result() {
        let mut bridge = make_bridge("miner", "wallet_miner");

        let msg = bridge.build_compute_result(
            "submitter1",
            "wl_001",
            "workload_key_abc",
            "input data for the computation that is long enough",
            "output result from the computation that is also fairly long and varied with numbers 12345 and symbols !@#$%",
            2.5,
            "aabbccdd11223344aabbccdd11223344",
        );

        assert_eq!(msg.target.as_deref(), Some("submitter1"));
        assert_eq!(msg.msg_type, MessageType::ComputeResult);
        assert_eq!(msg.payload["workload_id"], "wl_001");
        assert!(msg.payload["pop_proof"]["proof_hash"].is_string());
        assert_eq!(bridge.stats.workloads_completed, 1);
    }

    #[test]
    fn test_handle_compute_result_full_cycle() {
        // Submitter bridge
        let mut submitter = make_bridge("submitter", "wallet_sub");

        // Add a remote miner peer
        let ann = serde_json::json!({
            "wallet_address": "wallet_miner",
            "tflops": 5.4,
            "available": true,
        });
        submitter.handle_compute_announce("miner1", &ann, 1000.0);

        // Dispatch workload
        let wd = serde_json::json!({"prompt": "compute this please"});
        let (wl_id, _) = submitter
            .dispatch_workload("wk_123", wd, "inference", None, DEFAULT_FEE_PLANCKS, 1010.0)
            .unwrap();

        // Miner bridge builds a result
        let mut miner = make_bridge("miner1", "wallet_miner");

        let workload_data_str = r#"{"prompt":"compute this please"}"#;
        let result_str = "This is the computed result that is fairly long and varied enough for the PoP proof to validate with good quality score 12345 !@#$%";

        let result_msg = miner.build_compute_result(
            "submitter",
            &wl_id,
            "wk_123",
            workload_data_str,
            result_str,
            2.5,
            "aabbccdd11223344aabbccdd11223344",
        );

        // Submitter handles the result
        let mut chain = Chain::new();
        let reward =
            submitter.handle_compute_result("miner1", &result_msg.payload, &mut chain, 1050.0);

        assert!(reward.is_ok(), "Expected Ok, got: {:?}", reward);
        let reward_plancks = reward.unwrap();
        assert!(reward_plancks > 0);

        // Miner should have been credited on chain
        assert!(chain.balances.get("wallet_miner").unwrap_or(&0) > &0);

        // Workload should be completed
        assert_eq!(
            submitter.pending_workloads[&wl_id].status,
            WorkloadStatus::Completed
        );

        // Result should be stored
        assert!(submitter.completed_results.contains_key(&wl_id));
        let cr = &submitter.completed_results[&wl_id];
        assert_eq!(cr.miner_wallet, "wallet_miner");
        assert_eq!(cr.reward_plancks, reward_plancks);

        // Stats updated
        assert!(submitter.stats.plancks_paid_remote > 0);
    }

    #[test]
    fn test_handle_compute_result_invalid_proof() {
        let mut bridge = make_bridge("sub", "ws");
        let ann = serde_json::json!({"wallet_address":"wm","tflops":5.0,"available":true});
        bridge.handle_compute_announce("miner1", &ann, 1000.0);

        let (wl_id, _) = bridge
            .dispatch_workload(
                "k1",
                serde_json::json!("data"),
                "inference",
                None,
                100,
                1010.0,
            )
            .unwrap();

        // Build a bogus result with tampered proof
        let payload = serde_json::json!({
            "workload_id": wl_id,
            "workload_key": "k1",
            "miner_wallet": "wm",
            "result": "some result",
            "computation_time": 1.0,
            "tflops": 5.0,
            "pop_proof": {
                "proof_hash": "0000deadbeef",
                "workload_key": "k1",
                "data_hash": "bad",
                "result_hash": "bad",
                "miner_address": "wm",
                "computation_time": 1.0,
                "method": "deterministic",
                "timestamp": 1030.0,
                "version": "3.1",
                "device_type": "cuda",
                "gpu_backend": "Orin",
                "tflops_measured": 5.0,
                "hw_attestation_hash": "aabb",
            },
        });

        let mut chain = Chain::new();
        let result = bridge.handle_compute_result("miner1", &payload, &mut chain, 1050.0);
        assert!(result.is_err());

        // Miner should have been penalised
        assert!(bridge.compute_peers["miner1"].reputation < 1.0);
    }

    // ── EconomyBridge: Reject ───────────────────────────────────────────

    #[test]
    fn test_handle_compute_reject() {
        let mut bridge = make_bridge("sub", "ws");
        let ann = serde_json::json!({"wallet_address":"wm","tflops":5.0,"available":true});
        bridge.handle_compute_announce("miner1", &ann, 1000.0);

        let (wl_id, _) = bridge
            .dispatch_workload("k1", Value::Null, "inference", None, 100, 1010.0)
            .unwrap();

        let reject = serde_json::json!({
            "workload_id": wl_id,
            "reason": "LLM inference failed",
            "miner_node": "miner1",
        });

        bridge.handle_compute_reject("miner1", &reject);

        assert_eq!(
            bridge.pending_workloads[&wl_id].status,
            WorkloadStatus::Failed
        );
        assert_eq!(bridge.stats.workloads_failed, 1);
        assert!(bridge.compute_peers["miner1"].reputation < 1.0);
    }

    // ── EconomyBridge: Block announce ───────────────────────────────────

    #[test]
    fn test_handle_block_announce_ahead() {
        let bridge = make_bridge("n1", "w1");
        let payload = serde_json::json!({"block_index": 5});

        let msgs = bridge.handle_block_announce("peer1", &payload, 2);
        assert_eq!(msgs.len(), 3); // request blocks 3, 4, 5
        assert_eq!(msgs[0].msg_type, MessageType::BlockRequest);
        assert_eq!(msgs[0].payload["block_index"], 3);
        assert_eq!(msgs[2].payload["block_index"], 5);
        assert_eq!(msgs[0].target.as_deref(), Some("peer1"));
    }

    #[test]
    fn test_handle_block_announce_same_height() {
        let bridge = make_bridge("n1", "w1");
        let payload = serde_json::json!({"block_index": 2});
        let msgs = bridge.handle_block_announce("peer1", &payload, 5);
        assert!(msgs.is_empty());
    }

    #[test]
    fn test_build_block_announce() {
        let mut bridge = make_bridge("n1", "w1");
        let msg = bridge.build_block_announce(10, "hash10", "hash9", 1234.0, 3);
        assert!(msg.target.is_none());
        assert_eq!(msg.msg_type, MessageType::BlockAnnounce);
        assert_eq!(msg.payload["block_index"], 10);
        assert_eq!(msg.payload["block_hash"], "hash10");
        assert_eq!(bridge.stats.blocks_synced, 1);
    }

    #[test]
    fn test_build_block_response() {
        let bridge = make_bridge("n1", "w1");
        let block_data = serde_json::json!({"index": 5, "hash": "abc"});
        let msg = bridge.build_block_response("peer1", 5, block_data.clone());
        assert_eq!(msg.target.as_deref(), Some("peer1"));
        assert_eq!(msg.msg_type, MessageType::BlockResponse);
        assert_eq!(msg.payload["block_data"]["hash"], "abc");
    }

    // ── EconomyBridge: Economy Status ───────────────────────────────────

    #[test]
    fn test_build_economy_status() {
        let bridge = make_bridge("n1", "w1");
        let msg = bridge.build_economy_status(100, 50_000_000);
        assert!(msg.target.is_none());
        assert_eq!(msg.msg_type, MessageType::EconomyStatus);
        assert_eq!(msg.payload["blockchain_height"], 100);
    }

    // ── EconomyBridge: Cleanup ──────────────────────────────────────────

    #[test]
    fn test_cleanup_expired_workloads() {
        let mut bridge = make_bridge("sub", "ws");
        let ann = serde_json::json!({"wallet_address":"wm","tflops":5.0,"available":true});
        bridge.handle_compute_announce("r1", &ann, 1000.0);

        let (wl_id, _) = bridge
            .dispatch_workload("k1", Value::Null, "inference", None, 100, 1000.0)
            .unwrap();

        // Not expired yet
        let (exp, pruned) = bridge.cleanup(1200.0);
        assert_eq!(exp, 0);

        // Expired (pending for > 300s)
        let (exp, _) = bridge.cleanup(1400.0);
        assert_eq!(exp, 1);
        assert_eq!(
            bridge.pending_workloads[&wl_id].status,
            WorkloadStatus::Expired
        );
    }

    #[test]
    fn test_cleanup_stale_peers() {
        let mut bridge = make_bridge("sub", "ws");
        let ann = serde_json::json!({"wallet_address":"wm","tflops":5.0,"available":true});
        bridge.handle_compute_announce("r1", &ann, 1000.0);
        assert_eq!(bridge.compute_peers.len(), 1);

        // Not stale yet (500s < 600s)
        let (_, pruned) = bridge.cleanup(1500.0);
        assert_eq!(pruned, 0);

        // Stale (700s > 600s)
        let (_, pruned) = bridge.cleanup(1700.0);
        assert_eq!(pruned, 1);
        assert!(bridge.compute_peers.is_empty());
    }

    // ── EconomyBridge: network_compute_summary ──────────────────────────

    #[test]
    fn test_network_compute_summary_empty() {
        let bridge = make_bridge("n1", "w1");
        let summary = bridge.network_compute_summary(10, 1000.0);
        assert_eq!(summary["compute_peers"], 0);
        assert_eq!(summary["available_miners"], 0);
        assert_eq!(summary["local_tflops"], 5.4); // from test_device
        assert_eq!(summary["blockchain_height"], 10);
    }

    #[test]
    fn test_network_compute_summary_with_peers() {
        let mut bridge = make_bridge("n1", "w1");
        let ann1 = serde_json::json!({
            "wallet_address": "w2", "tflops": 10.0,
            "device_type": "cuda", "device_name": "RTX4090",
            "available": true,
        });
        let ann2 = serde_json::json!({
            "wallet_address": "w3", "tflops": 20.0,
            "device_type": "cuda", "device_name": "H100",
            "available": false,
        });
        bridge.handle_compute_announce("r1", &ann1, 1000.0);
        bridge.handle_compute_announce("r2", &ann2, 1000.0);

        let summary = bridge.network_compute_summary(50, 1100.0);
        assert_eq!(summary["compute_peers"], 2);
        assert_eq!(summary["available_miners"], 1); // only r1 is available
        // total = 10.0 + 20.0 + 5.4 (local) = 35.4
        assert_eq!(summary["total_network_tflops"], 35.4);
        let peers = summary["peers"].as_array().unwrap();
        assert_eq!(peers.len(), 2);
    }

    // ── EconomyBridge: Message Router ───────────────────────────────────

    #[test]
    fn test_route_compute_announce() {
        let mut bridge = make_bridge("n1", "w1");
        let mut chain = Chain::new();
        let payload = serde_json::json!({
            "wallet_address": "wr", "tflops": 8.0, "available": true,
        });
        let msgs = bridge.route_message(
            MessageType::ComputeAnnounce,
            "r1",
            &payload,
            &mut chain,
            1000.0,
        );
        assert!(msgs.is_empty()); // announce is ingest-only
        assert_eq!(bridge.compute_peers.len(), 1);
    }

    #[test]
    fn test_route_compute_request_accept() {
        let mut bridge = make_bridge("miner", "wm");
        let mut chain = Chain::new();
        let payload = serde_json::json!({
            "workload_id": "wl1", "deadline": 2000.0,
        });
        let msgs = bridge.route_message(
            MessageType::ComputeRequest,
            "sub1",
            &payload,
            &mut chain,
            1000.0,
        );
        assert_eq!(msgs.len(), 1);
        assert_eq!(msgs[0].msg_type, MessageType::ComputeClaim);
    }

    #[test]
    fn test_route_block_announce() {
        let mut bridge = make_bridge("n1", "w1");
        let mut chain = Chain::new(); // height = 1 (genesis only)
        let payload = serde_json::json!({"block_index": 3});
        let msgs = bridge.route_message(
            MessageType::BlockAnnounce,
            "peer1",
            &payload,
            &mut chain,
            1000.0,
        );
        // Should request blocks 2 and 3
        assert_eq!(msgs.len(), 2);
        assert_eq!(msgs[0].msg_type, MessageType::BlockRequest);
    }

    #[test]
    fn test_route_compute_reject() {
        let mut bridge = make_bridge("sub", "ws");
        let mut chain = Chain::new();

        // Add peer + dispatch workload
        let ann = serde_json::json!({"wallet_address":"wm","tflops":5.0,"available":true});
        bridge.handle_compute_announce("miner1", &ann, 1000.0);
        let (wl_id, _) = bridge
            .dispatch_workload("k1", Value::Null, "inference", None, 100, 1010.0)
            .unwrap();

        let payload = serde_json::json!({
            "workload_id": wl_id,
            "reason": "failed",
        });
        let msgs = bridge.route_message(
            MessageType::ComputeReject,
            "miner1",
            &payload,
            &mut chain,
            1020.0,
        );
        assert!(msgs.is_empty());
        assert_eq!(
            bridge.pending_workloads[&wl_id].status,
            WorkloadStatus::Failed
        );
    }

    // ── reconstruct_proof helper ────────────────────────────────────────

    #[test]
    fn test_reconstruct_proof_valid() {
        let device = test_device();
        let proof = ProofOfPower::generate(
            "wk1",
            "some workload data long enough",
            "some result data long enough to verify with challenge",
            "miner_addr",
            1.5,
            "deterministic",
            &device,
            "aabbccdd11223344aabbccdd11223344",
        );
        let dict = proof.to_dict();
        let val: Value = dict.into_iter().collect();

        let reconstructed = reconstruct_proof(&val);
        assert!(reconstructed.is_ok());
        let rp = reconstructed.unwrap();
        assert_eq!(rp.proof_hash, proof.proof_hash);
        assert_eq!(rp.miner_address, "miner_addr");
    }

    #[test]
    fn test_reconstruct_proof_missing_hash() {
        let val = serde_json::json!({"no_proof_hash": true});
        assert!(reconstruct_proof(&val).is_err());
    }

    // ── BridgeStats ─────────────────────────────────────────────────────

    #[test]
    fn test_bridge_stats_default() {
        let s = BridgeStats::default();
        assert_eq!(s.workloads_dispatched, 0);
        assert_eq!(s.plancks_earned_remote, 0);
    }

    #[test]
    fn test_bridge_stats_serializable() {
        let s = BridgeStats {
            workloads_dispatched: 5,
            workloads_completed: 3,
            ..Default::default()
        };
        let v = serde_json::to_value(&s).unwrap();
        assert_eq!(v["workloads_dispatched"], 5);
        assert_eq!(v["workloads_completed"], 3);
    }
}
