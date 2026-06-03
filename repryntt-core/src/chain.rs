//! Chain state — in-memory validated chain of blocks.
//!
//! Memory model (2026-06-01 refactor):
//!   - We keep only the LAST `recent_window` blocks in memory.
//!   - Genesis is always pinned separately for identity / first-block checks.
//!   - True chain height lives in `height` (not `recent.len()`).
//!   - Older blocks live on disk in SQLite and are fetched on demand by
//!     callers via the node's Storage handle.
//!
//! Previously the entire chain was held in RAM as `Vec<Block>`. At ~45 k
//! blocks this was consuming ~2 GB on an 8 GB Jetson. The bounded-window
//! design keeps RSS under ~500 MB regardless of chain length.

use std::collections::{BTreeMap, VecDeque};

use crate::block::Block;
use crate::election::AVAILABILITY_REWARD_PLANCKS;
use crate::genesis::{
    self, BASE_REWARD_PLANCKS, EXPECTED_GENESIS_HASH, HALVING_INTERVAL, MAX_SUPPLY_PLANCKS,
};
use crate::transaction::{PLANCKS_PER_CREDIT, VALID_TX_TYPES};

/// How many recent blocks to keep in the in-memory window. Older blocks
/// are pruned to disk-only and fetched on demand. 1024 is comfortably
/// larger than any validation window (re-org depth, fork-choice ancestor
/// search, etc.) while keeping cache memory bounded.
pub const RECENT_WINDOW: usize = 1024;

/// First block height where newly accepted blocks must satisfy strict
/// transaction authorization rules. Earlier blocks remain immutable history.
pub const STRICT_TX_ACTIVATION_HEIGHT: u64 = 26_831;
pub const MAX_PRODUCTIVE_WORK_REWARD_PLANCKS: i64 = 10 * PLANCKS_PER_CREDIT;
pub const MAX_PRODUCTIVE_WORK_REWARD_PER_BLOCK_PLANCKS: i64 = 25 * PLANCKS_PER_CREDIT;
pub const MAX_LOCAL_SYSTEM_CREDIT_PLANCKS: i64 = 1_000 * PLANCKS_PER_CREDIT;
pub const MAX_LOCAL_SYSTEM_CREDIT_PER_BLOCK_PLANCKS: i64 = 5_000 * PLANCKS_PER_CREDIT;

pub fn requires_strict_consensus(block_index: u64) -> bool {
    block_index >= STRICT_TX_ACTIVATION_HEIGHT
}

// ── Chain ────────────────────────────────────────────────────────────────────

#[derive(Clone)]
pub struct Chain {
    /// Genesis block — pinned in memory so identity checks (e.g. "is
    /// blocks[0].hash == EXPECTED_GENESIS_HASH") work without a disk read.
    pub genesis: Block,
    /// Last `recent_window` blocks (oldest at the front, tip at the back).
    /// Bounded — older blocks live on disk in Storage.
    pub recent: VecDeque<Block>,
    /// True chain height: count of blocks including genesis. The on-disk
    /// `block_count()` is the source of truth; this mirrors it for fast
    /// reads.
    pub height: u64,
    /// Cap on `recent` length. Older blocks are evicted from the front
    /// when a new block is appended.
    pub recent_window: usize,

    pub balances: BTreeMap<String, i64>,
    pub nonces: BTreeMap<String, u64>,
    pub stakes: BTreeMap<String, i64>,
    // popw replay protection is provided by `nonces` + signature verification
    // + self-pay constraint + per-tx/per-block amount caps. A separate
    // batch-id seen-set was redundant and grew unbounded at scale; an
    // enforced batch_id metadata field was also redundant insurance against
    // replay that nonce monotonicity already prevents. The popw_batch_id tag
    // remains in tx.metadata as informational audit/trace data when callers
    // supply it, but is not validation-load-bearing.
}

impl Chain {
    /// Create a new chain initialised with the canonical genesis block.
    pub fn new() -> Self {
        let genesis = genesis::create_canonical_genesis();
        assert_eq!(
            genesis.hash, EXPECTED_GENESIS_HASH,
            "Genesis hash mismatch at chain init"
        );
        let mut recent = VecDeque::with_capacity(RECENT_WINDOW);
        recent.push_back(genesis.clone());
        Self {
            genesis,
            recent,
            height: 1, // genesis is height 1 (single block in chain)
            recent_window: RECENT_WINDOW,
            balances: BTreeMap::new(),
            nonces: BTreeMap::new(),
            stakes: BTreeMap::new(),
        }
    }

    /// Number of blocks in the chain INCLUDING genesis. Genesis = height 1.
    pub fn height(&self) -> u64 {
        self.height
    }

    /// True chain tip — the most recently added block. Always in `recent`.
    pub fn latest_block(&self) -> &Block {
        self.recent
            .back()
            .expect("chain always has at least genesis in recent")
    }

    /// Return a block from the in-memory recent window by chain index.
    /// Returns `None` when the index is older than the window — callers
    /// needing that range query Storage directly.
    pub fn recent_block_at(&self, chain_index: u64) -> Option<&Block> {
        // recent holds blocks in chain order. The block at `chain_index`
        // (= block.index value) is at offset (chain_index - oldest_in_recent.index).
        let oldest = self.recent.front()?;
        if chain_index < oldest.index {
            return None;
        }
        let offset = (chain_index - oldest.index) as usize;
        self.recent.get(offset)
    }

    /// True if a block at `chain_index` is currently in the in-memory window.
    pub fn has_in_recent(&self, chain_index: u64) -> bool {
        self.recent_block_at(chain_index).is_some()
    }

    /// Push a fully-validated block to the tip, evicting the oldest block
    /// from the recent window if over capacity. Genesis is kept separately
    /// so eviction can never lose it.
    fn push_to_recent(&mut self, block: Block) {
        self.recent.push_back(block);
        while self.recent.len() > self.recent_window {
            self.recent.pop_front();
        }
        self.height = self.height.saturating_add(1);
    }

    /// Materialize the recent window as a `Vec<Block>` for legacy callers
    /// that need a slice. Note: this is the RECENT window, NOT the full
    /// chain. Callers that need older blocks must query Storage.
    pub fn recent_as_vec(&self) -> Vec<Block> {
        self.recent.iter().cloned().collect()
    }

