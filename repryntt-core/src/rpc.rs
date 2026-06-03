//! JSON-RPC 2.0 server — public API surface for repryntt blockchain.
//!
//! Provides wallet endpoints, block explorer feeds, staking, DAO, mining stats,
//! and transaction submission.  Uses the same length-prefixed JSON wire format
//! as the Python node for compatibility.
//!
//! Wire format: 4-byte big-endian length prefix + JSON payload.
//! JSON-RPC 2.0: `{"jsonrpc":"2.0","method":"...","params":{...},"id":1}`

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::chain::{Chain, is_local_system_credit_purpose};
use crate::contract::WorkloadContract;
use crate::dao::PlanetaryDAO;
use crate::genesis::{BLOCK_INTERVAL_SECS, HALVING_INTERVAL, MAX_SUPPLY_PLANCKS};
use crate::staking::StakingManager;
use crate::token::TokenRegistry;
use crate::transaction::{PLANCKS_PER_CREDIT, Transaction};

// ── JSON-RPC 2.0 types ──────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RpcRequest {
    pub jsonrpc: String,
    pub method: String,
    #[serde(default)]
    pub params: Value,
    pub id: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RpcResponse {
    pub jsonrpc: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<RpcError>,
    pub id: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RpcError {
    pub code: i32,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data: Option<Value>,
}

// Standard JSON-RPC 2.0 error codes
pub const PARSE_ERROR: i32 = -32700;
pub const INVALID_REQUEST: i32 = -32600;
pub const METHOD_NOT_FOUND: i32 = -32601;
pub const INVALID_PARAMS: i32 = -32602;
pub const INTERNAL_ERROR: i32 = -32603;

// Application error codes
pub const INSUFFICIENT_BALANCE: i32 = -32001;
pub const INVALID_SIGNATURE: i32 = -32002;
pub const NOT_FOUND: i32 = -32003;
pub const VALIDATION_ERROR: i32 = -32004;
pub const MUTATION_DISABLED: i32 = -32005;

impl RpcResponse {
    pub fn success(id: Value, result: Value) -> Self {
        Self {
            jsonrpc: "2.0".into(),
            result: Some(result),
            error: None,
            id,
        }
    }

    pub fn error(id: Value, code: i32, message: impl Into<String>) -> Self {
        Self {
            jsonrpc: "2.0".into(),
            result: None,
            error: Some(RpcError {
                code,
                message: message.into(),
                data: None,
            }),
            id,
        }
    }

    pub fn error_with_data(id: Value, code: i32, message: impl Into<String>, data: Value) -> Self {
        Self {
            jsonrpc: "2.0".into(),
            result: None,
            error: Some(RpcError {
                code,
                message: message.into(),
                data: Some(data),
            }),
            id,
        }
    }
}

// ── Wire format ──────────────────────────────────────────────────────────────

/// Encode a message with 4-byte big-endian length prefix.
pub fn wire_encode(data: &[u8]) -> Vec<u8> {
    let len = data.len() as u32;
    let mut buf = Vec::with_capacity(4 + data.len());
    buf.extend_from_slice(&len.to_be_bytes());
    buf.extend_from_slice(data);
    buf
}

/// Try to decode a length-prefixed message from a buffer.
///
/// Returns `Some((message_bytes, consumed))` or `None` if not enough data.
pub fn wire_decode(buf: &[u8]) -> Option<(&[u8], usize)> {
    if buf.len() < 4 {
        return None;
    }
    let len = u32::from_be_bytes([buf[0], buf[1], buf[2], buf[3]]) as usize;
    if buf.len() < 4 + len {
        return None;
    }
    Some((&buf[4..4 + len], 4 + len))
}

/// Parse a raw JSON-RPC request from bytes.
pub fn parse_request(data: &[u8]) -> Result<RpcRequest, RpcResponse> {
    let text = std::str::from_utf8(data)
        .map_err(|_| RpcResponse::error(Value::Null, PARSE_ERROR, "Invalid UTF-8"))?;

    serde_json::from_str::<RpcRequest>(text)
        .map_err(|e| RpcResponse::error(Value::Null, PARSE_ERROR, format!("Parse error: {}", e)))
}

// ── RPC Router ───────────────────────────────────────────────────────────────

/// Node state accessible to RPC handlers.
///
/// In production this would use `Arc<RwLock<...>>` for each field.
/// For the core library, we pass mutable refs directly.
pub struct NodeState {
    pub chain: Chain,
    pub staking: StakingManager,
    pub dao: PlanetaryDAO,
    pub contract: WorkloadContract,
    pub tokens: TokenRegistry,
    /// Flat balance map (mirrors chain.balances but as HashMap for staking/dao).
    pub balances: HashMap<String, i64>,
    /// Flat stake map (mirrors chain.stakes as HashMap).
    pub stakes: HashMap<String, i64>,
    /// Node address (our identity).
    pub node_address: String,
    /// Connected peer count.
    pub peer_count: usize,
    /// Snapshot of mempool pending transactions (populated by node layer).
    pub mempool_snapshot: Vec<Value>,
    /// Mempool size.
    pub mempool_size: usize,
    /// Current mining gate state.
    pub mining_state: String,
    /// Human-readable reason mining is paused, if any.
    pub mining_pause_reason: String,
    /// Fork/checkpoint status.
    pub fork_status: String,
    pub checkpoint_status: String,
    pub checkpoint_height: Option<u64>,
    pub checkpoint_hash: Option<String>,
    pub bootstrap_peer_count: usize,
    pub peer_diagnostics: Vec<String>,
    pub sync_state: String,
    pub last_sync_error: String,
    pub last_sync_at: f64,
    pub best_peer_height: u64,
    pub height_lag: u64,
    /// Effective compute capacity this node advertises after local share limits.
    pub local_effective_tflops: f64,
    /// Hardware-measured TFLOPS before local share limits.
    pub local_measured_tflops: f64,
    /// Operator-selected compute share in the range 0.0..=1.0.
    pub local_compute_share: f64,
    /// Total effective TFLOPS currently considered for availability rewards.
    pub availability_tflops: f64,
    /// Number of wallets currently considered availability contributors.
    pub availability_contributor_count: usize,
    /// Chain-local availability contributor snapshot.
    pub availability_contributors: Vec<Value>,
}

impl NodeState {
    /// Sync the flat HashMaps from Chain's BTreeMaps.
    pub fn sync_from_chain(&mut self) {
        self.balances = self
            .chain
            .balances
            .iter()
            .map(|(k, v)| (k.clone(), *v))
            .collect();
        self.stakes = self
            .chain
            .stakes
            .iter()
            .map(|(k, v)| (k.clone(), *v))
            .collect();
    }
}

/// All known RPC methods.
pub const METHODS: &[&str] = &[
    // Chain info
    "get_chain_height",
    "get_block",
    "get_latest_block",
    "get_blocks",
    "get_chain_info",
    // Wallet
    "get_balance",
    "get_nonce",
    "submit_transaction",
    "submit_productive_work",
    "submit_local_credit",
    // Staking
    "get_staking_info",
    "get_validators",
    "get_leaderboard",
    // Mining
    "get_mining_stats",
    "get_network_stats",
    // DAO
    "get_treasury",
    "get_proposals",
    "get_proposal",
    "submit_proposal",
    "vote_proposal",
    "execute_proposal",
    // Token
    "get_token",
    "get_token_balance",
    "get_tokens",
    // Contract
    "get_workload",
    "get_contract_stats",
    // Explorer (detailed)
    "get_transaction",
    "get_address_history",
    "get_mempool_txs",
    "get_richlist",
    "search",
    // Utility
    "ping",
];

/// Route a parsed JSON-RPC request to the appropriate handler.
pub fn handle_request(req: &RpcRequest, state: &mut NodeState) -> RpcResponse {
    if req.jsonrpc != "2.0" {
        return RpcResponse::error(req.id.clone(), INVALID_REQUEST, "jsonrpc must be \"2.0\"");
    }

    match req.method.as_str() {
        // ── Chain info ──────────────────────────────────────
        "ping" => handle_ping(req, state),
        "get_chain_height" => handle_get_chain_height(req, state),
        "get_block" => handle_get_block(req, state),
        "get_latest_block" => handle_get_latest_block(req, state),
        "get_blocks" => handle_get_blocks(req, state),
        "get_chain_info" => handle_get_chain_info(req, state),

        // ── Wallet ──────────────────────────────────────────
        "get_balance" => handle_get_balance(req, state),
        "get_nonce" => handle_get_nonce(req, state),
        "submit_transaction" => handle_submit_transaction(req, state),
        "submit_productive_work" => handle_submit_productive_work(req, state),
        "submit_local_credit" => handle_submit_local_credit(req, state),

        // ── Staking ─────────────────────────────────────────
        "get_staking_info" => handle_get_staking_info(req, state),
        "get_validators" => handle_get_validators(req, state),
        "get_leaderboard" => handle_get_leaderboard(req, state),

        // ── Mining/Network ──────────────────────────────────
        "get_mining_stats" => handle_get_mining_stats(req, state),
        "get_network_stats" => handle_get_network_stats(req, state),

        // ── DAO ─────────────────────────────────────────────
        "get_treasury" => handle_get_treasury(req, state),
        "get_proposals" => handle_get_proposals(req, state),
        "get_proposal" => handle_get_proposal(req, state),
        "submit_proposal" => handle_submit_proposal(req, state),
        "vote_proposal" => handle_vote_proposal(req, state),
        "execute_proposal" => handle_execute_proposal(req, state),

        // ── Token ───────────────────────────────────────────
        "get_token" => handle_get_token(req, state),
        "get_token_balance" => handle_get_token_balance(req, state),
        "get_tokens" => handle_get_tokens(req, state),

        // ── Contract ────────────────────────────────────────
        "get_workload" => handle_get_workload(req, state),
        "get_contract_stats" => handle_get_contract_stats(req, state),

        // ── Explorer (detailed) ─────────────────────────────
        "get_transaction" => handle_get_transaction(req, state),
        "get_address_history" => handle_get_address_history(req, state),
        "get_mempool_txs" => handle_get_mempool_txs(req, state),
        "get_richlist" => handle_get_richlist(req, state),
        "search" => handle_search(req, state),

        _ => RpcResponse::error(
            req.id.clone(),
            METHOD_NOT_FOUND,
            format!("Method not found: {}", req.method),
        ),
    }
}

// ── Handlers: Chain Info ─────────────────────────────────────────────────────

fn handle_ping(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    RpcResponse::success(
        req.id.clone(),
        serde_json::json!({
            "status": "ok",
            "block_height": state.chain.height(),
            "node_address": state.node_address,
            "peer_count": state.peer_count,
        }),
    )
}

fn handle_get_chain_height(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let latest = state.chain.latest_block();
    RpcResponse::success(
        req.id.clone(),
        serde_json::json!({
            "height": state.chain.height(),
            "latest_hash": latest.hash,
            "latest_timestamp": latest.timestamp,
        }),
    )
}

fn handle_get_block(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let index = match req.params.get("index").and_then(|v| v.as_u64()) {
        Some(i) => i as usize,
        None => {
            // Also accept "hash" parameter
            if let Some(hash) = req.params.get("hash").and_then(|v| v.as_str()) {
                match state.chain.recent_iter().enumerate().find(|(_, b)| b.hash == hash).map(|(i, _)| i) {
                    Some(i) => i,
                    None => {
                        return RpcResponse::error(req.id.clone(), NOT_FOUND, "Block not found");
                    }
                }
            } else {
                return RpcResponse::error(
                    req.id.clone(),
                    INVALID_PARAMS,
                    "Missing 'index' or 'hash' parameter",
                );
            }
        }
    };

    if index >= state.chain.height() as usize {
        return RpcResponse::error(req.id.clone(), NOT_FOUND, "Block index out of range");
    }

    let block = match state.chain.recent_block_at(index as u64) {
        Some(b) => b,
        None => {
            return RpcResponse::error(
                req.id.clone(), NOT_FOUND,
                "Block older than the in-memory recent window; query storage for deep history.",
            );
        }
    };
    RpcResponse::success(req.id.clone(), block_to_json(block))
}

fn handle_get_latest_block(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let block = state.chain.latest_block();
    RpcResponse::success(req.id.clone(), block_to_json(block))
}

fn handle_get_blocks(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let start = req
        .params
        .get("start")
        .and_then(|v| v.as_u64())
        .unwrap_or(0) as usize;
    let end = req
        .params
        .get("end")
        .and_then(|v| v.as_u64())
        .unwrap_or(state.chain.height()) as usize;

    // Cap batch size at 500 (like Python's IBD_BATCH_SIZE)
    let end_capped = end.min(start + 500).min(state.chain.height() as usize);

    if start >= state.chain.height() as usize {
        return RpcResponse::success(
            req.id.clone(),
            serde_json::json!({ "blocks": [], "count": 0 }),
        );
    }

    // Resolve from the recent window. Indices outside the window are
    // skipped silently (callers should query storage for deep history
    // once NodeState gets a storage handle wired through).
    let blocks: Vec<Value> = (start..end_capped)
        .filter_map(|i| state.chain.recent_block_at(i as u64))
        .map(block_to_json)
        .collect();

    RpcResponse::success(
        req.id.clone(),
        serde_json::json!({
            "blocks": blocks,
            "count": blocks.len(),
            "start": start,
            "end": end_capped,
        }),
    )
}

fn handle_get_chain_info(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let height = state.chain.height();
    let supply = current_supply(&state.chain);
    let halvings = if height > 0 {
        (height - 1) / HALVING_INTERVAL
    } else {
        0
    };

    RpcResponse::success(
        req.id.clone(),
        serde_json::json!({
            "height": height,
            "latest_hash": state.chain.latest_block().hash,
            "latest_timestamp": state.chain.latest_block().timestamp,
            "genesis_hash": state.chain.genesis.hash,
            "current_supply_plancks": supply,
            "current_supply_cr": supply as f64 / PLANCKS_PER_CREDIT as f64,
            "max_supply_cr": MAX_SUPPLY_PLANCKS as f64 / PLANCKS_PER_CREDIT as f64,
            "supply_percent": (supply as f64 / MAX_SUPPLY_PLANCKS as f64) * 100.0,
            "halvings_completed": halvings,
            "current_reward_plancks": Chain::coinbase_reward(height),
            "current_reward_cr": Chain::coinbase_reward(height) as f64 / PLANCKS_PER_CREDIT as f64,
            "next_halving_block": (halvings + 1) * HALVING_INTERVAL,
            "block_interval_secs": BLOCK_INTERVAL_SECS,
            "mining_state": state.mining_state,
            "mining_pause_reason": state.mining_pause_reason,
            "fork_status": state.fork_status,
            "checkpoint_status": state.checkpoint_status,
            "checkpoint_height": state.checkpoint_height,
            "checkpoint_hash": state.checkpoint_hash,
            "peer_count": state.peer_count,
            "best_peer_height": state.best_peer_height,
            "height_lag": state.height_lag,
            "sync_state": state.sync_state,
            "last_sync_error": state.last_sync_error,
            "last_sync_at": state.last_sync_at,
        }),
    )
}

// ── Handlers: Wallet ─────────────────────────────────────────────────────────

fn handle_get_balance(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let address = match req.params.get("address").and_then(|v| v.as_str()) {
        Some(a) => a,
        None => {
            return RpcResponse::error(req.id.clone(), INVALID_PARAMS, "Missing 'address'");
        }
    };

    let balance = state.chain.balances.get(address).copied().unwrap_or(0);
    let stake = state.chain.stakes.get(address).copied().unwrap_or(0);
    let nonce = state.chain.nonces.get(address).copied().unwrap_or(0);
    let reputation = state.staking.get_reputation(address);

    RpcResponse::success(
        req.id.clone(),
        serde_json::json!({
            "address": address,
            "balance_plancks": balance,
            "balance_cr": balance as f64 / PLANCKS_PER_CREDIT as f64,
            "stake_plancks": stake,
            "stake_cr": stake as f64 / PLANCKS_PER_CREDIT as f64,
            "reputation": reputation,
            "nonce": nonce,
        }),
    )
}

fn handle_get_nonce(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let address = match req.params.get("address").and_then(|v| v.as_str()) {
        Some(a) => a,
        None => {
            return RpcResponse::error(req.id.clone(), INVALID_PARAMS, "Missing 'address'");
        }
    };

    let chain_nonce = state.chain.nonces.get(address).copied().unwrap_or(0);
    let (nonce, pending_count) =
        mempool_next_nonce_from_snapshot(address, chain_nonce, &state.mempool_snapshot);
    RpcResponse::success(
        req.id.clone(),
        serde_json::json!({
            "address": address,
            "nonce": nonce,
            "chain_nonce": chain_nonce,
            "mempool_pending": pending_count,
        }),
    )
}

const RPC_SUBMITTABLE_TX_TYPES: &[&str] = &["transfer", "stake", "stake_withdraw"];
const PRODUCTIVE_WORK_TX_TYPES: &[&str] = &["workload_completion"];
const LOCAL_CREDIT_TX_TYPES: &[&str] = &["workload_completion"];

/// Build a Transaction from RPC submit_transaction params.
///
/// Returns the parsed fields and a Transaction, or an error string.
/// Used by both the RPC validation handler and the node-level mempool insertion.
pub fn build_transaction_from_params(params: &Value) -> Result<Transaction, (i32, String)> {
    build_transaction_from_params_for_types(
        params,
        RPC_SUBMITTABLE_TX_TYPES,
        "public submit_transaction",
    )
}

pub fn build_productive_work_transaction_from_params(
    params: &Value,
    node_address: &str,
) -> Result<Transaction, (i32, String)> {
    let tx = build_transaction_from_params_for_types(
        params,
        PRODUCTIVE_WORK_TX_TYPES,
        "local productive-work submission",
    )?;
    if tx.from_address != node_address || tx.to_address != node_address {
        return Err((
            INVALID_PARAMS,
            "Productive-work rewards must use the local node wallet".to_string(),
        ));
    }
    Ok(tx)
}

pub fn build_local_credit_transaction_from_params(
    params: &Value,
    node_address: &str,
) -> Result<Transaction, (i32, String)> {
    let tx = build_transaction_from_params_for_types(
        params,
        LOCAL_CREDIT_TX_TYPES,
        "local system-credit submission",
    )?;
    if tx.from_address != node_address {
        return Err((
            INVALID_PARAMS,
            "Local system credits must be signed by the local node wallet".to_string(),
        ));
    }

    let purpose = tx
        .metadata
        .get("purpose")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if !is_local_system_credit_purpose(purpose) {
        return Err((
            INVALID_PARAMS,
            "Local system credit missing supported purpose".to_string(),
        ));
    }

    Ok(tx)
}

fn build_transaction_from_params_for_types(
    params: &Value,
    allowed_tx_types: &[&str],
    context: &str,
) -> Result<Transaction, (i32, String)> {
    let from = params
        .get("from_address")
        .and_then(|v| v.as_str())
        .ok_or((INVALID_PARAMS, "Missing 'from_address'".to_string()))?;
    let to = params
        .get("to_address")
        .and_then(|v| v.as_str())
        .ok_or((INVALID_PARAMS, "Missing 'to_address'".to_string()))?;
    let amount = params
        .get("amount")
        .and_then(|v| v.as_i64())
        .filter(|&a| a > 0)
        .ok_or((INVALID_PARAMS, "Invalid 'amount'".to_string()))?;
    let tx_type = params
        .get("tx_type")
        .and_then(|v| v.as_str())
        .unwrap_or("transfer");
    if !allowed_tx_types.contains(&tx_type) {
        return Err((
            INVALID_PARAMS,
            format!(
                "Transaction type '{}' is not accepted through {}",
                tx_type, context
            ),
        ));
    }
    let nonce = params.get("nonce").and_then(|v| v.as_u64()).unwrap_or(0);
    let timestamp = params.get("timestamp").and_then(|v| v.as_f64());

    let public_key = match params.get("public_key").and_then(|v| v.as_str()) {
        Some(pk_hex) => Some(
            hex::decode(pk_hex)
                .map_err(|_| (INVALID_SIGNATURE, "Invalid hex in public_key".to_string()))?,
        ),
        None => None,
    };

    let metadata: std::collections::BTreeMap<String, Value> = params
        .get("metadata")
        .and_then(|v| v.as_object())
        .map(|obj| obj.iter().map(|(k, v)| (k.clone(), v.clone())).collect())
        .unwrap_or_default();

    let tx_version = params
        .get("tx_version")
        .and_then(|v| v.as_u64())
        .map(|v| v as u32)
        .unwrap_or(if metadata.is_empty() { 1 } else { 2 });

    let mut tx = Transaction::new(
        from, to, amount, tx_type, nonce, metadata, timestamp, public_key, tx_version,
    );
    tx.signature = params
        .get("signature")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());
    Ok(tx)
}

