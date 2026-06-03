//! Block production loop — the heartbeat of the blockchain node.
//!
//! Runs every 69 seconds:
//! 1. Compute current slot
//! 2. Run VRF leader election  
//! 3. If we're the leader → assemble and produce a block
//! 4. Include coinbase + availability rewards + pending transactions

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use serde_json::Value;

use crate::block::Block;
use crate::chain::{
    Chain, MAX_LOCAL_SYSTEM_CREDIT_PER_BLOCK_PLANCKS, MAX_PRODUCTIVE_WORK_REWARD_PER_BLOCK_PLANCKS,
    is_local_system_credit_purpose, is_productive_work_purpose,
};
use crate::checkpoint::{Checkpoint, CheckpointChainStatus, status_code, status_reason};
use crate::dao::PlanetaryDAO;
use crate::election::{self, ComputeContributors};
use crate::genesis::{
    BASE_REWARD_PLANCKS, BLOCK_INTERVAL_SECS, HALVING_INTERVAL, MAX_SUPPLY_PLANCKS,
};
use crate::gossip::GossipNode;
use crate::mempool::Mempool;
use crate::storage::{CHAIN_STATE_VERSION, Storage};
use crate::transaction::Transaction;

// ── Node Config ──────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct NodeConfig {
    pub address: String,
    /// Hardware-measured TFLOPS before the operator contribution limit.
    pub measured_tflops: f64,
    /// Operator-selected share of measured compute exposed to the network.
    pub compute_share: f64,
    /// Effective TFLOPS exposed to consensus/availability surfaces.
    pub tflops: f64,
    pub mining_enabled: bool,
}

// ── Block Producer ───────────────────────────────────────────────────────────

/// The block producer manages the chain, contributors, and block assembly.
pub struct BlockProducer {
    pub chain: Chain,
    /// Contributors eligible for block production leadership.
    pub contributors: ComputeContributors,
    /// Contributors eligible for baseline compute availability rewards.
    ///
    /// This stays separate from `contributors`: a node can offer compute
    /// capacity to the marketplace without being allowed to produce blocks.
    /// That prevents non-mining provider nodes from being elected for slots
    /// they will never produce.
    pub availability_contributors: ComputeContributors,
    pub config: NodeConfig,
    pub ibd_complete: bool,
    /// Fee-priority transaction mempool.
    pub mempool: Mempool,
    /// On-chain DAO governance state.
    pub dao: PlanetaryDAO,
    /// Latest signed checkpoint loaded for this node, if any.
    pub checkpoint: Option<Checkpoint>,
    /// Checkpoint verification status for diagnostics/mining gate.
    pub checkpoint_status: String,
    /// Fork status for diagnostics/mining gate.
    pub fork_status: String,
    /// Current mining state (`enabled`, `paused_*`, `disabled`).
    pub mining_state: String,
    /// Human-readable reason for the current mining state.
    pub mining_pause_reason: String,
    /// Last bootstrap peer count observed by the mining gate.
    pub bootstrap_peer_count: usize,
    /// Recent peer/network diagnostics.
    pub peer_diagnostics: Vec<String>,
    /// Startup load mode used for diagnostics (`fresh`, `fast_load`,
    /// `fast_rebuild`, or `full_replay`).
    pub load_mode: String,
    /// Live sync state for diagnostics and mining gates.
    pub sync_state: String,
    pub last_sync_error: String,
    pub last_sync_at: f64,
    pub best_peer_height: u64,
}

impl BlockProducer {
    pub fn new(config: NodeConfig) -> Self {
        let mut contributors = ComputeContributors::new();
        let mut availability_contributors = ComputeContributors::new();
        if config.tflops > 0.0 {
            availability_contributors.register(&config.address, config.tflops);
        }
        if config.mining_enabled && config.tflops > 0.0 {
            contributors.register(&config.address, config.tflops);
        }
        let mining_state = if config.mining_enabled {
            "enabled"
        } else {
            "disabled"
        };

        Self {
            chain: Chain::new(),
            contributors,
            availability_contributors,
            config,
            ibd_complete: false,
            mempool: Mempool::new(),
            dao: PlanetaryDAO::new(),
            checkpoint: None,
            checkpoint_status: "no_checkpoint".into(),
            fork_status: "unknown".into(),
            mining_state: mining_state.into(),
            mining_pause_reason: String::new(),
            bootstrap_peer_count: 0,
            peer_diagnostics: Vec::new(),
            load_mode: "fresh".into(),
            sync_state: "idle".into(),
            last_sync_error: String::new(),
            last_sync_at: 0.0,
            best_peer_height: 0,
        }
    }

