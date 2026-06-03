//! Canonical genesis block for the repryntt mainnet.
//!
//! The genesis block MUST hash to `EXPECTED_GENESIS_HASH`.  If it does not,
//! the Rust and Python implementations are incompatible and the chain cannot
//! synchronise.

use serde_json::Value;
use std::collections::BTreeMap;

use crate::block::Block;
use crate::transaction::Transaction;

// ── constants ────────────────────────────────────────────────────────────────

pub const NETWORK_MAGIC: &[u8; 4] = b"RPNT";
pub const NETWORK_MAGIC_HEX: &str = "52504e54";
pub const PROTOCOL_VERSION: u32 = 4;

pub const GENESIS_TIMESTAMP: f64 = 1_743_379_200.0; // 2025-03-31 00:00:00 UTC
pub const GENESIS_MINER: &str = "SYSTEM";
/// 128 hex zeros  (SHA3-512 width / 4 bits per hex = 128 hex chars)
pub const GENESIS_PREV_HASH: &str = "00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000";

pub const GENESIS_HEADLINE: &str = "AP 04/Apr/2026 Autonomous AI systems begin earning their own currency \
     through compute contribution — the robots are building their own economy";
pub const GENESIS_CREATOR: &str = "a1a4090aced69d411b6e62bf49944f295c85ed88";

pub const EXPECTED_GENESIS_HASH: &str = "84adf3566b7ede5500dbc0cd11f5096a2e12230b23b6833a0118330c04b5270f\
     17dab17e1e6fb8b41f725ac0ba895af23e9658af63d79a1cc76c2413bf13c1ef";

// ── supply constants ─────────────────────────────────────────────────────────

/// Maximum supply in Plancks  (21 000 000 × 10^8).
pub const MAX_SUPPLY_PLANCKS: i64 = 21_000_000 * 100_000_000;
/// Base coinbase reward per block: 10 CR in Plancks.
pub const BASE_REWARD_PLANCKS: i64 = 10 * 100_000_000;
/// Number of blocks between each halving.
pub const HALVING_INTERVAL: u64 = 420_000;
/// Target block interval in seconds.
pub const BLOCK_INTERVAL_SECS: u64 = 69;

// ── genesis creation ─────────────────────────────────────────────────────────

/// Build the canonical genesis block.
pub fn create_canonical_genesis() -> Block {
    let mut meta: BTreeMap<String, Value> = BTreeMap::new();
    meta.insert("block".into(), Value::String("genesis".into()));
    meta.insert("network".into(), Value::String("repryntt-mainnet".into()));
    meta.insert("headline".into(), Value::String(GENESIS_HEADLINE.into()));
    meta.insert("creator".into(), Value::String(GENESIS_CREATOR.into()));
    meta.insert("magic".into(), Value::String(NETWORK_MAGIC_HEX.into()));

    let genesis_tx = Transaction::new(
        "SYSTEM",
        GENESIS_CREATOR,
        0, // amount
        "reward",
        0, // nonce
        meta,
        Some(GENESIS_TIMESTAMP),
        None,
        1, // tx_version
    );

    Block::new(
        0,
        GENESIS_PREV_HASH,
        GENESIS_TIMESTAMP,
        vec![genesis_tx],
        GENESIS_MINER,
        BTreeMap::new(), // empty proof_of_power
    )
}

/// Assert that the genesis hash matches the expected value.
///
/// Call this at node startup.  If it fails, the serialisation / hashing
/// logic has diverged from Python and the chain CANNOT operate.
pub fn verify_genesis() -> bool {
    let genesis = create_canonical_genesis();
    genesis.hash == EXPECTED_GENESIS_HASH
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_genesis_hash_matches_expected() {
        let genesis = create_canonical_genesis();
        println!("Rust genesis hash  = {}", genesis.hash);
        println!("Expected (Python)  = {}", EXPECTED_GENESIS_HASH);

        // This is the single most important test.  If it passes,
        // the Rust and Python implementations are byte-compatible.
        assert_eq!(
            genesis.hash, EXPECTED_GENESIS_HASH,
            "GENESIS HASH MISMATCH — Rust and Python are incompatible!"
        );
    }

    #[test]
    fn test_genesis_fields() {
        let genesis = create_canonical_genesis();
        assert_eq!(genesis.index, 0);
        assert_eq!(genesis.miner_address, "SYSTEM");
        assert_eq!(genesis.timestamp, 1_743_379_200.0);
        assert_eq!(genesis.transactions.len(), 1);
        assert_eq!(genesis.transactions[0].from_address, "SYSTEM");
        assert_eq!(genesis.transactions[0].to_address, GENESIS_CREATOR);
        assert_eq!(genesis.transactions[0].amount, 0);
        assert_eq!(genesis.transactions[0].tx_type, "reward");
    }
}
