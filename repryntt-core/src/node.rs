//! Node orchestrator — ties all subsystems into a running repryntt node.
//!
//! Responsibilities:
//! - CLI argument parsing
//! - Chain loading from storage (or migration from Python DB)
//! - RPC TCP server (JSON-RPC 2.0 over raw TCP)
//! - Block production loop
//! - P2P gossip networking
//! - IBD (Initial Block Download) on startup
//! - Graceful shutdown via Ctrl-C (tokio signal)

use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::sync::{mpsc, watch};

use crate::checkpoint::{
    self, CheckpointChainStatus, status_reason, verify_chain_contains_checkpoint,
};
use crate::contract::WorkloadContract;
use crate::dao::PlanetaryDAO;
use crate::genesis;
use crate::gossip::{GossipEvent, GossipNode};
use crate::migrate;
use crate::producer::{BlockProducer, NodeConfig, run_block_loop};
use crate::rpc::{self, NodeState};
use crate::staking::StakingManager;
use crate::storage::Storage;
use crate::sync::{SyncError, SyncManager, SyncOutcome};
use crate::token::TokenRegistry;

const LIVE_SYNC_INTERVAL_SECS: u64 = 30;
const SYNC_DEBOUNCE_MS: u64 = 500;
// Restored PoPW queues are nonce-sensitive: if a miner misses nonce N, every
// later nonce is rejected as a future transaction. Each tx announce currently
// opens a short TCP connection, so keep startup replay below the per-IP active
// connection guard by sending one restored tx at a time.
const RESTORED_MEMPOOL_REBROADCAST_BATCH: usize = 1;
const RESTORED_MEMPOOL_REBROADCAST_INTERVAL_SECS: u64 = 2;
const RESTORED_MEMPOOL_REBROADCAST_PEER_WAIT_SECS: u64 = 60;
const RESTORED_MEMPOOL_REBROADCAST_RETRY_SECS: u64 = 300;
const PEER_REDISCOVERY_INTERVAL_SECS: u64 = 120;
const PEER_REDISCOVERY_MIN_PEERS: usize = 2;

#[derive(Debug, Clone, Copy)]
enum SyncTrigger {
    Startup,
    PeerEvent,
    BlockAnnounce,
}

// ── Configuration ────────────────────────────────────────────────────────────

/// Node startup configuration.
#[derive(Debug, Clone)]
pub struct NodeArgs {
    /// Wallet address (miner identity).
    pub address: String,
    /// Hardware-measured TFLOPS before operator contribution limits.
    pub measured_tflops: f64,
    /// Operator-selected share of measured compute exposed to the network.
    pub compute_share: f64,
    /// Effective TFLOPS rating exposed to consensus/availability surfaces.
    pub tflops: f64,
    /// Path to the Rust-side SQLite database.
    pub data_dir: PathBuf,
    /// Enable block production.
    pub mining: bool,
    /// JSON-RPC listen address.
    pub rpc_bind: SocketAddr,
    /// P2P gossip listen port.
    pub p2p_port: u16,
    /// Optional Python DB path for migration.
    pub migrate_from: Option<PathBuf>,
    /// Optional node_state.json path for migration.
    pub node_state_path: Option<PathBuf>,
    /// Skip IBD on startup (for single-node testing).
    pub skip_ibd: bool,
    /// Seed nodes (resolved from REPRYNTT_SEEDS or hardcoded fallback).
    pub seeds: Vec<std::net::SocketAddr>,
}

impl Default for NodeArgs {
    fn default() -> Self {
        Self {
            address: String::new(),
            measured_tflops: 5.4,
            compute_share: 1.0,
            tflops: 5.4,
            data_dir: PathBuf::from("data"),
            mining: true,
            rpc_bind: "127.0.0.1:9332".parse().unwrap(),
            p2p_port: 5001,
            migrate_from: None,
            node_state_path: None,
            skip_ibd: false,
            seeds: vec![],
        }
    }
}

impl NodeArgs {
    pub fn db_path(&self) -> PathBuf {
        self.data_dir.join("chain.db")
    }

    pub fn mempool_path(&self) -> PathBuf {
        self.data_dir.join("mempool.json")
    }

    /// Parse args from environment variables (simple, no external deps).
    pub fn from_env() -> Self {
        let mut args = Self::default();

        if let Ok(addr) = std::env::var("REPRYNTT_ADDRESS") {
            args.address = addr.trim().to_string();
        }
        if let Ok(tflops) = std::env::var("REPRYNTT_TFLOPS") {
            if let Ok(t) = tflops.parse::<f64>() {
                args.measured_tflops = t.max(0.0);
            }
        }
        if let Ok(dir) = std::env::var("REPRYNTT_DATA_DIR") {
            args.data_dir = PathBuf::from(dir);
        }
        args.compute_share = load_compute_share(&args.data_dir);
        if let Ok(share) = std::env::var("REPRYNTT_COMPUTE_SHARE") {
            if let Ok(s) = share.parse::<f64>() {
                args.compute_share = s.clamp(0.0, 1.0);
            }
        }
        args.tflops = args.measured_tflops * args.compute_share;
        if let Ok(mining) = std::env::var("REPRYNTT_MINING") {
            args.mining = mining != "0" && mining.to_lowercase() != "false";
        }
        if let Ok(bind) = std::env::var("REPRYNTT_RPC_BIND") {
            if let Ok(addr) = bind.parse() {
                args.rpc_bind = addr;
            }
        }
        if let Ok(port) = std::env::var("REPRYNTT_P2P_PORT") {
            if let Ok(p) = port.parse() {
                args.p2p_port = p;
            }
        }
        if let Ok(path) = std::env::var("REPRYNTT_MIGRATE_FROM") {
            args.migrate_from = Some(PathBuf::from(path));
        }
        if let Ok(path) = std::env::var("REPRYNTT_NODE_STATE") {
            args.node_state_path = Some(PathBuf::from(path));
        }
        if let Ok(val) = std::env::var("REPRYNTT_SKIP_IBD") {
            args.skip_ibd = val != "0" && val.to_lowercase() != "false";
        }

        // Resolve seeds (REPRYNTT_SEEDS env > node.conf > hardcoded fallback)
        args.seeds = crate::network::resolve_seeds(args.p2p_port, &args.data_dir);

        args
    }
}

fn default_compute_config_path(data_dir: &Path) -> PathBuf {
    if let Ok(path) = std::env::var("REPRYNTT_COMPUTE_CONFIG") {
        let path = path.trim();
        if !path.is_empty() {
            return PathBuf::from(path);
        }
    }
    if let Some(parent) = data_dir.parent() {
        return parent.join("data").join("compute_config.json");
    }
    data_dir.join("compute_config.json")
}

fn load_compute_share(data_dir: &Path) -> f64 {
    let path = default_compute_config_path(data_dir);
    let Ok(raw) = std::fs::read_to_string(path) else {
        return 1.0;
    };
    let Ok(value) = serde_json::from_str::<serde_json::Value>(&raw) else {
        return 1.0;
    };
    value
        .get("compute_share")
        .and_then(|v| v.as_f64())
        .unwrap_or(1.0)
        .clamp(0.0, 1.0)
}

// ── Node handle (returned by boot, used by tests) ───────────────────────────

/// A running node. Dropping it triggers graceful shutdown.
pub struct NodeHandle {
    /// Send on this channel to trigger shutdown.
    shutdown_tx: watch::Sender<bool>,
    /// Block producer (shared state).
    pub producer: Arc<Mutex<BlockProducer>>,
    /// Storage backend.
    pub storage: Arc<Storage>,
    /// Gossip node.
    pub gossip: Arc<GossipNode>,
}

impl NodeHandle {
    /// Request graceful shutdown.
    pub fn shutdown(&self) {
        let _ = self.shutdown_tx.send(true);
    }