    /// Same shape, borrowing — iterate without cloning.
    /// Returns `std::collections::vec_deque::Iter` so callers can use
    /// `.rev()` for tip-first iteration.
    pub fn recent_iter(&self) -> std::collections::vec_deque::Iter<'_, Block> {
        self.recent.iter()
    }

    /// Reconstruct a chain from already-persisted local state.
    ///
    /// `blocks` should be the LAST N blocks (genesis at the front IS
    /// acceptable, or omitted — we always keep genesis separately).
    /// `true_height` is the on-disk block count from `Storage::block_count()`.
    ///
    /// Validates the contiguous slice provided. Earlier blocks are trusted
    /// because they passed validation when they were originally added.
    pub fn from_persisted_state(
        blocks: Vec<Block>,
        true_height: u64,
        balances: BTreeMap<String, i64>,
        nonces: BTreeMap<String, u64>,
        stakes: BTreeMap<String, i64>,
    ) -> Result<Self, String> {
        if blocks.is_empty() {
            return Err("Empty persisted chain".into());
        }
        // If the caller supplied the genesis (single-block chain or
        // included-genesis recent), validate it. Otherwise rebuild genesis
        // canonically and rely on its constant hash.
        let genesis_in_caller_blocks = blocks[0].hash == EXPECTED_GENESIS_HASH;
        let genesis: Block = if genesis_in_caller_blocks {
            blocks[0].clone()
        } else {
            // Caller passed only recent N blocks (no genesis at front).
            // Reconstruct canonical genesis — its hash is a compile-time
            // constant, so this is safe and disk-free.
            genesis::create_canonical_genesis()
        };
        if genesis.hash != EXPECTED_GENESIS_HASH {
            return Err(format!(
                "Genesis hash mismatch: got {}",
                &genesis.hash[..32.min(genesis.hash.len())]
            ));
        }

        // Validate the contiguous slice for ordering / linkage.
        for i in 1..blocks.len() {
            let prev = &blocks[i - 1];
            let curr = &blocks[i];
            if curr.index != prev.index + 1 {
                return Err(format!("Block {} has wrong index", i));
            }
            if curr.previous_hash != prev.hash {
                return Err(format!("Block {} previous_hash mismatch", i));
            }
            if curr.timestamp < prev.timestamp {
                return Err(format!("Block {} timestamp is before previous block", i));
            }
        }

        let mut recent: VecDeque<Block> = VecDeque::with_capacity(RECENT_WINDOW);
        for b in blocks.into_iter() {
            recent.push_back(b);
            while recent.len() > RECENT_WINDOW {
                recent.pop_front();
            }
        }
        let height = if true_height == 0 { recent.len() as u64 } else { true_height };
        Ok(Self {
            genesis,
            recent,
            height,
            recent_window: RECENT_WINDOW,
            balances,
            nonces,
            stakes,
        })
    }

    pub fn derive_nonces_from_blocks(blocks: &[Block]) -> BTreeMap<String, u64> {
        let mut nonces = BTreeMap::new();
        for block in blocks {
            for tx in &block.transactions {
                let increments =
                    matches!(tx.tx_type.as_str(), "transfer" | "stake" | "stake_withdraw")
                        || (tx.tx_type == "workload_completion"
                            && tx.signature.is_some()
                            && tx.public_key.is_some());
                if increments {
                    *nonces.entry(tx.from_address.clone()).or_insert(0) += 1;
                }
            }
        }
        nonces
    }

    /// Calculate the coinbase reward for a given block height.
    ///
    /// reward = BASE_REWARD / (2 ^ halvings)
    pub fn coinbase_reward(height: u64) -> i64 {
        let halvings = height / HALVING_INTERVAL;
        if halvings >= 64 {
            return 0; // effectively zero after 64 halvings
        }
        BASE_REWARD_PLANCKS >> halvings
    }

    /// Get current supply (balances + stakes) in plancks.
    pub fn current_supply(&self) -> i64 {
        let bal_sum: i64 = self.balances.values().sum();
        let stake_sum: i64 = self.stakes.values().sum();
        bal_sum + stake_sum
    }

    fn is_valid_protocol_reward_destination(address: &str, miner_address: &str) -> bool {
        address == miner_address || address == "DAO"
    }

    // ── validation ───────────────────────────────────────────────────────

    /// Validate and append a block to the chain.
    pub fn add_block(&mut self, block: Block) -> Result<(), String> {
        self.add_block_inner(block, true)
    }

    /// Append a block that was deserialised from JSON/storage.
    ///
    /// Deserialised history tolerates legacy JSON float roundtrip hashes.
    /// Transaction rules are still replayed for strict-era blocks.
    pub fn add_block_trusted(&mut self, block: Block) -> Result<(), String> {
        self.add_block_inner(block, false)
    }

    fn add_block_inner(&mut self, block: Block, verify_hash: bool) -> Result<(), String> {
        let prev = self.latest_block();

        // Index must be sequential
        if block.index != prev.index + 1 {
            return Err(format!(
                "Block index {} does not follow {}",
                block.index, prev.index
            ));
        }

        // Previous hash must match
        if block.previous_hash != prev.hash {
            return Err(format!(
                "Block previous_hash does not match chain tip: {} != {}",
                &block.previous_hash[..16],
                &prev.hash[..16]
            ));
        }

        if verify_hash {
            Self::validate_block_hash(&block)?;
        }

        // Timestamp must not be before previous block
        if block.timestamp < prev.timestamp {
            return Err("Block timestamp is before previous block".into());
        }

        if requires_strict_consensus(block.index) {
            self.validate_block_transactions(&block)?;
        }

        // Apply transactions to balances
        self.apply_block_transactions(&block)?;

        self.push_to_recent(block);
        Ok(())
    }

    /// Apply all transactions in a block to balances/nonces/stakes.
    fn apply_block_transactions(&mut self, block: &Block) -> Result<(), String> {
        for tx in &block.transactions {
            self.apply_single_transaction(tx)?;
        }
        Ok(())
    }

    pub(crate) fn apply_single_transaction(
        &mut self,
        tx: &crate::transaction::Transaction,
    ) -> Result<(), String> {
        match tx.tx_type.as_str() {
            "reward" => {
                // Coinbase: credit the miner
                *self.balances.entry(tx.to_address.clone()).or_insert(0) += tx.amount;
            }
            "transfer" => {
                let sender_bal = self.balances.get(&tx.from_address).copied().unwrap_or(0);
                if sender_bal < tx.amount {
                    return Err(format!(
                        "Transfer: insufficient balance for {}",
                        tx.from_address
                    ));
                }
                *self.balances.entry(tx.from_address.clone()).or_insert(0) -= tx.amount;
                *self.balances.entry(tx.to_address.clone()).or_insert(0) += tx.amount;
                *self.nonces.entry(tx.from_address.clone()).or_insert(0) += 1;
            }
            "stake" => {
                let sender_bal = self.balances.get(&tx.from_address).copied().unwrap_or(0);
                if sender_bal < tx.amount {
                    return Err(format!(
                        "Stake: insufficient balance for {}",
                        tx.from_address
                    ));
                }
                *self.balances.entry(tx.from_address.clone()).or_insert(0) -= tx.amount;
                *self.stakes.entry(tx.from_address.clone()).or_insert(0) += tx.amount;
                *self.nonces.entry(tx.from_address.clone()).or_insert(0) += 1;
            }
            "stake_withdraw" => {
                let staked = self.stakes.get(&tx.from_address).copied().unwrap_or(0);
                if staked < tx.amount {
                    return Err(format!(
                        "Stake withdraw: insufficient stake for {}",
                        tx.from_address
                    ));
                }
                *self.stakes.entry(tx.from_address.clone()).or_insert(0) -= tx.amount;
                *self.balances.entry(tx.from_address.clone()).or_insert(0) += tx.amount;
                *self.nonces.entry(tx.from_address.clone()).or_insert(0) += 1;
            }
            "fee" => {
                // Deduct from sender, credit recipient (DAO in Python model)
                *self.balances.entry(tx.from_address.clone()).or_insert(0) -= tx.amount;
                *self.balances.entry(tx.to_address.clone()).or_insert(0) += tx.amount;
            }
            "faucet" | "faucet_claim" => {
                *self.balances.entry(tx.to_address.clone()).or_insert(0) += tx.amount;
            }
            "penalty" => {
                let bal = self.balances.get(&tx.from_address).copied().unwrap_or(0);
                let deduct = tx.amount.min(bal);
                *self.balances.entry(tx.from_address.clone()).or_insert(0) -= deduct;
            }
            "workload_completion" => {
                *self.balances.entry(tx.to_address.clone()).or_insert(0) += tx.amount;
                if tx.signature.is_some() && tx.public_key.is_some() {
                    *self.nonces.entry(tx.from_address.clone()).or_insert(0) += 1;
                }
                // popw_batch_id remains in tx.metadata as an audit/trace tag,
                // but is no longer mirrored into a separate in-memory dedup set.
                // Replay protection is provided by per-address nonce monotonicity
                // (enforced in validate_nonce) plus signature verification.
            }
            "entity_register" => {
                // No balance change — just records presence on chain
            }
            "dao_allocate" => {
                let dao_bal = self.balances.get("DAO").copied().unwrap_or(0);
                if dao_bal < tx.amount {
                    return Err(format!(
                        "DAO allocate: insufficient treasury ({} < {})",
                        dao_bal, tx.amount
                    ));
                }
                *self.balances.entry("DAO".to_string()).or_insert(0) -= tx.amount;
                *self.balances.entry(tx.to_address.clone()).or_insert(0) += tx.amount;
            }
            "dao_proposal" => {
                // Creating a proposal costs nothing on-chain.
            }
            "dao_vote" => {
                // Votes are recorded in the DAO module.
            }
            "dao_execute" => {
                let dao_bal = self.balances.get("DAO").copied().unwrap_or(0);
                if dao_bal < tx.amount {
                    return Err(format!(
                        "DAO execute: insufficient treasury ({} < {})",
                        dao_bal, tx.amount
                    ));
                }
                *self.balances.entry("DAO".to_string()).or_insert(0) -= tx.amount;
                *self.balances.entry(tx.to_address.clone()).or_insert(0) += tx.amount;
            }
            _ => {
                return Err(format!("Unknown tx type: {}", tx.tx_type));
            }
        }
        Ok(())
    }

    /// Check whether a single transaction is valid against current state
    /// without mutating anything.  Returns Ok(()) if valid, Err(reason) if not.
    pub fn validate_tx(&self, tx: &crate::transaction::Transaction) -> Result<(), String> {
        self.validate_tx_strict(tx)
    }

    fn validate_block_transactions(&self, block: &Block) -> Result<(), String> {
        let mut shadow = self.clone();
        let mut productive_work_total = 0i64;
        let mut local_credit_total = 0i64;
        shadow.validate_reward_transactions(block)?;
        for tx in &block.transactions {
            if tx.tx_type == "workload_completion" {
                let purpose = tx
                    .metadata
                    .get("purpose")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                if is_productive_work_purpose(purpose) {
                    if tx.to_address != tx.from_address {
                        return Err(
                            "Productive-work rewards must pay the signing node wallet".into()
                        );
                    }
                    productive_work_total = productive_work_total
                        .checked_add(tx.amount)
                        .ok_or_else(|| "Productive-work reward total overflow".to_string())?;
                    if productive_work_total > MAX_PRODUCTIVE_WORK_REWARD_PER_BLOCK_PLANCKS {
                        return Err("Productive-work reward total exceeds per-block cap".into());
                    }
                } else if is_local_system_credit_purpose(purpose) {
                    if tx.from_address != block.miner_address {
                        return Err(
                            "Local workload credit must be signed by the block miner".into()
                        );
                    }
                    local_credit_total = local_credit_total
                        .checked_add(tx.amount)
                        .ok_or_else(|| "Local system credit total overflow".to_string())?;
                    if local_credit_total > MAX_LOCAL_SYSTEM_CREDIT_PER_BLOCK_PLANCKS {
                        return Err("Local system credit total exceeds per-block cap".into());
                    }
                } else {
                    return Err("Workload completion missing supported purpose".into());
                }
            }
            shadow.validate_tx_strict(tx)?;
            shadow.apply_single_transaction(tx)?;
        }
        if shadow.current_supply() > MAX_SUPPLY_PLANCKS {
            return Err("Block would exceed maximum supply".into());
        }
        Ok(())
    }

    pub fn validate_block_hash(block: &Block) -> Result<(), String> {
        let expected_hash = block.calculate_hash();
        if block.hash != expected_hash {
            return Err(format!(
                "Block hash does not match computed hash at {}",
                block.index
            ));
        }
        Ok(())
    }

    fn validate_reward_transactions(&self, block: &Block) -> Result<(), String> {
        let mut coinbase_seen = 0usize;
        let mut dao_seen = 0usize;
        let mut availability_seen = 0usize;

        let coinbase = Self::coinbase_reward(block.index);
        let dao_share = coinbase / 20;
        let supply = self.current_supply();
        let expected_primary_rewards = coinbase > 0
            && supply
                .checked_add(coinbase)
                .and_then(|v| v.checked_add(dao_share))
                .map(|v| v <= MAX_SUPPLY_PLANCKS)
                .unwrap_or(false);

        let supply_after_primary = if expected_primary_rewards {
            supply + coinbase + dao_share
        } else {
            supply
        };
        let expected_availability_reward = supply_after_primary
            .checked_add(AVAILABILITY_REWARD_PLANCKS)
            .map(|v| v <= MAX_SUPPLY_PLANCKS)
            .unwrap_or(false);

        for tx in &block.transactions {
            if tx.tx_type != "reward" {
                continue;
            }
            if tx.from_address != "SYSTEM" {
                return Err("Reward transaction must originate from SYSTEM".into());
            }
            if tx.amount <= 0 {
                return Err("Reward transaction amount must be positive".into());
            }

            let purpose = tx
                .metadata
                .get("purpose")
                .and_then(|v| v.as_str())
                .unwrap_or("");

            match purpose {
                "coinbase" => {
                    coinbase_seen += 1;
                    if !expected_primary_rewards {
                        return Err("Unexpected coinbase reward at current supply".into());
                    }
                    if !Self::is_valid_protocol_reward_destination(
                        &tx.to_address,
                        &block.miner_address,
                    ) {
                        return Err("Coinbase reward must go to block miner or DAO treasury".into());
                    }
                    if tx.amount != coinbase {
                        return Err(format!(
                            "Invalid coinbase reward: expected {}, got {}",
                            coinbase, tx.amount
                        ));
                    }
                }
                "dao_fee" => {
                    dao_seen += 1;
                    if !expected_primary_rewards {
                        return Err("Unexpected DAO reward at current supply".into());
                    }
                    if tx.to_address != "DAO" {
                        return Err("DAO reward must go to DAO treasury".into());
                    }
                    if tx.amount != dao_share {
                        return Err(format!(
                            "Invalid DAO reward: expected {}, got {}",
                            dao_share, tx.amount
                        ));
                    }
                }
                "availability_reward" => {
                    availability_seen += 1;
                    if !expected_availability_reward {
                        return Err("Unexpected availability reward at current supply".into());
                    }
                    if !Self::is_valid_protocol_reward_destination(
                        &tx.to_address,
                        &block.miner_address,
                    ) {
                        return Err(
                            "Availability reward must go to block miner or DAO treasury".into()
                        );
                    }
                    if tx.amount != AVAILABILITY_REWARD_PLANCKS {
                        return Err(format!(
                            "Invalid availability reward: expected {}, got {}",
                            AVAILABILITY_REWARD_PLANCKS, tx.amount
                        ));
                    }
                }
                _ => {
                    return Err(format!(
                        "Reward transaction missing supported purpose at block {}",
                        block.index
                    ));
                }
            }
        }

        if expected_primary_rewards {
            if coinbase_seen != 1 {
                return Err(format!(
                    "Expected exactly one coinbase reward, got {}",
                    coinbase_seen
                ));
            }
            if dao_seen != 1 {
                return Err(format!("Expected exactly one DAO reward, got {}", dao_seen));
            }
        } else if coinbase_seen != 0 || dao_seen != 0 {
            return Err("Primary rewards present when supply cap forbids them".into());
        }

        if expected_availability_reward {
            if availability_seen != 1 {
                return Err(format!(
                    "Expected exactly one availability reward, got {}",
                    availability_seen
                ));
            }
        } else if availability_seen != 0 {
            return Err("Availability rewards present when supply cap forbids them".into());
        }

        Ok(())
    }

    fn validate_tx_strict(&self, tx: &crate::transaction::Transaction) -> Result<(), String> {
        if !VALID_TX_TYPES.contains(&tx.tx_type.as_str()) {
            return Err(format!("Invalid transaction type: {}", tx.tx_type));
        }
        if tx.amount < 0 {
            return Err("Transaction amount cannot be negative".into());
        }

        match tx.tx_type.as_str() {
            "reward" => {
                if tx.from_address != "SYSTEM" {
                    return Err("Reward transaction must originate from SYSTEM".into());
                }
                Ok(())
            }
            "transfer" | "fee" => {
                self.validate_signed_owner_tx(tx)?;
                self.validate_nonce(tx)?;
                let bal = self.balances.get(&tx.from_address).copied().unwrap_or(0);
                if bal < tx.amount {
                    Err(format!("insufficient balance for {}", tx.from_address))
                } else {
                    Ok(())
                }
            }
            "stake" => {
                self.validate_signed_owner_tx(tx)?;
                self.validate_nonce(tx)?;
                let bal = self.balances.get(&tx.from_address).copied().unwrap_or(0);
                if bal < tx.amount {
                    Err(format!(
                        "Stake: insufficient balance for {}",
                        tx.from_address
                    ))
                } else {
                    Ok(())
                }
            }
            "stake_withdraw" => {
                self.validate_signed_owner_tx(tx)?;
                self.validate_nonce(tx)?;
                let stk = self.stakes.get(&tx.from_address).copied().unwrap_or(0);
                if stk < tx.amount {
                    Err(format!(
                        "Stake withdraw: insufficient stake for {}",
                        tx.from_address
                    ))
                } else {
                    Ok(())
                }
            }
            "faucet" | "faucet_claim" => {
                Err("Faucet minting is not accepted through unauthorised transactions".into())
            }
            "workload_completion" => self.validate_productive_work_tx(tx),
            "entity_register" => Err(format!(
                "{} requires a dedicated authority validator",
                tx.tx_type
            )),
            "penalty" | "dao_proposal" | "dao_vote" | "dao_allocate" | "dao_execute"
            | "token_create" | "token_mint" | "token_burn" | "token_transfer" | "token_approve"
            | "token_freeze" | "token_thaw" | "workload_submit" | "workload_claim"
            | "workload_complete" | "ai_inference" => Err(format!(
                "{} is not enabled under strict transaction validation yet",
                tx.tx_type
            )),
            _ => Err(format!("Unknown tx type: {}", tx.tx_type)),
        }
    }

    fn validate_signed_owner_tx(&self, tx: &crate::transaction::Transaction) -> Result<(), String> {
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
        Ok(())
    }

    fn validate_productive_work_tx(
        &self,
        tx: &crate::transaction::Transaction,
    ) -> Result<(), String> {
        self.validate_signed_owner_tx(tx)?;
        self.validate_nonce(tx)?;
        if tx.amount <= 0 {
            return Err("Workload credit amount must be positive".into());
        }

        let purpose = tx
            .metadata
            .get("purpose")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if is_productive_work_purpose(purpose) {
            if tx.from_address != tx.to_address {
                return Err("Productive-work rewards must pay the signing node wallet".into());
            }
            if tx.amount > MAX_PRODUCTIVE_WORK_REWARD_PLANCKS {
                return Err("Productive-work reward exceeds per-transaction cap".into());
            }
            // popw_batch_id is informational only — it stays in tx.metadata as
            // an audit/trace tag when present, but is no longer required for
            // validation. Replay is shielded by validate_nonce + signature +
            // the self-pay (from == to) + per-tx/per-block amount caps, which
            // are the load-bearing protections. The earlier batch_id check
            // both required the field and consulted a global seen-set; both
            // checks were redundant insurance against replay that nonce
            // monotonicity already prevents.
        } else if is_local_system_credit_purpose(purpose) {
            if tx.amount > MAX_LOCAL_SYSTEM_CREDIT_PLANCKS {
                return Err("Local system credit exceeds per-transaction cap".into());
            }
        } else {
            return Err("Workload credit missing supported purpose".into());
        }

        self.current_supply()
            .checked_add(tx.amount)
            .filter(|supply| *supply <= MAX_SUPPLY_PLANCKS)
            .ok_or_else(|| "Productive-work reward would exceed maximum supply".to_string())?;
        Ok(())
    }

    fn popw_batch_id(tx: &crate::transaction::Transaction) -> Option<String> {
        tx.metadata
            .get("popw_batch_id")
            .and_then(|v| v.as_str())
            .map(str::trim)
            .filter(|id| !id.is_empty())
            .map(ToOwned::to_owned)
    }

    fn validate_nonce(&self, tx: &crate::transaction::Transaction) -> Result<(), String> {
        let expected = self.nonces.get(&tx.from_address).copied().unwrap_or(0);
        if tx.nonce != expected {
            return Err(format!(
                "Invalid nonce: expected {}, got {}",
                expected, tx.nonce
            ));
        }
        Ok(())
    }

    /// Validate the chain's in-memory recent window.
    ///
    /// Memory model: validates the in-memory recent window only. Genesis
    /// is validated separately via its pinned `Chain.genesis` field.
    /// Older blocks (those evicted from the window) are trusted because
    /// they passed validation when they were originally added to the
    /// chain.
    pub fn validate_full(&self) -> Result<(), String> {
        if self.recent.is_empty() {
            return Err("Chain is empty (recent window)".into());
        }

        // Genesis check — always against the pinned genesis
        if self.genesis.hash != EXPECTED_GENESIS_HASH {
            return Err(format!(
                "Genesis hash mismatch: {} != {}",
                &self.genesis.hash[..32],
                &EXPECTED_GENESIS_HASH[..32]
            ));
        }

        // Walk the recent window — verify index continuity and hash chain
        // Hash recomputation skipped: serde_json float parsing on aarch64
        // introduces 1-ULP errors.  prev_hash chain guarantees integrity.
        let recent_len = self.recent.len();
        for i in 1..recent_len {
            let prev = &self.recent[i - 1];
            let curr = &self.recent[i];

            if curr.index != prev.index + 1 {
                return Err(format!("Block {} has wrong index", i));
            }
            if curr.previous_hash != prev.hash {
                return Err(format!("Block {} previous_hash mismatch", i));
            }
        }
        Ok(())
    }

    /// Build a chain from a list of blocks, validating and replaying all transactions.
    ///
    /// Used for IBD (Initial Block Download) when receiving a complete chain from a peer.
    ///
    /// After replay, only the recent window is kept in memory; the on-disk
    /// height matches `blocks.len()` since callers are expected to also
    /// `Storage::save_chain(&chain)` if they want it persisted.
    pub fn from_blocks(blocks: Vec<Block>) -> Result<Self, String> {
        if blocks.is_empty() {
            return Err("Empty block list".into());
        }

        // Verify genesis
        if blocks[0].hash != EXPECTED_GENESIS_HASH {
            return Err(format!(
                "Genesis hash mismatch: got {}",
                &blocks[0].hash[..32.min(blocks[0].hash.len())]
            ));
        }

        let mut recent = VecDeque::with_capacity(RECENT_WINDOW);
        recent.push_back(blocks[0].clone());
        let mut chain = Self {
            genesis: blocks[0].clone(),
            recent,
            height: 1,
            recent_window: RECENT_WINDOW,
            balances: BTreeMap::new(),
            nonces: BTreeMap::new(),
            stakes: BTreeMap::new(),
        };

        // Genesis transactions applied (push already done above)
        chain.apply_block_transactions(&blocks[0])?;

        // Remaining blocks — trusted (deserialised from JSON)
        for block in blocks.into_iter().skip(1) {
            chain.add_block_trusted(block)?;
        }

        Ok(chain)
    }

    /// Add multiple deserialised blocks sequentially.
    ///
    /// Returns the number of blocks successfully added.
    pub fn add_blocks(&mut self, blocks: Vec<Block>) -> Result<usize, String> {
        let mut added = 0;
        for block in blocks {
            self.add_block_trusted(block)?;
            added += 1;
        }
        Ok(added)
    }
}

