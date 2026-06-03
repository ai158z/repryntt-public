//! Fee-priority mempool for repryntt blockchain.
//!
//! Replaces flat FIFO transaction pool with a fee-aware priority queue.
//! Higher-fee transactions get included first when blocks are tight.
//!
//! Block size limits enforced here — not in the node itself — so the
//! node just asks "give me a block's worth of transactions" and gets
//! the optimal set.
//!
//! Matches Python `fee_mempool.py` behaviour.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::env;
use std::fs;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

use crate::chain::{is_local_system_credit_purpose, is_productive_work_purpose};
use crate::transaction::{PLANCKS_PER_CREDIT, Transaction, VALID_TX_TYPES};

// ── Constants ────────────────────────────────────────────────────────────────

/// 1 MB max block payload.
pub const MAX_BLOCK_BYTES: usize = 1_048_576;

/// Hard cap on tx count per block.
pub const MAX_BLOCK_TXS: usize = 500;

/// Anti-spam floor: 0.00001 CR.
pub const MIN_FEE_PLANCKS: i64 = 1_000;

/// Evict lowest-fee txs above this pool size.
///
/// Dropped from 50_000 → 5_000 (2026-06-01) to keep node RAM bounded on
/// constrained hardware (Jetson 8GB). At 50k slots the pool's headroom
/// could reach hundreds of MB at peak; 5k still covers any realistic burst
/// for a non-mega-scale chain and clamps memory by 10×.
pub const MAX_MEMPOOL_SIZE: usize = 5_000;

/// 24 hours — reject stale txs.
pub const TX_EXPIRY_SECONDS: f64 = 3600.0 * 24.0;

/// Default number of sequential pending txs allowed per sender.
/// Scaled down with the pool size so a single spammer can't fill the pool.
pub const DEFAULT_MAX_PENDING_PER_SENDER: usize = 1_000;

/// Fee-free transaction types (system operations).
pub const FEE_EXEMPT_TYPES: &[&str] = &[
    "reward",
    "genesis",
    "workload_completion",
    "faucet",
    "faucet_claim",
    "stake",
    "stake_withdraw",
];

// ── Mempool Entry ────────────────────────────────────────────────────────────

/// On-disk shape for a persisted mempool entry. We persist only what's
/// necessary to faithfully reconstruct the entry; `size_bytes` and
/// `fee_per_byte` are re-derived on load.
#[derive(Debug, Clone, Serialize, Deserialize)]
struct PersistedEntry {
    tx: Transaction,
    fee_plancks: i64,
    added_at: f64,
}

/// Wraps a transaction with fee metadata for priority ordering.
#[derive(Debug, Clone)]
pub struct MempoolEntry {
    pub tx: Transaction,
    pub fee_plancks: i64,
    pub size_bytes: usize,
    pub fee_per_byte: f64,
    pub added_at: f64,
}

impl MempoolEntry {
    pub fn new(tx: Transaction, fee_plancks: i64, size_bytes: usize) -> Self {
        let fee_per_byte = fee_plancks as f64 / size_bytes.max(1) as f64;
        Self {
            tx,
            fee_plancks,
            size_bytes,
            fee_per_byte,
            added_at: now_f64(),
        }
    }
}

// ── Fee Mempool ──────────────────────────────────────────────────────────────

/// Priority mempool ordered by fee-per-byte.
///
/// Usage:
/// ```ignore
/// let mut pool = Mempool::new();
/// pool.add_transaction(tx, 5000)?;
/// let (txs, total_fees) = pool.select_for_block();
/// pool.remove_confirmed(&tx_hashes);
/// ```
pub struct Mempool {
    /// All entries indexed by tx_hash.
    by_hash: HashMap<String, MempoolEntry>,
    /// Per-sender nonce tracking: address → set of nonces in the pool.
    sender_nonces: HashMap<String, BTreeSet<u64>>,
    /// Config.
    max_block_bytes: usize,
    max_block_txs: usize,
    max_pool_size: usize,
}

