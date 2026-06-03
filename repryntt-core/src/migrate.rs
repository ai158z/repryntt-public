//! Migration tooling — import Python chain data into the Rust node.
//!
//! Reads the Python blockchain's SQLite database and JSON state files,
//! validates chain integrity by replaying all blocks, compares computed
//! vs stored balances, and writes the verified state into the Rust DB.
//!
//! # Usage
//!
//! ```ignore
//! let report = migrate_from_python(
//!     "path/to/python/blockchain.db",
//!     Some("path/to/python/node_state.json"),
//!     "path/to/rust/chain.db",
//! )?;
//! assert!(report.success);
//! ```

use std::collections::BTreeMap;
use std::path::Path;

use rusqlite::{Connection, params};
use serde_json::Value;

use crate::block::Block;
use crate::chain::Chain;
use crate::genesis::{self, EXPECTED_GENESIS_HASH};
use crate::storage::Storage;

// ── Migration report ─────────────────────────────────────────────────────────

/// Detailed result of a migration run.
#[derive(Debug)]
pub struct MigrationReport {
    pub success: bool,
    pub blocks_read: u64,
    pub blocks_imported: u64,
    pub balances_imported: u64,
    pub stakes_imported: u64,
    /// Addresses where replayed balance differs from stored balance.
    pub balance_diffs: Vec<BalanceDiff>,
    /// Addresses where replayed stake differs from stored stake.
    pub stake_diffs: Vec<StakeDiff>,
    /// Entity records imported from node_state.json.
    pub entities_imported: u64,
    /// Device records imported from node_state.json.
    pub devices_imported: u64,
    /// Chain height after import.
    pub final_height: u64,
    /// Any warnings generated during migration.
    pub warnings: Vec<String>,
    /// Fatal error if migration failed.
    pub error: Option<String>,
}

impl MigrationReport {
    fn new() -> Self {
        Self {
            success: false,
            blocks_read: 0,
            blocks_imported: 0,
            balances_imported: 0,
            stakes_imported: 0,
            balance_diffs: Vec::new(),
            stake_diffs: Vec::new(),
            entities_imported: 0,
            devices_imported: 0,
            final_height: 0,
            warnings: Vec::new(),
            error: None,
        }
    }
}

/// A balance mismatch between stored and replayed values.
#[derive(Debug, Clone)]
pub struct BalanceDiff {
    pub address: String,
    pub stored_plancks: i64,
    pub replayed_plancks: i64,
}

/// A stake mismatch between stored and replayed values.
#[derive(Debug, Clone)]
pub struct StakeDiff {
    pub address: String,
    pub stored_plancks: i64,
    pub replayed_plancks: i64,
}

// ── Node state (parsed from node_state.json) ────────────────────────────────

/// Python's node_state.json top-level structure.
#[derive(Debug, Clone)]
pub struct NodeState {
    pub balances: BTreeMap<String, i64>,
    pub entity_records: BTreeMap<String, Value>,
    pub entity_nullifiers: Vec<String>,
    pub device_registry: BTreeMap<String, Value>,
    pub entity_nodes: BTreeMap<String, Vec<String>>,
    pub contract_workloads: BTreeMap<String, Value>,
    pub contract_valid_keys: Vec<String>,
    pub faucet_used_wallets: Vec<String>,
    pub nonce_tracker: BTreeMap<String, u64>,
    pub total_supply: i64,
    pub block_height: u64,
}

impl NodeState {
    /// Parse from a serde_json::Value (the entire node_state.json).
    pub fn from_json(v: &Value) -> Result<Self, String> {
        let obj = v.as_object().ok_or("node_state.json is not an object")?;

        // Balances
        let mut balances = BTreeMap::new();
        if let Some(bals) = obj.get("balances").and_then(|v| v.as_object()) {
            for (addr, val) in bals {
                let plancks = val.as_i64().unwrap_or(0);
                balances.insert(addr.clone(), plancks);
            }
        }

        // Entity registry
        let mut entity_records = BTreeMap::new();
        let mut entity_nullifiers = Vec::new();
        if let Some(er) = obj.get("entity_registry").and_then(|v| v.as_object()) {
            if let Some(records) = er.get("records").and_then(|v| v.as_object()) {
                for (k, v) in records {
                    entity_records.insert(k.clone(), v.clone());
                }
            }
            if let Some(nulls) = er.get("nullifiers") {
                if let Some(arr) = nulls.as_array() {
                    for n in arr {
                        if let Some(s) = n.as_str() {
                            entity_nullifiers.push(s.to_string());
                        }
                    }
                }
            }
        }

        // Device registry
        let mut device_registry = BTreeMap::new();
        let mut entity_nodes = BTreeMap::new();
        if let Some(dr) = obj.get("device_registry").and_then(|v| v.as_object()) {
            if let Some(devices) = dr.get("devices").and_then(|v| v.as_object()) {
                for (k, v) in devices {
                    device_registry.insert(k.clone(), v.clone());
                }
            }
            if let Some(en) = dr.get("entity_nodes").and_then(|v| v.as_object()) {
                for (k, v) in en {
                    let mut wallets = Vec::new();
                    if let Some(arr) = v.as_array() {
                        for w in arr {
                            if let Some(s) = w.as_str() {
                                wallets.push(s.to_string());
                            }
                        }
                    }
                    entity_nodes.insert(k.clone(), wallets);
                }
            }
        }

        // Contract data
        let mut contract_workloads = BTreeMap::new();
        let mut contract_valid_keys = Vec::new();
        if let Some(cd) = obj.get("contract_data").and_then(|v| v.as_object()) {
            if let Some(wls) = cd.get("workloads").and_then(|v| v.as_object()) {
                for (k, v) in wls {
                    contract_workloads.insert(k.clone(), v.clone());
                }
            }
            if let Some(keys) = cd.get("valid_keys").and_then(|v| v.as_array()) {
                for k in keys {
                    if let Some(s) = k.as_str() {
                        contract_valid_keys.push(s.to_string());
                    }
                }
            }
        }

        // Faucet
        let mut faucet_used_wallets = Vec::new();
        if let Some(arr) = obj.get("faucet_used_wallets").and_then(|v| v.as_array()) {
            for w in arr {
                if let Some(s) = w.as_str() {
                    faucet_used_wallets.push(s.to_string());
                }
            }
        }

        // Nonce tracker
        let mut nonce_tracker = BTreeMap::new();
        if let Some(nt) = obj.get("nonce_tracker").and_then(|v| v.as_object()) {
            for (k, v) in nt {
                nonce_tracker.insert(k.clone(), v.as_u64().unwrap_or(0));
            }
        }

        let total_supply = obj
            .get("total_supply")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let block_height = obj
            .get("block_height")
            .and_then(|v| v.as_u64())
            .unwrap_or(0);

        Ok(Self {
            balances,
            entity_records,
            entity_nullifiers,
            device_registry,
            entity_nodes,
            contract_workloads,
            contract_valid_keys,
            faucet_used_wallets,
            nonce_tracker,
            total_supply,
            block_height,
        })
    }
}