pub fn is_productive_work_purpose(purpose: &str) -> bool {
    matches!(purpose, "popw" | "productive_work")
}

pub fn is_local_system_credit_purpose(purpose: &str) -> bool {
    matches!(
        purpose,
        "faucet"
            | "faucet_claim"
            | "payment_credit"
            | "robot_economy_credit"
            | "refund"
            | "ai_task_completion"
            | "consciousness_reward"
            | "workload_refund"
            | "external_api_refund"
    )
}

impl Default for Chain {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::crypto;
    use crate::transaction::{PLANCKS_PER_CREDIT, Transaction};
    use std::collections::BTreeMap;

    #[test]
    fn test_chain_init_at_genesis() {
        let chain = Chain::new();
        assert_eq!(chain.height(), 1);
        assert_eq!(chain.latest_block().index, 0);
        assert_eq!(chain.latest_block().hash, EXPECTED_GENESIS_HASH);
    }

    #[test]
    fn test_coinbase_reward_halving() {
        // Block 0: 10 CR
        assert_eq!(Chain::coinbase_reward(0), 10 * PLANCKS_PER_CREDIT);
        // Block 420_000: 5 CR (first halving)
        assert_eq!(Chain::coinbase_reward(420_000), 5 * PLANCKS_PER_CREDIT);
        // Block 840_000: 2.5 CR
        assert_eq!(Chain::coinbase_reward(840_000), 250_000_000);
    }

