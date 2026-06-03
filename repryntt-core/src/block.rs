//! Block type for the repryntt blockchain.
//!
//! CRITICAL: `calculate_hash()` MUST produce identical SHA3-512 hex output
//! to Python's `Block.calculate_hash()` so that genesis and all blocks
//! hash to the same value.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha3::{Digest, Sha3_512};
use std::collections::BTreeMap;

use crate::pycompat::python_json_dumps;
use crate::transaction::Transaction;

// ── Block ────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Block {
    pub index: u64,
    pub previous_hash: String,
    pub timestamp: f64,
    pub transactions: Vec<Transaction>,
    pub miner_address: String,
    pub proof_of_power: BTreeMap<String, Value>,
    pub hash: String,
}

impl Block {
    /// Create a new block, computing its hash.
    pub fn new(
        index: u64,
        previous_hash: &str,
        timestamp: f64,
        transactions: Vec<Transaction>,
        miner_address: &str,
        proof_of_power: BTreeMap<String, Value>,
    ) -> Self {
        let mut blk = Self {
            index,
            previous_hash: previous_hash.to_string(),
            timestamp,
            transactions,
            miner_address: miner_address.to_string(),
            proof_of_power,
            hash: String::new(),
        };
        blk.hash = blk.calculate_hash();
        blk
    }

    // ── hashing ──────────────────────────────────────────────────────────

    /// Canonical block hash — must match Python's `Block.calculate_hash()`.
    ///
    /// Python assembles a dict with sorted keys:
    ///   index, miner_address, previous_hash, proof_of_power, timestamp, transactions
    /// then `json.dumps(block_data, sort_keys=True)` and SHA3-512.
    ///
    /// We use a `BTreeMap<String, Value>` so the keys are naturally sorted.
    pub fn calculate_hash(&self) -> String {
        let tx_dicts: Vec<Value> = self
            .transactions
            .iter()
            .map(|tx| serde_json::to_value(tx.to_dict()).unwrap())
            .collect();

        let mut map: BTreeMap<String, Value> = BTreeMap::new();
        map.insert("index".into(), Value::Number(self.index.into()));
        map.insert(
            "miner_address".into(),
            Value::String(self.miner_address.clone()),
        );
        map.insert(
            "previous_hash".into(),
            Value::String(self.previous_hash.clone()),
        );
        map.insert(
            "proof_of_power".into(),
            serde_json::to_value(&self.proof_of_power).unwrap(),
        );
        map.insert("timestamp".into(), json_f64(self.timestamp));
        map.insert("transactions".into(), Value::Array(tx_dicts));

        let json_str = python_json_dumps(&serde_json::to_value(&map).unwrap());
        let mut hasher = Sha3_512::new();
        hasher.update(json_str.as_bytes());
        hex::encode(hasher.finalize())
    }

    // ── serialisation ────────────────────────────────────────────────────

    /// Produce a dict matching Python's `Block.to_dict()`.
    pub fn to_dict(&self) -> BTreeMap<String, Value> {
        let tx_dicts: Vec<Value> = self
            .transactions
            .iter()
            .map(|tx| serde_json::to_value(tx.to_dict()).unwrap())
            .collect();

        let mut d: BTreeMap<String, Value> = BTreeMap::new();
        d.insert("index".into(), Value::Number(self.index.into()));
        d.insert(
            "previous_hash".into(),
            Value::String(self.previous_hash.clone()),
        );
        d.insert("timestamp".into(), json_f64(self.timestamp));
        d.insert("transactions".into(), Value::Array(tx_dicts));
        d.insert(
            "miner_address".into(),
            Value::String(self.miner_address.clone()),
        );
        d.insert(
            "proof_of_power".into(),
            serde_json::to_value(&self.proof_of_power).unwrap(),
        );
        d.insert("hash".into(), Value::String(self.hash.clone()));
        d
    }

    /// Reconstruct from dict (Python's `Block.from_dict`).
    pub fn from_dict(data: &BTreeMap<String, Value>) -> Option<Self> {
        let index = data.get("index")?.as_u64()?;
        let previous_hash = data.get("previous_hash")?.as_str()?.to_string();
        let timestamp = data.get("timestamp")?.as_f64()?;
        let miner_address = data.get("miner_address")?.as_str()?.to_string();

        let proof_of_power: BTreeMap<String, Value> = data
            .get("proof_of_power")
            .and_then(|v| serde_json::from_value(v.clone()).ok())
            .unwrap_or_default();

        let transactions: Vec<Transaction> = data
            .get("transactions")
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| {
                        let map: BTreeMap<String, Value> =
                            serde_json::from_value(v.clone()).ok()?;
                        Transaction::from_dict(&map)
                    })
                    .collect()
            })
            .unwrap_or_default();

        let stored_hash = data
            .get("hash")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();

        let mut blk = Self {
            index,
            previous_hash,
            timestamp,
            transactions,
            miner_address,
            proof_of_power,
            hash: String::new(),
        };
        // If a stored hash is present use it, otherwise recompute.
        if stored_hash.is_empty() {
            blk.hash = blk.calculate_hash();
        } else {
            blk.hash = stored_hash;
        }
        Some(blk)
    }
}

// ── helpers ──────────────────────────────────────────────────────────────────

fn json_f64(v: f64) -> Value {
    serde_json::Number::from_f64(v)
        .map(Value::Number)
        .unwrap_or(Value::Null)
}