// ── Chain validation report ──────────────────────────────────────────────────

/// Result of validating a chain by full replay.
#[derive(Debug)]
pub struct ValidationReport {
    pub valid: bool,
    pub blocks_validated: u64,
    pub computed_balances: BTreeMap<String, i64>,
    pub computed_stakes: BTreeMap<String, i64>,
    pub computed_nonces: BTreeMap<String, u64>,
    pub errors: Vec<String>,
}

// ── Python DB reader ─────────────────────────────────────────────────────────

/// Read blocks from a Python-format SQLite database.
///
/// Returns blocks in ascending index order.  The database must have a `blocks`
/// table with columns `block_index`, `block_hash`, and `block_json`.
pub fn read_python_blocks<P: AsRef<Path>>(db_path: P) -> Result<Vec<Block>, String> {
    let conn = Connection::open(db_path.as_ref())
        .map_err(|e| format!("Failed to open Python DB: {}", e))?;

    let mut stmt = conn
        .prepare("SELECT block_json FROM blocks ORDER BY block_index ASC")
        .map_err(|e| format!("Failed to prepare query: {}", e))?;

    let rows = stmt
        .query_map([], |row| {
            let json_str: String = row.get(0)?;
            Ok(json_str)
        })
        .map_err(|e| format!("Failed to query blocks: {}", e))?;

    let mut blocks = Vec::new();
    for (i, row) in rows.enumerate() {
        let json_str = row.map_err(|e| format!("Row {} read error: {}", i, e))?;
        let map: BTreeMap<String, Value> = serde_json::from_str(&json_str)
            .map_err(|e| format!("Block {} JSON parse error: {}", i, e))?;
        let block =
            Block::from_dict(&map).ok_or_else(|| format!("Block {} deserialization failed", i))?;
        blocks.push(block);
    }

    Ok(blocks)
}

/// Read stored balances from a Python-format SQLite database.
pub fn read_python_balances<P: AsRef<Path>>(db_path: P) -> Result<BTreeMap<String, i64>, String> {
    let conn = Connection::open(db_path.as_ref())
        .map_err(|e| format!("Failed to open Python DB: {}", e))?;

    // Check if balances table exists
    let has_table: bool = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='balances'",
            [],
            |row| row.get::<_, i64>(0),
        )
        .map_err(|e| format!("Failed to check tables: {}", e))?
        > 0;

    if !has_table {
        return Ok(BTreeMap::new());
    }

    let mut stmt = conn
        .prepare("SELECT address, balance_plancks FROM balances")
        .map_err(|e| format!("Failed to prepare balance query: {}", e))?;

    let rows = stmt
        .query_map([], |row| {
            let addr: String = row.get(0)?;
            let bal: i64 = row.get(1)?;
            Ok((addr, bal))
        })
        .map_err(|e| format!("Failed to query balances: {}", e))?;

    let mut balances = BTreeMap::new();
    for row in rows {
        let (addr, bal) = row.map_err(|e| format!("Balance row error: {}", e))?;
        balances.insert(addr, bal);
    }

    Ok(balances)
}

/// Read stored stakes from a Python-format SQLite database.
pub fn read_python_stakes<P: AsRef<Path>>(db_path: P) -> Result<BTreeMap<String, i64>, String> {
    let conn = Connection::open(db_path.as_ref())
        .map_err(|e| format!("Failed to open Python DB: {}", e))?;

    let has_table: bool = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='stakes'",
            [],
            |row| row.get::<_, i64>(0),
        )
        .map_err(|e| format!("Failed to check tables: {}", e))?
        > 0;

    if !has_table {
        return Ok(BTreeMap::new());
    }

    let mut stmt = conn
        .prepare("SELECT address, stake_plancks FROM stakes")
        .map_err(|e| format!("Failed to prepare stake query: {}", e))?;

    let rows = stmt
        .query_map([], |row| {
            let addr: String = row.get(0)?;
            let stake: i64 = row.get(1)?;
            Ok((addr, stake))
        })
        .map_err(|e| format!("Failed to query stakes: {}", e))?;

    let mut stakes = BTreeMap::new();
    for row in rows {
        let (addr, stake) = row.map_err(|e| format!("Stake row error: {}", e))?;
        stakes.insert(addr, stake);
    }

    Ok(stakes)
}

/// Parse node_state.json from a file path.
pub fn read_node_state<P: AsRef<Path>>(path: P) -> Result<NodeState, String> {
    let content = std::fs::read_to_string(path.as_ref())
        .map_err(|e| format!("Failed to read node_state.json: {}", e))?;
    let value: Value =
        serde_json::from_str(&content).map_err(|e| format!("Invalid JSON: {}", e))?;
    NodeState::from_json(&value)
}

// ── Chain validation (replay) ────────────────────────────────────────────────

/// Validate a chain by replaying every block from genesis.
///
/// Returns the computed balances, stakes, nonces and any errors found.
/// This does NOT modify any database — it's a pure validation pass.
pub fn validate_chain(blocks: &[Block]) -> ValidationReport {
    let mut report = ValidationReport {
        valid: false,
        blocks_validated: 0,
        computed_balances: BTreeMap::new(),
        computed_stakes: BTreeMap::new(),
        computed_nonces: BTreeMap::new(),
        errors: Vec::new(),
    };

    if blocks.is_empty() {
        report.errors.push("Empty block list".into());
        return report;
    }

    // Verify genesis
    if blocks[0].hash != EXPECTED_GENESIS_HASH {
        report.errors.push(format!(
            "Genesis hash mismatch: got {}",
            &blocks[0].hash[..32.min(blocks[0].hash.len())]
        ));
        return report;
    }

    // Replay genesis transactions
    replay_block_transactions(&blocks[0], &mut report);
    report.blocks_validated = 1;

    // Walk the chain
    for i in 1..blocks.len() {
        let prev = &blocks[i - 1];
        let curr = &blocks[i];

        // Index continuity
        if curr.index != prev.index + 1 {
            report.errors.push(format!(
                "Block {} has wrong index (expected {})",
                curr.index,
                prev.index + 1
            ));
            return report;
        }

        // Hash chain
        if curr.previous_hash != prev.hash {
            report.errors.push(format!(
                "Block {} previous_hash mismatch: {} != {}",
                curr.index,
                &curr.previous_hash[..16.min(curr.previous_hash.len())],
                &prev.hash[..16.min(prev.hash.len())]
            ));
            return report;
        }

        // Timestamp ordering
        if curr.timestamp < prev.timestamp {
            report.errors.push(format!(
                "Block {} timestamp {} < prev {}",
                curr.index, curr.timestamp, prev.timestamp
            ));
            // Non-fatal warning — continue
        }

        // Replay transactions
        replay_block_transactions(curr, &mut report);
        report.blocks_validated = (i + 1) as u64;
    }

    if report.errors.is_empty() {
        report.valid = true;
    }

    report
}

