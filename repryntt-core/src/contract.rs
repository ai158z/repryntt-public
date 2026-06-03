//! Workload contracts — compute workload lifecycle management.
//!
//! Matches Python's `smartcontracts.py` `WorkloadContract` class:
//! machine registration, workload submission, claiming, completion, and rewards.

use sha3::{Digest, Sha3_256};
use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::transaction::PLANCKS_PER_CREDIT;

// ── Constants ────────────────────────────────────────────────────────────────

/// Submission fee: 0.01 CR.
pub const WORKLOAD_FEE_PLANCKS: i64 = PLANCKS_PER_CREDIT / 100; // 1_000_000

/// Completion reward: 0.1 CR.
pub const WORKLOAD_REWARD_PLANCKS: i64 = PLANCKS_PER_CREDIT / 10; // 10_000_000

/// Claim timeout in seconds — unclaimed work is released.
pub const CLAIM_TIMEOUT_SECS: f64 = 60.0;

/// Maximum purpose/description length.
pub const MAX_PURPOSE_LEN: usize = 200;

/// DAO treasury address (receives fees).
pub const DAO_TREASURY: &str = "dao_treasury";

/// Valid workload types.
pub const WORKLOAD_TYPE_COMPUTATIONAL: &str = "computational";
pub const WORKLOAD_TYPE_AI_INFERENCE: &str = "ai_inference";

pub const VALID_WORKLOAD_TYPES: &[&str] =
    &[WORKLOAD_TYPE_COMPUTATIONAL, WORKLOAD_TYPE_AI_INFERENCE];

// ── Workload ─────────────────────────────────────────────────────────────────

/// Status of a workload.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WorkloadStatus {
    Pending,
    Claimed,
    Completed,
}

impl std::fmt::Display for WorkloadStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Pending => write!(f, "pending"),
            Self::Claimed => write!(f, "claimed"),
            Self::Completed => write!(f, "completed"),
        }
    }
}

/// A compute workload submitted to the network.
#[derive(Debug, Clone)]
pub struct Workload {
    /// 64-char hex key (SHA3-256 of data_hash + submitter + timestamp).
    pub key: String,
    /// Address of the machine that submitted the workload.
    pub machine_address: String,
    /// Purpose description (max 200 chars).
    pub purpose: String,
    /// Hash of the workload data.
    pub data_hash: String,
    /// Current status.
    pub status: WorkloadStatus,
    /// Storage node addresses holding the data.
    pub storage_nodes: Vec<(String, u16)>,
    /// "computational" or "ai_inference".
    pub workload_type: String,
    /// When submitted.
    pub submitted_at: f64,
    /// Address of the miner that claimed the work (if claimed).
    pub claimed_by: Option<String>,
    /// When claimed.
    pub claimed_at: Option<f64>,
    /// Inline workload data (for ai_inference type).
    pub workload_data: serde_json::Value,
}

impl Workload {
    pub fn to_dict(&self) -> serde_json::Value {
        let storage: Vec<serde_json::Value> = self
            .storage_nodes
            .iter()
            .map(|(h, p)| serde_json::json!([h, p]))
            .collect();
        serde_json::json!({
            "key": self.key,
            "machine_address": self.machine_address,
            "purpose": self.purpose,
            "data_hash": self.data_hash,
            "status": self.status.to_string(),
            "storage_nodes": storage,
            "workload_type": self.workload_type,
            "submitted_at": self.submitted_at,
            "claimed_by": self.claimed_by,
            "claimed_at": self.claimed_at,
            "workload_data": self.workload_data,
        })
    }