    /// Get a snapshot of RPC NodeState from the current chain.
    pub fn rpc_state(&self) -> NodeState {
        let prod = self.producer.lock().unwrap();
        // Snapshot mempool
        let mempool_snap: Vec<serde_json::Value> = prod
            .mempool
            .entries()
            .iter()
            .map(|e| {
                serde_json::json!({
                    "tx_hash": e.tx.tx_hash,
                    "from_address": e.tx.from_address,
                    "to_address": e.tx.to_address,
                    "amount": e.tx.amount,
                    "amount_cr": e.tx.amount as f64 / 100_000_000.0,
                    "tx_type": e.tx.tx_type,
                    "nonce": e.tx.nonce,
                    "fee": e.fee_plancks,
                    "timestamp": e.tx.timestamp,
                    "size_bytes": e.size_bytes,
                    "metadata": e.tx.metadata,
                })
            })
            .collect();
        let mempool_sz = prod.mempool.size();
        let availability_contributors: Vec<serde_json::Value> = prod
            .availability_contributors
            .contributors
            .iter()
            .map(|(address, tflops)| {
                serde_json::json!({
                    "address": address,
                    "effective_tflops": tflops,
                })
            })
            .collect();
        let availability_tflops: f64 = prod
            .availability_contributors
            .contributors
            .values()
            .copied()
            .sum();
        let mut state = NodeState {
            chain: prod.chain.clone(),
            staking: StakingManager::new(),
            dao: PlanetaryDAO::new(),
            contract: WorkloadContract::new(),
            tokens: TokenRegistry::new(),
            balances: HashMap::new(),
            stakes: HashMap::new(),
            node_address: prod.config.address.clone(),
            peer_count: 0,
            mempool_snapshot: mempool_snap,
            mempool_size: mempool_sz,
            mining_state: prod.mining_state.clone(),
            mining_pause_reason: prod.mining_pause_reason.clone(),
            fork_status: prod.fork_status.clone(),
            checkpoint_status: prod.checkpoint_status.clone(),
            checkpoint_height: prod.checkpoint.as_ref().map(|c| c.height),
            checkpoint_hash: prod.checkpoint.as_ref().map(|c| c.hash.clone()),
            bootstrap_peer_count: prod.bootstrap_peer_count,
            peer_diagnostics: prod.peer_diagnostics.clone(),
            sync_state: prod.sync_state.clone(),
            last_sync_error: prod.last_sync_error.clone(),
            last_sync_at: prod.last_sync_at,
            best_peer_height: prod.best_peer_height,
            height_lag: prod.best_peer_height.saturating_sub(prod.chain.height()),
            local_effective_tflops: prod.config.tflops,
            local_measured_tflops: prod.config.measured_tflops,
            local_compute_share: prod.config.compute_share,
            availability_tflops,
            availability_contributor_count: availability_contributors.len(),
            availability_contributors,
        };
        // Copy DAO state from producer so proposals/votes persist across calls
        state.dao.proposals = prod.dao.proposals.clone();
        state.dao.allocations = prod.dao.allocations.clone();
        state.dao.proposal_counter = prod.dao.proposal_counter;
        state.sync_from_chain();
        state
    }
}

// ── Boot ─────────────────────────────────────────────────────────────────────