fn handle_submit_transaction(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    // Validate the transaction structure.
    // Actual mempool insertion is done by the node layer (handle_rpc_connection).
    let tx_data = &req.params;

    let tx = match build_transaction_from_params(tx_data) {
        Ok(t) => t,
        Err((code, msg)) => {
            return RpcResponse::error(req.id.clone(), code, msg);
        }
    };

    if let Err((code, reason)) = validate_rpc_submission_preflight(&tx, state) {
        return RpcResponse::error(req.id.clone(), code, reason);
    }

    RpcResponse::success(
        req.id.clone(),
        serde_json::json!({
            "accepted": true,
            "tx_hash": tx.tx_hash,
            "from": tx.from_address,
            "to": tx.to_address,
            "amount_plancks": tx.amount,
            "tx_type": tx.tx_type,
        }),
    )
}

fn handle_submit_productive_work(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let tx = match build_productive_work_transaction_from_params(&req.params, &state.node_address) {
        Ok(t) => t,
        Err((code, msg)) => {
            return RpcResponse::error(req.id.clone(), code, msg);
        }
    };

    if let Err((code, reason)) = validate_rpc_submission_preflight(&tx, state) {
        return RpcResponse::error(req.id.clone(), code, reason);
    }

    RpcResponse::success(
        req.id.clone(),
        serde_json::json!({
            "accepted": true,
            "tx_hash": tx.tx_hash,
            "from": tx.from_address,
            "to": tx.to_address,
            "amount_plancks": tx.amount,
            "tx_type": tx.tx_type,
        }),
    )
}