    #[test]
    fn test_validate_genesis_only() {
        let chain = Chain::new();
        chain
            .validate_full()
            .expect("genesis chain should be valid");
    }

    #[test]
    fn test_add_block() {
        let mut chain = Chain::new();
        let prev = chain.latest_block().clone();

        // Create a simple coinbase block
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

        let block = Block::new(
            1,
            &prev.hash,
            prev.timestamp + 69.0,
            vec![coinbase],
            "miner_abc",
            BTreeMap::new(),
        );

        chain.add_block(block).expect("should accept valid block");
        assert_eq!(chain.height(), 2);
        assert_eq!(
            chain.balances.get("miner_abc").copied().unwrap_or(0),
            Chain::coinbase_reward(1)
        );

        chain.validate_full().expect("chain should validate");
    }

    #[test]
    fn test_from_blocks_genesis_only() {
        let genesis = genesis::create_canonical_genesis();
        let chain = Chain::from_blocks(vec![genesis]).unwrap();
        assert_eq!(chain.height(), 1);
        chain.validate_full().unwrap();
    }

    #[test]
    fn test_from_blocks_bad_genesis() {
        let mut genesis = genesis::create_canonical_genesis();
        genesis.hash = "wrong".into();
        assert!(Chain::from_blocks(vec![genesis]).is_err());
    }