    pub fn from_dict(v: &serde_json::Value) -> Result<Self, String> {
        let status = match v["status"].as_str().unwrap_or("pending") {
            "claimed" => WorkloadStatus::Claimed,
            "completed" => WorkloadStatus::Completed,
            _ => WorkloadStatus::Pending,
        };
        let storage_nodes: Vec<(String, u16)> = v["storage_nodes"]
            .as_array()
            .unwrap_or(&Vec::new())
            .iter()
            .filter_map(|n| {
                let host = n.get(0)?.as_str()?.to_string();
                let port = n.get(1)?.as_u64()? as u16;
                Some((host, port))
            })
            .collect();
        Ok(Self {
            key: v["key"].as_str().ok_or("missing key")?.to_string(),
            machine_address: v["machine_address"]
                .as_str()
                .ok_or("missing machine_address")?
                .to_string(),
            purpose: v["purpose"].as_str().unwrap_or("").to_string(),
            data_hash: v["data_hash"].as_str().unwrap_or("").to_string(),
            status,
            storage_nodes,
            workload_type: v["workload_type"]
                .as_str()
                .unwrap_or(WORKLOAD_TYPE_COMPUTATIONAL)
                .to_string(),
            submitted_at: v["submitted_at"].as_f64().unwrap_or(0.0),
            claimed_by: v["claimed_by"].as_str().map(|s| s.to_string()),
            claimed_at: v["claimed_at"].as_f64(),
            workload_data: v["workload_data"].clone(),
        })
    }
}

/// Result of a completed workload.
#[derive(Debug, Clone)]
pub struct WorkloadResult {
    pub key: String,
    pub miner_address: String,
    pub result_data: serde_json::Value,
    pub completed_at: f64,
}

impl WorkloadResult {
    pub fn to_dict(&self) -> serde_json::Value {
        serde_json::json!({
            "key": self.key,
            "miner_address": self.miner_address,
            "result_data": self.result_data,
            "completed_at": self.completed_at,
        })
    }

    pub fn from_dict(v: &serde_json::Value) -> Result<Self, String> {
        Ok(Self {
            key: v["key"].as_str().ok_or("missing key")?.to_string(),
            miner_address: v["miner_address"]
                .as_str()
                .ok_or("missing miner_address")?
                .to_string(),
            result_data: v["result_data"].clone(),
            completed_at: v["completed_at"].as_f64().unwrap_or(0.0),
        })
    }
}

// ── Workload Contract ────────────────────────────────────────────────────────

/// The workload contract engine — manages compute workload lifecycle.
///
/// This is a **hardcoded singleton** (not user-definable), matching
/// Python's `WorkloadContract` exactly.
pub struct WorkloadContract {
    /// Submitted workloads: key → workload.
    pub workloads: HashMap<String, Workload>,
    /// Pending workload keys (FIFO order).
    pending_keys: Vec<String>,
    /// Registered machines: machine_address → deployment_key.
    pub registered_machines: HashMap<String, String>,
    /// Completed workload results: key → result.
    pub results: HashMap<String, WorkloadResult>,
}

impl WorkloadContract {
    pub fn new() -> Self {
        Self {
            workloads: HashMap::new(),
            pending_keys: Vec::new(),
            registered_machines: HashMap::new(),
            results: HashMap::new(),
        }
    }

    // ── Machine Registration ────────────────────────────────────

    /// Register a machine for workload processing.
    pub fn register_machine(
        &mut self,
        machine_address: &str,
        deployment_key: &str,
    ) -> Result<(), String> {
        if machine_address.is_empty() {
            return Err("Empty machine address".into());
        }
        if self.registered_machines.contains_key(machine_address) {
            return Err("Machine already registered".into());
        }
        self.registered_machines
            .insert(machine_address.to_string(), deployment_key.to_string());
        Ok(())
    }

    /// Check if a machine is registered.
    pub fn is_machine_registered(&self, machine_address: &str) -> bool {
        self.registered_machines.contains_key(machine_address)
    }

    // ── Workload Submission ─────────────────────────────────────