fn handle_submit_local_credit(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let tx = match build_local_credit_transaction_from_params(&req.params, &state.node_address) {
        Ok(t) => t,
        Err((code, msg)) => {
            return RpcResponse::error(req.id.clone(), code, msg);
        }
    };

    if let Err((code, reason)) = validate_rpc_submission_preflight(&tx, state) {
        return RpcResponse::error(req.id.clone(), code, reason);
    }

    RpcResponse::success(
        req.id.clone(),
        serde_json::json!({
            "accepted": true,
            "tx_hash": tx.tx_hash,
            "from": tx.from_address,
            "to": tx.to_address,
            "amount_plancks": tx.amount,
            "tx_type": tx.tx_type,
        }),
    )
}

fn validate_rpc_submission_preflight(
    tx: &Transaction,
    state: &NodeState,
) -> Result<(), (i32, String)> {
    if tx.signature.is_none() {
        return Err((INVALID_SIGNATURE, "Missing transaction signature".into()));
    }
    if tx.public_key.is_none() {
        return Err((INVALID_SIGNATURE, "Missing public key".into()));
    }
    if !tx.verify_signature() {
        return Err((INVALID_SIGNATURE, "Invalid transaction signature".into()));
    }
    if !tx.verify_address_matches_pubkey() {
        return Err((
            INVALID_SIGNATURE,
            "Address does not match public key".into(),
        ));
    }

    let chain_nonce = state
        .chain
        .nonces
        .get(&tx.from_address)
        .copied()
        .unwrap_or(0);
    let (expected_nonce, _) =
        mempool_next_nonce_from_snapshot(&tx.from_address, chain_nonce, &state.mempool_snapshot);
    if tx.nonce != expected_nonce {
        return Err((
            INVALID_PARAMS,
            format!(
                "Invalid nonce: expected {}, got {} (mempool-aware replay protection)",
                expected_nonce, tx.nonce
            ),
        ));
    }

    match tx.tx_type.as_str() {
        "transfer" | "stake" => {
            let bal = state
                .chain
                .balances
                .get(&tx.from_address)
                .copied()
                .unwrap_or(0);
            if bal < tx.amount {
                return Err((
                    INSUFFICIENT_BALANCE,
                    format!("insufficient balance for {}", tx.from_address),
                ));
            }
        }
        "stake_withdraw" => {
            let stk = state
                .chain
                .stakes
                .get(&tx.from_address)
                .copied()
                .unwrap_or(0);
            if stk < tx.amount {
                return Err((
                    INSUFFICIENT_BALANCE,
                    format!("insufficient stake for {}", tx.from_address),
                ));
            }
        }
        "workload_completion" => {
            let purpose = tx
                .metadata
                .get("purpose")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            if crate::chain::is_productive_work_purpose(purpose) && tx.from_address != tx.to_address
            {
                return Err((
                    INVALID_PARAMS,
                    "Productive-work rewards must pay the signing node wallet".into(),
                ));
            }
            if !crate::chain::is_productive_work_purpose(purpose)
                && !is_local_system_credit_purpose(purpose)
            {
                return Err((
                    INVALID_PARAMS,
                    "Workload credit missing supported purpose".into(),
                ));
            }
        }
        _ => {}
    }

    Ok(())
}

fn mempool_next_nonce_from_snapshot(
    address: &str,
    chain_nonce: u64,
    mempool_snapshot: &[Value],
) -> (u64, usize) {
    let mut pending = std::collections::BTreeSet::new();
    for tx in mempool_snapshot {
        if tx
            .get("from_address")
            .and_then(|v| v.as_str())
            .map(|from| from == address)
            .unwrap_or(false)
        {
            if let Some(nonce) = tx.get("nonce").and_then(|v| v.as_u64()) {
                pending.insert(nonce);
            }
        }
    }

    let mut next = chain_nonce;
    while pending.contains(&next) {
        next += 1;
    }
    (next, pending.len())
}

// ── Handlers: Staking ────────────────────────────────────────────────────────

fn handle_get_staking_info(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let address = match req.params.get("address").and_then(|v| v.as_str()) {
        Some(a) => a,
        None => {
            return RpcResponse::error(req.id.clone(), INVALID_PARAMS, "Missing 'address'");
        }
    };

    let stake = state.chain.stakes.get(address).copied().unwrap_or(0);
    let reputation = state.staking.get_reputation(address);
    let is_validator = state.staking.is_validator(address, &state.stakes);

    RpcResponse::success(
        req.id.clone(),
        serde_json::json!({
            "address": address,
            "stake_plancks": stake,
            "stake_cr": stake as f64 / PLANCKS_PER_CREDIT as f64,
            "reputation": reputation,
            "is_validator": is_validator,
            "min_stake_cr": crate::staking::MIN_STAKE_PLANCKS as f64 / PLANCKS_PER_CREDIT as f64,
        }),
    )
}

fn handle_get_validators(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let validators = state.staking.validators(&state.stakes);
    let validator_info: Vec<Value> = validators
        .iter()
        .map(|addr| {
            let stake = state.chain.stakes.get(addr).copied().unwrap_or(0);
            serde_json::json!({
                "address": addr,
                "stake_plancks": stake,
                "stake_cr": stake as f64 / PLANCKS_PER_CREDIT as f64,
                "reputation": state.staking.get_reputation(addr),
            })
        })
        .collect();

    RpcResponse::success(
        req.id.clone(),
        serde_json::json!({
            "validators": validator_info,
            "count": validator_info.len(),
        }),
    )
}