    pub fn set_checkpoint_status(
        &mut self,
        checkpoint: Option<Checkpoint>,
        status: CheckpointChainStatus,
    ) {
        self.checkpoint = checkpoint;
        self.checkpoint_status = status_code(&status).into();
        self.fork_status = match status {
            CheckpointChainStatus::Verified => "synced",
            CheckpointChainStatus::BelowCheckpoint => "isolated",
            CheckpointChainStatus::CheckpointMismatch
            | CheckpointChainStatus::InvalidCheckpoint(_) => "checkpoint_mismatch",
            CheckpointChainStatus::NoCheckpoint => "no_checkpoint",
        }
        .into();
        if !matches!(
            status,
            CheckpointChainStatus::Verified | CheckpointChainStatus::NoCheckpoint
        ) {
            self.mining_state = match status {
                CheckpointChainStatus::BelowCheckpoint => "paused_unsynced",
                _ => "paused_fork_detected",
            }
            .into();
            self.mining_pause_reason = status_reason(&status);
        }
    }

    pub fn set_mining_gate(
        &mut self,
        state: &str,
        reason: &str,
        bootstrap_peer_count: usize,
        diagnostics: Vec<String>,
    ) {
        self.mining_state = state.into();
        self.mining_pause_reason = reason.into();
        self.bootstrap_peer_count = bootstrap_peer_count;
        self.peer_diagnostics = diagnostics;
        if state == "enabled" {
            self.fork_status = if self.checkpoint_status == "verified" {
                "synced".into()
            } else {
                self.checkpoint_status.clone()
            };
        }
    }

    pub fn set_sync_status(&mut self, state: &str, error: &str, best_peer_height: u64) {
        self.sync_state = state.into();
        self.last_sync_error = error.into();
        self.best_peer_height = best_peer_height;
        if state == "complete" {
            self.last_sync_at = now_f64();
        }
    }

    /// Register or update compute capacity for availability rewards only.
    ///
    /// This does not affect leader election. The marketplace/provider
    /// announcement layer will use this when remote machines advertise
    /// verified compute capacity.
    pub fn register_availability_contributor(&mut self, address: &str, tflops: f64) {
        self.availability_contributors.register(address, tflops);
    }

    /// Get current supply (balances + stakes) in plancks.
    pub fn current_supply(&self) -> i64 {
        let bal_sum: i64 = self.chain.balances.values().sum();
        let stake_sum: i64 = self.chain.stakes.values().sum();
        bal_sum + stake_sum
    }

    fn reward_address(&self) -> String {
        let configured = std::env::var("REPRYNTT_REWARD_ADDRESS")
            .or_else(|_| std::env::var("REPRYNTT_TREASURY_REWARD_ADDRESS"))
            .unwrap_or_default();
        let configured = configured.trim();
        if configured.eq_ignore_ascii_case("DAO") {
            "DAO".to_string()
        } else {
            self.config.address.clone()
        }
    }