    /// Submit a new workload.
    ///
    /// Charges `WORKLOAD_FEE_PLANCKS` from the submitter's balance.
    /// `balances` is the chain's balance map for fee deduction.
    pub fn submit_workload(
        &mut self,
        machine_address: &str,
        purpose: &str,
        data_hash: &str,
        storage_nodes: Vec<(String, u16)>,
        workload_type: &str,
        balances: &mut HashMap<String, i64>,
    ) -> Result<String, String> {
        // Machine must be registered
        if !self.registered_machines.contains_key(machine_address) {
            return Err("Machine not registered".into());
        }

        // Validate
        if purpose.len() > MAX_PURPOSE_LEN {
            return Err(format!("Purpose too long (max {})", MAX_PURPOSE_LEN));
        }
        if !VALID_WORKLOAD_TYPES.contains(&workload_type) {
            return Err(format!("Invalid workload type: {}", workload_type));
        }
        if data_hash.is_empty() {
            return Err("Empty data hash".into());
        }

        // Charge fee from submitter
        let balance = balances.get(machine_address).copied().unwrap_or(0);
        if balance < WORKLOAD_FEE_PLANCKS {
            return Err(format!(
                "Insufficient balance for fee ({} < {})",
                balance, WORKLOAD_FEE_PLANCKS
            ));
        }
        *balances.get_mut(machine_address).unwrap() -= WORKLOAD_FEE_PLANCKS;
        *balances.entry(DAO_TREASURY.to_string()).or_insert(0) += WORKLOAD_FEE_PLANCKS;

        // Generate workload key
        let key = workload_key(data_hash, machine_address, now_f64());
        if self.workloads.contains_key(&key) {
            return Err("Workload key collision".into());
        }

        let workload = Workload {
            key: key.clone(),
            machine_address: machine_address.to_string(),
            purpose: purpose.to_string(),
            data_hash: data_hash.to_string(),
            status: WorkloadStatus::Pending,
            storage_nodes,
            workload_type: workload_type.to_string(),
            submitted_at: now_f64(),
            claimed_by: None,
            claimed_at: None,
            workload_data: serde_json::Value::Null,
        };

        self.workloads.insert(key.clone(), workload);
        self.pending_keys.push(key.clone());
        Ok(key)
    }

    /// Submit an AI inference workload with inline data.
    pub fn submit_ai_inference(
        &mut self,
        requester: &str,
        prompt: &str,
        max_tokens: u32,
        temperature: f64,
        fee_plancks: i64,
        balances: &mut HashMap<String, i64>,
    ) -> Result<String, String> {
        if prompt.is_empty() {
            return Err("Empty prompt".into());
        }

        // Charge custom fee (at least WORKLOAD_FEE_PLANCKS)
        let fee = fee_plancks.max(WORKLOAD_FEE_PLANCKS);
        let balance = balances.get(requester).copied().unwrap_or(0);
        if balance < fee {
            return Err(format!("Insufficient balance ({} < {})", balance, fee));
        }
        *balances.get_mut(requester).unwrap() -= fee;
        *balances.entry(DAO_TREASURY.to_string()).or_insert(0) += fee;

        // Data hash from prompt content
        let data_hash = sha3_hash(prompt);
        let key = workload_key(&data_hash, requester, now_f64());

        let workload_data = serde_json::json!({
            "prompt": prompt,
            "max_tokens": max_tokens,
            "temperature": temperature,
        });

        let workload = Workload {
            key: key.clone(),
            machine_address: requester.to_string(),
            purpose: format!("AI inference: {}...", &prompt[..prompt.len().min(50)]),
            data_hash,
            status: WorkloadStatus::Pending,
            storage_nodes: Vec::new(),
            workload_type: WORKLOAD_TYPE_AI_INFERENCE.to_string(),
            submitted_at: now_f64(),
            claimed_by: None,
            claimed_at: None,
            workload_data,
        };

        self.workloads.insert(key.clone(), workload);
        self.pending_keys.push(key.clone());
        Ok(key)
    }

    // ── Claiming ────────────────────────────────────────────────

    /// Claim a workload for processing.
    pub fn claim_workload(
        &mut self,
        workload_key: &str,
        miner_address: &str,
    ) -> Result<(), String> {
        let workload = self
            .workloads
            .get_mut(workload_key)
            .ok_or("Workload not found")?;

        if workload.status != WorkloadStatus::Pending {
            return Err(format!(
                "Workload is {} (expected pending)",
                workload.status
            ));
        }

        workload.status = WorkloadStatus::Claimed;
        workload.claimed_by = Some(miner_address.to_string());
        workload.claimed_at = Some(now_f64());

        self.pending_keys.retain(|k| k != workload_key);
        Ok(())
    }