/// Boot the node: load chain, optionally migrate, start all subsystems.
///
/// Returns a `NodeHandle` that keeps the node alive. Call `handle.shutdown()`
/// or drop it to stop.
pub async fn boot(args: NodeArgs) -> Result<NodeHandle, String> {
    if args.address.trim().is_empty() {
        return Err(
            "Missing REPRYNTT_ADDRESS. Runtime node identity must be this device's \
             wallet, not the genesis creator. Run `repryntt chain install` or set \
             REPRYNTT_ADDRESS to the address in ~/.repryntt/wallet/node_wallet.json."
                .to_string(),
        );
    }

    // Ensure data dir exists
    std::fs::create_dir_all(&args.data_dir)
        .map_err(|e| format!("Cannot create data dir: {}", e))?;

    let db_path = args.db_path();

    // ── Migration (if requested) ─────────────────────────────────────────
    if let Some(ref py_db) = args.migrate_from {
        println!("📦 Migrating from Python database: {}", py_db.display());
        let report = migrate::migrate_from_python(py_db, args.node_state_path.as_ref(), &db_path)?;
        println!(
            "   {} blocks imported, {} balances, {} stakes",
            report.blocks_imported, report.balances_imported, report.stakes_imported
        );
        if !report.warnings.is_empty() {
            for w in &report.warnings {
                println!("   ⚠  {}", w);
            }
        }
        if !report.success {
            return Err(format!("Migration failed: {:?}", report.error));
        }
        println!(
            "   ✅ Migration complete — chain height {}",
            report.final_height
        );
    }

    // ── Open storage & load chain ────────────────────────────────────────
    let storage = Arc::new(Storage::open(&db_path).map_err(|e| format!("Cannot open DB: {}", e))?);

    let config = NodeConfig {
        address: args.address.clone(),
        measured_tflops: args.measured_tflops,
        compute_share: args.compute_share,
        tflops: args.tflops,
        mining_enabled: args.mining,
    };

    let mut producer = BlockProducer::new(config);

    // Load existing chain from storage
    producer.load(&storage)?;
    let height = producer.chain.height();
    let tip_hash = producer.chain.latest_block().hash.clone();

    // Restore pending transactions saved before the previous shutdown so
    // locally-submitted workloads aren't lost when the node restarts.
    let mempool_path = args.mempool_path();
    match producer.mempool.load_from_path(&mempool_path) {
        Ok(0) => {}
        Ok(n) => println!("📋 Mempool restored: {} pending tx(s)", n),
        Err(e) => eprintln!(
            "⚠️  Mempool load failed ({}): {}",
            mempool_path.display(),
            e
        ),
    }
    let confirmed_history = producer.chain.recent_as_vec();
    prune_confirmed_mempool(&mut producer, &confirmed_history, &mempool_path);
    println!(
        "⛓  Chain loaded: height={}, balances={}, stakes={}, load_mode={}",
        height,
        producer.chain.balances.len(),
        producer.chain.stakes.len(),
        producer.load_mode
    );

    let checkpoint = checkpoint::load_latest_checkpoint(&args.data_dir)?;
    let checkpoint_status = verify_chain_contains_checkpoint(&producer.chain, checkpoint.as_ref());
    match &checkpoint_status {
        CheckpointChainStatus::Verified => {
            if let Some(cp) = checkpoint.as_ref() {
                println!(
                    "✅ Checkpoint verified: height={} hash={}… signer={}…",
                    cp.height,
                    &cp.hash[..16.min(cp.hash.len())],
                    &cp.signer_address[..16.min(cp.signer_address.len())]
                );
            }
        }
        CheckpointChainStatus::NoCheckpoint => {
            println!("⚠️  No signed checkpoint installed; fork safety is not fully armed yet");
        }
        CheckpointChainStatus::BelowCheckpoint => {
            println!("⏸️  Local chain is below checkpoint; node will sync before mining");
        }
        CheckpointChainStatus::CheckpointMismatch | CheckpointChainStatus::InvalidCheckpoint(_) => {
            return Err(format!(
                "Checkpoint safety halt: {}",
                status_reason(&checkpoint_status)
            ));
        }
    }
    producer.set_checkpoint_status(checkpoint.clone(), checkpoint_status.clone());

    let producer = Arc::new(Mutex::new(producer));

    // ── Shutdown channel ─────────────────────────────────────────────────
    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    let (sync_tx, sync_rx) = mpsc::channel::<SyncTrigger>(64);

    // ── Gossip / P2P ─────────────────────────────────────────────────────
    let gossip = Arc::new(GossipNode::new(
        &args.address,
        args.p2p_port,
        args.seeds.clone(),
    ));
    gossip.set_chain_tip(height, tip_hash).await;

    // ── IBD (Initial Block Download) ─────────────────────────────────────
    if !args.skip_ibd {
        let sync_mgr = SyncManager::new(gossip.clone()).with_checkpoint(checkpoint.clone());
        let local_height = {
            let p = producer.lock().unwrap();
            p.chain.height()
        };

        // Start gossip briefly to discover peers for IBD
        let gossip_for_ibd = gossip.clone();
        let mut ibd_shutdown = shutdown_rx.clone();
        let ibd_handle = tokio::spawn(async move {
            tokio::select! {
                _ = gossip_for_ibd.run() => {},
                _ = ibd_shutdown.changed() => {},
            }
        });

        // Wait briefly for peer discovery
        tokio::time::sleep(Duration::from_secs(2)).await;

        match sync_mgr.initial_block_download(local_height).await {
            Ok(new_blocks) if !new_blocks.is_empty() => {
                let confirmed_blocks = new_blocks.clone();
                let mut p = producer.lock().unwrap();
                let added = p
                    .chain
                    .add_blocks(new_blocks)
                    .map_err(|e| format!("IBD chain add failed: {}", e))?;
                if added > 0 {
                    prune_confirmed_mempool(&mut p, &confirmed_blocks, &mempool_path);
                    p.save(&storage)
                        .map_err(|e| format!("IBD save failed: {}", e))?;
                    let h = p.chain.height();
                    let tip = p.chain.latest_block().hash.clone();
                    println!("📥 IBD: imported {} blocks → height {}", added, h);
                    gossip.set_chain_tip(h, tip).await;
                }
            }
            Ok(_) => println!("📥 IBD: already at tip"),
            Err(e) => println!("📥 IBD: skipped ({})", e),
        }

        // Stop the temporary gossip task
        ibd_handle.abort();
    }

    // Re-check checkpoint after IBD, then mark IBD complete. The mining gate
    // still decides whether block production is allowed.
    {
        let mut p = producer.lock().unwrap();
        let status = verify_chain_contains_checkpoint(&p.chain, checkpoint.as_ref());
        match &status {
            CheckpointChainStatus::CheckpointMismatch
            | CheckpointChainStatus::InvalidCheckpoint(_) => {
                return Err(format!(
                    "Checkpoint safety halt after IBD: {}",
                    status_reason(&status)
                ));
            }
            _ => p.set_checkpoint_status(checkpoint.clone(), status),
        }
        p.ibd_complete = true;
    }

    // ── Start subsystem tasks ────────────────────────────────────────────

    // Give gossip access to storage so it can serve blocks from disk during IBD
    gossip.set_storage(storage.clone()).await;

    // 1. P2P gossip (permanent)
    let gossip_task = gossip.clone();
    let mut gossip_shutdown = shutdown_rx.clone();
    tokio::spawn(async move {
        tokio::select! {
            _ = gossip_task.run() => {},
            _ = gossip_shutdown.changed() => {},
        }
    });

    // 2. Block production loop
    if args.mining {
        let prod_arc = producer.clone();
        let store_arc = storage.clone();
        let gossip_arc = gossip.clone();
        let mut mining_shutdown = shutdown_rx.clone();
        tokio::spawn(async move {
            tokio::select! {
                _ = run_block_loop(prod_arc, store_arc, gossip_arc) => {},
                _ = mining_shutdown.changed() => {},
            }
        });
    }

    // 3. Event processing (gossip events → chain)
    {
        let prod_arc = producer.clone();
        let store_arc = storage.clone();
        let gossip_arc = gossip.clone();
        let sync_tx_events = sync_tx.clone();
        let mempool_path_events = mempool_path.clone();
        let mut event_shutdown = shutdown_rx.clone();
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    _ = tokio::time::sleep(Duration::from_millis(500)) => {
                        process_gossip_events(&prod_arc, &store_arc, &gossip_arc, &sync_tx_events, &mempool_path_events).await;
                    }
                    _ = event_shutdown.changed() => break,
                }
            }
        });
    }

    // 4. Live catch-up sync. IBD only runs at startup; this keeps non-mining
    // full nodes moving forward as peers announce or produce new blocks.
    {
        let prod_arc = producer.clone();
        let store_arc = storage.clone();
        let gossip_arc = gossip.clone();
        let checkpoint_for_sync = checkpoint.clone();
        let mempool_path_sync = mempool_path.clone();
        let mut sync_shutdown = shutdown_rx.clone();
        let mut sync_rx = sync_rx;
        tokio::spawn(async move {
            let sync_mgr =
                SyncManager::new(gossip_arc.clone()).with_checkpoint(checkpoint_for_sync);
            loop {
                tokio::select! {
                    trigger = sync_rx.recv() => {
                        if trigger.is_none() {
                            break;
                        }
                        tokio::time::sleep(Duration::from_millis(SYNC_DEBOUNCE_MS)).await;
                        while sync_rx.try_recv().is_ok() {}
                        live_sync_once(&prod_arc, &store_arc, &gossip_arc, &sync_mgr, &mempool_path_sync).await;
                    }
                    _ = tokio::time::sleep(Duration::from_secs(LIVE_SYNC_INTERVAL_SECS)) => {
                        live_sync_once(&prod_arc, &store_arc, &gossip_arc, &sync_mgr, &mempool_path_sync).await;
                    }
                    _ = sync_shutdown.changed() => break,
                }
            }
        });
    }
    let _ = sync_tx.try_send(SyncTrigger::Startup);

    // 5. RPC server
    {
        let prod_arc = producer.clone();
        let gossip_arc = gossip.clone();
        let rpc_bind = args.rpc_bind;
        let node_address = args.address.clone();
        let mut rpc_shutdown = shutdown_rx.clone();
        tokio::spawn(async move {
            if let Err(e) = run_rpc_server(
                rpc_bind,
                prod_arc,
                gossip_arc,
                &node_address,
                &mut rpc_shutdown,
            )
            .await
            {
                eprintln!("RPC server error: {}", e);
            }
        });
    }

    // 6. Periodic save (chain state)
    {
        let prod_arc = producer.clone();
        let store_arc = storage.clone();
        let mut save_shutdown = shutdown_rx.clone();
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    _ = tokio::time::sleep(Duration::from_secs(60)) => {
                        let p = prod_arc.lock().unwrap();
                        if let Err(e) = p.save(&store_arc) {
                            eprintln!("⚠️  Periodic save failed: {}", e);
                        }
                    }
                    _ = save_shutdown.changed() => break,
                }
            }
        });
    }

    // 7. Periodic mempool save — keeps pending transactions durable across
    // operator-initiated restarts so locally-submitted workloads aren't lost.
    {
        let prod_arc = producer.clone();
        let mempool_path = mempool_path.clone();
        let mut mp_shutdown = shutdown_rx.clone();
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    _ = tokio::time::sleep(Duration::from_secs(30)) => {
                        let snapshot = {
                            let p = prod_arc.lock().unwrap();
                            if p.mempool.size() == 0 {
                                None
                            } else {
                                Some(p.mempool.entries().iter().map(|e| (**e).clone()).collect::<Vec<_>>())
                            }
                        };
                        // Reconstruct a temporary Mempool for serialization without
                        // holding the producer lock across the disk write.
                        if let Some(entries) = snapshot {
                            let mut tmp = crate::mempool::Mempool::new();
                            for entry in entries {
                                let _ = tmp.add_transaction(entry.tx, entry.fee_plancks);
                            }
                            if let Err(e) = tmp.save_to_path(&mempool_path) {
                                eprintln!("⚠️  Mempool save failed: {}", e);
                            }
                        }
                    }
                    _ = mp_shutdown.changed() => {
                        // One final save on graceful shutdown.
                        let snapshot = {
                            let p = prod_arc.lock().unwrap();
                            p.mempool.entries().iter().map(|e| (**e).clone()).collect::<Vec<_>>()
                        };
                        let mut tmp = crate::mempool::Mempool::new();
                        for entry in snapshot {
                            let _ = tmp.add_transaction(entry.tx, entry.fee_plancks);
                        }
                        if let Err(e) = tmp.save_to_path(&mempool_path) {
                            eprintln!("⚠️  Mempool shutdown save failed: {}", e);
                        }
                        break;
                    }
                }
            }
        });
    }

    // 8. Background peer rediscovery. Seed DNS and home networking are both
    // allowed to be temporarily broken at startup; keep retrying so non-technical
    // users do not need to edit env files or restart by hand.
    {
        let gossip_arc = gossip.clone();
        let data_dir = args.data_dir.clone();
        let p2p_port = args.p2p_port;
        let mut discovery_shutdown = shutdown_rx.clone();
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    _ = tokio::time::sleep(Duration::from_secs(PEER_REDISCOVERY_INTERVAL_SECS)) => {
                        run_peer_rediscovery(&gossip_arc, p2p_port, &data_dir).await;
                    }
                    _ = discovery_shutdown.changed() => break,
                }
            }
        });
    }

    // 9. Restored-mempool rebroadcast — txs loaded from disk on startup
    // need to be re-announced to peers, otherwise a node that holds the only
    // copy of a pending tx and never receives a fresh submit_* RPC will leave
    // that tx stranded indefinitely. Pace the rebroadcast below the default
    // per-IP tx announce limiter so restarts do not look like spam.
    {
        let prod_arc = producer.clone();
        let gossip_arc = gossip.clone();
        let mut rb_shutdown = shutdown_rx.clone();
        tokio::spawn(async move {
            tokio::select! {
                _ = tokio::time::sleep(Duration::from_secs(10)) => {
                    loop {
                        let mut waited = 0u64;
                        let peer_count = gossip_arc.peers.lock().await.count();
                        while peer_count == 0 && waited < RESTORED_MEMPOOL_REBROADCAST_PEER_WAIT_SECS {
                            tokio::select! {
                                _ = tokio::time::sleep(Duration::from_secs(5)) => {
                                    waited += 5;
                                }
                                _ = rb_shutdown.changed() => return,
                            }
                            if gossip_arc.peers.lock().await.count() > 0 {
                                break;
                            }
                        }

                        let mut pending: Vec<crate::transaction::Transaction> = {
                            let p = prod_arc.lock().unwrap();
                            p.mempool.entries().iter().map(|e| e.tx.clone()).collect()
                        };
                        pending.sort_by(|a, b| {
                            a.from_address
                                .cmp(&b.from_address)
                                .then_with(|| a.nonce.cmp(&b.nonce))
                                .then_with(|| {
                                    a.timestamp
                                        .partial_cmp(&b.timestamp)
                                        .unwrap_or(std::cmp::Ordering::Equal)
                                })
                        });

                        if pending.is_empty() {
                            return;
                        }

                        let peer_count = gossip_arc.peers.lock().await.count();
                        if peer_count == 0 {
                            eprintln!(
                                "⚠️  Restored mempool has {} tx(s), but no peers are connected yet; retrying in {}s",
                                pending.len(),
                                RESTORED_MEMPOOL_REBROADCAST_RETRY_SECS,
                            );
                            tokio::select! {
                                _ = tokio::time::sleep(Duration::from_secs(RESTORED_MEMPOOL_REBROADCAST_RETRY_SECS)) => continue,
                                _ = rb_shutdown.changed() => return,
                            }
                        }

                        println!(
                            "📡 Rebroadcasting {} restored mempool tx(s) to {} peer(s) in batches of {} every {}s",
                            pending.len(),
                            peer_count,
                            RESTORED_MEMPOOL_REBROADCAST_BATCH,
                            RESTORED_MEMPOOL_REBROADCAST_INTERVAL_SECS
                        );
                        let total = pending.len();
                        for (batch_idx, batch) in pending
                            .chunks(RESTORED_MEMPOOL_REBROADCAST_BATCH)
                            .enumerate()
                        {
                            for tx in batch {
                                gossip_arc.broadcast_tx(tx).await;
                            }
                            let sent = ((batch_idx + 1) * RESTORED_MEMPOOL_REBROADCAST_BATCH)
                                .min(total);
                            if sent < total {
                                println!("📡 Restored mempool rebroadcast {}/{}", sent, total);
                                tokio::time::sleep(Duration::from_secs(
                                    RESTORED_MEMPOOL_REBROADCAST_INTERVAL_SECS,
                                ))
                                .await;
                            }
                        }

                        tokio::select! {
                            _ = tokio::time::sleep(Duration::from_secs(RESTORED_MEMPOOL_REBROADCAST_RETRY_SECS)) => {}
                            _ = rb_shutdown.changed() => return,
                        }
                    }
                }
                _ = rb_shutdown.changed() => {}
            }
        });
    }

    Ok(NodeHandle {
        shutdown_tx,
        producer,
        storage,
        gossip,
    })
}