    /// Attempt to produce a block for the current time slot.
    ///
    /// Returns `Some(block)` if we're the elected leader and have transactions,
    /// `None` otherwise.
    pub fn try_produce_block(&mut self) -> Option<Block> {
        if !self.ibd_complete {
            return None;
        }

        let now = now_f64();
        let slot = election::slot_number(now);
        let prev_hash = &self.chain.latest_block().hash;

        // VRF leader election
        let result = election::elect_leader(prev_hash, slot, &self.contributors)?;

        if result.leader != self.config.address {
            return None; // Not our slot
        }

        let block_height = self.chain.height();
        let mut supply = self.current_supply();
        let reward_address = self.reward_address();

        // ── Assemble transactions ─────────────────────────────────────
        let mut txs: Vec<Transaction> = Vec::new();

        // Coinbase reward (first — provides balance for subsequent txs)
        let halvings = block_height / HALVING_INTERVAL;
        let coinbase = if halvings >= 64 {
            0
        } else {
            BASE_REWARD_PLANCKS >> halvings
        };
        let dao_share = coinbase / 20; // 5%
        let primary_reward_total = coinbase + dao_share;
        if coinbase > 0 && supply + primary_reward_total <= MAX_SUPPLY_PLANCKS {
            let mut meta = BTreeMap::new();
            meta.insert("purpose".into(), Value::String("coinbase".into()));

            let coinbase_tx = Transaction::new(
                "SYSTEM",
                &reward_address,
                coinbase,
                "reward",
                0,
                meta,
                Some(now),
                None,
                1,
            );
            txs.push(coinbase_tx);
            supply += coinbase;

            // Route 5% of coinbase to DAO treasury — Satoshi-style: fund governance
            // from block rewards, not from confiscation.
            if dao_share > 0 {
                let mut dao_meta = BTreeMap::new();
                dao_meta.insert("purpose".into(), Value::String("dao_fee".into()));
                let dao_tx = Transaction::new(
                    "SYSTEM",
                    "DAO",
                    dao_share,
                    "reward",
                    0,
                    dao_meta,
                    Some(now),
                    None,
                    1,
                );
                txs.push(dao_tx);
                supply += dao_share;
            }
        }

        // Availability rewards
        let avail_rewards = election::availability_rewards(
            &self.availability_contributors,
            supply,
            MAX_SUPPLY_PLANCKS,
        );
        for (addr, reward) in avail_rewards {
            let mut meta = BTreeMap::new();
            meta.insert(
                "purpose".into(),
                Value::String("availability_reward".into()),
            );
            if let Some(&tflops) = self.availability_contributors.contributors.get(&addr) {
                meta.insert(
                    "tflops".into(),
                    serde_json::Number::from_f64(tflops)
                        .map(Value::Number)
                        .unwrap_or(Value::Null),
                );
            }

            let avail_tx = Transaction::new(
                "SYSTEM",
                &addr,
                reward,
                "reward",
                0,
                meta,
                Some(now),
                None,
                1,
            );
            txs.push(avail_tx);
        }

        // Select pending transactions from mempool (fee-priority order, after rewards)
        let (pending, _total_fees) = self.mempool.select_for_block();

        // Pre-validate each pending tx against current chain state.
        // Skip (and evict) any that would fail block validation so one bad tx
        // doesn't block the entire chain.
        let mut validation_state = self.chain.clone();
        let mut valid_pending: Vec<crate::transaction::Transaction> = Vec::new();
        let mut evict_hashes: Vec<String> = Vec::new();
        let mut remaining = pending;
        let mut productive_work_total = 0i64;
        let mut local_credit_total = 0i64;

        while !remaining.is_empty() {
            let mut progressed = false;
            let mut still_waiting = Vec::new();

            for ptx in remaining {
                let workload_purpose = ptx
                    .metadata
                    .get("purpose")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                if ptx.tx_type == "workload_completion"
                    && !is_productive_work_purpose(workload_purpose)
                    && ptx.from_address != self.config.address
                {
                    eprintln!(
                        "Evicting local workload credit {}: signer is not local block miner",
                        &ptx.tx_hash[..32],
                    );
                    evict_hashes.push(ptx.tx_hash.clone());
                    continue;
                }
                match validation_state.validate_tx(&ptx) {
                    Ok(()) => {
                        if ptx.tx_type == "workload_completion" {
                            if is_productive_work_purpose(workload_purpose) {
                                let next_total = match productive_work_total.checked_add(ptx.amount)
                                {
                                    Some(total) => total,
                                    None => {
                                        eprintln!(
                                            "Evicting invalid mempool tx {}: Productive-work reward total overflow",
                                            &ptx.tx_hash[..32],
                                        );
                                        evict_hashes.push(ptx.tx_hash.clone());
                                        continue;
                                    }
                                };
                                if next_total > MAX_PRODUCTIVE_WORK_REWARD_PER_BLOCK_PLANCKS {
                                    still_waiting.push(ptx);
                                    continue;
                                }
                                productive_work_total = next_total;
                            } else if is_local_system_credit_purpose(workload_purpose) {
                                let next_total = match local_credit_total.checked_add(ptx.amount) {
                                    Some(total) => total,
                                    None => {
                                        eprintln!(
                                            "Evicting invalid mempool tx {}: Local system credit total overflow",
                                            &ptx.tx_hash[..32],
                                        );
                                        evict_hashes.push(ptx.tx_hash.clone());
                                        continue;
                                    }
                                };
                                if next_total > MAX_LOCAL_SYSTEM_CREDIT_PER_BLOCK_PLANCKS {
                                    still_waiting.push(ptx);
                                    continue;
                                }
                                local_credit_total = next_total;
                            }
                        }
                        if let Err(reason) = validation_state.apply_single_transaction(&ptx) {
                            if reason.contains("Invalid nonce") {
                                still_waiting.push(ptx);
                            } else {
                                eprintln!(
                                    "Evicting invalid mempool tx {}: {}",
                                    &ptx.tx_hash[..32],
                                    reason
                                );
                                evict_hashes.push(ptx.tx_hash.clone());
                            }
                            continue;
                        }
                        progressed = true;
                        valid_pending.push(ptx);
                    }
                    Err(reason) if reason.contains("Invalid nonce") => {
                        still_waiting.push(ptx);
                    }
                    Err(reason) => {
                        eprintln!(
                            "Evicting invalid mempool tx {}: {}",
                            &ptx.tx_hash[..32],
                            reason
                        );
                        evict_hashes.push(ptx.tx_hash.clone());
                    }
                }
            }

            if !progressed {
                break;
            }
            remaining = still_waiting;
        }
        // Remove invalid txs from mempool immediately
        if !evict_hashes.is_empty() {
            self.mempool.remove_confirmed(&evict_hashes);
        }
        let pending_hashes: Vec<String> = valid_pending.iter().map(|t| t.tx_hash.clone()).collect();
        txs.extend(valid_pending);

        if txs.is_empty() {
            return None; // No transactions to include
        }

        // ── Build block ───────────────────────────────────────────────
        let block = Block::new(
            block_height,
            &self.chain.latest_block().hash,
            now,
            txs,
            &self.config.address,
            BTreeMap::new(), // proof_of_power (populated by caller if workload was done)
        );
        let block = canonicalize_produced_block(block);

        // Validate and add to chain
        match self.chain.add_block(block.clone()) {
            Ok(()) => {
                // Remove confirmed transactions from mempool
                self.mempool.remove_confirmed(&pending_hashes);
                Some(block)
            }
            Err(e) => {
                eprintln!("Block production failed validation: {}", e);
                None
            }
        }
    }

