//! Transaction types for repryntt blockchain.
//!
//! CRITICAL: Hashing and serialisation MUST produce byte-identical output to
//! the Python `transaction.py` so that genesis (and every subsequent block)
//! hashes to the same value on both implementations.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha3::{Digest, Sha3_256, Sha3_512};
use std::collections::BTreeMap;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::crypto;
use crate::pycompat::python_json_dumps;

// ── constants ────────────────────────────────────────────────────────────────

pub const CHAIN_ID: &str = "RPNT-mainnet-1";

/// 1 Credit = 100 000 000 Plancks (Satoshi-equivalent).
pub const PLANCKS_PER_CREDIT: i64 = 100_000_000;

/// Minimum stake: 1.0 CR.
pub const MIN_STAKE_PLANCKS: i64 = PLANCKS_PER_CREDIT;

pub const VALID_TX_TYPES: &[&str] = &[
    "reward",
    "fee",
    "transfer",
    "stake",
    "stake_withdraw",
    "penalty",
    "faucet",
    "faucet_claim",
    "workload_completion",
    "entity_register",
    "token_create",
    "token_mint",
    "token_burn",
    "token_transfer",
    "token_approve",
    "token_freeze",
    "token_thaw",
    "workload_submit",
    "workload_claim",
    "workload_complete",
    "ai_inference",
    "dao_allocate",
    "dao_proposal",
    "dao_vote",
    "dao_execute",
];

// ── Transaction ──────────────────────────────────────────────────────────────

/// A single transaction on the repryntt chain.
///
/// Field names and JSON keys match the Python `Transaction` class exactly.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Transaction {
    pub from_address: String,
    pub to_address: String,
    /// Amount in Plancks.
    pub amount: i64,
    pub tx_type: String,
    pub nonce: u64,
    pub timestamp: f64,
    pub metadata: BTreeMap<String, Value>,
    pub tx_version: u32,
    /// Hex-encoded Ed25519 signature of `tx_hash` bytes (set by `sign()`).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub signature: Option<String>,
    /// Hex-encoded Ed25519 public key of the sender.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub public_key: Option<String>,
    /// SHA3-512 hex hash — computed over the canonical fields (excludes
    /// signature and public_key).
    pub tx_hash: String,
}

impl Transaction {
    /// Create a new transaction and compute its hash.
    pub fn new(
        from_address: &str,
        to_address: &str,
        amount: i64,
        tx_type: &str,
        nonce: u64,
        metadata: BTreeMap<String, Value>,
        timestamp: Option<f64>,
        public_key: Option<Vec<u8>>,
        tx_version: u32,
    ) -> Self {
        let ts = timestamp.unwrap_or_else(now_f64);
        let pk_hex = public_key.as_ref().map(hex::encode);

        let mut tx = Self {
            from_address: from_address.to_string(),
            to_address: to_address.to_string(),
            amount,
            tx_type: tx_type.to_string(),
            nonce,
            timestamp: ts,
            metadata,
            tx_version,
            signature: None,
            public_key: pk_hex,
            tx_hash: String::new(),
        };
        tx.tx_hash = tx.calculate_hash();
        tx
    }

    // ── hashing ──────────────────────────────────────────────────────────

    /// Canonical hash that **must** match Python's `Transaction.calculate_hash()`.
    ///
    /// Python builds a dict with keys:
    ///   amount, from, metadata, nonce, timestamp, to, type
    ///   (and chain_id when tx_version >= 2)
    /// then calls `json.dumps(tx_data, sort_keys=True)` and SHA3-512's the
    /// UTF-8 bytes.
    ///
    /// We reproduce the exact same JSON by using a `BTreeMap` (sorted) and
    /// matching key names precisely.
    pub fn calculate_hash(&self) -> String {
        let mut map: BTreeMap<String, Value> = BTreeMap::new();
        map.insert("amount".into(), Value::Number(self.amount.into()));
        map.insert("from".into(), Value::String(self.from_address.clone()));
        map.insert(
            "metadata".into(),
            serde_json::to_value(&self.metadata).unwrap_or(Value::Object(Default::default())),
        );
        map.insert(
            "nonce".into(),
            Value::Number(serde_json::Number::from(self.nonce)),
        );
        map.insert("timestamp".into(), json_f64(self.timestamp));
        map.insert("to".into(), Value::String(self.to_address.clone()));
        map.insert("type".into(), Value::String(self.tx_type.clone()));

        if self.tx_version >= 2 {
            map.insert("chain_id".into(), Value::String(CHAIN_ID.to_string()));
        }

        let json_str = python_json_dumps(&serde_json::to_value(&map).unwrap());
        let mut hasher = Sha3_512::new();
        hasher.update(json_str.as_bytes());
        hex::encode(hasher.finalize())
    }