async fn run_peer_rediscovery(gossip: &Arc<GossipNode>, p2p_port: u16, data_dir: &Path) {
    let current_peers = gossip.peers.lock().await.count();
    if current_peers >= PEER_REDISCOVERY_MIN_PEERS {
        return;
    }

    let data_dir = data_dir.to_path_buf();
    let seeds =
        tokio::task::spawn_blocking(move || crate::network::resolve_seeds(p2p_port, &data_dir))
            .await
            .unwrap_or_default();
    if seeds.is_empty() {
        eprintln!("⚠️  Peer rediscovery: no seed peers resolved");
        return;
    }

    println!(
        "🔎 Peer rediscovery: {} peer(s) connected, trying {} seed(s)",
        current_peers,
        seeds.len()
    );
    for seed in seeds {
        if gossip.peers.lock().await.count() >= PEER_REDISCOVERY_MIN_PEERS {
            break;
        }
        let _ = gossip.connect_to_peer(seed).await;
    }
}

// ── RPC TCP server ───────────────────────────────────────────────────────────

/// Run a TCP server that speaks JSON-RPC 2.0 (framed with rpc::wire_encode/decode).
async fn run_rpc_server(
    bind: SocketAddr,
    producer: Arc<Mutex<BlockProducer>>,
    gossip: Arc<GossipNode>,
    node_address: &str,
    shutdown: &mut watch::Receiver<bool>,
) -> Result<(), String> {
    let listener = TcpListener::bind(bind)
        .await
        .map_err(|e| format!("RPC bind failed: {}", e))?;

    println!("🔌 RPC server listening on {}", bind);

    loop {
        tokio::select! {
            accept = listener.accept() => {
                match accept {
                    Ok((stream, peer)) => {
                        let prod = producer.clone();
                        let gos = gossip.clone();
                        let addr = node_address.to_string();
                        tokio::spawn(async move {
                            if let Err(e) = handle_rpc_connection(stream, &prod, &gos, &addr).await {
                                eprintln!("RPC client {} error: {}", peer, e);
                            }
                        });
                    }
                    Err(e) => {
                        eprintln!("RPC accept error: {}", e);
                    }
                }
            }
            _ = shutdown.changed() => {
                println!("🔌 RPC server shutting down");
                break;
            }
        }
    }

    Ok(())
}