fn handle_get_leaderboard(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let top_n = req
        .params
        .get("top_n")
        .and_then(|v| v.as_u64())
        .unwrap_or(20) as usize;

    let entries = state.staking.leaderboard(&state.stakes, top_n);
    let leaderboard: Vec<Value> = entries
        .iter()
        .map(|e| {
            serde_json::json!({
                "address": e.address,
                "total_earned_cr": e.total_earned_cr(),
                "workloads_completed": e.workloads_completed,
                "reputation": e.reputation,
                "stake_cr": e.stake_cr(),
            })
        })
        .collect();

    RpcResponse::success(
        req.id.clone(),
        serde_json::json!({
            "leaderboard": leaderboard,
            "count": leaderboard.len(),
        }),
    )
}

// ── Handlers: Mining / Network ───────────────────────────────────────────────

fn handle_get_mining_stats(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let height = state.chain.height();
    let supply = current_supply(&state.chain);
    let halvings = if height > 0 {
        (height - 1) / HALVING_INTERVAL
    } else {
        0
    };
    let reward = Chain::coinbase_reward(height);
    let staked: i64 = state.chain.stakes.values().sum();
    let staker_count = state.chain.stakes.len();

    RpcResponse::success(
        req.id.clone(),
        serde_json::json!({
            "block_height": height,
            "current_reward_plancks": reward,
            "current_reward_cr": reward as f64 / PLANCKS_PER_CREDIT as f64,
            "halvings_completed": halvings,
            "next_halving_block": (halvings + 1) * HALVING_INTERVAL,
            "blocks_to_halving": ((halvings + 1) * HALVING_INTERVAL) - height,
            "block_time_seconds": BLOCK_INTERVAL_SECS,
            "total_supply_plancks": supply,
            "total_supply_cr": supply as f64 / PLANCKS_PER_CREDIT as f64,
            "max_supply_cr": MAX_SUPPLY_PLANCKS as f64 / PLANCKS_PER_CREDIT as f64,
            "supply_percent": (supply as f64 / MAX_SUPPLY_PLANCKS as f64) * 100.0,
            "total_staked_plancks": staked,
            "total_staked_cr": staked as f64 / PLANCKS_PER_CREDIT as f64,
            "staker_count": staker_count,
            "availability_reward_cr": crate::staking::AVAILABILITY_REWARD_PLANCKS as f64 / PLANCKS_PER_CREDIT as f64,
            "local_effective_tflops": state.local_effective_tflops,
            "local_measured_tflops": state.local_measured_tflops,
            "local_compute_share": state.local_compute_share,
            "availability_tflops": state.availability_tflops,
            "availability_contributor_count": state.availability_contributor_count,
            "availability_contributors": state.availability_contributors,
        }),
    )
}

fn handle_get_network_stats(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let height = state.chain.height();
    let supply = current_supply(&state.chain);
    let staked: i64 = state.chain.stakes.values().sum();
    let active_wallets = state.chain.balances.len();
    let halvings = if height > 0 {
        (height - 1) / HALVING_INTERVAL
    } else {
        0
    };

    RpcResponse::success(
        req.id.clone(),
        serde_json::json!({
            "block_height": height,
            "total_supply_cr": supply as f64 / PLANCKS_PER_CREDIT as f64,
            "max_supply_cr": MAX_SUPPLY_PLANCKS as f64 / PLANCKS_PER_CREDIT as f64,
            "supply_percent": (supply as f64 / MAX_SUPPLY_PLANCKS as f64) * 100.0,
            "active_wallets": active_wallets,
            "total_staked_cr": staked as f64 / PLANCKS_PER_CREDIT as f64,
            "peers": state.peer_count,
            "active_peers": state.peer_count,
            "bootstrap_peers": state.bootstrap_peer_count,
            "fork_status": state.fork_status,
            "mining_state": state.mining_state,
            "mining_pause_reason": state.mining_pause_reason,
            "checkpoint_status": state.checkpoint_status,
            "checkpoint_height": state.checkpoint_height,
            "checkpoint_hash": state.checkpoint_hash,
            "best_peer_height": state.best_peer_height,
            "height_lag": state.height_lag,
            "sync_state": state.sync_state,
            "last_sync_error": state.last_sync_error,
            "last_sync_at": state.last_sync_at,
            "peer_diagnostics": state.peer_diagnostics,
            "halving_interval": HALVING_INTERVAL,
            "current_halvings": halvings,
            "block_interval_secs": BLOCK_INTERVAL_SECS,
            "node_address": state.node_address,
            "local_effective_tflops": state.local_effective_tflops,
            "local_measured_tflops": state.local_measured_tflops,
            "local_compute_share": state.local_compute_share,
            "availability_tflops": state.availability_tflops,
            "availability_contributor_count": state.availability_contributor_count,
            "availability_contributors": state.availability_contributors,
        }),
    )
}

// ── Handlers: DAO ────────────────────────────────────────────────────────────

fn handle_get_treasury(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let balance = state.dao.treasury_balance(&state.balances);
    let stats = state.dao.stats();

    RpcResponse::success(
        req.id.clone(),
        serde_json::json!({
            "treasury_plancks": balance,
            "treasury_cr": balance as f64 / PLANCKS_PER_CREDIT as f64,
            "total_proposals": stats.total_proposals,
            "active_proposals": stats.active,
            "executed_proposals": stats.executed,
            "rejected_proposals": stats.rejected,
            "total_allocated_plancks": stats.total_allocated_plancks,
            "total_allocated_cr": stats.total_allocated_plancks as f64 / PLANCKS_PER_CREDIT as f64,
        }),
    )
}

fn handle_get_proposals(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let status_filter = req.params.get("status").and_then(|v| v.as_str());

    let proposals: Vec<Value> = if let Some(status) = status_filter {
        let filter = match status {
            "active" => Some(crate::dao::ProposalStatus::Active),
            "executed" => Some(crate::dao::ProposalStatus::Executed),
            "rejected" => Some(crate::dao::ProposalStatus::Rejected),
            "passed" => Some(crate::dao::ProposalStatus::Passed),
            "expired" => Some(crate::dao::ProposalStatus::Expired),
            _ => None,
        };
        state
            .dao
            .get_proposals(filter)
            .iter()
            .map(|p| p.to_dict())
            .collect()
    } else {
        state
            .dao
            .get_proposals(None)
            .iter()
            .map(|p| p.to_dict())
            .collect()
    };

    RpcResponse::success(
        req.id.clone(),
        serde_json::json!({
            "proposals": proposals,
            "count": proposals.len(),
        }),
    )
}

fn handle_get_proposal(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let id = match req.params.get("proposal_id").and_then(|v| v.as_str()) {
        Some(id) => id,
        None => {
            return RpcResponse::error(req.id.clone(), INVALID_PARAMS, "Missing 'proposal_id'");
        }
    };

    match state.dao.get_proposal(id) {
        Some(p) => RpcResponse::success(req.id.clone(), p.to_dict()),
        None => RpcResponse::error(req.id.clone(), NOT_FOUND, "Proposal not found"),
    }
}

fn handle_submit_proposal(req: &RpcRequest, state: &mut NodeState) -> RpcResponse {
    if !dao_mutations_enabled() {
        return dao_mutation_disabled(req);
    }
    let proposer = match req.params.get("proposer").and_then(|v| v.as_str()) {
        Some(a) => a,
        None => return RpcResponse::error(req.id.clone(), INVALID_PARAMS, "Missing 'proposer'"),
    };
    let title = match req.params.get("title").and_then(|v| v.as_str()) {
        Some(t) => t,
        None => return RpcResponse::error(req.id.clone(), INVALID_PARAMS, "Missing 'title'"),
    };
    let description = req
        .params
        .get("description")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let amount = match req.params.get("amount_plancks").and_then(|v| v.as_i64()) {
        Some(a) if a > 0 => a,
        _ => return RpcResponse::error(req.id.clone(), INVALID_PARAMS, "Invalid 'amount_plancks'"),
    };
    let recipient = match req.params.get("recipient").and_then(|v| v.as_str()) {
        Some(r) => r,
        None => return RpcResponse::error(req.id.clone(), INVALID_PARAMS, "Missing 'recipient'"),
    };

    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();

    match state
        .dao
        .create_proposal(proposer, title, description, amount, recipient, now, None)
    {
        Ok(proposal_id) => RpcResponse::success(
            req.id.clone(),
            serde_json::json!({
                "proposal_id": proposal_id,
                "status": "active",
                "proposer": proposer,
                "title": title,
                "amount_plancks": amount,
                "recipient": recipient,
            }),
        ),
        Err(e) => RpcResponse::error(req.id.clone(), INVALID_PARAMS, e),
    }
}

fn handle_vote_proposal(req: &RpcRequest, state: &mut NodeState) -> RpcResponse {
    if !dao_mutations_enabled() {
        return dao_mutation_disabled(req);
    }
    let proposal_id = match req.params.get("proposal_id").and_then(|v| v.as_str()) {
        Some(id) => id,
        None => return RpcResponse::error(req.id.clone(), INVALID_PARAMS, "Missing 'proposal_id'"),
    };
    let voter = match req.params.get("voter").and_then(|v| v.as_str()) {
        Some(v) => v,
        None => return RpcResponse::error(req.id.clone(), INVALID_PARAMS, "Missing 'voter'"),
    };
    let direction_str = req
        .params
        .get("direction")
        .and_then(|v| v.as_str())
        .unwrap_or("for");
    let direction = match direction_str {
        "for" | "yes" => crate::dao::VoteDirection::For,
        "against" | "no" => crate::dao::VoteDirection::Against,
        _ => {
            return RpcResponse::error(
                req.id.clone(),
                INVALID_PARAMS,
                "direction must be 'for' or 'against'",
            );
        }
    };

    // Weight = voter's stake (min 1 for non-stakers to allow participation)
    let stake_weight = state.stakes.get(voter).copied().unwrap_or(0).max(1) as u64;

    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();

    match state
        .dao
        .vote(proposal_id, voter, direction, stake_weight, now)
    {
        Ok(result) => RpcResponse::success(
            req.id.clone(),
            serde_json::json!({
                "proposal_id": proposal_id,
                "voter": voter,
                "votes_for": result.votes_for,
                "votes_against": result.votes_against,
            }),
        ),
        Err(e) => RpcResponse::error(req.id.clone(), INVALID_PARAMS, e),
    }
}