/// Apply a single block's transactions to the validation report's state.
fn replay_block_transactions(block: &Block, report: &mut ValidationReport) {
    for tx in &block.transactions {
        match tx.tx_type.as_str() {
            "reward" => {
                *report
                    .computed_balances
                    .entry(tx.to_address.clone())
                    .or_insert(0) += tx.amount;
            }
            "transfer" => {
                let sender_bal = report
                    .computed_balances
                    .get(&tx.from_address)
                    .copied()
                    .unwrap_or(0);
                if sender_bal < tx.amount {
                    report.errors.push(format!(
                        "Block {}: transfer insufficient balance for {} ({} < {})",
                        block.index, tx.from_address, sender_bal, tx.amount
                    ));
                }
                *report
                    .computed_balances
                    .entry(tx.from_address.clone())
                    .or_insert(0) -= tx.amount;
                *report
                    .computed_balances
                    .entry(tx.to_address.clone())
                    .or_insert(0) += tx.amount;
                *report
                    .computed_nonces
                    .entry(tx.from_address.clone())
                    .or_insert(0) += 1;
            }
            "stake" => {
                let sender_bal = report
                    .computed_balances
                    .get(&tx.from_address)
                    .copied()
                    .unwrap_or(0);
                if sender_bal < tx.amount {
                    report.errors.push(format!(
                        "Block {}: stake insufficient balance for {}",
                        block.index, tx.from_address
                    ));
                }
                *report
                    .computed_balances
                    .entry(tx.from_address.clone())
                    .or_insert(0) -= tx.amount;
                *report
                    .computed_stakes
                    .entry(tx.from_address.clone())
                    .or_insert(0) += tx.amount;
                *report
                    .computed_nonces
                    .entry(tx.from_address.clone())
                    .or_insert(0) += 1;
            }
            "stake_withdraw" => {
                let staked = report
                    .computed_stakes
                    .get(&tx.from_address)
                    .copied()
                    .unwrap_or(0);
                if staked < tx.amount {
                    report.errors.push(format!(
                        "Block {}: stake_withdraw insufficient for {}",
                        block.index, tx.from_address
                    ));
                }
                *report
                    .computed_stakes
                    .entry(tx.from_address.clone())
                    .or_insert(0) -= tx.amount;
                *report
                    .computed_balances
                    .entry(tx.from_address.clone())
                    .or_insert(0) += tx.amount;
                *report
                    .computed_nonces
                    .entry(tx.from_address.clone())
                    .or_insert(0) += 1;
            }
            "fee" => {
                *report
                    .computed_balances
                    .entry(tx.from_address.clone())
                    .or_insert(0) -= tx.amount;
                *report
                    .computed_balances
                    .entry(tx.to_address.clone())
                    .or_insert(0) += tx.amount;
            }
            "faucet" => {
                *report
                    .computed_balances
                    .entry(tx.to_address.clone())
                    .or_insert(0) += tx.amount;
            }
            "penalty" => {
                let bal = report
                    .computed_balances
                    .get(&tx.from_address)
                    .copied()
                    .unwrap_or(0);
                let deduct = tx.amount.min(bal);
                *report
                    .computed_balances
                    .entry(tx.from_address.clone())
                    .or_insert(0) -= deduct;
            }
            "workload_completion" => {
                *report
                    .computed_balances
                    .entry(tx.to_address.clone())
                    .or_insert(0) += tx.amount;
            }
            "entity_register" => {
                // No balance change
            }
            other => {
                report.errors.push(format!(
                    "Block {}: unknown tx type '{}'",
                    block.index, other
                ));
            }
        }
    }
}

// ── State comparison ─────────────────────────────────────────────────────────

/// Compare stored balances against replayed balances.
///
/// Returns diffs where the two disagree. Zero-balance entries are ignored.
pub fn compare_balances(
    stored: &BTreeMap<String, i64>,
    replayed: &BTreeMap<String, i64>,
) -> Vec<BalanceDiff> {
    let mut diffs = Vec::new();

    // All addresses from both maps
    let mut all_addrs: Vec<&String> = stored.keys().chain(replayed.keys()).collect();
    all_addrs.sort();
    all_addrs.dedup();

    for addr in all_addrs {
        let s = stored.get(addr).copied().unwrap_or(0);
        let r = replayed.get(addr).copied().unwrap_or(0);
        if s != r {
            // Skip zero/zero
            if s == 0 && r == 0 {
                continue;
            }
            diffs.push(BalanceDiff {
                address: addr.clone(),
                stored_plancks: s,
                replayed_plancks: r,
            });
        }
    }

    diffs
}

/// Compare stored stakes against replayed stakes.
pub fn compare_stakes(
    stored: &BTreeMap<String, i64>,
    replayed: &BTreeMap<String, i64>,
) -> Vec<StakeDiff> {
    let mut diffs = Vec::new();

    let mut all_addrs: Vec<&String> = stored.keys().chain(replayed.keys()).collect();
    all_addrs.sort();
    all_addrs.dedup();

    for addr in all_addrs {
        let s = stored.get(addr).copied().unwrap_or(0);
        let r = replayed.get(addr).copied().unwrap_or(0);
        if s != r && !(s == 0 && r == 0) {
            diffs.push(StakeDiff {
                address: addr.clone(),
                stored_plancks: s,
                replayed_plancks: r,
            });
        }
    }

    diffs
}

// ── Snapshot export ──────────────────────────────────────────────────────────

/// Export current Rust chain state to JSON for external comparison.
pub fn export_snapshot(chain: &Chain) -> Value {
    let mut balances_map = serde_json::Map::new();
    for (addr, bal) in &chain.balances {
        balances_map.insert(addr.clone(), Value::from(*bal));
    }

    let mut stakes_map = serde_json::Map::new();
    for (addr, stake) in &chain.stakes {
        stakes_map.insert(addr.clone(), Value::from(*stake));
    }

    let mut nonces_map = serde_json::Map::new();
    for (addr, nonce) in &chain.nonces {
        nonces_map.insert(addr.clone(), Value::from(*nonce));
    }

    serde_json::json!({
        "height": chain.height(),
        "tip_hash": chain.latest_block().hash,
        "balances": Value::Object(balances_map),
        "stakes": Value::Object(stakes_map),
        "nonces": Value::Object(nonces_map),
    })
}