    /// Persist chain state to storage.
    ///
    /// Note (bounded-chain refactor): only the recent window is in memory.
    /// We pass it to `save_chain`, which upserts those rows (no truncate)
    /// and re-derives `chain_meta.height` from `MAX(block_index)+1` so
    /// older history is preserved.
    pub fn save(&self, storage: &Storage) -> Result<(), String> {
        let recent = self.chain.recent_as_vec();
        storage
            .save_chain(
                &recent,
                &self.chain.balances,
                &self.chain.nonces,
                &self.chain.stakes,
            )
            .map_err(|e| format!("Storage error: {}", e))?;

        // Persist DAO state alongside chain DB
        if let Some(db_path) = storage.db_path() {
            if let Some(dir) = db_path.parent() {
                let dao_path = dir.join("dao_state.json");
                let dao_json = serde_json::to_string_pretty(&self.dao.to_dict())
                    .map_err(|e| format!("DAO serialize: {}", e))?;
                std::fs::write(&dao_path, dao_json).map_err(|e| format!("DAO write: {}", e))?;
            }
        }
        Ok(())
    }

    /// Load chain from storage. Prefer guarded fast-load from persisted state;
    /// fall back to full replay for fresh, legacy, or suspicious databases.
    pub fn load(&mut self, storage: &Storage) -> Result<(), String> {
        let started = Instant::now();
        if !force_full_replay() {
            match self.try_fast_load(storage) {
                Ok(Some(mode)) => {
                    self.load_mode = mode;
                    self.restore_dao_state(storage);
                    println!(
                        "⚡ Chain {} complete in {:.2}s",
                        self.load_mode,
                        started.elapsed().as_secs_f64()
                    );
                    return Ok(());
                }
                Ok(None) => {}
                Err(e) => {
                    eprintln!(
                        "⚠️  Fast chain load unavailable: {}; falling back to full replay",
                        e
                    );
                }
            }
        } else {
            println!("🔎 REPRYNTT_FORCE_FULL_REPLAY=1 — rebuilding chain state from blocks");
        }

        let blocks = storage
            .load_chain()
            .map_err(|e| format!("Load error: {}", e))?;

        if blocks.is_empty() {
            return Ok(()); // Fresh start
        }

        // Rebuild chain from loaded blocks
        self.chain = Chain::new(); // Start from genesis

        // If stored genesis doesn't match, stop instead of destroying data.
        if !blocks.is_empty() && blocks[0].hash != self.chain.genesis.hash {
            return Err(
                "Stored genesis mismatch; refusing to wipe chain database automatically. \
                 Back up chain.db and recover manually."
                    .into(),
            );
        }

        // Add blocks 1..N (genesis is already in chain)
        for block in blocks.into_iter().skip(1) {
            self.chain
                .add_block_trusted(block)
                .map_err(|e| format!("Chain validation failed at block: {}", e))?;
        }

        self.load_mode = "full_replay".into();
        storage
            .save_runtime_state(
                self.chain.height(),
                &self.chain.latest_block().hash,
                &self.chain.balances,
                &self.chain.nonces,
                &self.chain.stakes,
            )
            .map_err(|e| format!("State snapshot write failed: {}", e))?;

        // Restore DAO state
        self.restore_dao_state(storage);

        println!(
            "🔁 Chain full_replay complete in {:.2}s",
            started.elapsed().as_secs_f64()
        );
        Ok(())
    }