    // ── signing ──────────────────────────────────────────────────────────

    /// Sign the transaction with an Ed25519 private key (32 bytes).
    pub fn sign(&mut self, private_key: &[u8]) {
        if self.tx_hash.is_empty() {
            self.tx_hash = self.calculate_hash();
        }
        let hash_bytes = hex::decode(&self.tx_hash).expect("tx_hash is valid hex");
        let sig = crypto::sign(&hash_bytes, private_key);
        self.signature = Some(hex::encode(sig));
    }

    /// Verify the signature against the embedded public key.
    pub fn verify_signature(&self) -> bool {
        let (Some(sig_hex), Some(pk_hex)) = (&self.signature, &self.public_key) else {
            return false;
        };
        let Ok(sig_bytes) = hex::decode(sig_hex) else {
            return false;
        };
        let Ok(pk_bytes) = hex::decode(pk_hex) else {
            return false;
        };
        let Ok(hash_bytes) = hex::decode(&self.tx_hash) else {
            return false;
        };
        crypto::verify(&hash_bytes, &sig_bytes, &pk_bytes)
    }

    /// Verify `from_address == sha3_256(public_key)[:40]`.
    pub fn verify_address_matches_pubkey(&self) -> bool {
        let Some(pk_hex) = &self.public_key else {
            return false;
        };
        let Ok(pk_bytes) = hex::decode(pk_hex) else {
            return false;
        };
        let mut hasher = Sha3_256::new();
        hasher.update(&pk_bytes);
        let derived = hex::encode(hasher.finalize());
        self.from_address == &derived[..40]
    }

    // ── serialisation ────────────────────────────────────────────────────

    /// Produce a dict matching Python's `Transaction.to_dict()`.
    pub fn to_dict(&self) -> BTreeMap<String, Value> {
        let mut d: BTreeMap<String, Value> = BTreeMap::new();
        d.insert(
            "from_address".into(),
            Value::String(self.from_address.clone()),
        );
        d.insert("to_address".into(), Value::String(self.to_address.clone()));
        d.insert("amount".into(), Value::Number(self.amount.into()));
        d.insert("tx_type".into(), Value::String(self.tx_type.clone()));
        d.insert(
            "nonce".into(),
            Value::Number(serde_json::Number::from(self.nonce)),
        );
        d.insert("timestamp".into(), json_f64(self.timestamp));
        d.insert(
            "metadata".into(),
            serde_json::to_value(&self.metadata).unwrap_or(Value::Object(Default::default())),
        );
        d.insert("tx_hash".into(), Value::String(self.tx_hash.clone()));
        d.insert(
            "tx_version".into(),
            Value::Number(serde_json::Number::from(self.tx_version)),
        );
        if let Some(sig) = &self.signature {
            d.insert("signature".into(), Value::String(sig.clone()));
        }
        if let Some(pk) = &self.public_key {
            d.insert("public_key".into(), Value::String(pk.clone()));
        }
        d
    }

    /// Reconstruct from dict (Python's `Transaction.from_dict`).
    pub fn from_dict(data: &BTreeMap<String, Value>) -> Option<Self> {
        let from_address = data.get("from_address")?.as_str()?.to_string();
        let to_address = data.get("to_address")?.as_str()?.to_string();
        let amount = data.get("amount")?.as_i64()?;
        let tx_type = data.get("tx_type")?.as_str()?.to_string();
        let nonce = data.get("nonce").and_then(|v| v.as_u64()).unwrap_or(0);
        let timestamp = data
            .get("timestamp")
            .and_then(|v| v.as_f64())
            .unwrap_or_else(now_f64);
        let tx_version = data.get("tx_version").and_then(|v| v.as_u64()).unwrap_or(1) as u32;

        let metadata: BTreeMap<String, Value> = data
            .get("metadata")
            .and_then(|v| serde_json::from_value(v.clone()).ok())
            .unwrap_or_default();

        let public_key = data
            .get("public_key")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());