impl Mempool {
    pub fn new() -> Self {
        Self {
            by_hash: HashMap::new(),
            sender_nonces: HashMap::new(),
            max_block_bytes: MAX_BLOCK_BYTES,
            max_block_txs: MAX_BLOCK_TXS,
            max_pool_size: MAX_MEMPOOL_SIZE,
        }
    }

    pub fn with_limits(max_block_bytes: usize, max_block_txs: usize, max_pool_size: usize) -> Self {
        Self {
            by_hash: HashMap::new(),
            sender_nonces: HashMap::new(),
            max_block_bytes,
            max_block_txs,
            max_pool_size,
        }
    }

    // ── Add / Remove ─────────────────────────────────────────────────────

    /// Add a transaction to the mempool.
    ///
    /// Returns `Ok(())` on success, `Err(reason)` if rejected.
    pub fn add_transaction(&mut self, tx: Transaction, fee_plancks: i64) -> Result<(), String> {
        // Must have a hash
        if tx.tx_hash.is_empty() {
            return Err("Transaction has no hash".into());
        }

        // Check tx_type is valid
        if !VALID_TX_TYPES.contains(&tx.tx_type.as_str()) {
            return Err(format!("Invalid transaction type: {}", tx.tx_type));
        }

        let exempt = FEE_EXEMPT_TYPES.contains(&tx.tx_type.as_str());

        // Fee floor for non-exempt types
        if !exempt && fee_plancks < MIN_FEE_PLANCKS {
            return Err(format!(
                "Fee {} < minimum {} plancks",
                fee_plancks, MIN_FEE_PLANCKS
            ));
        }

        // Duplicate check
        if self.by_hash.contains_key(&tx.tx_hash) {
            return Err("Transaction already in mempool".into());
        }

        // Estimate serialized size
        let size_bytes = estimate_tx_size(&tx);

        // Track sender nonce
        let sender = tx.from_address.clone();
        let nonce = tx.nonce;

        if sender != "SYSTEM"
            && self
                .sender_nonces
                .get(&sender)
                .map(|nonces| nonces.contains(&nonce))
                .unwrap_or(false)
        {
            return Err("Sender nonce already in mempool".into());
        }

        let entry = MempoolEntry::new(tx, fee_plancks, size_bytes);
        self.by_hash.insert(entry.tx.tx_hash.clone(), entry);

        self.sender_nonces.entry(sender).or_default().insert(nonce);

        // Evict if over capacity
        if self.by_hash.len() > self.max_pool_size {
            self.evict_lowest();
        }

        Ok(())
    }