fn handle_execute_proposal(req: &RpcRequest, state: &mut NodeState) -> RpcResponse {
    if !dao_mutations_enabled() {
        return dao_mutation_disabled(req);
    }
    let proposal_id = match req.params.get("proposal_id").and_then(|v| v.as_str()) {
        Some(id) => id,
        None => return RpcResponse::error(req.id.clone(), INVALID_PARAMS, "Missing 'proposal_id'"),
    };

    match state.dao.execute_proposal(proposal_id, &mut state.balances) {
        Ok(result) => RpcResponse::success(
            req.id.clone(),
            serde_json::json!({
                "proposal_id": result.proposal_id,
                "executed": true,
                "amount_plancks": result.amount_plancks,
                "recipient": result.recipient,
                "treasury_remaining": state.dao.treasury_balance(&state.balances),
            }),
        ),
        Err(e) => RpcResponse::error(req.id.clone(), INVALID_PARAMS, e),
    }
}

fn dao_mutations_enabled() -> bool {
    std::env::var("REPRYNTT_ENABLE_UNSAFE_DAO_RPC")
        .map(|v| matches!(v.as_str(), "1" | "true" | "TRUE" | "yes" | "YES"))
        .unwrap_or(false)
}

fn dao_mutation_disabled(req: &RpcRequest) -> RpcResponse {
    RpcResponse::error(
        req.id.clone(),
        MUTATION_DISABLED,
        "DAO mutation RPC is disabled until signed on-chain governance transactions are activated",
    )
}

// ── Handlers: Token ──────────────────────────────────────────────────────────

fn handle_get_token(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let token_id = match req.params.get("token_id").and_then(|v| v.as_str()) {
        Some(id) => id,
        None => {
            // Try symbol lookup
            if let Some(symbol) = req.params.get("symbol").and_then(|v| v.as_str()) {
                match state.tokens.get_by_symbol(symbol) {
                    Some(meta) => {
                        return RpcResponse::success(req.id.clone(), meta.to_dict());
                    }
                    None => {
                        return RpcResponse::error(req.id.clone(), NOT_FOUND, "Token not found");
                    }
                }
            }
            return RpcResponse::error(
                req.id.clone(),
                INVALID_PARAMS,
                "Missing 'token_id' or 'symbol'",
            );
        }
    };

    match state.tokens.tokens.get(token_id) {
        Some(meta) => RpcResponse::success(req.id.clone(), meta.to_dict()),
        None => RpcResponse::error(req.id.clone(), NOT_FOUND, "Token not found"),
    }
}

fn handle_get_token_balance(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let token_id = match req.params.get("token_id").and_then(|v| v.as_str()) {
        Some(id) => id,
        None => {
            return RpcResponse::error(req.id.clone(), INVALID_PARAMS, "Missing 'token_id'");
        }
    };
    let address = match req.params.get("address").and_then(|v| v.as_str()) {
        Some(a) => a,
        None => {
            return RpcResponse::error(req.id.clone(), INVALID_PARAMS, "Missing 'address'");
        }
    };

    let balance = state.tokens.balance_of(token_id, address);
    RpcResponse::success(
        req.id.clone(),
        serde_json::json!({
            "token_id": token_id,
            "address": address,
            "balance": balance,
        }),
    )
}

fn handle_get_tokens(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let stats = state.tokens.stats();
    let tokens: Vec<Value> = state
        .tokens
        .tokens
        .values()
        .map(|meta| meta.to_dict())
        .collect();

    RpcResponse::success(
        req.id.clone(),
        serde_json::json!({
            "tokens": tokens,
            "total_tokens": stats.total_tokens,
            "total_holders": stats.total_holders,
        }),
    )
}

// ── Handlers: Contract ───────────────────────────────────────────────────────

fn handle_get_workload(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let key = match req.params.get("workload_key").and_then(|v| v.as_str()) {
        Some(k) => k,
        None => {
            return RpcResponse::error(req.id.clone(), INVALID_PARAMS, "Missing 'workload_key'");
        }
    };

    if let Some(workload) = state.contract.workloads.get(key) {
        let mut response = workload.to_dict();
        // Include result if completed
        if let Some(result) = state.contract.get_result(key) {
            response["result"] = result.to_dict();
        }
        RpcResponse::success(req.id.clone(), response)
    } else {
        RpcResponse::error(req.id.clone(), NOT_FOUND, "Workload not found")
    }
}

fn handle_get_contract_stats(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let stats = state.contract.stats();
    RpcResponse::success(
        req.id.clone(),
        serde_json::json!({
            "total_workloads": stats.total_workloads,
            "pending": stats.pending,
            "claimed": stats.claimed,
            "completed": stats.completed,
            "registered_machines": stats.registered_machines,
            "results_stored": stats.results_stored,
        }),
    )
}

// ── Helpers ──────────────────────────────────────────────────────────────────

fn current_supply(chain: &Chain) -> i64 {
    let bal_sum: i64 = chain.balances.values().sum();
    let stake_sum: i64 = chain.stakes.values().sum();
    bal_sum + stake_sum
}

fn block_to_json(block: &crate::block::Block) -> Value {
    let txs: Vec<Value> = block
        .transactions
        .iter()
        .map(|tx| {
            serde_json::json!({
                "tx_hash": tx.tx_hash,
                "from_address": tx.from_address,
                "to_address": tx.to_address,
                "amount": tx.amount,
                "amount_cr": tx.amount as f64 / PLANCKS_PER_CREDIT as f64,
                "tx_type": tx.tx_type,
                "nonce": tx.nonce,
                "timestamp": tx.timestamp,
                "metadata": tx.metadata,
            })
        })
        .collect();

    serde_json::json!({
        "index": block.index,
        "hash": block.hash,
        "previous_hash": block.previous_hash,
        "timestamp": block.timestamp,
        "miner": block.miner_address,
        "transaction_count": block.transactions.len(),
        "transactions": txs,
    })
}

/// Rich transaction detail including block context and confirmations.
fn tx_to_detail(
    tx: &crate::transaction::Transaction,
    block: &crate::block::Block,
    chain_height: u64,
) -> Value {
    let confirmations = if chain_height >= block.index {
        chain_height - block.index + 1
    } else {
        0
    };
    serde_json::json!({
        "tx_hash": tx.tx_hash,
        "from_address": tx.from_address,
        "to_address": tx.to_address,
        "amount": tx.amount,
        "amount_cr": tx.amount as f64 / PLANCKS_PER_CREDIT as f64,
        "tx_type": tx.tx_type,
        "nonce": tx.nonce,
        "timestamp": tx.timestamp,
        "metadata": tx.metadata,
        "block_index": block.index,
        "block_hash": block.hash,
        "miner": block.miner_address,
        "confirmations": confirmations,
        "status": "confirmed",
    })
}

// ── Handlers: Explorer (detailed) ────────────────────────────────────────────

/// Find a transaction by hash across all blocks.
fn handle_get_transaction(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let tx_hash = match req.params.get("tx_hash").and_then(|v| v.as_str()) {
        Some(h) => h,
        None => return RpcResponse::error(req.id.clone(), INVALID_PARAMS, "Missing 'tx_hash'"),
    };

    let height = state.chain.height();

    // Search blocks from newest to oldest for faster lookup of recent txs
    for block in state.chain.recent_iter().rev() {
        for tx in &block.transactions {
            if tx.tx_hash == tx_hash {
                return RpcResponse::success(req.id.clone(), tx_to_detail(tx, block, height));
            }
        }
    }

    // Pending transactions live in the mempool, not in blocks yet. Returning a
    // pending detail here keeps explorers from showing "not found" for txs that
    // are legitimately waiting to be mined.
    for tx in &state.mempool_snapshot {
        if tx.get("tx_hash").and_then(|v| v.as_str()) == Some(tx_hash) {
            let mut detail = tx.clone();
            if let Some(map) = detail.as_object_mut() {
                map.insert("status".into(), Value::String("pending".into()));
                map.insert("confirmations".into(), Value::Number(0.into()));
                map.insert("block_index".into(), Value::Null);
                map.insert("block_hash".into(), Value::Null);
                map.insert("miner".into(), Value::Null);
            }
            return RpcResponse::success(req.id.clone(), detail);
        }
    }

    RpcResponse::error(req.id.clone(), NOT_FOUND, "Transaction not found")
}