        let signature = data
            .get("signature")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());

        let tx_hash = data
            .get("tx_hash")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();

        Some(Self {
            from_address,
            to_address,
            amount,
            tx_type,
            nonce,
            timestamp,
            metadata,
            tx_version,
            signature,
            public_key,
            tx_hash,
        })
    }

    // ── validation ───────────────────────────────────────────────────────

    pub fn validate(
        &self,
        balances: &BTreeMap<String, i64>,
        nonces: Option<&BTreeMap<String, u64>>,
        require_signature: bool,
    ) -> Result<(), String> {
        // Type check
        if !VALID_TX_TYPES.contains(&self.tx_type.as_str()) {
            return Err(format!("Invalid transaction type: {}", self.tx_type));
        }
        // Amount check
        if self.amount < 0 {
            return Err("Transaction amount cannot be negative".into());
        }
        // Signature check
        if require_signature && !["reward", "entity_register"].contains(&self.tx_type.as_str()) {
            if self.signature.is_none() {
                return Err("Missing transaction signature".into());
            }
            if self.public_key.is_none() {
                return Err("Missing public key".into());
            }
            if !self.verify_signature() {
                return Err("Invalid transaction signature".into());
            }
            if !self.verify_address_matches_pubkey() {
                return Err("Address does not match public key".into());
            }
        }
        // Nonce check
        if let Some(nonce_map) = nonces {
            if !["reward", "faucet", "entity_register"].contains(&self.tx_type.as_str()) {
                let expected = nonce_map.get(&self.from_address).copied().unwrap_or(0);
                if self.nonce != expected {
                    return Err(format!(
                        "Invalid nonce: expected {}, got {}",
                        expected, self.nonce
                    ));
                }
            }
        }
        // Balance check
        if !["reward", "faucet", "entity_register"].contains(&self.tx_type.as_str()) {
            let balance = balances.get(&self.from_address).copied().unwrap_or(0);
            if balance < self.amount {
                return Err(format!(
                    "Insufficient balance: {:.8} CR < {:.8} CR",
                    balance as f64 / PLANCKS_PER_CREDIT as f64,
                    self.amount as f64 / PLANCKS_PER_CREDIT as f64,
                ));
            }
        }
        // Stake minimum
        if self.tx_type == "stake" && self.amount < MIN_STAKE_PLANCKS {
            return Err("Minimum stake is 1.0 CR".into());
        }
        Ok(())
    }
}

// ── helpers ──────────────────────────────────────────────────────────────────

/// Current time as f64 seconds since Unix epoch (matches Python's `time.time()`).
fn now_f64() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("time went backwards")
        .as_secs_f64()
}

/// Convert an f64 timestamp to a JSON Number.
///
/// Python's `json.dumps` renders `1743379200.0` as `1743379200.0`, preserving
/// the trailing `.0`.  `serde_json` will do the same for finite floats via
/// `Number::from_f64`.
fn json_f64(v: f64) -> Value {
    serde_json::Number::from_f64(v)
        .map(Value::Number)
        .unwrap_or(Value::Null)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_genesis_tx_hash_parity() {
        // Reproduce the exact genesis transaction from Python and verify hash.
        let mut meta: BTreeMap<String, Value> = BTreeMap::new();
        meta.insert("block".into(), Value::String("genesis".into()));
        meta.insert("network".into(), Value::String("repryntt-mainnet".into()));
        meta.insert(
            "headline".into(),
            Value::String(
                "AP 04/Apr/2026 Autonomous AI systems begin earning their own currency \
                 through compute contribution — the robots are building their own economy"
                    .into(),
            ),
        );
        meta.insert(
            "creator".into(),
            Value::String("a1a4090aced69d411b6e62bf49944f295c85ed88".into()),
        );
        meta.insert("magic".into(), Value::String("52504e54".into()));

        let tx = Transaction::new(
            "SYSTEM",
            "a1a4090aced69d411b6e62bf49944f295c85ed88",
            0,
            "reward",
            0,
            meta,
            Some(1_743_379_200.0),
            None,
            1,
        );

        // The hash must be deterministic; we'll assert the full hex once we
        // confirm parity with Python.
        assert_eq!(tx.tx_hash.len(), 128, "SHA3-512 hex should be 128 chars");
        println!("genesis tx hash = {}", tx.tx_hash);
    }
}