/// Handle a single RPC connection (one request-response per connection).
async fn handle_rpc_connection(
    mut stream: tokio::net::TcpStream,
    producer: &Arc<Mutex<BlockProducer>>,
    gossip: &Arc<GossipNode>,
    node_address: &str,
) -> Result<(), String> {
    // Read the 4-byte wire-frame header (big-endian length)
    let mut header = [0u8; 4];
    stream
        .read_exact(&mut header)
        .await
        .map_err(|e| format!("Read header error: {}", e))?;

    let payload_len = u32::from_be_bytes(header) as usize;
    if payload_len == 0 || payload_len > 4 * 1024 * 1024 {
        return Err(format!("Invalid payload length: {}", payload_len));
    }

    // Read exactly the payload
    let mut request_bytes = vec![0u8; payload_len];
    stream
        .read_exact(&mut request_bytes)
        .await
        .map_err(|e| format!("Read payload error: {}", e))?;

    let response = match rpc::parse_request(&request_bytes) {
        Ok(req) => {
            // Build RPC state snapshot (with DAO from producer)
            let mut state = {
                let (peer_count, best_peer_height) = {
                    let peers = gossip.peers.lock().await;
                    (
                        peers.count(),
                        peers
                            .best_height_peer()
                            .map(|p| p.chain_height)
                            .unwrap_or(0),
                    )
                };
                let p = producer.lock().unwrap();
                // Snapshot mempool
                let mempool_snap: Vec<serde_json::Value> = p
                    .mempool
                    .entries()
                    .iter()
                    .map(|e| {
                        serde_json::json!({
                            "tx_hash": e.tx.tx_hash,
                            "from_address": e.tx.from_address,
                            "to_address": e.tx.to_address,
                            "amount": e.tx.amount,
                            "amount_cr": e.tx.amount as f64 / 100_000_000.0,
                            "tx_type": e.tx.tx_type,
                            "nonce": e.tx.nonce,
                            "fee": e.fee_plancks,
                            "timestamp": e.tx.timestamp,
                            "size_bytes": e.size_bytes,
                            "metadata": e.tx.metadata,
                        })
                    })
                    .collect();
                let mempool_sz = p.mempool.size();
                let availability_contributors: Vec<serde_json::Value> = p
                    .availability_contributors
                    .contributors
                    .iter()
                    .map(|(address, tflops)| {
                        serde_json::json!({
                            "address": address,
                            "effective_tflops": tflops,
                        })
                    })
                    .collect();
                let availability_tflops: f64 = p
                    .availability_contributors
                    .contributors
                    .values()
                    .copied()
                    .sum();
                let mut st = NodeState {
                    chain: p.chain.clone(),
                    staking: StakingManager::new(),
                    dao: PlanetaryDAO::new(),
                    contract: WorkloadContract::new(),
                    tokens: TokenRegistry::new(),
                    balances: HashMap::new(),
                    stakes: HashMap::new(),
                    node_address: node_address.to_string(),
                    peer_count,
                    mempool_snapshot: mempool_snap,
                    mempool_size: mempool_sz,
                    mining_state: p.mining_state.clone(),
                    mining_pause_reason: p.mining_pause_reason.clone(),
                    fork_status: p.fork_status.clone(),
                    checkpoint_status: p.checkpoint_status.clone(),
                    checkpoint_height: p.checkpoint.as_ref().map(|c| c.height),
                    checkpoint_hash: p.checkpoint.as_ref().map(|c| c.hash.clone()),
                    bootstrap_peer_count: p.bootstrap_peer_count,
                    peer_diagnostics: p.peer_diagnostics.clone(),
                    sync_state: p.sync_state.clone(),
                    last_sync_error: p.last_sync_error.clone(),
                    last_sync_at: p.last_sync_at,
                    best_peer_height: best_peer_height.max(p.best_peer_height),
                    height_lag: best_peer_height
                        .max(p.best_peer_height)
                        .saturating_sub(p.chain.height()),
                    local_effective_tflops: p.config.tflops,
                    local_measured_tflops: p.config.measured_tflops,
                    local_compute_share: p.config.compute_share,
                    availability_tflops,
                    availability_contributor_count: availability_contributors.len(),
                    availability_contributors,
                };
                // Copy DAO state from producer
                st.dao.proposals = p.dao.proposals.clone();
                st.dao.allocations = p.dao.allocations.clone();
                st.dao.proposal_counter = p.dao.proposal_counter;
                st.sync_from_chain();
                st
            };

            // For DAO mutation methods, handle on the mutable state then write back
            let is_dao_mutation = matches!(
                req.method.as_str(),
                "submit_proposal" | "vote_proposal" | "execute_proposal"
            );

            let mut resp = rpc::handle_request(&req, &mut state);

            // Write DAO mutations back to producer
            if is_dao_mutation && resp.error.is_none() {
                let mut p = producer.lock().unwrap();
                p.dao.proposals = state.dao.proposals;
                p.dao.allocations = state.dao.allocations;
                p.dao.proposal_counter = state.dao.proposal_counter;
            }

            // If a transaction submission was accepted, add it to the mempool.
            if req.method == "submit_transaction"
                || req.method == "submit_productive_work"
                || req.method == "submit_local_credit"
            {
                if let Some(result) = resp.result.as_ref() {
                    if result
                        .get("accepted")
                        .and_then(|v| v.as_bool())
                        .unwrap_or(false)
                    {
                        let built_tx = match req.method.as_str() {
                            "submit_productive_work" => {
                                rpc::build_productive_work_transaction_from_params(
                                    &req.params,
                                    &state.node_address,
                                )
                            }
                            "submit_local_credit" => {
                                rpc::build_local_credit_transaction_from_params(
                                    &req.params,
                                    &state.node_address,
                                )
                            }
                            _ => rpc::build_transaction_from_params(&req.params),
                        };

                        match built_tx {
                            Ok(tx) => {
                                let fee =
                                    req.params.get("fee").and_then(|v| v.as_i64()).unwrap_or(0);
                                let add_result = {
                                    let mut p = producer.lock().unwrap();
                                    let balances = p.chain.balances.clone();
                                    let nonces = p.chain.nonces.clone();
                                    let stakes = p.chain.stakes.clone();
                                    p.mempool.add_transaction_validated(
                                        tx.clone(),
                                        fee,
                                        &balances,
                                        &nonces,
                                        &stakes,
                                    )
                                };
                                match add_result {
                                    Ok(()) => {
                                        println!(
                                            "📥 TX added to mempool ({})",
                                            result
                                                .get("tx_hash")
                                                .and_then(|v| v.as_str())
                                                .unwrap_or("?")
                                        );
                                        // Propagate to peers so any node — not just
                                        // the one that received the submission —
                                        // can include this tx in a block.
                                        gossip.broadcast_tx(&tx).await;
                                    }
                                    Err(e) => {
                                        eprintln!("⚠️ Mempool rejected TX: {}", e);
                                        resp = rpc::RpcResponse::error(
                                            req.id.clone(),
                                            rpc::INVALID_PARAMS,
                                            format!("Mempool rejected TX: {}", e),
                                        );
                                    }
                                }
                            }
                            Err((code, msg)) => {
                                resp = rpc::RpcResponse::error(req.id.clone(), code, msg);
                            }
                        }
                    }
                }
            }

            resp
        }
        Err(err_resp) => err_resp,
    };

    let resp_json = serde_json::to_vec(&response).map_err(|e| format!("Serialize error: {}", e))?;
    let framed = rpc::wire_encode(&resp_json);

    stream
        .write_all(&framed)
        .await
        .map_err(|e| format!("Write error: {}", e))?;

    Ok(())
}

// ── Gossip event processor ───────────────────────────────────────────────────