/// Get all transactions involving an address, paginated.
fn handle_get_address_history(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let address = match req.params.get("address").and_then(|v| v.as_str()) {
        Some(a) => a,
        None => return RpcResponse::error(req.id.clone(), INVALID_PARAMS, "Missing 'address'"),
    };
    let page = req.params.get("page").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
    let limit = req
        .params
        .get("limit")
        .and_then(|v| v.as_u64())
        .unwrap_or(50)
        .min(200) as usize;

    let height = state.chain.height();
    let balance = state.chain.balances.get(address).copied().unwrap_or(0);
    let stake = state.chain.stakes.get(address).copied().unwrap_or(0);
    let nonce = state.chain.nonces.get(address).copied().unwrap_or(0);

    // Collect all transactions involving this address (newest first)
    let mut all_txs: Vec<Value> = Vec::new();
    let mut total_received: i64 = 0;
    let mut total_sent: i64 = 0;
    let mut first_seen: Option<f64> = None;
    let mut last_seen: f64 = 0.0;

    for block in state.chain.recent_iter().rev() {
        for tx in &block.transactions {
            let is_sender = tx.from_address == address;
            let is_receiver = tx.to_address == address;
            if is_sender || is_receiver {
                let mut detail = tx_to_detail(tx, block, height);
                if let Some(obj) = detail.as_object_mut() {
                    obj.insert(
                        "direction".to_string(),
                        if is_sender && is_receiver {
                            Value::String("self".to_string())
                        } else if is_sender {
                            Value::String("sent".to_string())
                        } else {
                            Value::String("received".to_string())
                        },
                    );
                }
                if is_receiver {
                    total_received += tx.amount;
                }
                if is_sender {
                    total_sent += tx.amount;
                }
                if first_seen.is_none() || tx.timestamp < first_seen.unwrap() {
                    first_seen = Some(tx.timestamp);
                }
                if tx.timestamp > last_seen {
                    last_seen = tx.timestamp;
                }
                all_txs.push(detail);
            }
        }
    }

    let total_count = all_txs.len();
    let start = page * limit;
    let page_txs: Vec<Value> = all_txs.into_iter().skip(start).take(limit).collect();

    RpcResponse::success(
        req.id.clone(),
        serde_json::json!({
            "address": address,
            "balance_plancks": balance,
            "balance_cr": balance as f64 / PLANCKS_PER_CREDIT as f64,
            "stake_plancks": stake,
            "stake_cr": stake as f64 / PLANCKS_PER_CREDIT as f64,
            "nonce": nonce,
            "total_received_plancks": total_received,
            "total_received_cr": total_received as f64 / PLANCKS_PER_CREDIT as f64,
            "total_sent_plancks": total_sent,
            "total_sent_cr": total_sent as f64 / PLANCKS_PER_CREDIT as f64,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "transaction_count": total_count,
            "transactions": page_txs,
            "page": page,
            "limit": limit,
            "has_more": start + limit < total_count,
        }),
    )
}

/// Get pending transactions in the mempool.
fn handle_get_mempool_txs(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    RpcResponse::success(
        req.id.clone(),
        serde_json::json!({
            "pending_transactions": state.mempool_snapshot,
            "count": state.mempool_size,
        }),
    )
}

/// Top addresses by balance (rich list).
fn handle_get_richlist(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let limit = req
        .params
        .get("limit")
        .and_then(|v| v.as_u64())
        .unwrap_or(50)
        .min(500) as usize;
    let offset = req
        .params
        .get("offset")
        .and_then(|v| v.as_u64())
        .unwrap_or(0) as usize;

    let supply = current_supply(&state.chain);
    let mut entries: Vec<(&String, &i64)> = state.chain.balances.iter().collect();
    entries.sort_by(|a, b| b.1.cmp(a.1)); // descending

    let total_addresses = entries.len();
    let page: Vec<Value> = entries
        .iter()
        .skip(offset)
        .take(limit)
        .enumerate()
        .map(|(i, (addr, bal))| {
            let stake = state.chain.stakes.get(*addr).copied().unwrap_or(0);
            let total = **bal + stake;
            serde_json::json!({
                "rank": offset + i + 1,
                "address": addr,
                "balance_plancks": bal,
                "balance_cr": **bal as f64 / PLANCKS_PER_CREDIT as f64,
                "stake_plancks": stake,
                "stake_cr": stake as f64 / PLANCKS_PER_CREDIT as f64,
                "total_plancks": total,
                "total_cr": total as f64 / PLANCKS_PER_CREDIT as f64,
                "percent_supply": if supply > 0 { (total as f64 / supply as f64) * 100.0 } else { 0.0 },
            })
        })
        .collect();

    RpcResponse::success(
        req.id.clone(),
        serde_json::json!({
            "richlist": page,
            "total_addresses": total_addresses,
            "offset": offset,
            "limit": limit,
        }),
    )
}