    /// Add a transaction with basic balance/nonce validation.
    ///
    /// This is the higher-level entry point that checks:
    /// - Sufficient balance for the transfer amount + fee
    /// - Correct nonce (matches expected next nonce)
    /// - Signature validity (for non-system tx types)
    pub fn add_transaction_validated(
        &mut self,
        tx: Transaction,
        fee_plancks: i64,
        balances: &BTreeMap<String, i64>,
        nonces: &BTreeMap<String, u64>,
        stakes: &BTreeMap<String, i64>,
    ) -> Result<(), String> {
        let tx_type = tx.tx_type.as_str();

        if !["transfer", "stake", "stake_withdraw", "workload_completion"].contains(&tx_type) {
            return Err(format!(
                "{} is not accepted through the public mempool",
                tx_type
            ));
        }

        if self.by_hash.contains_key(&tx.tx_hash) {
            return Err("Transaction already in mempool".into());
        }

        let chain_nonce = nonces.get(&tx.from_address).copied().unwrap_or(0);
        if tx.nonce < chain_nonce {
            return Err(format!(
                "Invalid nonce: expected at least {}, got {} (already mined)",
                chain_nonce, tx.nonce
            ));
        }

        let pending_for_sender = self
            .sender_nonces
            .get(&tx.from_address)
            .map(|nonces| nonces.len())
            .unwrap_or(0);
        let max_pending = max_pending_per_sender();
        if pending_for_sender >= max_pending {
            return Err(format!(
                "Sender pending transaction limit reached: {} >= {}",
                pending_for_sender, max_pending
            ));
        }
        if tx.nonce >= chain_nonce.saturating_add(max_pending as u64) {
            return Err(format!(
                "Invalid nonce: {} is too far ahead of chain nonce {} (limit {})",
                tx.nonce, chain_nonce, max_pending
            ));
        }
        if self
            .sender_nonces
            .get(&tx.from_address)
            .map(|nonces| nonces.contains(&tx.nonce))
            .unwrap_or(false)
        {
            return Err("Sender nonce already in mempool".into());
        }

        if tx.signature.is_none() {
            return Err("Missing transaction signature".into());
        }
        if tx.public_key.is_none() {
            return Err("Missing public key".into());
        }
        if !tx.verify_signature() {
            return Err("Invalid transaction signature".into());
        }
        if !tx.verify_address_matches_pubkey() {
            return Err("Address does not match public key".into());
        }

        match tx_type {
            "transfer" | "stake" => {
                let sender_balance = balances.get(&tx.from_address).copied().unwrap_or(0);
                if sender_balance < tx.amount {
                    return Err(format!(
                        "Insufficient balance: {:.8} CR < {:.8} CR",
                        sender_balance as f64 / PLANCKS_PER_CREDIT as f64,
                        tx.amount as f64 / PLANCKS_PER_CREDIT as f64,
                    ));
                }
            }
            "stake_withdraw" => {
                let staked = stakes.get(&tx.from_address).copied().unwrap_or(0);
                if staked < tx.amount {
                    return Err(format!(
                        "Insufficient stake: {:.8} CR < {:.8} CR",
                        staked as f64 / PLANCKS_PER_CREDIT as f64,
                        tx.amount as f64 / PLANCKS_PER_CREDIT as f64,
                    ));
                }
            }
            "workload_completion" => {
                let purpose = tx
                    .metadata
                    .get("purpose")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                if is_productive_work_purpose(purpose) && tx.from_address != tx.to_address {
                    return Err("Productive-work rewards must pay the signing node wallet".into());
                }
                if !is_productive_work_purpose(purpose) && !is_local_system_credit_purpose(purpose)
                {
                    return Err("Workload credit missing supported purpose".into());
                }
            }
            _ => unreachable!(),
        }

        self.add_transaction(tx, fee_plancks)
    }

    /// Remove transactions that were included in a block.
    pub fn remove_confirmed(&mut self, tx_hashes: &[String]) {
        for hash in tx_hashes {
            if let Some(entry) = self.by_hash.remove(hash) {
                if let Some(nonces) = self.sender_nonces.get_mut(&entry.tx.from_address) {
                    nonces.remove(&entry.tx.nonce);
                    if nonces.is_empty() {
                        self.sender_nonces.remove(&entry.tx.from_address);
                    }
                }
            }
        }
    }

    // ── Block Selection ──────────────────────────────────────────────────