async fn live_sync_once(
    producer: &Arc<Mutex<BlockProducer>>,
    storage: &Arc<Storage>,
    gossip: &Arc<GossipNode>,
    sync_mgr: &SyncManager,
    mempool_path: &Path,
) {
    let best_peer_height = {
        let peers = gossip.peers.lock().await;
        peers
            .best_height_peer()
            .map(|p| p.chain_height)
            .unwrap_or(0)
    };

    let (local_height, local_chain) = {
        let p = producer.lock().unwrap();
        (p.chain.height(), p.chain.clone())
    };

    {
        let mut p = producer.lock().unwrap();
        p.set_sync_status("syncing", "", best_peer_height);
    }

    let outcome = match sync_mgr.sync_chain(&local_chain).await {
        Ok(outcome) => outcome,
        Err(e) => {
            let msg = e.to_string();
            if !msg.contains("No peers available") {
                eprintln!("⚠️  Live sync skipped: {}", msg);
            }
            let state = match e {
                SyncError::NoPeers => "idle",
                SyncError::ChainValidation(_) => "failed_chain_validation",
                _ => "failed",
            };
            let mut p = producer.lock().unwrap();
            p.set_sync_status(state, &msg, best_peer_height);
            return;
        }
    };

    let new_blocks = match outcome {
        SyncOutcome::UpToDate => {
            let mut p = producer.lock().unwrap();
            p.set_sync_status("complete", "", best_peer_height.max(local_height));
            return;
        }
        SyncOutcome::Append(blocks) => blocks,
        SyncOutcome::Reorg {
            ancestor_height,
            blocks,
        } => {
            let (tip_update, shared_blocks) = {
                let mut p = producer.lock().unwrap();
                match crate::chain::Chain::from_blocks(blocks) {
                    Ok(chain) => {
                        let confirmed_blocks = chain.recent_as_vec();
                        p.chain = chain;
                        prune_confirmed_mempool(&mut p, &confirmed_blocks, mempool_path);
                        if let Err(e) = p.save(storage) {
                            eprintln!("⚠️  Live sync reorg save failed: {}", e);
                        }
                        let height = p.chain.height();
                        let tip = p.chain.latest_block().hash.clone();
                        p.set_sync_status("complete", "", best_peer_height.max(height));
                        println!(
                            "🔀 Live sync: reorged from ancestor {} → height {}",
                            ancestor_height, height
                        );
                        (Some((height, tip)), Some(p.chain.recent_as_vec()))
                    }
                    Err(e) => {
                        p.set_sync_status(
                            "failed_chain_validation",
                            &format!("Chain validation: {}", e),
                            best_peer_height,
                        );
                        eprintln!("⚠️  Live sync rejected reorg branch: {}", e);
                        (None, None)
                    }
                }
            };
            if let Some((h, tip)) = tip_update {
                gossip.set_chain_tip(h, tip).await;
            }
            if let Some(blocks) = shared_blocks {
                gossip.update_shared_blocks(blocks).await;
            }
            return;
        }
    };

    if new_blocks.is_empty() {
        let mut p = producer.lock().unwrap();
        p.set_sync_status("complete", "", best_peer_height);
        return;
    }

    let (tip_update, shared_blocks) = {
        let mut p = producer.lock().unwrap();
        let confirmed_blocks = new_blocks.clone();
        match p.chain.add_blocks(new_blocks) {
            Ok(added) if added > 0 => {
                prune_confirmed_mempool(&mut p, &confirmed_blocks, mempool_path);
                if let Err(e) = p.save(storage) {
                    eprintln!("⚠️  Live sync save failed: {}", e);
                }
                let height = p.chain.height();
                let tip = p.chain.latest_block().hash.clone();
                p.set_sync_status("complete", "", best_peer_height.max(height));
                println!(
                    "📥 Live sync: imported {} block(s) → height {}",
                    added, height
                );
                (Some((height, tip)), Some(p.chain.recent_as_vec()))
            }
            Ok(_) => {
                p.set_sync_status("complete", "", best_peer_height);
                (None, None)
            }
            Err(e) => {
                eprintln!("⚠️  Live sync rejected downloaded blocks: {}", e);
                p.set_sync_status(
                    "failed_chain_validation",
                    &format!("Chain validation: {}", e),
                    best_peer_height,
                );
                (None, None)
            }
        }
    };

    if let Some((h, tip)) = tip_update {
        gossip.set_chain_tip(h, tip).await;
    }
    if let Some(blocks) = shared_blocks {
        gossip.update_shared_blocks(blocks).await;
    }
}

fn confirmed_tx_hashes(blocks: &[crate::block::Block]) -> Vec<String> {
    blocks
        .iter()
        .flat_map(|block| block.transactions.iter())
        .filter_map(|tx| {
            if tx.tx_hash.is_empty() {
                None
            } else {
                Some(tx.tx_hash.clone())
            }
        })
        .collect()
}

fn prune_confirmed_mempool(
    producer: &mut BlockProducer,
    blocks: &[crate::block::Block],
    mempool_path: &Path,
) {
    let hashes = confirmed_tx_hashes(blocks);
    if hashes.is_empty() || producer.mempool.size() == 0 {
        return;
    }

    let before = producer.mempool.size();
    producer.mempool.remove_confirmed(&hashes);
    let removed = before.saturating_sub(producer.mempool.size());
    if removed == 0 {
        return;
    }

    if let Err(e) = producer.mempool.save_to_path(mempool_path) {
        eprintln!("⚠️  Mempool prune save failed: {}", e);
    }
    println!("📋 Mempool pruned {} confirmed tx(s)", removed);
}

async fn process_gossip_events(
    producer: &Arc<Mutex<BlockProducer>>,
    storage: &Arc<Storage>,
    gossip: &Arc<GossipNode>,
    sync_tx: &mpsc::Sender<SyncTrigger>,
    mempool_path: &Path,
) {
    let events = gossip.drain_events().await;

    for event in events {
        match event {
            GossipEvent::NewBlock(block) => {
                let tip_update = {
                    let mut p = producer.lock().unwrap();
                    let result = p.chain.add_block(block.clone());
                    match result {
                        Ok(()) => {
                            prune_confirmed_mempool(
                                &mut p,
                                std::slice::from_ref(&block),
                                mempool_path,
                            );
                            if let Err(e) = p.save(storage) {
                                eprintln!("⚠️  Save after new block failed: {}", e);
                            }
                            Some((block.index + 1, block.hash.clone()))
                        }
                        Err(e) => {
                            if !e.contains("does not follow") {
                                eprintln!("⚠️  Rejected block {}: {}", block.index, e);
                            }
                            let _ = sync_tx.try_send(SyncTrigger::BlockAnnounce);
                            None
                        }
                    }
                };
                // Update gossip height + tip outside the lock
                if let Some((h, tip)) = tip_update {
                    gossip.set_chain_tip(h, tip).await;
                    let _ = sync_tx.try_send(SyncTrigger::BlockAnnounce);
                }
            }
            GossipEvent::NewTransaction(tx) => {
                let tx_hash = tx.tx_hash.clone();
                let add_result = {
                    let mut p = producer.lock().unwrap();
                    let balances = p.chain.balances.clone();
                    let nonces = p.chain.nonces.clone();
                    let stakes = p.chain.stakes.clone();
                    p.mempool.add_transaction_validated(
                        tx.clone(),
                        crate::mempool::MIN_FEE_PLANCKS,
                        &balances,
                        &nonces,
                        &stakes,
                    )
                };
                if let Err(e) = add_result {
                    if !e.contains("already in mempool")
                        && !e.contains("already mined")
                        && !e.contains("Nonce")
                    {
                        eprintln!("⚠️ Gossiped TX rejected ({}): {}", &tx_hash[..16], e);
                    }
                    continue;
                }
                println!("📥 Gossiped TX accepted ({})", &tx_hash[..16]);
                // Relay to other peers so a 3+ node network converges. The
                // gossip layer dedupes by tx_hash via SeenTracker, so any
                // re-broadcast that comes back to us is dropped silently.
                gossip.broadcast_tx(&tx).await;
            }
            GossipEvent::PeerHeight {
                node_id,
                height: _,
                genesis_hash,
            } => {
                if genesis_hash != genesis::EXPECTED_GENESIS_HASH {
                    eprintln!(
                        "⚠️  Peer {} on different genesis — ignoring",
                        &node_id[..16.min(node_id.len())]
                    );
                } else {
                    let _ = sync_tx.try_send(SyncTrigger::PeerEvent);
                }
                // Sync manager handles catching up
            }
            GossipEvent::PeerConnected(_) => {
                let _ = sync_tx.try_send(SyncTrigger::PeerEvent);
            }
            _ => {
                // PeerList and PeerDisconnected are handled by gossip internals.
            }
        }
    }
}

// ── Health check ─────────────────────────────────────────────────────────────

/// Quick health check — returns a summary of node state.
pub async fn health_check(handle: &NodeHandle) -> HealthStatus {
    let block_count = handle.storage.block_count().unwrap_or(0);
    let (peer_count, best_peer_height) = {
        let peers = handle.gossip.peers.lock().await;
        (
            peers.count(),
            peers
                .best_height_peer()
                .map(|p| p.chain_height)
                .unwrap_or(0),
        )
    };
    let p = handle.producer.lock().unwrap();
    let chain_height = p.chain.height();

    HealthStatus {
        chain_height,
        stored_blocks: block_count,
        balance_count: p.chain.balances.len(),
        stake_count: p.chain.stakes.len(),
        mempool_size: p.mempool.size(),
        peer_count,
        best_peer_height,
        height_lag: best_peer_height.saturating_sub(chain_height),
        mining_enabled: p.config.mining_enabled,
        mining_state: p.mining_state.clone(),
        mining_pause_reason: p.mining_pause_reason.clone(),
        ibd_complete: p.ibd_complete,
        tip_hash: p.chain.latest_block().hash.clone(),
        load_mode: p.load_mode.clone(),
        sync_state: p.sync_state.clone(),
        last_sync_error: p.last_sync_error.clone(),
        last_sync_at: p.last_sync_at,
    }
}