    /// Release a claim (e.g., after timeout).
    pub fn release_claim(&mut self, workload_key: &str) -> Result<(), String> {
        let workload = self
            .workloads
            .get_mut(workload_key)
            .ok_or("Workload not found")?;

        if workload.status != WorkloadStatus::Claimed {
            return Err("Workload not claimed".into());
        }

        workload.status = WorkloadStatus::Pending;
        workload.claimed_by = None;
        workload.claimed_at = None;
        self.pending_keys.push(workload_key.to_string());
        Ok(())
    }

    /// Get the next unclaimed workload key (FIFO).
    pub fn get_unclaimed_workload(&self) -> Option<&str> {
        self.pending_keys.first().map(|s| s.as_str())
    }

    /// Release timed-out claims and return them to pending.
    pub fn release_expired_claims(&mut self) -> Vec<String> {
        let now = now_f64();
        let expired: Vec<String> = self
            .workloads
            .iter()
            .filter(|(_, w)| {
                w.status == WorkloadStatus::Claimed
                    && w.claimed_at
                        .map(|t| now - t > CLAIM_TIMEOUT_SECS)
                        .unwrap_or(false)
            })
            .map(|(k, _)| k.clone())
            .collect();

        for key in &expired {
            if let Some(w) = self.workloads.get_mut(key) {
                w.status = WorkloadStatus::Pending;
                w.claimed_by = None;
                w.claimed_at = None;
                self.pending_keys.push(key.clone());
            }
        }
        expired
    }

    // ── Completion ──────────────────────────────────────────────

    /// Complete a workload and pay the miner.
    ///
    /// Pays `WORKLOAD_REWARD_PLANCKS` from DAO treasury to miner.
    pub fn complete_workload(
        &mut self,
        workload_key: &str,
        miner_address: &str,
        result: serde_json::Value,
        balances: &mut HashMap<String, i64>,
    ) -> Result<(), String> {
        let workload = self
            .workloads
            .get_mut(workload_key)
            .ok_or("Workload not found")?;

        if workload.status == WorkloadStatus::Completed {
            return Err("Already completed".into());
        }

        // Verify claimer is the completer (if claimed)
        if let Some(claimer) = &workload.claimed_by {
            if claimer != miner_address {
                return Err("Not the claimer".into());
            }
        }

        workload.status = WorkloadStatus::Completed;

        // Pay reward from DAO treasury
        let dao_balance = balances.get(DAO_TREASURY).copied().unwrap_or(0);
        let reward = WORKLOAD_REWARD_PLANCKS.min(dao_balance);
        if reward > 0 {
            *balances.get_mut(DAO_TREASURY).unwrap() -= reward;
            *balances.entry(miner_address.to_string()).or_insert(0) += reward;
        }

        // Store result
        self.results.insert(
            workload_key.to_string(),
            WorkloadResult {
                key: workload_key.to_string(),
                miner_address: miner_address.to_string(),
                result_data: result,
                completed_at: now_f64(),
            },
        );

        self.pending_keys.retain(|k| k != workload_key);
        Ok(())
    }

    /// Get the result for a completed workload.
    pub fn get_result(&self, workload_key: &str) -> Option<&WorkloadResult> {
        self.results.get(workload_key)
    }

    // ── Queries ─────────────────────────────────────────────────

    /// Number of pending workloads.
    pub fn pending_count(&self) -> usize {
        self.pending_keys.len()
    }

    /// Total workloads submitted.
    pub fn total_workloads(&self) -> usize {
        self.workloads.len()
    }

    /// Total completed workloads.
    pub fn completed_count(&self) -> usize {
        self.results.len()
    }