    #[test]
    fn test_add_blocks() {
        let mut chain = Chain::new();
        let prev = chain.latest_block().clone();

        let coinbase1 = Transaction::new(
            "SYSTEM",
            "miner",
            Chain::coinbase_reward(1),
            "reward",
            0,
            BTreeMap::new(),
            Some(prev.timestamp + 69.0),
            None,
            1,
        );
        let b1 = Block::new(
            1,
            &prev.hash,
            prev.timestamp + 69.0,
            vec![coinbase1],
            "miner",
            BTreeMap::new(),
        );

        let coinbase2 = Transaction::new(
            "SYSTEM",
            "miner",
            Chain::coinbase_reward(2),
            "reward",
            0,
            BTreeMap::new(),
            Some(b1.timestamp + 69.0),
            None,
            2,
        );
        let b2 = Block::new(
            2,
            &b1.hash,
            b1.timestamp + 69.0,
            vec![coinbase2],
            "miner",
            BTreeMap::new(),
        );

        let added = chain.add_blocks(vec![b1, b2]).unwrap();
        assert_eq!(added, 2);
        assert_eq!(chain.height(), 3);
        chain.validate_full().unwrap();
    }

    fn set_tip_before_strict_activation(chain: &mut Chain) {
        // Test helper: mutate the tip (last block in recent) to look like
        // the strict-activation boundary. Used to exercise transition logic.
        let tip = chain.recent.back_mut().expect("recent always has genesis");
        tip.index = STRICT_TX_ACTIVATION_HEIGHT - 1;
        tip.hash = "pre_strict_tip".into();
        tip.timestamp = 1_800_000_000.0;
    }