#[derive(Debug, Clone)]
pub struct HealthStatus {
    pub chain_height: u64,
    pub stored_blocks: u64,
    pub balance_count: usize,
    pub stake_count: usize,
    pub mempool_size: usize,
    pub peer_count: usize,
    pub best_peer_height: u64,
    pub height_lag: u64,
    pub mining_enabled: bool,
    pub mining_state: String,
    pub mining_pause_reason: String,
    pub ibd_complete: bool,
    pub tip_hash: String,
    pub load_mode: String,
    pub sync_state: String,
    pub last_sync_error: String,
    pub last_sync_at: f64,
}

impl std::fmt::Display for HealthStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "height={} stored={} balances={} stakes={} mempool={} peers={} best_peer_height={} lag={} mining={} mining_state={} pause={} ibd={} sync={} last_sync_at={:.0} last_sync_error={} load={} tip={}…",
            self.chain_height,
            self.stored_blocks,
            self.balance_count,
            self.stake_count,
            self.mempool_size,
            self.peer_count,
            self.best_peer_height,
            self.height_lag,
            self.mining_enabled,
            self.mining_state,
            self.mining_pause_reason,
            self.ibd_complete,
            self.sync_state,
            self.last_sync_at,
            self.last_sync_error,
            self.load_mode,
            &self.tip_hash[..16.min(self.tip_hash.len())]
        )
    }
}