    fn try_fast_load(&mut self, storage: &Storage) -> Result<Option<String>, String> {
        let block_count = storage
            .block_count()
            .map_err(|e| format!("Block count read failed: {}", e))?;
        if block_count == 0 {
            return Ok(None);
        }

        let genesis = storage
            .get_block(0)
            .map_err(|e| format!("Genesis read failed: {}", e))?
            .ok_or_else(|| "Missing genesis block".to_string())?;
        if genesis.hash != crate::genesis::EXPECTED_GENESIS_HASH {
            return Err("Stored genesis mismatch".into());
        }

        let latest = storage
            .get_latest_block()
            .map_err(|e| format!("Latest block read failed: {}", e))?
            .ok_or_else(|| "Missing latest block".to_string())?;
        let expected_height = latest.index + 1;
        if expected_height != block_count {
            return Err(format!(
                "Block count/index mismatch: count={} latest_index={}",
                block_count, latest.index
            ));
        }

        let meta = storage
            .get_chain_meta()
            .map_err(|e| format!("Chain metadata read failed: {}", e))?;
        let mut mode = "fast_load".to_string();
        if let Some(meta) = meta.as_ref() {
            if meta.state_version != CHAIN_STATE_VERSION {
                return Err(format!("Unsupported state version {}", meta.state_version));
            }
            if meta.genesis_hash != crate::genesis::EXPECTED_GENESIS_HASH {
                return Err("Stored metadata genesis mismatch".into());
            }
            if meta.height != expected_height || meta.tip_hash != latest.hash {
                return Err("Stored metadata does not match latest block".into());
            }
        } else {
            mode = "fast_rebuild".into();
        }

        let blocks = storage
            .load_chain()
            .map_err(|e| format!("Block load failed: {}", e))?;
        let balances = storage
            .get_all_balances()
            .map_err(|e| format!("Balance state read failed: {}", e))?;
        let stakes = storage
            .get_all_stakes()
            .map_err(|e| format!("Stake state read failed: {}", e))?;
        if expected_height > 1 && balances.is_empty() && stakes.is_empty() {
            return Err("Persisted balances/stakes missing for non-genesis chain".into());
        }

        let mut nonces = storage
            .get_all_nonces()
            .map_err(|e| format!("Nonce state read failed: {}", e))?;
        if nonces.is_empty() && expected_height > 1 {
            let derived_nonces = Chain::derive_nonces_from_blocks(&blocks);
            if !derived_nonces.is_empty() {
                mode = "fast_rebuild".into();
                nonces = derived_nonces;
            }
        }

        self.chain = Chain::from_persisted_state(blocks, expected_height, balances, nonces, stakes)?;

        if mode == "fast_rebuild" {
            storage
                .save_runtime_state(
                    self.chain.height(),
                    &self.chain.latest_block().hash,
                    &self.chain.balances,
                    &self.chain.nonces,
                    &self.chain.stakes,
                )
                .map_err(|e| format!("Fast rebuild state write failed: {}", e))?;
        }

        Ok(Some(mode))
    }