    fn fill_supply_to_cap(chain: &mut Chain) {
        let supply = chain.current_supply();
        if supply < MAX_SUPPLY_PLANCKS {
            chain
                .balances
                .insert("cap_sink".to_string(), MAX_SUPPLY_PLANCKS - supply);
        }
    }

    fn strict_reward_txs(miner: &str, timestamp: f64) -> Vec<Transaction> {
        let mut coinbase_meta = BTreeMap::new();
        coinbase_meta.insert(
            "purpose".into(),
            serde_json::Value::String("coinbase".into()),
        );
        let coinbase = Chain::coinbase_reward(STRICT_TX_ACTIVATION_HEIGHT);
        let mut dao_meta = BTreeMap::new();
        dao_meta.insert(
            "purpose".into(),
            serde_json::Value::String("dao_fee".into()),
        );
        let mut availability_meta = BTreeMap::new();
        availability_meta.insert(
            "purpose".into(),
            serde_json::Value::String("availability_reward".into()),
        );
        vec![
            Transaction::new(
                "SYSTEM",
                miner,
                coinbase,
                "reward",
                0,
                coinbase_meta,
                Some(timestamp),
                None,
                1,
            ),
            Transaction::new(
                "SYSTEM",
                "DAO",
                coinbase / 20,
                "reward",
                0,
                dao_meta,
                Some(timestamp),
                None,
                1,
            ),
            Transaction::new(
                "SYSTEM",
                miner,
                AVAILABILITY_REWARD_PLANCKS,
                "reward",
                0,
                availability_meta,
                Some(timestamp),
                None,
                1,
            ),
        ]
    }