// ══════════════════════════════════════════════════════════════════════════════
// Tests
// ══════════════════════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;
    use crate::block::Block;
    use crate::genesis;

    fn test_args(data_dir: &Path) -> NodeArgs {
        NodeArgs {
            address: genesis::GENESIS_CREATOR.to_string(),
            measured_tflops: 5.4,
            compute_share: 1.0,
            tflops: 5.4,
            data_dir: data_dir.to_path_buf(),
            mining: false,                            // don't mine during tests
            rpc_bind: "127.0.0.1:0".parse().unwrap(), // OS-assigned port
            p2p_port: 0,
            migrate_from: None,
            node_state_path: None,
            skip_ibd: true,
            seeds: vec![],
        }
    }

    #[test]
    fn test_compute_share_loads_from_config_file() {
        let tmp = tempfile::tempdir().unwrap();
        let rust_dir = tmp.path().join("rust_chain");
        let config_dir = tmp.path().join("data");
        std::fs::create_dir_all(&rust_dir).unwrap();
        std::fs::create_dir_all(&config_dir).unwrap();
        std::fs::write(
            config_dir.join("compute_config.json"),
            r#"{"compute_share": 0.25}"#,
        )
        .unwrap();

        assert_eq!(load_compute_share(&rust_dir), 0.25);
    }

    #[test]
    fn test_compute_share_clamps_invalid_range() {
        let tmp = tempfile::tempdir().unwrap();
        let rust_dir = tmp.path().join("rust_chain");
        let config_dir = tmp.path().join("data");
        std::fs::create_dir_all(&rust_dir).unwrap();
        std::fs::create_dir_all(&config_dir).unwrap();
        std::fs::write(
            config_dir.join("compute_config.json"),
            r#"{"compute_share": 9.0}"#,
        )
        .unwrap();

        assert_eq!(load_compute_share(&rust_dir), 1.0);
    }

    #[tokio::test]
    async fn test_boot_fresh_node() {
        let tmp = tempfile::tempdir().unwrap();
        let args = test_args(tmp.path());

        let handle = boot(args).await.unwrap();
        let health = health_check(&handle).await;

        assert_eq!(health.chain_height, 1); // genesis only
        assert!(health.ibd_complete);
        assert_eq!(health.tip_hash, genesis::EXPECTED_GENESIS_HASH);

        handle.shutdown();
    }

    #[tokio::test]
    async fn test_boot_with_existing_chain() {
        let tmp = tempfile::tempdir().unwrap();

        // Pre-populate storage with some blocks
        {
            let store = Storage::open(tmp.path().join("chain.db")).unwrap();
            let config = NodeConfig {
                address: genesis::GENESIS_CREATOR.to_string(),
                measured_tflops: 5.4,
                compute_share: 1.0,
                tflops: 5.4,
                mining_enabled: true,
            };
            let mut producer = BlockProducer::new(config);
            producer.ibd_complete = true;
            for _ in 0..5 {
                producer.try_produce_block();
            }
            producer.save(&store).unwrap();
        }

        let args = test_args(tmp.path());
        let handle = boot(args).await.unwrap();
        let health = health_check(&handle).await;

        assert_eq!(health.chain_height, 6); // genesis + 5
        assert!(health.balance_count > 0);

        handle.shutdown();
    }

    #[tokio::test]
    async fn test_boot_with_migration() {
        let tmp_py = tempfile::tempdir().unwrap();
        let tmp_rust = tempfile::tempdir().unwrap();

        // Create a Python-format DB
        let py_db_path = tmp_py.path().join("blockchain.db");
        {
            let conn = rusqlite::Connection::open(&py_db_path).unwrap();
            conn.execute_batch(
                "CREATE TABLE blocks (
                    block_index INTEGER PRIMARY KEY,
                    block_hash TEXT NOT NULL,
                    block_json TEXT NOT NULL,
                    created_at REAL DEFAULT (strftime('%s','now'))
                );
                CREATE TABLE balances (
                    address TEXT PRIMARY KEY,
                    balance_plancks INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL DEFAULT (strftime('%s','now'))
                );
                CREATE TABLE stakes (
                    address TEXT PRIMARY KEY,
                    stake_plancks INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL DEFAULT (strftime('%s','now'))
                );
                CREATE TABLE headers (
                    block_index INTEGER PRIMARY KEY,
                    header_json TEXT NOT NULL
                );",
            )
            .unwrap();

            // Insert genesis
            let genesis_blk = genesis::create_canonical_genesis();
            let dict = genesis_blk.to_dict();
            let json_str = serde_json::to_string(&dict).unwrap();
            conn.execute(
                "INSERT INTO blocks (block_index, block_hash, block_json) VALUES (?1, ?2, ?3)",
                rusqlite::params![0i64, genesis_blk.hash, json_str],
            )
            .unwrap();

            // Insert a few more blocks (using Rust producer for correctness)
            let config = NodeConfig {
                address: genesis::GENESIS_CREATOR.to_string(),
                measured_tflops: 5.4,
                compute_share: 1.0,
                tflops: 5.4,
                mining_enabled: true,
            };
            let mut producer = BlockProducer::new(config);
            producer.ibd_complete = true;
            for _ in 0..3 {
                if let Some(block) = producer.try_produce_block() {
                    let d = block.to_dict();
                    let js = serde_json::to_string(&d).unwrap();
                    conn.execute(
                        "INSERT INTO blocks (block_index, block_hash, block_json) VALUES (?1, ?2, ?3)",
                        rusqlite::params![block.index as i64, block.hash, js],
                    )
                    .unwrap();
                }
            }
        }

        let mut args = test_args(tmp_rust.path());
        args.migrate_from = Some(py_db_path);

        let handle = boot(args).await.unwrap();
        let health = health_check(&handle).await;

        assert_eq!(health.chain_height, 4); // genesis + 3
        assert!(health.balance_count > 0);

        handle.shutdown();
    }

    #[tokio::test]
    async fn test_node_args_default() {
        let args = NodeArgs::default();
        assert!(args.address.is_empty());
        assert_eq!(args.rpc_bind.port(), 9332);
        assert_eq!(args.p2p_port, 5001);
        assert!(args.mining);
        assert!(args.migrate_from.is_none());
        assert!(!args.skip_ibd);
    }

    #[tokio::test]
    async fn test_health_check_display() {
        let tmp = tempfile::tempdir().unwrap();
        let args = test_args(tmp.path());
        let handle = boot(args).await.unwrap();
        let health = health_check(&handle).await;

        let display = format!("{}", health);
        assert!(display.contains("height=1"));
        assert!(display.contains("mining=false"));
        assert!(display.contains("ibd=true"));

        handle.shutdown();
    }

    #[tokio::test]
    async fn test_rpc_state_snapshot() {
        let tmp = tempfile::tempdir().unwrap();
        let args = test_args(tmp.path());
        let handle = boot(args).await.unwrap();

        let state = handle.rpc_state();
        assert_eq!(state.chain.height(), 1);
        assert_eq!(state.node_address, genesis::GENESIS_CREATOR);

        // Verify sync_from_chain worked
        assert_eq!(state.balances.len(), state.chain.balances.len());

        handle.shutdown();
    }

    #[tokio::test]
    async fn test_rpc_via_tcp() {
        let tmp = tempfile::tempdir().unwrap();
        let mut args = test_args(tmp.path());
        // Use a random port for the test
        args.rpc_bind = "127.0.0.1:0".parse().unwrap();

        // Boot with mining off, skip IBD
        let handle = boot(args).await.unwrap();

        // Give RPC server a moment to bind
        tokio::time::sleep(Duration::from_millis(100)).await;

        // We can't easily test the TCP server since it binds to port 0
        // and we don't expose the actual port. Verify RPC logic directly:
        let mut state = handle.rpc_state();
        let req = rpc::RpcRequest {
            jsonrpc: "2.0".to_string(),
            method: "get_chain_height".to_string(),
            params: serde_json::Value::Null,
            id: serde_json::Value::from(1),
        };
        let resp = rpc::handle_request(&req, &mut state);
        let json = serde_json::to_value(&resp).unwrap();
        let result = json.get("result").unwrap();
        assert_eq!(result.get("height").unwrap().as_u64().unwrap(), 1);

        handle.shutdown();
    }

    #[tokio::test]
    async fn test_process_gossip_new_block() {
        let tmp = tempfile::tempdir().unwrap();
        let args = test_args(tmp.path());
        let handle = boot(args).await.unwrap();

        // Build a valid block manually (VRF is probabilistic, so we construct directly)
        let block = {
            let p = handle.producer.lock().unwrap();
            let prev = p.chain.latest_block();
            Block::new(
                prev.index + 1,
                &prev.hash,
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_secs_f64(),
                vec![],
                "test_gossip_miner",
                std::collections::BTreeMap::new(),
            )
        };

        // Feed it through the chain directly (simulating what gossip processing does)
        {
            let mut p = handle.producer.lock().unwrap();
            p.chain.add_block(block).unwrap();
        }

        let health = health_check(&handle).await;
        assert_eq!(health.chain_height, 2); // genesis + 1 block

        handle.shutdown();
    }

    #[tokio::test]
    async fn test_shutdown_is_clean() {
        let tmp = tempfile::tempdir().unwrap();
        let mut args = test_args(tmp.path());
        args.mining = true; // start mining loop

        let handle = boot(args).await.unwrap();

        // Let it run briefly
        tokio::time::sleep(Duration::from_millis(50)).await;

        // Shutdown should not panic
        handle.shutdown();

        // Can still read state after shutdown signal
        let health = health_check(&handle).await;
        assert!(health.chain_height >= 1);
    }

    #[tokio::test]
    async fn test_migration_then_produce() {
        let tmp_py = tempfile::tempdir().unwrap();
        let tmp_rust = tempfile::tempdir().unwrap();

        // Python DB with genesis only
        let py_db_path = tmp_py.path().join("blockchain.db");
        {
            let conn = rusqlite::Connection::open(&py_db_path).unwrap();
            conn.execute_batch(
                "CREATE TABLE blocks (
                    block_index INTEGER PRIMARY KEY,
                    block_hash TEXT NOT NULL,
                    block_json TEXT NOT NULL,
                    created_at REAL DEFAULT (strftime('%s','now'))
                );
                CREATE TABLE balances (
                    address TEXT PRIMARY KEY,
                    balance_plancks INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL DEFAULT (strftime('%s','now'))
                );
                CREATE TABLE stakes (
                    address TEXT PRIMARY KEY,
                    stake_plancks INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL DEFAULT (strftime('%s','now'))
                );
                CREATE TABLE headers (
                    block_index INTEGER PRIMARY KEY,
                    header_json TEXT NOT NULL
                );",
            )
            .unwrap();

            let genesis_blk = genesis::create_canonical_genesis();
            let dict = genesis_blk.to_dict();
            let json_str = serde_json::to_string(&dict).unwrap();
            conn.execute(
                "INSERT INTO blocks (block_index, block_hash, block_json) VALUES (?1, ?2, ?3)",
                rusqlite::params![0i64, genesis_blk.hash, json_str],
            )
            .unwrap();
        }

        let mut args = test_args(tmp_rust.path());
        args.migrate_from = Some(py_db_path);

        let handle = boot(args).await.unwrap();

        // Add blocks on top of migrated chain (built manually — VRF is probabilistic)
        {
            let mut p = handle.producer.lock().unwrap();
            for _ in 0..3 {
                let prev = p.chain.latest_block().clone();
                let blk = Block::new(
                    prev.index + 1,
                    &prev.hash,
                    std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap()
                        .as_secs_f64(),
                    vec![],
                    "test_miner",
                    std::collections::BTreeMap::new(),
                );
                p.chain.add_block(blk).unwrap();
            }
        }

        let health = health_check(&handle).await;
        assert_eq!(health.chain_height, 4); // genesis + 3

        // Save and verify persistence
        {
            let p = handle.producer.lock().unwrap();
            p.save(&handle.storage).unwrap();
        }

        let stored_count = handle.storage.block_count().unwrap();
        assert_eq!(stored_count, 4);

        handle.shutdown();
    }

    #[tokio::test]
    async fn test_shadow_compare_after_migration() {
        let tmp_py = tempfile::tempdir().unwrap();
        let tmp_rust = tempfile::tempdir().unwrap();

        // Build a Python DB with genesis + 5 blocks
        let py_db_path = tmp_py.path().join("blockchain.db");
        {
            let conn = rusqlite::Connection::open(&py_db_path).unwrap();
            conn.execute_batch(
                "CREATE TABLE blocks (
                    block_index INTEGER PRIMARY KEY,
                    block_hash TEXT NOT NULL,
                    block_json TEXT NOT NULL,
                    created_at REAL DEFAULT (strftime('%s','now'))
                );
                CREATE TABLE balances (
                    address TEXT PRIMARY KEY,
                    balance_plancks INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL DEFAULT (strftime('%s','now'))
                );
                CREATE TABLE stakes (
                    address TEXT PRIMARY KEY,
                    stake_plancks INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL DEFAULT (strftime('%s','now'))
                );
                CREATE TABLE headers (
                    block_index INTEGER PRIMARY KEY,
                    header_json TEXT NOT NULL
                );",
            )
            .unwrap();

            let config = NodeConfig {
                address: genesis::GENESIS_CREATOR.to_string(),
                measured_tflops: 5.4,
                compute_share: 1.0,
                tflops: 5.4,
                mining_enabled: true,
            };
            let mut producer = BlockProducer::new(config);
            producer.ibd_complete = true;

            // Store genesis
            let genesis_blk = &producer.chain.genesis;
            let dict = genesis_blk.to_dict();
            let json_str = serde_json::to_string(&dict).unwrap();
            conn.execute(
                "INSERT INTO blocks (block_index, block_hash, block_json) VALUES (?1, ?2, ?3)",
                rusqlite::params![0i64, genesis_blk.hash, json_str],
            )
            .unwrap();

            for _ in 0..5 {
                if let Some(block) = producer.try_produce_block() {
                    let d = block.to_dict();
                    let js = serde_json::to_string(&d).unwrap();
                    conn.execute(
                        "INSERT INTO blocks (block_index, block_hash, block_json) VALUES (?1, ?2, ?3)",
                        rusqlite::params![block.index as i64, block.hash, js],
                    )
                    .unwrap();
                }
            }
        }

        // Migrate
        let mut args = test_args(tmp_rust.path());
        args.migrate_from = Some(py_db_path.clone());

        let handle = boot(args).await.unwrap();

        // Shadow compare
        let shadow =
            migrate::shadow_compare(&py_db_path, handle.storage.db_path().unwrap()).unwrap();
        assert!(shadow.chains_match(), "Chains should match after migration");
        assert_eq!(shadow.matching_blocks, 6);

        handle.shutdown();
    }
}