// ── Full migration ───────────────────────────────────────────────────────────

/// Migrate from a Python blockchain database into a Rust database.
///
/// Steps:
/// 1. Read all blocks from the Python SQLite DB.
/// 2. Read stored balances/stakes from the Python DB.
/// 3. Optionally read node_state.json for entity/device/contract state.
/// 4. Validate the chain by full replay.
/// 5. Compare replayed state against stored state.
/// 6. Write validated blocks + state into the Rust DB.
/// 7. Return a detailed migration report.
pub fn migrate_from_python<P1, P2, P3>(
    python_db_path: P1,
    node_state_path: Option<P2>,
    rust_db_path: P3,
) -> Result<MigrationReport, String>
where
    P1: AsRef<Path>,
    P2: AsRef<Path>,
    P3: AsRef<Path>,
{
    let mut report = MigrationReport::new();

    // ── Step 1: Read blocks from Python DB ───────────────────────────────
    let blocks = read_python_blocks(&python_db_path)?;
    report.blocks_read = blocks.len() as u64;

    if blocks.is_empty() {
        report.error = Some("Python database contains no blocks".into());
        return Ok(report);
    }

    // ── Step 2: Read stored balances/stakes ──────────────────────────────
    let stored_balances = read_python_balances(&python_db_path)?;
    let stored_stakes = read_python_stakes(&python_db_path)?;

    // ── Step 3: Read node state (optional) ───────────────────────────────
    let node_state = if let Some(ref ns_path) = node_state_path {
        match read_node_state(ns_path) {
            Ok(ns) => {
                report.entities_imported = ns.entity_records.len() as u64;
                report.devices_imported = ns.device_registry.len() as u64;
                Some(ns)
            }
            Err(e) => {
                report
                    .warnings
                    .push(format!("Could not read node_state.json (non-fatal): {}", e));
                None
            }
        }
    } else {
        None
    };

    // ── Step 4: Validate chain by full replay ────────────────────────────
    let validation = validate_chain(&blocks);

    if !validation.valid {
        report.error = Some(format!(
            "Chain validation failed after {} blocks: {:?}",
            validation.blocks_validated, validation.errors
        ));
        return Ok(report);
    }

    // ── Step 5: Compare replayed state against stored ────────────────────
    // Use node_state balances if available (authoritative), else use DB balances
    let ref_balances = if let Some(ref ns) = node_state {
        &ns.balances
    } else {
        &stored_balances
    };

    report.balance_diffs = compare_balances(ref_balances, &validation.computed_balances);
    report.stake_diffs = compare_stakes(&stored_stakes, &validation.computed_stakes);

    // Balance diffs are warnings, not errors — replaycomputed state is authoritative
    for diff in &report.balance_diffs {
        report.warnings.push(format!(
            "Balance diff for {}: stored={} replayed={}",
            diff.address, diff.stored_plancks, diff.replayed_plancks
        ));
    }
    for diff in &report.stake_diffs {
        report.warnings.push(format!(
            "Stake diff for {}: stored={} replayed={}",
            diff.address, diff.stored_plancks, diff.replayed_plancks
        ));
    }

    // ── Step 6: Write to Rust DB ─────────────────────────────────────────
    let rust_store =
        Storage::open(&rust_db_path).map_err(|e| format!("Failed to open Rust DB: {}", e))?;

    // Use replayed (authoritative) balances/stakes
    rust_store
        .save_chain(
            &blocks,
            &validation.computed_balances,
            &validation.computed_nonces,
            &validation.computed_stakes,
        )
        .map_err(|e| format!("Failed to write chain: {}", e))?;

    report.blocks_imported = blocks.len() as u64;
    report.balances_imported = validation.computed_balances.len() as u64;
    report.stakes_imported = validation.computed_stakes.len() as u64;
    report.final_height = blocks.len() as u64;
    report.success = true;

    Ok(report)
}

// ── Incremental migration (append new blocks only) ───────────────────────────

/// Append blocks from a Python DB that are newer than the Rust chain's tip.
///
/// Useful for shadow-mode: the Python node keeps producing, and we periodically
/// sync the Rust DB to match.
pub fn incremental_sync<P1, P2>(
    python_db_path: P1,
    rust_db_path: P2,
) -> Result<MigrationReport, String>
where
    P1: AsRef<Path>,
    P2: AsRef<Path>,
{
    let mut report = MigrationReport::new();

    // Read all Python blocks
    let all_blocks = read_python_blocks(&python_db_path)?;
    report.blocks_read = all_blocks.len() as u64;

    if all_blocks.is_empty() {
        report.error = Some("Python database contains no blocks".into());
        return Ok(report);
    }

    // Open Rust DB and find current height
    let rust_store =
        Storage::open(&rust_db_path).map_err(|e| format!("Failed to open Rust DB: {}", e))?;
    let rust_height = rust_store
        .block_count()
        .map_err(|e| format!("Failed to read Rust chain height: {}", e))?;

    if rust_height == 0 {
        // Fresh DB — do a full migration
        return migrate_from_python(python_db_path, None::<&str>, rust_db_path);
    }

    let new_blocks: Vec<Block> = all_blocks
        .into_iter()
        .filter(|b| b.index >= rust_height)
        .collect();

    if new_blocks.is_empty() {
        report.success = true;
        report.final_height = rust_height;
        report.warnings.push("No new blocks to sync".into());
        return Ok(report);
    }

    // Validate the new blocks form a valid continuation
    // First, verify the first new block links to our current tip
    if rust_height > 0 {
        let tip = rust_store
            .get_block(rust_height - 1)
            .map_err(|e| format!("Failed to read Rust tip: {}", e))?
            .ok_or("Rust tip block missing")?;

        if new_blocks[0].previous_hash != tip.hash {
            report.error = Some(format!(
                "Fork detected: new block {} previous_hash {} != tip {}",
                new_blocks[0].index,
                &new_blocks[0].previous_hash[..16],
                &tip.hash[..16]
            ));
            return Ok(report);
        }
    }

    // Load full chain for replay (need existing state)
    let existing_blocks = rust_store
        .load_chain()
        .map_err(|e| format!("Failed to load existing chain: {}", e))?;

    let mut chain = Chain::from_blocks(existing_blocks)
        .map_err(|e| format!("Failed to rebuild Rust chain: {}", e))?;

    // Add new blocks
    let mut imported = 0u64;
    for block in &new_blocks {
        match chain.add_block_trusted(block.clone()) {
            Ok(()) => imported += 1,
            Err(e) => {
                report.error = Some(format!("Failed to add block {}: {}", block.index, e));
                break;
            }
        }
    }

    // Save updated state
    if imported > 0 {
        let recent = chain.recent_as_vec();
        rust_store
            .save_chain(&recent, &chain.balances, &chain.nonces, &chain.stakes)
            .map_err(|e| format!("Failed to save updated chain: {}", e))?;
    }

    report.blocks_imported = imported;
    report.balances_imported = chain.balances.len() as u64;
    report.stakes_imported = chain.stakes.len() as u64;
    report.final_height = chain.height();
    report.success = report.error.is_none();

    Ok(report)
}