    /// Select the optimal set of transactions for the next block.
    ///
    /// Greedy knapsack: take highest fee-per-byte txs until block is full.
    /// Returns `(transactions, total_fee_plancks)`.
    pub fn select_for_block(&self) -> (Vec<Transaction>, i64) {
        let now = now_f64();

        // Sort by fee_per_byte descending, but never invert nonce order for
        // the same sender. Account-ordered selection lets one wallet pipeline
        // many PoPW mint txs without miners picking nonce N+1 before nonce N.
        let mut candidates: Vec<&MempoolEntry> = self.by_hash.values().collect();
        candidates.sort_by(|a, b| {
            if a.tx.from_address == b.tx.from_address {
                return a.tx.nonce.cmp(&b.tx.nonce).then_with(|| {
                    a.added_at
                        .partial_cmp(&b.added_at)
                        .unwrap_or(std::cmp::Ordering::Equal)
                });
            }
            b.fee_per_byte
                .partial_cmp(&a.fee_per_byte)
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        let mut selected = Vec::new();
        let mut total_bytes: usize = 0;
        let mut total_fees: i64 = 0;

        for entry in candidates {
            // Skip expired
            if now - entry.added_at > TX_EXPIRY_SECONDS {
                continue;
            }

            if selected.len() >= self.max_block_txs {
                break;
            }

            // Skip if this tx would exceed block size, but try smaller ones
            if total_bytes + entry.size_bytes > self.max_block_bytes {
                continue;
            }

            selected.push(entry.tx.clone());
            total_bytes += entry.size_bytes;
            total_fees += entry.fee_plancks;
        }

        (selected, total_fees)
    }

    // ── Maintenance ──────────────────────────────────────────────────────

    /// Remove transactions older than `TX_EXPIRY_SECONDS`.
    pub fn purge_expired(&mut self) -> usize {
        let now = now_f64();
        let expired: Vec<String> = self
            .by_hash
            .iter()
            .filter(|(_, e)| now - e.added_at > TX_EXPIRY_SECONDS)
            .map(|(h, _)| h.clone())
            .collect();

        let count = expired.len();
        self.remove_confirmed(&expired);
        count
    }

    // ── Queries ──────────────────────────────────────────────────────────

    /// Number of transactions in the mempool.
    pub fn size(&self) -> usize {
        self.by_hash.len()
    }

    /// Get all entries (for mempool snapshot).
    pub fn entries(&self) -> Vec<&MempoolEntry> {
        self.by_hash.values().collect()
    }

    /// Check if a transaction is in the mempool.
    pub fn contains(&self, tx_hash: &str) -> bool {
        self.by_hash.contains_key(tx_hash)
    }

    /// Get a transaction by its hash.
    pub fn get(&self, tx_hash: &str) -> Option<&Transaction> {
        self.by_hash.get(tx_hash).map(|e| &e.tx)
    }

    /// Get the next expected nonce for a sender, accounting for txs already
    /// in the pool.
    pub fn pending_nonce(&self, address: &str, chain_nonce: u64) -> u64 {
        match self.sender_nonces.get(address) {
            Some(nonces) => {
                // Walk from chain_nonce upward finding the contiguous sequence
                let mut next = chain_nonce;
                while nonces.contains(&next) {
                    next += 1;
                }
                next
            }
            None => chain_nonce,
        }
    }

    /// Persist the current mempool entries to a JSON file.
    ///
    /// Writes atomically: serialize to a sibling `.tmp` file, then rename.
    /// Pending transactions survive node restarts so locally-submitted work
    /// is not lost during operator-initiated reboots.
    pub fn save_to_path(&self, path: &Path) -> std::io::Result<()> {
        let entries: Vec<PersistedEntry> = self
            .by_hash
            .values()
            .map(|e| PersistedEntry {
                tx: e.tx.clone(),
                fee_plancks: e.fee_plancks,
                added_at: e.added_at,
            })
            .collect();
        let body = serde_json::to_vec_pretty(&entries)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                fs::create_dir_all(parent)?;
            }
        }
        let tmp = path.with_extension("tmp");
        fs::write(&tmp, body)?;
        fs::rename(&tmp, path)?;
        Ok(())
    }

    /// Load entries from a JSON file produced by `save_to_path`.
    ///
    /// Returns the number of entries successfully added. Silently skips entries
    /// that fail to re-add (e.g. nonce no longer valid because their tx was
    /// already mined into the chain while the node was offline).
    pub fn load_from_path(&mut self, path: &Path) -> std::io::Result<usize> {
        if !path.exists() {
            return Ok(0);
        }
        let body = fs::read(path)?;
        let entries: Vec<PersistedEntry> = serde_json::from_slice(&body)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
        let mut restored = 0;
        for entry in entries {
            if self.add_transaction(entry.tx, entry.fee_plancks).is_ok() {
                restored += 1;
            }
        }
        Ok(restored)
    }