    fn restore_dao_state(&mut self, storage: &Storage) {
        if let Some(db_path) = storage.db_path() {
            if let Some(dir) = db_path.parent() {
                let dao_path = dir.join("dao_state.json");
                if dao_path.exists() {
                    match std::fs::read_to_string(&dao_path) {
                        Ok(json_str) => {
                            match serde_json::from_str::<serde_json::Value>(&json_str) {
                                Ok(v) => {
                                    self.dao = PlanetaryDAO::from_dict(&v);
                                    println!(
                                        "📋 DAO state restored ({} proposals)",
                                        self.dao.proposals.len()
                                    );
                                }
                                Err(e) => eprintln!("⚠️  DAO state parse error: {}", e),
                            }
                        }
                        Err(e) => eprintln!("⚠️  DAO state read error: {}", e),
                    }
                }
            }
        }
    }
}

fn force_full_replay() -> bool {
    std::env::var("REPRYNTT_FORCE_FULL_REPLAY")
        .map(|v| {
            matches!(
                v.trim().to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
        .unwrap_or(false)
}

// ── Async block loop ─────────────────────────────────────────────────────────

/// Run the block production loop as a tokio task.
///
/// This is the main loop that runs every `BLOCK_INTERVAL_SECS` seconds.
pub async fn run_block_loop(
    producer: Arc<Mutex<BlockProducer>>,
    storage: Arc<Storage>,
    gossip: Arc<GossipNode>,
) {
    println!(
        "⏱️  Block production loop started — {}s intervals (VRF leader election)",
        BLOCK_INTERVAL_SECS
    );

    loop {
        tokio::time::sleep(tokio::time::Duration::from_secs(BLOCK_INTERVAL_SECS)).await;

        let (state, reason) = evaluate_mining_gate(&producer, &gossip).await;
        if state != "enabled" {
            if !reason.is_empty() {
                eprintln!("⛔ Mining {}: {}", state, reason);
            }
            continue;
        }

        let block = {
            let mut prod = producer.lock().unwrap();
            prod.try_produce_block()
        };

        if let Some(block) = block {
            let next_height = block.index + 1;
            let block_hash = block.hash.clone();
            println!(
                "🎯 Block {} produced — hash: {}…  txs: {}",
                block.index,
                &block.hash[..16],
                block.transactions.len()
            );
            // Save to disk
            {
                let prod = producer.lock().unwrap();
                if let Err(e) = prod.save(&storage) {
                    eprintln!("⚠️  Save failed: {}", e);
                }
            }
            gossip.set_chain_tip(next_height, block_hash).await;
            // Push the new block to every peer so the network converges
            // immediately rather than waiting for the next pull-based height
            // poll. This is what prevents same-height forks from sticking when
            // multiple nodes mine: receivers either append (if it extends
            // their tip) or trigger bounded reorg to follow the longer chain.
            gossip.broadcast_block(&block).await;
        }
    }
}

async fn evaluate_mining_gate(
    producer: &Arc<Mutex<BlockProducer>>,
    gossip: &Arc<GossipNode>,
) -> (String, String) {
    let active_peers = gossip.peers.lock().await.count();
    let (best_peer_height, best_peer_addr) = {
        let peers = gossip.peers.lock().await;
        peers
            .best_height_peer()
            .map(|p| (p.chain_height, Some(p.addr.to_string())))
            .unwrap_or((0, None))
    };
    let allow_solo = std::env::var("REPRYNTT_ALLOW_SOLO_MINING")
        .or_else(|_| std::env::var("REPRYNTT_ALLOW_ISOLATED_MINING"))
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(false);
    let min_outbound_peers = std::env::var("REPRYNTT_MIN_OUTBOUND_PEERS")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .unwrap_or(1);
    let max_lag = std::env::var("REPRYNTT_MINING_MAX_LAG")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .unwrap_or(0);
    let require_checkpoint = std::env::var("REPRYNTT_REQUIRE_CHECKPOINT")
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(false);

    let bootstrap_peers = 0;

    let mut diagnostics = Vec::new();
    let (state, reason) = {
        let prod = producer.lock().unwrap();
        let local_height = prod.chain.height();
        if !prod.config.mining_enabled {
            (
                "disabled".to_string(),
                "mining disabled by configuration".to_string(),
            )
        } else if !prod.ibd_complete {
            (
                "paused_unsynced".to_string(),
                "initial block download is not complete".to_string(),
            )
        } else if prod.checkpoint_status == "below_checkpoint" {
            (
                "paused_unsynced".to_string(),
                "local chain is below latest signed checkpoint".to_string(),
            )
        } else if prod.checkpoint_status == "checkpoint_mismatch"
            || prod.checkpoint_status == "invalid_checkpoint"
        {
            (
                "paused_fork_detected".to_string(),
                prod.mining_pause_reason.clone(),
            )
        } else if require_checkpoint && prod.checkpoint_status == "no_checkpoint" {
            (
                "paused_unsynced".to_string(),
                "REPRYNTT_REQUIRE_CHECKPOINT=1 but no signed checkpoint is installed".to_string(),
            )
        } else if active_peers < min_outbound_peers && !allow_solo {
            (
                "paused_isolated".to_string(),
                format!(
                    "requires at least {} active P2P peer(s), currently connected to {}",
                    min_outbound_peers, active_peers
                ),
            )
        } else if best_peer_height > local_height.saturating_add(max_lag) {
            (
                "paused_unsynced".to_string(),
                format!(
                    "local height {} is behind best peer {} by {} block(s)",
                    local_height,
                    best_peer_height,
                    best_peer_height.saturating_sub(local_height)
                ),
            )
        } else if prod.sync_state == "failed_chain_validation" {
            (
                "paused_sync_error".to_string(),
                if prod.last_sync_error.is_empty() {
                    "recent sync failed with a chain validation error".to_string()
                } else {
                    format!("recent sync failed: {}", prod.last_sync_error)
                },
            )
        } else {
            ("enabled".to_string(), String::new())
        }
    };

    if active_peers == 0 {
        diagnostics.push(format!(
            "no active P2P sessions; set REPRYNTT_ALLOW_SOLO_MINING=true only for isolated testing"
        ));
    }
    if let Some(addr) = best_peer_addr {
        diagnostics.push(format!("best_peer={} height={}", addr, best_peer_height));
    }

    {
        let mut prod = producer.lock().unwrap();
        prod.set_mining_gate(&state, &reason, bootstrap_peers, diagnostics);
    }
    (state, reason)
}

// ── Helpers ──────────────────────────────────────────────────────────────────

fn now_f64() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("time went backwards")
        .as_secs_f64()
}

fn canonicalize_produced_block(block: Block) -> Block {
    let block_dict = block.to_dict();
    let block_json = serde_json::to_string(&block_dict).expect("block JSON serialization");
    let block_dict: BTreeMap<String, Value> =
        serde_json::from_str(&block_json).expect("block JSON deserialization");
    let mut block = Block::from_dict(&block_dict)
        .expect("locally produced block must survive canonical wire roundtrip");
    block.hash = block.calculate_hash();
    block
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::crypto;
    use crate::mempool::MIN_FEE_PLANCKS;

    fn test_config() -> NodeConfig {
        NodeConfig {
            address: "a1a4090aced69d411b6e62bf49944f295c85ed88".to_string(),
            measured_tflops: 5.4,
            compute_share: 1.0,
            tflops: 5.4,
            mining_enabled: true,
        }
    }

    #[test]
    fn test_producer_init() {
        let producer = BlockProducer::new(test_config());
        assert_eq!(producer.chain.height(), 1); // Genesis
        assert_eq!(producer.contributors.count(), 1);
        assert_eq!(producer.availability_contributors.count(), 1);
        assert_eq!(producer.current_supply(), 0);
    }

    #[test]
    fn test_non_mining_node_can_offer_availability_without_leader_eligibility() {
        let mut config = test_config();
        config.mining_enabled = false;
        let mut producer = BlockProducer::new(config);
        producer.ibd_complete = true;

        assert_eq!(producer.contributors.count(), 0);
        assert_eq!(producer.availability_contributors.count(), 1);
        assert!(
            producer.try_produce_block().is_none(),
            "non-mining compute providers must not become block producers"
        );
    }

    #[test]
    fn test_produce_block_not_ibd_complete() {
        let mut producer = BlockProducer::new(test_config());
        // IBD not complete — should not produce
        assert!(producer.try_produce_block().is_none());
    }

    #[test]
    fn test_produce_block_single_miner() {
        let mut producer = BlockProducer::new(test_config());
        producer.ibd_complete = true;

        // With only one miner, we're always the leader
        let block = producer.try_produce_block();
        assert!(block.is_some(), "Single miner should always produce");

        let block = block.unwrap();
        assert_eq!(block.index, 1);
        assert_eq!(block.miner_address, test_config().address);
        assert!(!block.transactions.is_empty()); // At least coinbase
        let wire_json = serde_json::to_string(&block.to_dict()).unwrap();
        let wire_dict: BTreeMap<String, Value> = serde_json::from_str(&wire_json).unwrap();
        let wire_block = Block::from_dict(&wire_dict).unwrap();
        Chain::validate_block_hash(&wire_block).expect("wire block hash should validate");

        // Chain should be at height 2
        assert_eq!(producer.chain.height(), 2);

        // Miner should have earned the coinbase
        let balance = producer
            .chain
            .balances
            .get(&test_config().address)
            .copied()
            .unwrap_or(0);
        assert!(balance > 0, "Miner should have earned coinbase");
    }

    #[test]
    fn test_produce_multiple_blocks() {
        let mut producer = BlockProducer::new(test_config());
        producer.ibd_complete = true;

        for _ in 0..5 {
            let block = producer.try_produce_block();
            assert!(block.is_some());
        }

        assert_eq!(producer.chain.height(), 6); // Genesis + 5 blocks
        producer
            .chain
            .validate_full()
            .expect("chain should validate");
    }

    #[test]
    fn test_save_and_load() {
        let config = test_config();
        let storage = Storage::in_memory().unwrap();

        // Produce some blocks
        let mut producer = BlockProducer::new(config.clone());
        producer.ibd_complete = true;
        for _ in 0..3 {
            producer.try_produce_block();
        }
        producer.save(&storage).unwrap();

        // Load into a new producer
        let mut producer2 = BlockProducer::new(config);
        producer2.load(&storage).unwrap();

        assert_eq!(producer2.load_mode, "fast_load");
        assert_eq!(producer2.chain.height(), producer.chain.height());
        assert_eq!(producer2.chain.nonces, producer.chain.nonces);
        assert_eq!(
            producer2.chain.latest_block().hash,
            producer.chain.latest_block().hash
        );
    }

    #[test]
    fn test_fast_rebuild_legacy_state_derives_nonces() {
        let config = test_config();
        let storage = Storage::in_memory().unwrap();

        let mut producer = BlockProducer::new(config.clone());
        producer.chain.balances.insert("alice".into(), 1_000);
        let mut tx = Transaction::new(
            "alice",
            "bob",
            100,
            "transfer",
            0,
            BTreeMap::new(),
            None,
            None,
            1,
        );
        tx.tx_hash = tx.calculate_hash();
        let block = Block::new(
            1,
            &producer.chain.latest_block().hash,
            producer.chain.latest_block().timestamp + 1.0,
            vec![tx],
            &config.address,
            BTreeMap::new(),
        );
        producer.chain.add_block_trusted(block).unwrap();

        for block in producer.chain.recent_iter() {
            storage.put_block(block).unwrap();
        }
        for (addr, bal) in &producer.chain.balances {
            storage.put_balance(addr, *bal).unwrap();
        }

        let mut loaded = BlockProducer::new(config);
        loaded.load(&storage).unwrap();

        assert_eq!(loaded.load_mode, "fast_rebuild");
        assert_eq!(loaded.chain.height(), 2);
        assert_eq!(loaded.chain.nonces.get("alice").copied(), Some(1));
        assert!(storage.get_chain_meta().unwrap().is_some());
        assert_eq!(storage.get_nonce("alice").unwrap(), 1);
    }

    #[test]
    fn test_tx_pool_included() {
        let mut producer = BlockProducer::new(test_config());
        producer.ibd_complete = true;

        // First produce a block so our miner has some balance
        producer.try_produce_block().unwrap();

        // Add a signed transfer to the mempool
        let (sk, pk) = crypto::generate_keypair();
        let from = crypto::address_from_pubkey(&pk);
        producer.chain.balances.insert(from.clone(), 5_000_000_000);
        let mut meta = BTreeMap::new();
        meta.insert("note".into(), Value::String("test transfer".into()));
        let mut tx = Transaction::new(
            &from,
            "recipient_123",
            100_000_000, // 1 CR
            "transfer",
            0,
            meta,
            None,
            Some(pk),
            1,
        );
        tx.sign(&sk);
        producer
            .mempool
            .add_transaction(tx, MIN_FEE_PLANCKS)
            .unwrap();

        // Next block should include the transfer
        let block = producer.try_produce_block().unwrap();
        let has_transfer = block.transactions.iter().any(|tx| tx.tx_type == "transfer");
        assert!(has_transfer, "Block should include the pending transfer");

        // Mempool should be drained after block production
        assert_eq!(
            producer.mempool.size(),
            0,
            "Mempool should be empty after block"
        );
    }
}