/// Universal search: tx hash, block hash, block index, or address.
fn handle_search(req: &RpcRequest, state: &NodeState) -> RpcResponse {
    let query = match req.params.get("query").and_then(|v| v.as_str()) {
        Some(q) => q.trim(),
        None => return RpcResponse::error(req.id.clone(), INVALID_PARAMS, "Missing 'query'"),
    };

    // Try block index (numeric)
    if let Ok(idx) = query.parse::<u64>() {
        if (idx as usize) < state.chain.height() as usize {
            return RpcResponse::success(
                req.id.clone(),
                serde_json::json!({
                    "type": "block",
                    "block": block_to_json(state.chain.recent_iter().collect::<Vec<_>>().as_slice()[idx as usize]),
                }),
            );
        }
    }

    // Try block hash
    for block in state.chain.recent_iter().collect::<Vec<_>>().as_slice() {
        if block.hash == query {
            return RpcResponse::success(
                req.id.clone(),
                serde_json::json!({
                    "type": "block",
                    "block": block_to_json(block),
                }),
            );
        }
    }

    // Try transaction hash
    let height = state.chain.height();
    for block in state.chain.recent_iter().rev() {
        for tx in &block.transactions {
            if tx.tx_hash == query {
                return RpcResponse::success(
                    req.id.clone(),
                    serde_json::json!({
                        "type": "transaction",
                        "transaction": tx_to_detail(tx, block, height),
                    }),
                );
            }
        }
    }

    // Try address
    if state.chain.balances.contains_key(query)
        || state.chain.stakes.contains_key(query)
        || query == "DAO"
        || query == "SYSTEM"
        || query == "FAUCET"
        || query == "STAKE_POOL"
    {
        let balance = state.chain.balances.get(query).copied().unwrap_or(0);
        let stake = state.chain.stakes.get(query).copied().unwrap_or(0);
        return RpcResponse::success(
            req.id.clone(),
            serde_json::json!({
                "type": "address",
                "address": query,
                "balance_plancks": balance,
                "balance_cr": balance as f64 / PLANCKS_PER_CREDIT as f64,
                "stake_plancks": stake,
                "stake_cr": stake as f64 / PLANCKS_PER_CREDIT as f64,
            }),
        );
    }

    RpcResponse::error(req.id.clone(), NOT_FOUND, "No results found")
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::chain::Chain;
    use crate::contract::WorkloadContract;
    use crate::crypto;
    use crate::dao::PlanetaryDAO;
    use crate::staking::StakingManager;
    use crate::token::TokenRegistry;
    use std::collections::BTreeMap;

    fn make_state() -> NodeState {
        let chain = Chain::new();
        let mut state = NodeState {
            chain,
            staking: StakingManager::new(),
            dao: PlanetaryDAO::new(),
            contract: WorkloadContract::new(),
            tokens: TokenRegistry::new(),
            balances: HashMap::new(),
            stakes: HashMap::new(),
            node_address: "test_node_001".into(),
            peer_count: 5,
            mempool_snapshot: Vec::new(),
            mempool_size: 0,
            mining_state: "enabled".into(),
            mining_pause_reason: String::new(),
            fork_status: "synced".into(),
            checkpoint_status: "no_checkpoint".into(),
            checkpoint_height: None,
            checkpoint_hash: None,
            bootstrap_peer_count: 0,
            peer_diagnostics: Vec::new(),
            sync_state: "idle".into(),
            last_sync_error: String::new(),
            last_sync_at: 0.0,
            best_peer_height: 0,
            height_lag: 0,
            local_effective_tflops: 5.4,
            local_measured_tflops: 5.4,
            local_compute_share: 1.0,
            availability_tflops: 5.4,
            availability_contributor_count: 1,
            availability_contributors: vec![serde_json::json!({
                "address": "test_node_001",
                "effective_tflops": 5.4,
            })],
        };
        state.sync_from_chain();
        state
    }

    fn req(method: &str, params: Value) -> RpcRequest {
        RpcRequest {
            jsonrpc: "2.0".into(),
            method: method.into(),
            params,
            id: Value::Number(1.into()),
        }
    }

    fn assert_success(resp: &RpcResponse) -> &Value {
        assert!(
            resp.error.is_none(),
            "Expected success, got error: {:?}",
            resp.error
        );
        resp.result.as_ref().unwrap()
    }

    fn assert_error(resp: &RpcResponse, expected_code: i32) {
        assert!(
            resp.error.is_some(),
            "Expected error code {}, got success",
            expected_code
        );
        assert_eq!(resp.error.as_ref().unwrap().code, expected_code);
    }

    fn signed_transfer_params(to: &str, amount: i64, nonce: u64) -> (Value, String) {
        let (sk, pk) = crypto::generate_keypair();
        let from = crypto::address_from_pubkey(&pk);
        let mut tx = Transaction::new(
            &from,
            to,
            amount,
            "transfer",
            nonce,
            BTreeMap::new(),
            Some(1_800_000_000.0),
            Some(pk),
            2,
        );
        tx.sign(&sk);
        (
            serde_json::json!({
                "from_address": from,
                "to_address": to,
                "amount": amount,
                "tx_type": "transfer",
                "nonce": nonce,
                "timestamp": tx.timestamp,
                "tx_version": tx.tx_version,
                "signature": tx.signature.clone().unwrap(),
                "public_key": tx.public_key.clone().unwrap(),
            }),
            tx.from_address,
        )
    }

    fn signed_productive_work_params(amount: i64, nonce: u64) -> (Value, String) {
        let (sk, pk) = crypto::generate_keypair();
        let from = crypto::address_from_pubkey(&pk);
        let mut metadata = BTreeMap::new();
        metadata.insert("purpose".into(), serde_json::Value::String("popw".into()));
        metadata.insert(
            "source".into(),
            serde_json::Value::String("proof_of_productive_work".into()),
        );
        metadata.insert(
            "popw_batch_id".into(),
            serde_json::Value::String(format!("test-popw-{from}-{nonce}")),
        );
        let mut tx = Transaction::new(
            &from,
            &from,
            amount,
            "workload_completion",
            nonce,
            metadata.clone(),
            Some(1_800_000_000.0),
            Some(pk),
            2,
        );
        tx.sign(&sk);
        (
            serde_json::json!({
                "from_address": from,
                "to_address": from,
                "amount": amount,
                "tx_type": "workload_completion",
                "nonce": nonce,
                "timestamp": tx.timestamp,
                "metadata": metadata,
                "tx_version": tx.tx_version,
                "signature": tx.signature.clone().unwrap(),
                "public_key": tx.public_key.clone().unwrap(),
            }),
            tx.from_address,
        )
    }

    fn signed_local_credit_params(to: &str, amount: i64, nonce: u64) -> (Value, String) {
        let (sk, pk) = crypto::generate_keypair();
        let from = crypto::address_from_pubkey(&pk);
        let mut metadata = BTreeMap::new();
        metadata.insert(
            "purpose".into(),
            serde_json::Value::String("robot_economy_credit".into()),
        );
        metadata.insert(
            "source".into(),
            serde_json::Value::String("robot_economy_test".into()),
        );
        let mut tx = Transaction::new(
            &from,
            to,
            amount,
            "workload_completion",
            nonce,
            metadata.clone(),
            Some(1_800_000_000.0),
            Some(pk),
            2,
        );
        tx.sign(&sk);
        (
            serde_json::json!({
                "from_address": from,
                "to_address": to,
                "amount": amount,
                "tx_type": "workload_completion",
                "nonce": nonce,
                "timestamp": tx.timestamp,
                "metadata": metadata,
                "tx_version": tx.tx_version,
                "signature": tx.signature.clone().unwrap(),
                "public_key": tx.public_key.clone().unwrap(),
            }),
            tx.from_address,
        )
    }

    // ── Wire format ─────────────────────────────────────────────

    #[test]
    fn test_wire_encode_decode() {
        let data = b"hello world";
        let encoded = wire_encode(data);
        assert_eq!(encoded.len(), 4 + data.len());

        let (decoded, consumed) = wire_decode(&encoded).unwrap();
        assert_eq!(decoded, data);
        assert_eq!(consumed, encoded.len());
    }

    #[test]
    fn test_wire_decode_incomplete() {
        assert!(wire_decode(b"ab").is_none()); // too short for length
        let encoded = wire_encode(b"hello");
        assert!(wire_decode(&encoded[..6]).is_none()); // partial message
    }

    #[test]
    fn test_wire_decode_multiple() {
        let msg1 = wire_encode(b"first");
        let msg2 = wire_encode(b"second");
        let mut buf = Vec::new();
        buf.extend_from_slice(&msg1);
        buf.extend_from_slice(&msg2);

        let (d1, c1) = wire_decode(&buf).unwrap();
        assert_eq!(d1, b"first");
        let (d2, _c2) = wire_decode(&buf[c1..]).unwrap();
        assert_eq!(d2, b"second");
    }

    // ── Request parsing ─────────────────────────────────────────

    #[test]
    fn test_parse_request_valid() {
        let json = r#"{"jsonrpc":"2.0","method":"ping","params":{},"id":1}"#;
        let req = parse_request(json.as_bytes()).unwrap();
        assert_eq!(req.method, "ping");
    }

    #[test]
    fn test_parse_request_invalid_json() {
        let result = parse_request(b"not json");
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.error.unwrap().code, PARSE_ERROR);
    }

    #[test]
    fn test_parse_request_invalid_utf8() {
        let result = parse_request(&[0xFF, 0xFE]);
        assert!(result.is_err());
    }

    // ── Method routing ──────────────────────────────────────────

    #[test]
    fn test_unknown_method() {
        let mut state = make_state();
        let r = req("nonexistent_method", serde_json::json!({}));
        let resp = handle_request(&r, &mut state);
        assert_error(&resp, METHOD_NOT_FOUND);
    }

    #[test]
    fn test_invalid_jsonrpc_version() {
        let mut state = make_state();
        let r = RpcRequest {
            jsonrpc: "1.0".into(),
            method: "ping".into(),
            params: Value::Null,
            id: Value::Number(1.into()),
        };
        let resp = handle_request(&r, &mut state);
        assert_error(&resp, INVALID_REQUEST);
    }

    // ── Ping ────────────────────────────────────────────────────

    #[test]
    fn test_ping() {
        let mut state = make_state();
        let r = req("ping", serde_json::json!({}));
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["status"], "ok");
        assert_eq!(result["block_height"], 1);
        assert_eq!(result["node_address"], "test_node_001");
        assert_eq!(result["peer_count"], 5);
    }

    // ── Chain info ──────────────────────────────────────────────

    #[test]
    fn test_get_chain_height() {
        let mut state = make_state();
        let r = req("get_chain_height", serde_json::json!({}));
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["height"], 1);
        assert!(result["latest_hash"].as_str().unwrap().len() > 0);
    }

    #[test]
    fn test_get_latest_block() {
        let mut state = make_state();
        let r = req("get_latest_block", serde_json::json!({}));
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["index"], 0);
        assert!(result["hash"].as_str().is_some());
    }

    #[test]
    fn test_get_block_by_index() {
        let mut state = make_state();
        let r = req("get_block", serde_json::json!({"index": 0}));
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["index"], 0);
    }

    #[test]
    fn test_get_block_not_found() {
        let mut state = make_state();
        let r = req("get_block", serde_json::json!({"index": 999}));
        let resp = handle_request(&r, &mut state);
        assert_error(&resp, NOT_FOUND);
    }

    #[test]
    fn test_get_block_missing_params() {
        let mut state = make_state();
        let r = req("get_block", serde_json::json!({}));
        let resp = handle_request(&r, &mut state);
        assert_error(&resp, INVALID_PARAMS);
    }

    #[test]
    fn test_get_blocks_range() {
        let mut state = make_state();
        let r = req("get_blocks", serde_json::json!({"start": 0, "end": 10}));
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["count"], 1); // Only genesis
        assert_eq!(result["blocks"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn test_get_chain_info() {
        let mut state = make_state();
        let r = req("get_chain_info", serde_json::json!({}));
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["height"], 1);
        assert_eq!(result["max_supply_cr"], 21_000_000.0);
        assert!(result["genesis_hash"].as_str().is_some());
    }

    // ── Wallet ──────────────────────────────────────────────────

    #[test]
    fn test_get_balance() {
        let mut state = make_state();
        let r = req("get_balance", serde_json::json!({"address": "alice"}));
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["address"], "alice");
        assert_eq!(result["balance_plancks"], 0);
        assert_eq!(result["nonce"], 0);
    }

    #[test]
    fn test_get_balance_missing_address() {
        let mut state = make_state();
        let r = req("get_balance", serde_json::json!({}));
        let resp = handle_request(&r, &mut state);
        assert_error(&resp, INVALID_PARAMS);
    }

    #[test]
    fn test_get_nonce() {
        let mut state = make_state();
        let r = req("get_nonce", serde_json::json!({"address": "alice"}));
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["nonce"], 0);
    }

    #[test]
    fn test_get_nonce_includes_contiguous_mempool_pending() {
        let mut state = make_state();
        state.chain.nonces.insert("alice".into(), 7);
        state.mempool_snapshot = vec![
            serde_json::json!({"from_address": "alice", "nonce": 7}),
            serde_json::json!({"from_address": "alice", "nonce": 8}),
            serde_json::json!({"from_address": "alice", "nonce": 10}),
            serde_json::json!({"from_address": "bob", "nonce": 9}),
        ];
        let r = req("get_nonce", serde_json::json!({"address": "alice"}));
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["chain_nonce"], 7);
        assert_eq!(result["nonce"], 9);
        assert_eq!(result["mempool_pending"], 3);
    }

    // ── Submit transaction ──────────────────────────────────────

    #[test]
    fn test_submit_transaction_valid() {
        let mut state = make_state();
        let (params, from) = signed_transfer_params("bob", PLANCKS_PER_CREDIT, 0);
        state.chain.balances.insert(from, 100 * PLANCKS_PER_CREDIT);
        state.sync_from_chain();

        let r = req("submit_transaction", params);
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["accepted"], true);
        assert!(result["tx_hash"].as_str().is_some());
    }

    #[test]
    fn test_submit_productive_work_valid() {
        let mut state = make_state();
        let (params, from) = signed_productive_work_params(PLANCKS_PER_CREDIT, 0);
        state.node_address = from;

        let r = req("submit_productive_work", params);
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["accepted"], true);
        assert_eq!(result["tx_type"], "workload_completion");
        assert!(result["tx_hash"].as_str().is_some());
    }

    #[test]
    fn test_submit_local_credit_valid() {
        let mut state = make_state();
        let (params, from) = signed_local_credit_params("robot_wallet_001", PLANCKS_PER_CREDIT, 0);
        state.node_address = from;

        let r = req("submit_local_credit", params);
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["accepted"], true);
        assert_eq!(result["to"], "robot_wallet_001");
        assert_eq!(result["tx_type"], "workload_completion");
    }

    #[test]
    fn test_submit_local_credit_rejects_public_submit_transaction() {
        let mut state = make_state();
        let (params, from) = signed_local_credit_params("robot_wallet_001", PLANCKS_PER_CREDIT, 0);
        state.node_address = from;

        let r = req("submit_transaction", params);
        let resp = handle_request(&r, &mut state);
        assert_error(&resp, INVALID_PARAMS);
    }

    #[test]
    fn test_submit_transaction_rejects_productive_work() {
        let mut state = make_state();
        let (params, _) = signed_productive_work_params(PLANCKS_PER_CREDIT, 0);

        let r = req("submit_transaction", params);
        let resp = handle_request(&r, &mut state);
        assert_error(&resp, INVALID_PARAMS);
    }

    #[test]
    fn test_submit_transaction_insufficient_balance() {
        let mut state = make_state();
        let (params, _) = signed_transfer_params("bob", PLANCKS_PER_CREDIT, 0);
        let r = req("submit_transaction", params);
        let resp = handle_request(&r, &mut state);
        assert_error(&resp, INSUFFICIENT_BALANCE);
    }

    #[test]
    fn test_submit_transaction_requires_signature() {
        let mut state = make_state();
        state
            .chain
            .balances
            .insert("alice".to_string(), 100 * PLANCKS_PER_CREDIT);
        state.sync_from_chain();

        let r = req(
            "submit_transaction",
            serde_json::json!({
                "from_address": "alice",
                "to_address": "bob",
                "amount": PLANCKS_PER_CREDIT,
                "tx_type": "transfer",
            }),
        );
        let resp = handle_request(&r, &mut state);
        assert_error(&resp, INVALID_SIGNATURE);
    }

    #[test]
    fn test_submit_transaction_rejects_faucet_claim() {
        let mut state = make_state();
        let r = req(
            "submit_transaction",
            serde_json::json!({
                "from_address": "faucet",
                "to_address": "alice",
                "amount": PLANCKS_PER_CREDIT,
                "tx_type": "faucet_claim",
            }),
        );
        let resp = handle_request(&r, &mut state);
        assert_error(&resp, INVALID_PARAMS);
    }

    #[test]
    fn test_submit_transaction_missing_fields() {
        let mut state = make_state();
        let r = req(
            "submit_transaction",
            serde_json::json!({"from_address": "alice"}),
        );
        let resp = handle_request(&r, &mut state);
        assert_error(&resp, INVALID_PARAMS);
    }

    // ── Staking ─────────────────────────────────────────────────

    #[test]
    fn test_get_staking_info() {
        let mut state = make_state();
        let r = req("get_staking_info", serde_json::json!({"address": "alice"}));
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["is_validator"], false);
        assert_eq!(result["stake_plancks"], 0);
    }

    #[test]
    fn test_get_validators_empty() {
        let mut state = make_state();
        let r = req("get_validators", serde_json::json!({}));
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["count"], 0);
    }

    #[test]
    fn test_get_leaderboard() {
        let mut state = make_state();
        let r = req("get_leaderboard", serde_json::json!({"top_n": 5}));
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["count"], 0);
    }

    // ── Mining / Network ────────────────────────────────────────

    #[test]
    fn test_get_mining_stats() {
        let mut state = make_state();
        let r = req("get_mining_stats", serde_json::json!({}));
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["block_height"], 1);
        assert_eq!(result["block_time_seconds"], BLOCK_INTERVAL_SECS);
        assert!(result["current_reward_cr"].as_f64().unwrap() > 0.0);
        assert_eq!(result["local_effective_tflops"], 5.4);
        assert_eq!(result["local_measured_tflops"], 5.4);
        assert_eq!(result["local_compute_share"], 1.0);
        assert_eq!(result["availability_contributor_count"], 1);
    }

    #[test]
    fn test_get_network_stats() {
        let mut state = make_state();
        let r = req("get_network_stats", serde_json::json!({}));
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["peers"], 5);
        assert_eq!(result["block_interval_secs"], BLOCK_INTERVAL_SECS);
        assert_eq!(result["local_compute_share"], 1.0);
        assert_eq!(result["availability_tflops"], 5.4);
        assert_eq!(result["availability_contributors"][0]["address"], "test_node_001");
    }

    // ── DAO ─────────────────────────────────────────────────────

    #[test]
    fn test_get_treasury() {
        let mut state = make_state();
        let r = req("get_treasury", serde_json::json!({}));
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["treasury_plancks"], 0);
        assert_eq!(result["total_proposals"], 0);
    }

    #[test]
    fn test_get_proposals_empty() {
        let mut state = make_state();
        let r = req("get_proposals", serde_json::json!({}));
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["count"], 0);
    }

    #[test]
    fn test_get_proposal_not_found() {
        let mut state = make_state();
        let r = req("get_proposal", serde_json::json!({"proposal_id": "ghost"}));
        let resp = handle_request(&r, &mut state);
        assert_error(&resp, NOT_FOUND);
    }

    // ── Token ───────────────────────────────────────────────────

    #[test]
    fn test_get_token_not_found() {
        let mut state = make_state();
        let r = req("get_token", serde_json::json!({"token_id": "nonexistent"}));
        let resp = handle_request(&r, &mut state);
        assert_error(&resp, NOT_FOUND);
    }

    #[test]
    fn test_get_token_balance_empty() {
        let mut state = make_state();
        let r = req(
            "get_token_balance",
            serde_json::json!({"token_id": "test", "address": "alice"}),
        );
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["balance"], 0);
    }

    #[test]
    fn test_get_tokens_empty() {
        let mut state = make_state();
        let r = req("get_tokens", serde_json::json!({}));
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["total_tokens"], 0);
    }

    // ── Contract ────────────────────────────────────────────────

    #[test]
    fn test_get_workload_not_found() {
        let mut state = make_state();
        let r = req("get_workload", serde_json::json!({"workload_key": "ghost"}));
        let resp = handle_request(&r, &mut state);
        assert_error(&resp, NOT_FOUND);
    }

    #[test]
    fn test_get_contract_stats() {
        let mut state = make_state();
        let r = req("get_contract_stats", serde_json::json!({}));
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["total_workloads"], 0);
        assert_eq!(result["registered_machines"], 0);
    }

    // ── Integration: full chain with balance ────────────────────

    #[test]
    fn test_rpc_with_funded_chain() {
        use std::collections::BTreeMap;

        let mut state = make_state();
        // Simulate a mined block
        let prev = state.chain.latest_block().clone();
        let coinbase = Transaction::new(
            "SYSTEM",
            "miner_abc",
            Chain::coinbase_reward(1),
            "reward",
            0,
            BTreeMap::new(),
            Some(prev.timestamp + 69.0),
            None,
            1,
        );
        let block = crate::block::Block::new(
            1,
            &prev.hash,
            prev.timestamp + 69.0,
            vec![coinbase],
            "miner_abc",
            BTreeMap::new(),
        );
        state.chain.add_block(block).unwrap();
        state.sync_from_chain();

        // Check balance via RPC
        let r = req("get_balance", serde_json::json!({"address": "miner_abc"}));
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["balance_plancks"], Chain::coinbase_reward(1));
        assert!(result["balance_cr"].as_f64().unwrap() > 0.0);

        // Check chain height
        let r = req("get_chain_height", serde_json::json!({}));
        let resp = handle_request(&r, &mut state);
        let result = assert_success(&resp);
        assert_eq!(result["height"], 2);
    }

    // ── Batch: all methods return something ─────────────────────

    #[test]
    fn test_all_methods_listed() {
        let mut state = make_state();
        for method in METHODS {
            let r = req(method, serde_json::json!({}));
            let resp = handle_request(&r, &mut state);
            // Should not be METHOD_NOT_FOUND
            if let Some(err) = &resp.error {
                assert_ne!(
                    err.code, METHOD_NOT_FOUND,
                    "Method {} returned METHOD_NOT_FOUND",
                    method
                );
            }
        }
    }

    // ── Response serialization ──────────────────────────────────

    #[test]
    fn test_response_serialization() {
        let resp = RpcResponse::success(Value::Number(42.into()), serde_json::json!({"ok": true}));
        let json = serde_json::to_string(&resp).unwrap();
        assert!(json.contains("\"jsonrpc\":\"2.0\""));
        assert!(json.contains("\"ok\":true"));
        assert!(!json.contains("\"error\""));
    }

    #[test]
    fn test_error_response_serialization() {
        let resp = RpcResponse::error(Value::Number(1.into()), INTERNAL_ERROR, "boom");
        let json = serde_json::to_string(&resp).unwrap();
        assert!(json.contains("\"code\":-32603"));
        assert!(json.contains("\"boom\""));
        assert!(!json.contains("\"result\""));
    }
}