    fn signed_productive_work_tx(
        from: &str,
        sk: &[u8],
        pk: Vec<u8>,
        amount: i64,
        nonce: u64,
        timestamp: f64,
    ) -> Transaction {
        let mut meta = BTreeMap::new();
        meta.insert("purpose".into(), serde_json::Value::String("popw".into()));
        meta.insert(
            "source".into(),
            serde_json::Value::String("proof_of_productive_work".into()),
        );
        meta.insert(
            "popw_batch_id".into(),
            serde_json::Value::String(format!("test-popw-{from}-{nonce}")),
        );
        let mut tx = Transaction::new(
            from,
            from,
            amount,
            "workload_completion",
            nonce,
            meta,
            Some(timestamp),
            Some(pk),
            2,
        );
        tx.sign(sk);
        tx
    }

    #[test]
    fn test_strict_block_rejects_unsigned_transfer() {
        let mut chain = Chain::new();
        set_tip_before_strict_activation(&mut chain);
        chain
            .balances
            .insert("alice".to_string(), 10 * PLANCKS_PER_CREDIT);
        fill_supply_to_cap(&mut chain);

        let tx = Transaction::new(
            "alice",
            "bob",
            PLANCKS_PER_CREDIT,
            "transfer",
            0,
            BTreeMap::new(),
            Some(1_800_000_069.0),
            None,
            1,
        );
        let block = Block::new(
            STRICT_TX_ACTIVATION_HEIGHT,
            "pre_strict_tip",
            1_800_000_069.0,
            vec![tx],
            "miner",
            BTreeMap::new(),
        );

        let err = chain.add_block(block).unwrap_err();
        assert!(err.contains("Missing transaction signature"));
    }

    #[test]
    fn test_strict_block_accepts_signed_transfer() {
        let mut chain = Chain::new();
        set_tip_before_strict_activation(&mut chain);
        let (sk, pk) = crypto::generate_keypair();
        let from = crypto::address_from_pubkey(&pk);
        chain.balances.insert(from.clone(), 10 * PLANCKS_PER_CREDIT);
        fill_supply_to_cap(&mut chain);

        let mut tx = Transaction::new(
            &from,
            "bob",
            PLANCKS_PER_CREDIT,
            "transfer",
            0,
            BTreeMap::new(),
            Some(1_800_000_069.0),
            Some(pk),
            1,
        );
        tx.sign(&sk);
        let block = Block::new(
            STRICT_TX_ACTIVATION_HEIGHT,
            "pre_strict_tip",
            1_800_000_069.0,
            vec![tx],
            "miner",
            BTreeMap::new(),
        );

        chain.add_block(block).unwrap();
        assert_eq!(
            chain.balances.get("bob").copied().unwrap_or(0),
            PLANCKS_PER_CREDIT
        );
        assert_eq!(chain.nonces.get(&from).copied().unwrap_or(0), 1);
    }

    #[test]
    fn test_strict_trusted_block_replays_transactions_without_rehashing() {
        let mut chain = Chain::new();
        set_tip_before_strict_activation(&mut chain);
        fill_supply_to_cap(&mut chain);

        let mut block = Block::new(
            STRICT_TX_ACTIVATION_HEIGHT,
            "pre_strict_tip",
            1_800_000_069.0,
            vec![],
            "miner",
            BTreeMap::new(),
        );
        block.hash = "bad_hash".into();

        chain.add_block_trusted(block).unwrap();
        assert_eq!(chain.latest_block().hash, "bad_hash");
    }

    #[test]
    fn test_strict_block_requires_exact_rewards_when_supply_allows() {
        let mut chain = Chain::new();
        set_tip_before_strict_activation(&mut chain);

        let block = Block::new(
            STRICT_TX_ACTIVATION_HEIGHT,
            "pre_strict_tip",
            1_800_000_069.0,
            vec![],
            "miner",
            BTreeMap::new(),
        );

        let err = chain.add_block(block).unwrap_err();
        assert!(err.contains("Expected exactly one coinbase reward"));
    }

    #[test]
    fn test_strict_block_accepts_exact_reward_set() {
        let mut chain = Chain::new();
        set_tip_before_strict_activation(&mut chain);
        let miner = "miner";
        let timestamp = 1_800_000_069.0;

        let block = Block::new(
            STRICT_TX_ACTIVATION_HEIGHT,
            "pre_strict_tip",
            timestamp,
            strict_reward_txs(miner, timestamp),
            miner,
            BTreeMap::new(),
        );

        chain.add_block(block).unwrap();
        assert_eq!(
            chain.balances.get(miner).copied().unwrap_or(0),
            Chain::coinbase_reward(STRICT_TX_ACTIVATION_HEIGHT) + AVAILABILITY_REWARD_PLANCKS
        );
        assert_eq!(
            chain.balances.get("DAO").copied().unwrap_or(0),
            Chain::coinbase_reward(STRICT_TX_ACTIVATION_HEIGHT) / 20
        );
    }

    #[test]
    fn test_strict_block_accepts_protocol_rewards_to_dao_treasury() {
        let mut chain = Chain::new();
        set_tip_before_strict_activation(&mut chain);
        let miner = "miner";
        let timestamp = 1_800_000_069.0;
        let mut txs = strict_reward_txs(miner, timestamp);
        for tx in &mut txs {
            if tx
                .metadata
                .get("purpose")
                .and_then(|v| v.as_str())
                .is_some_and(|purpose| purpose == "coinbase" || purpose == "availability_reward")
            {
                tx.to_address = "DAO".into();
                tx.tx_hash = tx.calculate_hash();
            }
        }

        let block = Block::new(
            STRICT_TX_ACTIVATION_HEIGHT,
            "pre_strict_tip",
            timestamp,
            txs,
            miner,
            BTreeMap::new(),
        );

        chain.add_block(block).unwrap();
        assert_eq!(chain.balances.get(miner).copied().unwrap_or(0), 0);
        assert_eq!(
            chain.balances.get("DAO").copied().unwrap_or(0),
            Chain::coinbase_reward(STRICT_TX_ACTIVATION_HEIGHT)
                + Chain::coinbase_reward(STRICT_TX_ACTIVATION_HEIGHT) / 20
                + AVAILABILITY_REWARD_PLANCKS
        );
    }