    pub fn stats(&self) -> ContractStats {
        let pending = self
            .workloads
            .values()
            .filter(|w| w.status == WorkloadStatus::Pending)
            .count();
        let claimed = self
            .workloads
            .values()
            .filter(|w| w.status == WorkloadStatus::Claimed)
            .count();
        let completed = self
            .workloads
            .values()
            .filter(|w| w.status == WorkloadStatus::Completed)
            .count();
        ContractStats {
            total_workloads: self.workloads.len(),
            pending,
            claimed,
            completed,
            registered_machines: self.registered_machines.len(),
            results_stored: self.results.len(),
        }
    }

    // ── Serialization ───────────────────────────────────────────

    pub fn to_dict(&self) -> serde_json::Value {
        let workloads: serde_json::Map<String, serde_json::Value> = self
            .workloads
            .iter()
            .map(|(k, v)| (k.clone(), v.to_dict()))
            .collect();
        let machines: serde_json::Map<String, serde_json::Value> = self
            .registered_machines
            .iter()
            .map(|(k, v)| (k.clone(), serde_json::Value::String(v.clone())))
            .collect();
        let results: serde_json::Map<String, serde_json::Value> = self
            .results
            .iter()
            .map(|(k, v)| (k.clone(), v.to_dict()))
            .collect();

        serde_json::json!({
            "workloads": workloads,
            "registered_machines": machines,
            "results": results,
            "pending_keys": self.pending_keys,
        })
    }

    pub fn from_dict(v: &serde_json::Value) -> Self {
        let mut contract = Self::new();

        if let Some(wls) = v["workloads"].as_object() {
            for (k, wv) in wls {
                if let Ok(w) = Workload::from_dict(wv) {
                    contract.workloads.insert(k.clone(), w);
                }
            }
        }

        if let Some(machines) = v["registered_machines"].as_object() {
            for (k, mv) in machines {
                if let Some(dk) = mv.as_str() {
                    contract
                        .registered_machines
                        .insert(k.clone(), dk.to_string());
                }
            }
        }

        if let Some(results) = v["results"].as_object() {
            for (k, rv) in results {
                if let Ok(r) = WorkloadResult::from_dict(rv) {
                    contract.results.insert(k.clone(), r);
                }
            }
        }

        if let Some(keys) = v["pending_keys"].as_array() {
            contract.pending_keys = keys
                .iter()
                .filter_map(|k| k.as_str().map(|s| s.to_string()))
                .collect();
        }

        contract
    }
}

impl Default for WorkloadContract {
    fn default() -> Self {
        Self::new()
    }
}

/// Contract statistics.
#[derive(Debug, Clone)]
pub struct ContractStats {
    pub total_workloads: usize,
    pub pending: usize,
    pub claimed: usize,
    pub completed: usize,
    pub registered_machines: usize,
    pub results_stored: usize,
}

// ── Helpers ──────────────────────────────────────────────────────────────────

/// Deterministic workload key from content.
fn workload_key(data_hash: &str, submitter: &str, timestamp: f64) -> String {
    let mut hasher = Sha3_256::new();
    hasher.update(data_hash.as_bytes());
    hasher.update(submitter.as_bytes());
    hasher.update(timestamp.to_be_bytes());
    hex::encode(hasher.finalize())
}

fn sha3_hash(input: &str) -> String {
    let mut hasher = Sha3_256::new();
    hasher.update(input.as_bytes());
    hex::encode(hasher.finalize())
}

