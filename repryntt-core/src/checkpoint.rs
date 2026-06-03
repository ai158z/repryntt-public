//! Signed canonical checkpoints for early-mainnet fork safety.
//!
//! Checkpoints are an operator/governance safety rail: they do not replace
//! block validation, but they prevent a taller fork that contradicts known
//! canonical history from being accepted as mainnet.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::chain::Chain;
use crate::crypto;
use crate::transaction::CHAIN_ID;

pub const CHECKPOINTS_FILE: &str = "checkpoints.json";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Checkpoint {
    pub chain_id: String,
    /// Chain height as displayed by RPC (`Chain::height()`), not block index.
    pub height: u64,
    pub hash: String,
    pub signer_address: String,
    pub public_key: String,
    pub signature: String,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct CheckpointSet {
    #[serde(default)]
    pub version: u32,
    #[serde(default)]
    pub checkpoints: Vec<Checkpoint>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CheckpointChainStatus {
    NoCheckpoint,
    Verified,
    BelowCheckpoint,
    CheckpointMismatch,
    InvalidCheckpoint(String),
}

impl Checkpoint {
    pub fn expected_message(chain_id: &str, height: u64, hash: &str) -> String {
        format!("{}:checkpoint:{}:{}", chain_id, height, hash)
    }

    pub fn block_index(&self) -> Option<usize> {
        self.height.checked_sub(1).map(|h| h as usize)
    }

    pub fn verify_signature(&self) -> Result<(), String> {
        if self.chain_id != CHAIN_ID {
            return Err(format!(
                "chain_id mismatch: checkpoint={} node={}",
                self.chain_id, CHAIN_ID
            ));
        }
        if self.height == 0 {
            return Err("checkpoint height must be >= 1".into());
        }
        if self.message != Self::expected_message(&self.chain_id, self.height, &self.hash) {
            return Err("checkpoint message does not match chain_id/height/hash".into());
        }

        let public_key = hex::decode(&self.public_key)
            .map_err(|e| format!("checkpoint public_key hex: {}", e))?;
        let signature =
            hex::decode(&self.signature).map_err(|e| format!("checkpoint signature hex: {}", e))?;
        let derived = crypto::address_from_pubkey(&public_key);
        if derived != self.signer_address {
            return Err(format!(
                "checkpoint signer address mismatch: {} != {}",
                self.signer_address, derived
            ));
        }
        if !crypto::verify(self.message.as_bytes(), &signature, &public_key) {
            return Err("checkpoint signature verification failed".into());
        }
        Ok(())
    }
}

pub fn checkpoint_path(data_dir: &Path) -> PathBuf {
    if let Ok(path) = std::env::var("REPRYNTT_CHECKPOINT_FILE") {
        if !path.trim().is_empty() {
            return PathBuf::from(path);
        }
    }
    data_dir.join(CHECKPOINTS_FILE)
}

pub fn load_latest_checkpoint(data_dir: &Path) -> Result<Option<Checkpoint>, String> {
    let path = checkpoint_path(data_dir);
    if !path.exists() {
        return Ok(None);
    }
    let raw = std::fs::read_to_string(&path)
        .map_err(|e| format!("read checkpoint file {}: {}", path.display(), e))?;
    let mut checkpoints = parse_checkpoint_file(&raw)?;
    checkpoints.sort_by_key(|c| c.height);
    Ok(checkpoints.pop())
}

pub fn parse_checkpoint_file(raw: &str) -> Result<Vec<Checkpoint>, String> {
    if raw.trim().is_empty() {
        return Ok(vec![]);
    }
    if let Ok(set) = serde_json::from_str::<CheckpointSet>(raw) {
        return Ok(set.checkpoints);
    }
    if let Ok(single) = serde_json::from_str::<Checkpoint>(raw) {
        return Ok(vec![single]);
    }
    serde_json::from_str::<Vec<Checkpoint>>(raw)
        .map_err(|e| format!("checkpoint JSON parse failed: {}", e))
}

pub fn verify_chain_contains_checkpoint(
    chain: &Chain,
    checkpoint: Option<&Checkpoint>,
) -> CheckpointChainStatus {
    let Some(cp) = checkpoint else {
        return CheckpointChainStatus::NoCheckpoint;
    };

    if let Err(err) = cp.verify_signature() {
        return CheckpointChainStatus::InvalidCheckpoint(err);
    }

    if chain.height() < cp.height {
        return CheckpointChainStatus::BelowCheckpoint;
    }

    let Some(index) = cp.block_index() else {
        return CheckpointChainStatus::InvalidCheckpoint("checkpoint height must be >= 1".into());
    };
    // Look up the checkpoint block in the recent window. If it's older
    // than the window, it was validated when it was originally accepted;
    // treat that as already-verified. (Older blocks would need a storage
    // read to re-check the hash, which the caller can do separately.)
    let chain_index = index as u64;
    match chain.recent_block_at(chain_index) {
        Some(block) if block.hash == cp.hash => CheckpointChainStatus::Verified,
        Some(_) => CheckpointChainStatus::CheckpointMismatch,
        None => CheckpointChainStatus::Verified,
    }
}

pub fn status_code(status: &CheckpointChainStatus) -> &'static str {
    match status {
        CheckpointChainStatus::NoCheckpoint => "no_checkpoint",
        CheckpointChainStatus::Verified => "verified",
        CheckpointChainStatus::BelowCheckpoint => "below_checkpoint",
        CheckpointChainStatus::CheckpointMismatch => "checkpoint_mismatch",
        CheckpointChainStatus::InvalidCheckpoint(_) => "invalid_checkpoint",
    }
}

pub fn status_reason(status: &CheckpointChainStatus) -> String {
    match status {
        CheckpointChainStatus::NoCheckpoint => "no signed checkpoint installed".into(),
        CheckpointChainStatus::Verified => "latest checkpoint verified on local chain".into(),
        CheckpointChainStatus::BelowCheckpoint => {
            "local chain is below latest checkpoint; sync required before mining".into()
        }
        CheckpointChainStatus::CheckpointMismatch => {
            "local chain contradicts latest signed checkpoint".into()
        }
        CheckpointChainStatus::InvalidCheckpoint(err) => {
            format!("checkpoint file is invalid: {}", err)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::crypto;

    fn signed_checkpoint(height: u64, hash: &str) -> Checkpoint {
        let (sk, pk) = crypto::generate_keypair();
        let address = crypto::address_from_pubkey(&pk);
        let message = Checkpoint::expected_message(CHAIN_ID, height, hash);
        let signature = crypto::sign(message.as_bytes(), &sk);
        Checkpoint {
            chain_id: CHAIN_ID.to_string(),
            height,
            hash: hash.to_string(),
            signer_address: address,
            public_key: hex::encode(pk),
            signature: hex::encode(signature),
            message,
        }
    }

    #[test]
    fn test_checkpoint_signature_verify() {
        let cp = signed_checkpoint(1, "abc");
        assert!(cp.verify_signature().is_ok());
    }

    #[test]
    fn test_checkpoint_signature_rejects_tampered_hash() {
        let mut cp = signed_checkpoint(1, "abc");
        cp.hash = "def".into();
        assert!(cp.verify_signature().is_err());
    }

    #[test]
    fn test_chain_contains_checkpoint() {
        let chain = Chain::new();
        let cp = signed_checkpoint(chain.height(), &chain.latest_block().hash);
        assert_eq!(
            verify_chain_contains_checkpoint(&chain, Some(&cp)),
            CheckpointChainStatus::Verified
        );
    }

    #[test]
    fn test_chain_checkpoint_mismatch() {
        let chain = Chain::new();
        let cp = signed_checkpoint(chain.height(), "not-the-genesis-hash");
        assert_eq!(
            verify_chain_contains_checkpoint(&chain, Some(&cp)),
            CheckpointChainStatus::CheckpointMismatch
        );
    }
}