    /// Get mempool statistics.
    pub fn stats(&self) -> MempoolStats {
        if self.by_hash.is_empty() {
            return MempoolStats::default();
        }

        let mut fees: Vec<f64> = self.by_hash.values().map(|e| e.fee_per_byte).collect();
        fees.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));

        let total_bytes: usize = self.by_hash.values().map(|e| e.size_bytes).sum();

        MempoolStats {
            size: self.by_hash.len(),
            total_bytes,
            min_fee_per_byte: fees.first().copied().unwrap_or(0.0),
            max_fee_per_byte: fees.last().copied().unwrap_or(0.0),
            median_fee_per_byte: fees[fees.len() / 2],
        }
    }

    // ── Internal ─────────────────────────────────────────────────────────

    /// Evict the lowest-fee-per-byte transaction.
    fn evict_lowest(&mut self) {
        let worst_hash = self
            .by_hash
            .iter()
            .min_by(|a, b| {
                a.1.fee_per_byte
                    .partial_cmp(&b.1.fee_per_byte)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .map(|(h, _)| h.clone());

        if let Some(hash) = worst_hash {
            self.remove_confirmed(&[hash]);
        }
    }
}

impl Default for Mempool {
    fn default() -> Self {
        Self::new()
    }
}

// ── Stats ────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Default)]
pub struct MempoolStats {
    pub size: usize,
    pub total_bytes: usize,
    pub min_fee_per_byte: f64,
    pub max_fee_per_byte: f64,
    pub median_fee_per_byte: f64,
}

// ── Fee Estimation ───────────────────────────────────────────────────────────

/// Estimate the fee needed to get into the next block based on current
/// mempool state.
///
/// Returns fee in plancks for a transaction of `tx_size_bytes`.
pub fn estimate_fee(mempool: &Mempool, tx_size_bytes: usize) -> i64 {
    let stats = mempool.stats();

    if stats.size == 0 {
        // Empty mempool — just pay the minimum
        return MIN_FEE_PLANCKS;
    }

    // If mempool is less than half full, use minimum fee
    if stats.size < MAX_BLOCK_TXS / 2 {
        return MIN_FEE_PLANCKS;
    }

    // Use median fee-per-byte as a baseline, bump 20% for priority
    let target_fpb = stats.median_fee_per_byte * 1.2;
    let fee = (target_fpb * tx_size_bytes as f64).ceil() as i64;

    fee.max(MIN_FEE_PLANCKS)
}

// ── Helpers ──────────────────────────────────────────────────────────────────

/// Estimate the serialized size of a transaction in bytes.
pub fn estimate_tx_size(tx: &Transaction) -> usize {
    // Use the canonical dict → JSON serialization to get a realistic size
    let dict = tx.to_dict();
    match serde_json::to_string(&dict) {
        Ok(json) => json.len(),
        Err(_) => 256, // conservative default
    }
}

fn now_f64() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

fn max_pending_per_sender() -> usize {
    env::var("REPRYNTT_MEMPOOL_MAX_PENDING_PER_SENDER")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .filter(|v| *v > 0)
        .unwrap_or(DEFAULT_MAX_PENDING_PER_SENDER)
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::crypto;
    use serde_json::Value;

    /// Helper: create a transfer tx with a given fee in metadata.
    fn make_transfer(from: &str, to: &str, amount: i64, nonce: u64) -> Transaction {
        Transaction::new(
            from,
            to,
            amount,
            "transfer",
            nonce,
            BTreeMap::new(),
            None,
            None,
            2,
        )
    }

    /// Helper: create a reward tx (fee-exempt).
    fn make_reward(to: &str, amount: i64) -> Transaction {
        let mut meta = BTreeMap::new();
        meta.insert("purpose".into(), Value::String("coinbase".into()));
        Transaction::new("SYSTEM", to, amount, "reward", 0, meta, None, None, 1)
    }

    fn make_signed_transfer(to: &str, amount: i64, nonce: u64) -> Transaction {
        let (sk, pk) = crypto::generate_keypair();
        let from = crypto::address_from_pubkey(&pk);
        let mut tx = Transaction::new(
            &from,
            to,
            amount,
            "transfer",
            nonce,
            BTreeMap::new(),
            None,
            Some(pk),
            2,
        );
        tx.sign(&sk);
        tx
    }

    #[test]
    fn test_mempool_persistence_roundtrip() {
        // Use a temp file in /tmp keyed by PID to avoid collisions.
        let path = std::env::temp_dir().join(format!(
            "repryntt_mempool_test_{}_{}.json",
            std::process::id(),
            now_f64() as u64
        ));

        let mut pool = Mempool::new();
        let tx_a = make_reward("alice", 100);
        let tx_b = make_reward("bob", 200);
        pool.add_transaction(tx_a.clone(), 0).unwrap();
        pool.add_transaction(tx_b.clone(), 0).unwrap();
        assert_eq!(pool.size(), 2);

        pool.save_to_path(&path)
            .expect("save_to_path should succeed");

        let mut restored = Mempool::new();
        let n = restored
            .load_from_path(&path)
            .expect("load_from_path should succeed");
        assert_eq!(n, 2);
        assert_eq!(restored.size(), 2);
        assert!(restored.contains(&tx_a.tx_hash));
        assert!(restored.contains(&tx_b.tx_hash));

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn test_mempool_load_missing_file_is_zero() {
        let path = std::env::temp_dir().join(format!(
            "repryntt_mempool_test_missing_{}.json",
            std::process::id()
        ));
        let _ = std::fs::remove_file(&path);
        let mut pool = Mempool::new();
        let n = pool.load_from_path(&path).expect("missing file is ok");
        assert_eq!(n, 0);
    }

    #[test]
    fn test_mempool_add_and_size() {
        let mut pool = Mempool::new();
        let tx = make_transfer("alice", "bob", 100_000_000, 0);
        pool.add_transaction(tx, 5000).unwrap();
        assert_eq!(pool.size(), 1);
    }

    #[test]
    fn test_mempool_reject_duplicate() {
        let mut pool = Mempool::new();
        let tx = make_transfer("alice", "bob", 100_000_000, 0);
        let hash = tx.tx_hash.clone();
        pool.add_transaction(tx.clone(), 5000).unwrap();
        let result = pool.add_transaction(tx, 5000);
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("already in mempool"));
        assert!(pool.contains(&hash));
    }

    #[test]
    fn test_mempool_reject_low_fee() {
        let mut pool = Mempool::new();
        let tx = make_transfer("alice", "bob", 100_000_000, 0);
        let result = pool.add_transaction(tx, 500); // Below MIN_FEE_PLANCKS
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("minimum"));
    }

    #[test]
    fn test_mempool_fee_exempt() {
        let mut pool = Mempool::new();
        let tx = make_reward("miner", 1_000_000_000);
        // Fee-exempt types accepted with 0 fee
        pool.add_transaction(tx, 0).unwrap();
        assert_eq!(pool.size(), 1);
    }

    #[test]
    fn test_mempool_remove_confirmed() {
        let mut pool = Mempool::new();
        let tx1 = make_transfer("alice", "bob", 100_000_000, 0);
        let tx2 = make_transfer("alice", "carol", 50_000_000, 1);
        let h1 = tx1.tx_hash.clone();
        let h2 = tx2.tx_hash.clone();

        pool.add_transaction(tx1, 5000).unwrap();
        pool.add_transaction(tx2, 3000).unwrap();
        assert_eq!(pool.size(), 2);

        pool.remove_confirmed(&[h1.clone()]);
        assert_eq!(pool.size(), 1);
        assert!(!pool.contains(&h1));
        assert!(pool.contains(&h2));
    }

    #[test]
    fn test_select_for_block_priority_order() {
        let mut pool = Mempool::new();

        // Low fee tx
        let tx_low = make_transfer("alice", "bob", 100_000_000, 0);
        pool.add_transaction(tx_low.clone(), 2_000).unwrap();

        // High fee tx
        let tx_high = make_transfer("carol", "dave", 200_000_000, 0);
        pool.add_transaction(tx_high.clone(), 100_000).unwrap();

        // Medium fee tx
        let tx_mid = make_transfer("eve", "frank", 50_000_000, 0);
        pool.add_transaction(tx_mid.clone(), 10_000).unwrap();

        let (selected, total_fees) = pool.select_for_block();
        assert_eq!(selected.len(), 3);
        assert_eq!(total_fees, 112_000);

        // Highest fee should be first
        assert_eq!(selected[0].tx_hash, tx_high.tx_hash);
        // Medium fee second
        assert_eq!(selected[1].tx_hash, tx_mid.tx_hash);
        // Lowest fee last
        assert_eq!(selected[2].tx_hash, tx_low.tx_hash);
    }

    #[test]
    fn test_select_for_block_respects_tx_limit() {
        let mut pool = Mempool::with_limits(MAX_BLOCK_BYTES, 3, MAX_MEMPOOL_SIZE);

        for i in 0..5u64 {
            let tx = make_transfer(&format!("sender_{}", i), "recv", 10_000_000, 0);
            pool.add_transaction(tx, 5_000 + (i as i64 * 1_000))
                .unwrap();
        }

        let (selected, _) = pool.select_for_block();
        assert_eq!(selected.len(), 3); // Capped at max_block_txs
    }

    #[test]
    fn test_eviction_at_capacity() {
        let mut pool = Mempool::with_limits(MAX_BLOCK_BYTES, MAX_BLOCK_TXS, 3);

        // Fill to capacity
        for i in 0..3u64 {
            let tx = make_transfer(&format!("s{}", i), "r", 10_000_000, 0);
            pool.add_transaction(tx, 5_000 + (i as i64 * 1_000))
                .unwrap();
        }
        assert_eq!(pool.size(), 3);

        // Adding one more should evict the lowest-fee tx
        let tx_high = make_transfer("s_new", "r", 10_000_000, 0);
        pool.add_transaction(tx_high, 50_000).unwrap();
        assert_eq!(pool.size(), 3); // Still at max
    }

    #[test]
    fn test_pending_nonce() {
        let mut pool = Mempool::new();

        // Add txs with nonces 0, 1, 2
        for i in 0..3u64 {
            let tx = make_transfer("alice", "bob", 10_000_000, i);
            pool.add_transaction(tx, 5_000).unwrap();
        }

        // Chain nonce is 0, pending goes 0,1,2, so next is 3
        assert_eq!(pool.pending_nonce("alice", 0), 3);

        // For unknown sender, returns chain nonce
        assert_eq!(pool.pending_nonce("unknown", 5), 5);
    }

    #[test]
    fn test_validated_add_allows_sequential_pending_nonces() {
        let mut pool = Mempool::new();
        let (sk, pk) = crypto::generate_keypair();
        let from = crypto::address_from_pubkey(&pk);
        let mut tx0 = Transaction::new(
            &from,
            "bob",
            100_000_000,
            "transfer",
            0,
            BTreeMap::new(),
            None,
            Some(pk.clone()),
            2,
        );
        tx0.sign(&sk);
        let mut tx1 = Transaction::new(
            &from,
            "bob",
            100_000_000,
            "transfer",
            1,
            BTreeMap::new(),
            None,
            Some(pk),
            2,
        );
        tx1.sign(&sk);

        let mut balances = BTreeMap::new();
        balances.insert(from.clone(), 500_000_000i64);
        let nonces: BTreeMap<String, u64> = BTreeMap::new();
        let stakes: BTreeMap<String, i64> = BTreeMap::new();

        pool.add_transaction_validated(tx0, 5_000, &balances, &nonces, &stakes)
            .unwrap();
        let result = pool.add_transaction_validated(tx1, 5_000, &balances, &nonces, &stakes);
        assert!(result.is_ok());
        assert_eq!(pool.pending_nonce(&from, 0), 2);
    }

    #[test]
    fn test_validated_add_allows_future_nonce() {
        let mut pool = Mempool::new();
        let tx = make_signed_transfer("bob", 100_000_000, 2);

        let mut balances = BTreeMap::new();
        balances.insert(tx.from_address.clone(), 500_000_000i64);
        let nonces: BTreeMap<String, u64> = BTreeMap::new();
        let stakes: BTreeMap<String, i64> = BTreeMap::new();

        let result = pool.add_transaction_validated(tx, 5_000, &balances, &nonces, &stakes);
        assert!(result.is_ok());
    }

    #[test]
    fn test_purge_expired() {
        let mut pool = Mempool::new();
        let tx = make_transfer("alice", "bob", 100_000_000, 0);
        pool.add_transaction(tx, 5_000).unwrap();

        // Manually set added_at to long ago
        let hash = pool.by_hash.keys().next().unwrap().clone();
        pool.by_hash.get_mut(&hash).unwrap().added_at = 0.0; // epoch = very expired

        let purged = pool.purge_expired();
        assert_eq!(purged, 1);
        assert_eq!(pool.size(), 0);
    }

    #[test]
    fn test_stats_empty() {
        let pool = Mempool::new();
        let stats = pool.stats();
        assert_eq!(stats.size, 0);
        assert_eq!(stats.total_bytes, 0);
    }

    #[test]
    fn test_stats_with_entries() {
        let mut pool = Mempool::new();
        let tx1 = make_transfer("alice", "bob", 100_000_000, 0);
        let tx2 = make_transfer("carol", "dave", 200_000_000, 0);
        pool.add_transaction(tx1, 5_000).unwrap();
        pool.add_transaction(tx2, 10_000).unwrap();

        let stats = pool.stats();
        assert_eq!(stats.size, 2);
        assert!(stats.total_bytes > 0);
        assert!(stats.min_fee_per_byte > 0.0);
        assert!(stats.max_fee_per_byte >= stats.min_fee_per_byte);
    }

    #[test]
    fn test_fee_estimation_empty() {
        let pool = Mempool::new();
        assert_eq!(estimate_fee(&pool, 256), MIN_FEE_PLANCKS);
    }

    #[test]
    fn test_fee_estimation_low_load() {
        let mut pool = Mempool::new();
        // Add a few txs (well below half of MAX_BLOCK_TXS)
        for i in 0..5u64 {
            let tx = make_transfer(&format!("s{}", i), "r", 10_000_000, 0);
            pool.add_transaction(tx, 5_000).unwrap();
        }
        // Low load → minimum fee
        assert_eq!(estimate_fee(&pool, 256), MIN_FEE_PLANCKS);
    }

    #[test]
    fn test_get_transaction() {
        let mut pool = Mempool::new();
        let tx = make_transfer("alice", "bob", 100_000_000, 0);
        let hash = tx.tx_hash.clone();
        pool.add_transaction(tx, 5_000).unwrap();

        let retrieved = pool.get(&hash).unwrap();
        assert_eq!(retrieved.from_address, "alice");
        assert_eq!(retrieved.to_address, "bob");

        assert!(pool.get("nonexistent").is_none());
    }

    #[test]
    fn test_validated_add_insufficient_balance() {
        let mut pool = Mempool::new();
        let tx = make_signed_transfer("bob", 100_000_000, 0);

        let balances: BTreeMap<String, i64> = BTreeMap::new(); // No balance
        let nonces: BTreeMap<String, u64> = BTreeMap::new();
        let stakes: BTreeMap<String, i64> = BTreeMap::new();

        let result = pool.add_transaction_validated(tx, 5_000, &balances, &nonces, &stakes);
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("Insufficient balance"));
    }

    #[test]
    fn test_validated_add_rejects_old_nonce() {
        let mut pool = Mempool::new();
        let tx = make_signed_transfer("bob", 100_000_000, 5); // nonce 5

        let mut balances = BTreeMap::new();
        balances.insert(tx.from_address.clone(), 500_000_000i64);
        let mut nonces: BTreeMap<String, u64> = BTreeMap::new();
        nonces.insert(tx.from_address.clone(), 6);
        let stakes: BTreeMap<String, i64> = BTreeMap::new();

        let result = pool.add_transaction_validated(tx, 5_000, &balances, &nonces, &stakes);
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("Invalid nonce"));
    }

    #[test]
    fn test_validated_add_rejects_system_type() {
        let mut pool = Mempool::new();
        let tx = make_reward("miner", 1_000_000_000);

        let balances = BTreeMap::new();
        let nonces = BTreeMap::new();
        let stakes = BTreeMap::new();

        let result = pool.add_transaction_validated(tx, 0, &balances, &nonces, &stakes);
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("public mempool"));
    }
}