// ── Shadow mode comparison ───────────────────────────────────────────────────

/// Compare a Python DB against a Rust DB block-by-block.
///
/// Returns a list of block indices where the hashes differ.
/// Both chains must start from the same genesis.
pub fn shadow_compare<P1, P2>(python_db_path: P1, rust_db_path: P2) -> Result<ShadowReport, String>
where
    P1: AsRef<Path>,
    P2: AsRef<Path>,
{
    let py_blocks = read_python_blocks(&python_db_path)?;
    let rust_store =
        Storage::open(&rust_db_path).map_err(|e| format!("Failed to open Rust DB: {}", e))?;

    let mut report = ShadowReport {
        python_height: py_blocks.len() as u64,
        rust_height: rust_store
            .block_count()
            .map_err(|e| format!("Failed to read Rust height: {}", e))?,
        matching_blocks: 0,
        divergent_indices: Vec::new(),
    };

    let compare_height = report.python_height.min(report.rust_height);

    for i in 0..compare_height {
        let py_hash = &py_blocks[i as usize].hash;
        let rust_block = rust_store
            .get_block(i)
            .map_err(|e| format!("Failed to read Rust block {}: {}", i, e))?;

        match rust_block {
            Some(rb) if rb.hash == *py_hash => {
                report.matching_blocks += 1;
            }
            Some(rb) => {
                report.divergent_indices.push(DivergentBlock {
                    index: i,
                    python_hash: py_hash.clone(),
                    rust_hash: rb.hash,
                });
            }
            None => {
                report.divergent_indices.push(DivergentBlock {
                    index: i,
                    python_hash: py_hash.clone(),
                    rust_hash: "MISSING".into(),
                });
            }
        }
    }

    Ok(report)
}

/// Result of shadow-mode comparison.
#[derive(Debug)]
pub struct ShadowReport {
    pub python_height: u64,
    pub rust_height: u64,
    pub matching_blocks: u64,
    pub divergent_indices: Vec<DivergentBlock>,
}

impl ShadowReport {
    pub fn chains_match(&self) -> bool {
        self.python_height == self.rust_height && self.divergent_indices.is_empty()
    }
}

/// A single block where Python and Rust hashes differ.
#[derive(Debug, Clone)]
pub struct DivergentBlock {
    pub index: u64,
    pub python_hash: String,
    pub rust_hash: String,
}