fn now_f64() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn setup_contract() -> (WorkloadContract, HashMap<String, i64>) {
        let mut contract = WorkloadContract::new();
        contract
            .register_machine("machine_a", "deploy_key_1")
            .unwrap();

        let mut balances = HashMap::new();
        balances.insert("machine_a".to_string(), 100 * PLANCKS_PER_CREDIT);
        balances.insert(DAO_TREASURY.to_string(), 1000 * PLANCKS_PER_CREDIT);

        (contract, balances)
    }

    // ── Machine Registration ────────────────────────────────────

    #[test]
    fn test_register_machine() {
        let mut contract = WorkloadContract::new();
        contract.register_machine("m1", "key1").unwrap();
        assert!(contract.is_machine_registered("m1"));
        assert!(!contract.is_machine_registered("m2"));
    }

    #[test]
    fn test_register_duplicate_machine() {
        let mut contract = WorkloadContract::new();
        contract.register_machine("m1", "key1").unwrap();
        assert!(contract.register_machine("m1", "key2").is_err());
    }

    #[test]
    fn test_register_empty_address() {
        let mut contract = WorkloadContract::new();
        assert!(contract.register_machine("", "key1").is_err());
    }

    // ── Workload Submission ─────────────────────────────────────

    #[test]
    fn test_submit_workload() {
        let (mut contract, mut balances) = setup_contract();
        let balance_before = balances["machine_a"];

        let key = contract
            .submit_workload(
                "machine_a",
                "Test computation",
                "datahash123",
                vec![("node1".into(), 5001)],
                WORKLOAD_TYPE_COMPUTATIONAL,
                &mut balances,
            )
            .unwrap();

        assert!(!key.is_empty());
        assert_eq!(key.len(), 64); // SHA3-256 hex
        assert_eq!(contract.pending_count(), 1);
        assert_eq!(balances["machine_a"], balance_before - WORKLOAD_FEE_PLANCKS);
        assert!(balances[DAO_TREASURY] > 0);
    }

    #[test]
    fn test_submit_unregistered_machine() {
        let mut contract = WorkloadContract::new();
        let mut balances = HashMap::new();
        balances.insert("rogue".to_string(), 100 * PLANCKS_PER_CREDIT);

        assert!(
            contract
                .submit_workload(
                    "rogue",
                    "hack",
                    "hash",
                    vec![],
                    WORKLOAD_TYPE_COMPUTATIONAL,
                    &mut balances,
                )
                .is_err()
        );
    }

    #[test]
    fn test_submit_insufficient_balance() {
        let (mut contract, mut balances) = setup_contract();
        balances.insert("machine_a".to_string(), 0); // broke

        assert!(
            contract
                .submit_workload(
                    "machine_a",
                    "work",
                    "hash",
                    vec![],
                    WORKLOAD_TYPE_COMPUTATIONAL,
                    &mut balances,
                )
                .is_err()
        );
    }

    #[test]
    fn test_submit_invalid_type() {
        let (mut contract, mut balances) = setup_contract();
        assert!(
            contract
                .submit_workload(
                    "machine_a",
                    "work",
                    "hash",
                    vec![],
                    "invalid_type",
                    &mut balances,
                )
                .is_err()
        );
    }

    #[test]
    fn test_submit_empty_data_hash() {
        let (mut contract, mut balances) = setup_contract();
        assert!(
            contract
                .submit_workload(
                    "machine_a",
                    "work",
                    "",
                    vec![],
                    WORKLOAD_TYPE_COMPUTATIONAL,
                    &mut balances,
                )
                .is_err()
        );
    }

    #[test]
    fn test_submit_purpose_too_long() {
        let (mut contract, mut balances) = setup_contract();
        let long_purpose = "x".repeat(MAX_PURPOSE_LEN + 1);
        assert!(
            contract
                .submit_workload(
                    "machine_a",
                    &long_purpose,
                    "hash",
                    vec![],
                    WORKLOAD_TYPE_COMPUTATIONAL,
                    &mut balances,
                )
                .is_err()
        );
    }

    // ── AI Inference ────────────────────────────────────────────

    #[test]
    fn test_submit_ai_inference() {
        let mut contract = WorkloadContract::new();
        let mut balances = HashMap::new();
        balances.insert("requester".to_string(), 10 * PLANCKS_PER_CREDIT);
        balances.insert(DAO_TREASURY.to_string(), 0);

        let key = contract
            .submit_ai_inference(
                "requester",
                "Explain quantum computing",
                512,
                0.7,
                WORKLOAD_FEE_PLANCKS,
                &mut balances,
            )
            .unwrap();

        assert_eq!(key.len(), 64);
        let w = contract.workloads.get(&key).unwrap();
        assert_eq!(w.workload_type, WORKLOAD_TYPE_AI_INFERENCE);
        assert!(!w.workload_data.is_null());
    }

    #[test]
    fn test_submit_ai_inference_empty_prompt() {
        let mut contract = WorkloadContract::new();
        let mut balances = HashMap::new();
        balances.insert("r".to_string(), 10 * PLANCKS_PER_CREDIT);
        assert!(
            contract
                .submit_ai_inference("r", "", 100, 0.5, WORKLOAD_FEE_PLANCKS, &mut balances)
                .is_err()
        );
    }

    // ── Claiming ────────────────────────────────────────────────

    #[test]
    fn test_claim_workload() {
        let (mut contract, mut balances) = setup_contract();
        let key = contract
            .submit_workload(
                "machine_a",
                "work",
                "hash1",
                vec![],
                WORKLOAD_TYPE_COMPUTATIONAL,
                &mut balances,
            )
            .unwrap();

        contract.claim_workload(&key, "miner_1").unwrap();
        let w = contract.workloads.get(&key).unwrap();
        assert_eq!(w.status, WorkloadStatus::Claimed);
        assert_eq!(w.claimed_by.as_deref(), Some("miner_1"));
        assert_eq!(contract.pending_count(), 0);
    }

    #[test]
    fn test_claim_already_claimed() {
        let (mut contract, mut balances) = setup_contract();
        let key = contract
            .submit_workload(
                "machine_a",
                "work",
                "hash1",
                vec![],
                WORKLOAD_TYPE_COMPUTATIONAL,
                &mut balances,
            )
            .unwrap();

        contract.claim_workload(&key, "miner_1").unwrap();
        assert!(contract.claim_workload(&key, "miner_2").is_err());
    }

    #[test]
    fn test_claim_nonexistent() {
        let mut contract = WorkloadContract::new();
        assert!(contract.claim_workload("ghost", "miner").is_err());
    }

    #[test]
    fn test_release_claim() {
        let (mut contract, mut balances) = setup_contract();
        let key = contract
            .submit_workload(
                "machine_a",
                "work",
                "hash1",
                vec![],
                WORKLOAD_TYPE_COMPUTATIONAL,
                &mut balances,
            )
            .unwrap();

        contract.claim_workload(&key, "miner_1").unwrap();
        assert_eq!(contract.pending_count(), 0);

        contract.release_claim(&key).unwrap();
        assert_eq!(contract.pending_count(), 1);
        assert_eq!(
            contract.workloads.get(&key).unwrap().status,
            WorkloadStatus::Pending
        );
    }

    #[test]
    fn test_get_unclaimed_workload() {
        let (mut contract, mut balances) = setup_contract();
        assert!(contract.get_unclaimed_workload().is_none());

        let key = contract
            .submit_workload(
                "machine_a",
                "work",
                "hash1",
                vec![],
                WORKLOAD_TYPE_COMPUTATIONAL,
                &mut balances,
            )
            .unwrap();

        assert_eq!(contract.get_unclaimed_workload(), Some(key.as_str()));
    }

    // ── Completion ──────────────────────────────────────────────

    #[test]
    fn test_complete_workload() {
        let (mut contract, mut balances) = setup_contract();
        let key = contract
            .submit_workload(
                "machine_a",
                "work",
                "hash1",
                vec![],
                WORKLOAD_TYPE_COMPUTATIONAL,
                &mut balances,
            )
            .unwrap();

        contract.claim_workload(&key, "miner_1").unwrap();

        let miner_before = balances.get("miner_1").copied().unwrap_or(0);
        contract
            .complete_workload(
                &key,
                "miner_1",
                serde_json::json!({"result": "42"}),
                &mut balances,
            )
            .unwrap();

        let w = contract.workloads.get(&key).unwrap();
        assert_eq!(w.status, WorkloadStatus::Completed);
        assert!(contract.get_result(&key).is_some());
        assert!(balances.get("miner_1").unwrap() > &miner_before);
    }

    #[test]
    fn test_complete_wrong_miner() {
        let (mut contract, mut balances) = setup_contract();
        let key = contract
            .submit_workload(
                "machine_a",
                "work",
                "hash1",
                vec![],
                WORKLOAD_TYPE_COMPUTATIONAL,
                &mut balances,
            )
            .unwrap();

        contract.claim_workload(&key, "miner_1").unwrap();
        assert!(
            contract
                .complete_workload(&key, "imposter", serde_json::json!({}), &mut balances,)
                .is_err()
        );
    }

    #[test]
    fn test_complete_already_completed() {
        let (mut contract, mut balances) = setup_contract();
        let key = contract
            .submit_workload(
                "machine_a",
                "work",
                "hash1",
                vec![],
                WORKLOAD_TYPE_COMPUTATIONAL,
                &mut balances,
            )
            .unwrap();

        contract.claim_workload(&key, "miner_1").unwrap();
        contract
            .complete_workload(&key, "miner_1", serde_json::json!({}), &mut balances)
            .unwrap();
        assert!(
            contract
                .complete_workload(&key, "miner_1", serde_json::json!({}), &mut balances)
                .is_err()
        );
    }

    // ── Stats ───────────────────────────────────────────────────

    #[test]
    fn test_contract_stats() {
        let (mut contract, mut balances) = setup_contract();
        contract
            .submit_workload(
                "machine_a",
                "w1",
                "h1",
                vec![],
                WORKLOAD_TYPE_COMPUTATIONAL,
                &mut balances,
            )
            .unwrap();
        let k2 = contract
            .submit_workload(
                "machine_a",
                "w2",
                "h2",
                vec![],
                WORKLOAD_TYPE_COMPUTATIONAL,
                &mut balances,
            )
            .unwrap();

        contract.claim_workload(&k2, "miner").unwrap();

        let stats = contract.stats();
        assert_eq!(stats.total_workloads, 2);
        assert_eq!(stats.pending, 1);
        assert_eq!(stats.claimed, 1);
        assert_eq!(stats.completed, 0);
        assert_eq!(stats.registered_machines, 1);
    }

    // ── Serialization ───────────────────────────────────────────

    #[test]
    fn test_contract_roundtrip() {
        let (mut contract, mut balances) = setup_contract();
        let key = contract
            .submit_workload(
                "machine_a",
                "persist me",
                "hash_abc",
                vec![("store1".into(), 5001)],
                WORKLOAD_TYPE_COMPUTATIONAL,
                &mut balances,
            )
            .unwrap();
        contract.claim_workload(&key, "miner_1").unwrap();
        contract
            .complete_workload(
                &key,
                "miner_1",
                serde_json::json!({"answer": 42}),
                &mut balances,
            )
            .unwrap();

        let dict = contract.to_dict();
        let c2 = WorkloadContract::from_dict(&dict);

        assert_eq!(c2.total_workloads(), 1);
        assert_eq!(c2.completed_count(), 1);
        assert!(c2.is_machine_registered("machine_a"));
        assert!(c2.get_result(&key).is_some());
    }

    #[test]
    fn test_workload_roundtrip() {
        let w = Workload {
            key: "abc".repeat(22)[..64].to_string(),
            machine_address: "machine_1".into(),
            purpose: "Test".into(),
            data_hash: "hash123".into(),
            status: WorkloadStatus::Pending,
            storage_nodes: vec![("node1".into(), 5001)],
            workload_type: WORKLOAD_TYPE_COMPUTATIONAL.into(),
            submitted_at: 1000.0,
            claimed_by: None,
            claimed_at: None,
            workload_data: serde_json::Value::Null,
        };

        let dict = w.to_dict();
        let w2 = Workload::from_dict(&dict).unwrap();
        assert_eq!(w.key, w2.key);
        assert_eq!(w.machine_address, w2.machine_address);
        assert_eq!(w2.status, WorkloadStatus::Pending);
    }
}