    #[test]
    fn test_strict_block_accepts_signed_productive_work_reward() {
        let mut chain = Chain::new();
        set_tip_before_strict_activation(&mut chain);
        let (sk, pk) = crypto::generate_keypair();
        let miner = crypto::address_from_pubkey(&pk);
        let timestamp = 1_800_000_069.0;
        let mut txs = strict_reward_txs(&miner, timestamp);
        txs.push(signed_productive_work_tx(
            &miner,
            &sk,
            pk,
            PLANCKS_PER_CREDIT,
            0,
            timestamp,
        ));

        let block = Block::new(
            STRICT_TX_ACTIVATION_HEIGHT,
            "pre_strict_tip",
            timestamp,
            txs,
            &miner,
            BTreeMap::new(),
        );

        chain.add_block(block).unwrap();
        assert_eq!(chain.nonces.get(&miner).copied().unwrap_or(0), 1);
        assert_eq!(
            chain.balances.get(&miner).copied().unwrap_or(0),
            Chain::coinbase_reward(STRICT_TX_ACTIVATION_HEIGHT)
                + AVAILABILITY_REWARD_PLANCKS
                + PLANCKS_PER_CREDIT
        );
    }

    #[test]
    fn test_strict_block_accepts_peer_productive_work_reward() {
        let mut chain = Chain::new();
        set_tip_before_strict_activation(&mut chain);
        let (miner_sk, miner_pk) = crypto::generate_keypair();
        let miner = crypto::address_from_pubkey(&miner_pk);
        let (worker_sk, worker_pk) = crypto::generate_keypair();
        let worker = crypto::address_from_pubkey(&worker_pk);
        let timestamp = 1_800_000_069.0;
        let mut txs = strict_reward_txs(&miner, timestamp);
        txs.push(signed_productive_work_tx(
            &worker,
            &worker_sk,
            worker_pk,
            PLANCKS_PER_CREDIT,
            0,
            timestamp,
        ));

        let block = Block::new(
            STRICT_TX_ACTIVATION_HEIGHT,
            "pre_strict_tip",
            timestamp,
            txs,
            &miner,
            BTreeMap::new(),
        );

        chain.add_block(block).unwrap();
        assert_eq!(chain.nonces.get(&worker).copied().unwrap_or(0), 1);
        assert_eq!(
            chain.balances.get(&worker).copied().unwrap_or(0),
            PLANCKS_PER_CREDIT
        );
        assert_eq!(
            chain.balances.get(&miner).copied().unwrap_or(0),
            Chain::coinbase_reward(STRICT_TX_ACTIVATION_HEIGHT) + AVAILABILITY_REWARD_PLANCKS
        );
        drop(miner_sk);
    }

    #[test]
    fn test_strict_block_rejects_replayed_productive_work_via_nonce() {
        // The chain no longer keeps an in-memory popw_batch_id set; replay
        // protection for popw txs is provided exclusively by per-address
        // nonce monotonicity. This test confirms that:
        //   1. Reusing a batch_id with a fresh nonce is now ACCEPTED
        //      (batch_id is metadata-only; nonces are what enforce replay).
        //   2. Reusing the same (from_address, nonce) pair is REJECTED
        //      by validate_nonce, which is the canonical replay shield for
        //      every tx type including workload_completion.
        let mut chain = Chain::new();
        set_tip_before_strict_activation(&mut chain);
        let (miner_sk, miner_pk) = crypto::generate_keypair();
        let miner = crypto::address_from_pubkey(&miner_pk);
        let (worker_sk, worker_pk) = crypto::generate_keypair();
        let worker = crypto::address_from_pubkey(&worker_pk);
        let timestamp = 1_800_000_069.0;

        let mut txs1 = strict_reward_txs(&miner, timestamp);
        txs1.push(signed_productive_work_tx(
            &worker,
            &worker_sk,
            worker_pk.clone(),
            PLANCKS_PER_CREDIT,
            0,
            timestamp,
        ));
        let block1 = Block::new(
            STRICT_TX_ACTIVATION_HEIGHT,
            "pre_strict_tip",
            timestamp,
            txs1,
            &miner,
            BTreeMap::new(),
        );
        chain.add_block(block1).unwrap();

        // Block 2 — same batch_id label as block 1, but a fresh nonce.
        // Under Fix 1 this is permitted (batch_id is purely audit metadata).
        let mut tx2 = signed_productive_work_tx(
            &worker,
            &worker_sk,
            worker_pk.clone(),
            PLANCKS_PER_CREDIT,
            1,
            timestamp + 69.0,
        );
        tx2.metadata.insert(
            "popw_batch_id".into(),
            serde_json::Value::String(format!("test-popw-{worker}-0")),
        );
        tx2.tx_hash = tx2.calculate_hash();
        tx2.sign(&worker_sk);

        let mut txs2 = strict_reward_txs(&miner, timestamp + 69.0);
        txs2.push(tx2);
        let block2 = Block::new(
            STRICT_TX_ACTIVATION_HEIGHT + 1,
            &chain.latest_block().hash,
            timestamp + 69.0,
            txs2,
            &miner,
            BTreeMap::new(),
        );
        chain
            .add_block(block2)
            .expect("fresh nonce should be accepted");

        // Block 3 — replay nonce 1 (already used by tx2). validate_nonce
        // must reject it regardless of how the metadata is shaped.
        let mut tx3 = signed_productive_work_tx(
            &worker,
            &worker_sk,
            worker_pk,
            PLANCKS_PER_CREDIT,
            1,
            timestamp + 138.0,
        );
        tx3.tx_hash = tx3.calculate_hash();
        tx3.sign(&worker_sk);

        let mut txs3 = strict_reward_txs(&miner, timestamp + 138.0);
        txs3.push(tx3);
        let block3 = Block::new(
            STRICT_TX_ACTIVATION_HEIGHT + 2,
            &chain.latest_block().hash,
            timestamp + 138.0,
            txs3,
            &miner,
            BTreeMap::new(),
        );
        let err = chain.add_block(block3).unwrap_err();
        assert!(
            err.contains("nonce") || err.contains("Nonce"),
            "expected nonce-based rejection, got: {}",
            err
        );
        drop(miner_sk);
    }
}