// ══════════════════════════════════════════════════════════════════════════════
// Tests
// ══════════════════════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;
    use crate::block::Block;
    use crate::genesis;
    use crate::transaction::Transaction;
    use std::collections::BTreeMap;

    // ── helpers ──────────────────────────────────────────────────────────

    /// Write a Python-format DB to a temp file and return its path.
    fn python_db_to_file(
        blocks: &[Block],
        balances: &BTreeMap<String, i64>,
        stakes: &BTreeMap<String, i64>,
    ) -> tempfile::NamedTempFile {
        let file = tempfile::NamedTempFile::new().unwrap();
        let conn = Connection::open(file.path()).unwrap();
        conn.execute_batch(
            "CREATE TABLE blocks (
                block_index INTEGER PRIMARY KEY,
                block_hash  TEXT NOT NULL,
                block_json  TEXT NOT NULL,
                created_at  REAL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE balances (
                address         TEXT PRIMARY KEY,
                balance_plancks INTEGER NOT NULL DEFAULT 0,
                updated_at      REAL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE stakes (
                address       TEXT PRIMARY KEY,
                stake_plancks INTEGER NOT NULL DEFAULT 0,
                updated_at    REAL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE headers (
                block_index  INTEGER PRIMARY KEY,
                header_json  TEXT NOT NULL
            );",
        )
        .unwrap();

        for block in blocks {
            let dict = block.to_dict();
            let json_str = serde_json::to_string(&dict).unwrap();
            conn.execute(
                "INSERT INTO blocks (block_index, block_hash, block_json) VALUES (?1, ?2, ?3)",
                params![block.index as i64, block.hash, json_str],
            )
            .unwrap();
        }
        for (addr, bal) in balances {
            conn.execute(
                "INSERT INTO balances (address, balance_plancks) VALUES (?1, ?2)",
                params![addr, bal],
            )
            .unwrap();
        }
        for (addr, stake) in stakes {
            conn.execute(
                "INSERT INTO stakes (address, stake_plancks) VALUES (?1, ?2)",
                params![addr, stake],
            )
            .unwrap();
        }

        drop(conn);
        file
    }

    /// Build a small chain: genesis + N reward blocks for a miner.
    fn build_test_chain(num_blocks: u64) -> Vec<Block> {
        let genesis = genesis::create_canonical_genesis();
        let mut blocks = vec![genesis];
        let miner = "test_miner_abc";

        for i in 1..=num_blocks {
            let prev = blocks.last().unwrap();
            let reward = Chain::coinbase_reward(i);
            let reward_tx = Transaction::new(
                "SYSTEM",
                miner,
                reward,
                "reward",
                0,
                BTreeMap::new(),
                Some(prev.timestamp + 15.0),
                None,
                2,
            );
            let block = Block::new(
                i,
                &prev.hash,
                prev.timestamp + 15.0,
                vec![reward_tx],
                miner,
                BTreeMap::new(),
            );
            blocks.push(block);
        }

        blocks
    }

    /// Build a chain with transfers (to test balance replay).
    fn build_chain_with_transfers() -> (Vec<Block>, BTreeMap<String, i64>) {
        let genesis = genesis::create_canonical_genesis();
        let mut blocks = vec![genesis];
        let miner = "miner_alice";

        // Block 1: reward to miner
        let prev = blocks.last().unwrap();
        let reward = Chain::coinbase_reward(1);
        let reward_tx = Transaction::new(
            "SYSTEM",
            miner,
            reward,
            "reward",
            0,
            BTreeMap::new(),
            Some(prev.timestamp + 15.0),
            None,
            2,
        );
        let b1 = Block::new(
            1,
            &prev.hash,
            prev.timestamp + 15.0,
            vec![reward_tx],
            miner,
            BTreeMap::new(),
        );
        blocks.push(b1);

        // Block 2: transfer 1 CR from miner to bob
        let prev = blocks.last().unwrap();
        let transfer_tx = Transaction::new(
            miner,
            "bob",
            100_000_000, // 1 CR
            "transfer",
            0,
            BTreeMap::new(),
            Some(prev.timestamp + 15.0),
            None,
            2,
        );
        let reward_tx2 = Transaction::new(
            "SYSTEM",
            miner,
            Chain::coinbase_reward(2),
            "reward",
            0,
            BTreeMap::new(),
            Some(prev.timestamp + 15.0),
            None,
            2,
        );
        let b2 = Block::new(
            2,
            &prev.hash,
            prev.timestamp + 15.0,
            vec![reward_tx2, transfer_tx],
            miner,
            BTreeMap::new(),
        );
        blocks.push(b2);

        // Expected balances after replay
        let mut expected = BTreeMap::new();
        let r1 = Chain::coinbase_reward(1);
        let r2 = Chain::coinbase_reward(2);
        expected.insert(miner.to_string(), r1 + r2 - 100_000_000);
        expected.insert("bob".to_string(), 100_000_000i64);

        (blocks, expected)
    }

    // ── validation tests ─────────────────────────────────────────────────

    #[test]
    fn test_validate_genesis_only() {
        let blocks = vec![genesis::create_canonical_genesis()];
        let report = validate_chain(&blocks);
        assert!(report.valid);
        assert_eq!(report.blocks_validated, 1);
        assert!(report.errors.is_empty());
    }

    #[test]
    fn test_validate_chain_with_rewards() {
        let blocks = build_test_chain(10);
        let report = validate_chain(&blocks);
        assert!(report.valid);
        assert_eq!(report.blocks_validated, 11); // genesis + 10
        assert!(report.errors.is_empty());

        // Miner should have accumulated rewards
        let miner_bal = report
            .computed_balances
            .get("test_miner_abc")
            .copied()
            .unwrap_or(0);
        assert!(miner_bal > 0);
    }

    #[test]
    fn test_validate_chain_with_transfers() {
        let (blocks, expected_balances) = build_chain_with_transfers();
        let report = validate_chain(&blocks);
        assert!(report.valid);
        assert_eq!(report.blocks_validated, 3);

        for (addr, expected) in &expected_balances {
            let actual = report.computed_balances.get(addr).copied().unwrap_or(0);
            assert_eq!(
                actual, *expected,
                "Balance mismatch for {}: got {} expected {}",
                addr, actual, expected
            );
        }
    }

    #[test]
    fn test_validate_empty_chain() {
        let report = validate_chain(&[]);
        assert!(!report.valid);
        assert_eq!(report.errors.len(), 1);
        assert!(report.errors[0].contains("Empty"));
    }

    #[test]
    fn test_validate_bad_genesis() {
        let mut bad_genesis = genesis::create_canonical_genesis();
        bad_genesis.hash = "badhash".to_string();
        let report = validate_chain(&[bad_genesis]);
        assert!(!report.valid);
        assert!(report.errors[0].contains("Genesis hash mismatch"));
    }

    #[test]
    fn test_validate_broken_chain_link() {
        let mut blocks = build_test_chain(3);
        // Break the hash chain
        blocks[2].previous_hash = "wrong_hash_xyz".to_string();
        let report = validate_chain(&blocks);
        assert!(!report.valid);
        assert!(
            report
                .errors
                .iter()
                .any(|e| e.contains("previous_hash mismatch"))
        );
    }

    #[test]
    fn test_validate_wrong_index() {
        let mut blocks = build_test_chain(3);
        blocks[2].index = 99; // wrong index
        let report = validate_chain(&blocks);
        assert!(!report.valid);
        assert!(report.errors.iter().any(|e| e.contains("wrong index")));
    }

    // ── comparison tests ─────────────────────────────────────────────────

    #[test]
    fn test_compare_balances_identical() {
        let mut a = BTreeMap::new();
        a.insert("alice".to_string(), 100i64);
        a.insert("bob".to_string(), 200i64);
        let diffs = compare_balances(&a, &a);
        assert!(diffs.is_empty());
    }

    #[test]
    fn test_compare_balances_with_diffs() {
        let mut stored = BTreeMap::new();
        stored.insert("alice".to_string(), 100i64);
        stored.insert("bob".to_string(), 200i64);

        let mut replayed = BTreeMap::new();
        replayed.insert("alice".to_string(), 100i64);
        replayed.insert("bob".to_string(), 250i64);
        replayed.insert("carol".to_string(), 50i64);

        let diffs = compare_balances(&stored, &replayed);
        assert_eq!(diffs.len(), 2); // bob differs, carol is new
    }

    #[test]
    fn test_compare_stakes_empty() {
        let a: BTreeMap<String, i64> = BTreeMap::new();
        let diffs = compare_stakes(&a, &a);
        assert!(diffs.is_empty());
    }

    // ── node state parsing tests ─────────────────────────────────────────

    #[test]
    fn test_parse_node_state_basic() {
        let json = serde_json::json!({
            "balances": {"alice": 1000, "bob": 2000},
            "contract_data": {"workloads": {}, "valid_keys": [], "deployment_keys": {}},
            "entity_registry": {
                "records": {},
                "nullifiers": [],
                "human_tree": {"leaves": [], "root": "abc"},
                "machine_tree": {"leaves": [], "root": "def"}
            },
            "device_registry": {"devices": {}, "entity_nodes": {}},
            "faucet_used_wallets": [],
            "nonce_tracker": {},
            "total_supply": 3000,
            "block_height": 5
        });

        let ns = NodeState::from_json(&json).unwrap();
        assert_eq!(ns.balances.len(), 2);
        assert_eq!(*ns.balances.get("alice").unwrap(), 1000);
        assert_eq!(ns.total_supply, 3000);
        assert_eq!(ns.block_height, 5);
    }

    #[test]
    fn test_parse_node_state_with_entities() {
        let json = serde_json::json!({
            "balances": {"miner": 5000},
            "contract_data": {
                "workloads": {"key1": {"status": "pending"}},
                "valid_keys": ["key1"],
                "deployment_keys": {}
            },
            "entity_registry": {
                "records": {"commitment_abc": {"entity_type": "human", "epoch": 1}},
                "nullifiers": ["null1", "null2"],
                "human_tree": {"leaves": [], "root": "abc"},
                "machine_tree": {"leaves": [], "root": "def"}
            },
            "device_registry": {
                "devices": {"wallet_xyz": {"trust_tier": 2}},
                "entity_nodes": {"commitment_abc": ["wallet_xyz"]}
            },
            "faucet_used_wallets": ["wallet_a"],
            "nonce_tracker": {"miner": 3},
            "total_supply": 5000,
            "block_height": 10
        });

        let ns = NodeState::from_json(&json).unwrap();
        assert_eq!(ns.entity_records.len(), 1);
        assert_eq!(ns.entity_nullifiers.len(), 2);
        assert_eq!(ns.device_registry.len(), 1);
        assert_eq!(ns.entity_nodes.len(), 1);
        assert_eq!(ns.contract_workloads.len(), 1);
        assert_eq!(ns.contract_valid_keys.len(), 1);
        assert_eq!(ns.faucet_used_wallets.len(), 1);
        assert_eq!(*ns.nonce_tracker.get("miner").unwrap(), 3);
    }

    #[test]
    fn test_parse_node_state_not_object() {
        let json = serde_json::json!([1, 2, 3]);
        assert!(NodeState::from_json(&json).is_err());
    }

    // ── snapshot export tests ────────────────────────────────────────────

    #[test]
    fn test_export_snapshot() {
        let chain = Chain::new();
        let snap = export_snapshot(&chain);
        assert_eq!(snap["height"], 1); // genesis
        assert_eq!(snap["tip_hash"], genesis::EXPECTED_GENESIS_HASH);
    }

    // ── storage helper tests ─────────────────────────────────────────────

    #[test]
    fn test_storage_get_all_balances() {
        let store = Storage::in_memory().unwrap();
        store.put_balance("alice", 100).unwrap();
        store.put_balance("bob", 200).unwrap();
        let all = store.get_all_balances().unwrap();
        assert_eq!(all.len(), 2);
        assert_eq!(*all.get("alice").unwrap(), 100);
        assert_eq!(*all.get("bob").unwrap(), 200);
    }

    #[test]
    fn test_storage_get_all_stakes() {
        let store = Storage::in_memory().unwrap();
        store.put_stake("alice", 500).unwrap();
        let all = store.get_all_stakes().unwrap();
        assert_eq!(all.len(), 1);
        assert_eq!(*all.get("alice").unwrap(), 500);
    }

    #[test]
    fn test_storage_get_latest_block() {
        let store = Storage::in_memory().unwrap();
        assert!(store.get_latest_block().unwrap().is_none());

        let genesis = genesis::create_canonical_genesis();
        store.put_block(&genesis).unwrap();
        let latest = store.get_latest_block().unwrap().unwrap();
        assert_eq!(latest.hash, genesis.hash);
    }

    #[test]
    fn test_storage_get_block_range() {
        let store = Storage::in_memory().unwrap();
        let blocks = build_test_chain(5);
        for block in &blocks {
            store.put_block(block).unwrap();
        }

        let range = store.get_block_range(1, 4).unwrap();
        assert_eq!(range.len(), 3);
        assert_eq!(range[0].index, 1);
        assert_eq!(range[2].index, 3);
    }

    #[test]
    fn test_storage_get_block_range_empty() {
        let store = Storage::in_memory().unwrap();
        let range = store.get_block_range(0, 10).unwrap();
        assert!(range.is_empty());
    }

    // ── full migration tests ─────────────────────────────────────────────

    #[test]
    fn test_migrate_genesis_only() {
        let blocks = vec![genesis::create_canonical_genesis()];
        let py_db = python_db_to_file(&blocks, &BTreeMap::new(), &BTreeMap::new());
        let rust_db = tempfile::NamedTempFile::new().unwrap();

        let report = migrate_from_python(py_db.path(), None::<&str>, rust_db.path()).unwrap();

        assert!(report.success);
        assert_eq!(report.blocks_read, 1);
        assert_eq!(report.blocks_imported, 1);
        assert_eq!(report.final_height, 1);
    }

    #[test]
    fn test_migrate_chain_with_rewards() {
        let blocks = build_test_chain(10);

        // Compute expected balances
        let mut expected_bal = BTreeMap::new();
        let total: i64 = (1..=10).map(|h| Chain::coinbase_reward(h)).sum();
        expected_bal.insert("test_miner_abc".to_string(), total);

        let py_db = python_db_to_file(&blocks, &expected_bal, &BTreeMap::new());
        let rust_db = tempfile::NamedTempFile::new().unwrap();

        let report = migrate_from_python(py_db.path(), None::<&str>, rust_db.path()).unwrap();

        assert!(report.success);
        assert_eq!(report.blocks_read, 11);
        assert_eq!(report.blocks_imported, 11);
        assert!(report.balance_diffs.is_empty(), "no balance diffs expected");
    }

    #[test]
    fn test_migrate_chain_with_transfers() {
        let (blocks, expected_bals) = build_chain_with_transfers();
        let py_db = python_db_to_file(&blocks, &expected_bals, &BTreeMap::new());
        let rust_db = tempfile::NamedTempFile::new().unwrap();

        let report = migrate_from_python(py_db.path(), None::<&str>, rust_db.path()).unwrap();

        assert!(report.success);
        assert_eq!(report.blocks_imported, 3);
        assert!(report.balance_diffs.is_empty());

        // Verify the Rust DB has the correct data
        let store = Storage::open(rust_db.path()).unwrap();
        let all_bals = store.get_all_balances().unwrap();
        for (addr, expected) in &expected_bals {
            assert_eq!(
                *all_bals.get(addr).unwrap_or(&0),
                *expected,
                "Rust DB balance mismatch for {}",
                addr
            );
        }
    }

    #[test]
    fn test_migrate_detects_balance_drift() {
        let blocks = build_test_chain(5);

        // Store wrong balances on purpose
        let mut wrong_bals = BTreeMap::new();
        wrong_bals.insert("test_miner_abc".to_string(), 999_999i64); // wrong

        let py_db = python_db_to_file(&blocks, &wrong_bals, &BTreeMap::new());
        let rust_db = tempfile::NamedTempFile::new().unwrap();

        let report = migrate_from_python(py_db.path(), None::<&str>, rust_db.path()).unwrap();

        // Migration succeeds (replay is authoritative) but reports diffs
        assert!(report.success);
        assert!(!report.balance_diffs.is_empty());
        assert!(report.warnings.iter().any(|w| w.contains("Balance diff")));
    }

    #[test]
    fn test_migrate_empty_python_db() {
        let py_db = python_db_to_file(&[], &BTreeMap::new(), &BTreeMap::new());
        let rust_db = tempfile::NamedTempFile::new().unwrap();

        let report = migrate_from_python(py_db.path(), None::<&str>, rust_db.path()).unwrap();

        assert!(!report.success);
        assert!(report.error.as_ref().unwrap().contains("no blocks"));
    }

    #[test]
    fn test_migrate_corrupt_chain_rejected() {
        let mut blocks = build_test_chain(5);
        // Break the hash link at block 3
        blocks[3].previous_hash = "corrupted_hash".to_string();

        let py_db = python_db_to_file(&blocks, &BTreeMap::new(), &BTreeMap::new());
        let rust_db = tempfile::NamedTempFile::new().unwrap();

        let report = migrate_from_python(py_db.path(), None::<&str>, rust_db.path()).unwrap();

        assert!(!report.success);
        assert!(report.error.as_ref().unwrap().contains("validation failed"));
    }

    // ── incremental sync tests ───────────────────────────────────────────

    #[test]
    fn test_incremental_sync_fresh_db() {
        let blocks = build_test_chain(5);
        let py_db = python_db_to_file(&blocks, &BTreeMap::new(), &BTreeMap::new());
        let rust_db = tempfile::NamedTempFile::new().unwrap();

        // First: full migration
        let r1 = migrate_from_python(py_db.path(), None::<&str>, rust_db.path()).unwrap();
        assert!(r1.success);
        assert_eq!(r1.blocks_imported, 6);

        // Then: incremental with same data → no new blocks
        let r2 = incremental_sync(py_db.path(), rust_db.path()).unwrap();
        assert!(r2.success);
        assert_eq!(r2.blocks_imported, 0);
        assert!(r2.warnings.iter().any(|w| w.contains("No new blocks")));
    }

    #[test]
    fn test_incremental_sync_new_blocks() {
        let blocks_5 = build_test_chain(5);
        let blocks_10 = build_test_chain(10);

        // Start with 5-block chain in Rust
        let py_db_5 = python_db_to_file(&blocks_5, &BTreeMap::new(), &BTreeMap::new());
        let rust_db = tempfile::NamedTempFile::new().unwrap();
        let r1 = migrate_from_python(py_db_5.path(), None::<&str>, rust_db.path()).unwrap();
        assert!(r1.success);

        // Python chain grows to 10 blocks
        let py_db_10 = python_db_to_file(&blocks_10, &BTreeMap::new(), &BTreeMap::new());
        let r2 = incremental_sync(py_db_10.path(), rust_db.path()).unwrap();
        assert!(r2.success);
        assert_eq!(r2.blocks_imported, 5);
        assert_eq!(r2.final_height, 11); // genesis + 10
    }

    // ── shadow comparison tests ──────────────────────────────────────────

    #[test]
    fn test_shadow_compare_identical() {
        let blocks = build_test_chain(5);
        let py_db = python_db_to_file(&blocks, &BTreeMap::new(), &BTreeMap::new());
        let rust_db = tempfile::NamedTempFile::new().unwrap();

        // Migrate first
        migrate_from_python(py_db.path(), None::<&str>, rust_db.path()).unwrap();

        let shadow = shadow_compare(py_db.path(), rust_db.path()).unwrap();
        assert!(shadow.chains_match());
        assert_eq!(shadow.matching_blocks, 6);
        assert!(shadow.divergent_indices.is_empty());
    }

    #[test]
    fn test_shadow_compare_height_mismatch() {
        let blocks_5 = build_test_chain(5);
        let blocks_3 = build_test_chain(3);

        let py_db = python_db_to_file(&blocks_5, &BTreeMap::new(), &BTreeMap::new());
        let rust_db = tempfile::NamedTempFile::new().unwrap();
        // Only migrate 3-block chain to Rust
        let rust_store = Storage::open(rust_db.path()).unwrap();
        rust_store
            .save_chain(
                &blocks_3,
                &BTreeMap::new(),
                &BTreeMap::new(),
                &BTreeMap::new(),
            )
            .unwrap();
        drop(rust_store);

        let shadow = shadow_compare(py_db.path(), rust_db.path()).unwrap();
        assert!(!shadow.chains_match()); // heights differ
        assert_eq!(shadow.python_height, 6);
        assert_eq!(shadow.rust_height, 4);
        // But matching blocks should still be correct for the overlap
        assert_eq!(shadow.matching_blocks, 4);
    }

    // ── stake migration tests ────────────────────────────────────────────

    #[test]
    fn test_migrate_with_stakes() {
        let genesis = genesis::create_canonical_genesis();
        let miner = "staker_alice";

        // Block 1: reward
        let reward = Chain::coinbase_reward(1);
        let reward_tx = Transaction::new(
            "SYSTEM",
            miner,
            reward,
            "reward",
            0,
            BTreeMap::new(),
            Some(genesis.timestamp + 15.0),
            None,
            2,
        );
        let b1 = Block::new(
            1,
            &genesis.hash,
            genesis.timestamp + 15.0,
            vec![reward_tx],
            miner,
            BTreeMap::new(),
        );

        // Block 2: stake half
        let stake_amount = reward / 2;
        let stake_tx = Transaction::new(
            miner,
            "STAKE_POOL",
            stake_amount,
            "stake",
            0,
            BTreeMap::new(),
            Some(genesis.timestamp + 30.0),
            None,
            2,
        );
        let reward_tx2 = Transaction::new(
            "SYSTEM",
            miner,
            Chain::coinbase_reward(2),
            "reward",
            0,
            BTreeMap::new(),
            Some(genesis.timestamp + 30.0),
            None,
            2,
        );
        let b2 = Block::new(
            2,
            &b1.hash,
            genesis.timestamp + 30.0,
            vec![reward_tx2, stake_tx],
            miner,
            BTreeMap::new(),
        );

        let blocks = vec![genesis, b1, b2];
        let mut expected_stakes = BTreeMap::new();
        expected_stakes.insert(miner.to_string(), stake_amount);

        let py_db = python_db_to_file(&blocks, &BTreeMap::new(), &expected_stakes);
        let rust_db = tempfile::NamedTempFile::new().unwrap();

        let report = migrate_from_python(py_db.path(), None::<&str>, rust_db.path()).unwrap();

        assert!(report.success);
        assert!(report.stake_diffs.is_empty());

        // Verify stakes in Rust DB
        let store = Storage::open(rust_db.path()).unwrap();
        let all_stakes = store.get_all_stakes().unwrap();
        assert_eq!(
            *all_stakes.get(miner).unwrap_or(&0),
            stake_amount,
            "Stake not preserved"
        );
    }
}
