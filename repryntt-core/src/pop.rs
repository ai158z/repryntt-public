//! Proof of Power — workload verification and reward calculation.
//!
//! Matches Python's `proof_of_power.py` exactly:
//! - Proof generation: SHA3-512 hash binding workload→miner→result
//! - Proof verification: 10-step validation with anti-fraud checks
//! - Quality scoring: 0.0–1.0 composite (time + richness + entropy + attestation)
//! - Reward calculation: base × halving × TFLOPS_mult × quality

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha3::{Digest, Sha3_256, Sha3_512};
use std::collections::BTreeMap;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::genesis::{BASE_REWARD_PLANCKS, HALVING_INTERVAL};

// ── Constants ────────────────────────────────────────────────────────────────

pub const MIN_COMPUTATION_TIME: f64 = 0.01; // 10ms minimum
pub const MAX_COMPUTATION_TIME: f64 = 600.0; // 10 minutes maximum
pub const BASELINE_TFLOPS: f64 = 20.0; // Fallback reference
pub const TOKENS_PER_TFLOP_SECOND: f64 = 50.0; // Rough: 50 tokens/s at 20 TFLOPS
pub const MAX_PLAUSIBLE_TFLOPS: f64 = 500.0; // Ceiling for 2026 hardware
pub const PROOF_VERSION: &str = "3.1";

// ── Device Info ──────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DeviceInfo {
    pub device_type: String, // "cuda" | "rocm" | "mps" | "cpu"
    pub device_name: String,
    pub tflops_fp16: f64,
    pub tflops_fp32: f64,
    pub memory_gb: f64,
    pub benchmark_time_s: f64,
}

impl Default for DeviceInfo {
    fn default() -> Self {
        Self {
            device_type: "cpu".to_string(),
            device_name: "Unknown".to_string(),
            tflops_fp16: 0.0,
            tflops_fp32: 0.0,
            memory_gb: 0.0,
            benchmark_time_s: 0.0,
        }
    }
}

impl DeviceInfo {
    /// The effective TFLOPS (max of FP16 and FP32).
    pub fn effective_tflops(&self) -> f64 {
        self.tflops_fp16.max(self.tflops_fp32).max(0.001)
    }

    /// Is this a GPU device?
    pub fn is_gpu(&self) -> bool {
        matches!(self.device_type.as_str(), "cuda" | "rocm" | "mps" | "xpu")
    }
}

// ── Proof of Power ───────────────────────────────────────────────────────────

/// A Proof of Power that binds a workload computation to its miner.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProofOfPower {
    pub proof_hash: String,
    pub workload_key: String,
    pub data_hash: String,
    pub result_hash: String,
    pub miner_address: String,
    pub computation_time: f64,
    pub method: String,
    pub timestamp: f64,
    pub version: String,
    pub device_type: String,
    pub gpu_backend: String,
    pub tflops_measured: f64,
    pub hw_attestation_hash: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub challenge_response: Option<String>,
}

impl ProofOfPower {
    /// Generate a proof from a completed workload.
    ///
    /// `device`: the node's REAL measured device info (never trust caller claims).
    pub fn generate(
        workload_key: &str,
        workload_data: &str,
        computation_result: &str,
        miner_address: &str,
        computation_time: f64,
        method: &str,
        device: &DeviceInfo,
        hw_attestation_hash: &str,
    ) -> Self {
        let now = now_f64();

        let data_hash = hash_data(workload_data);
        let result_hash = hash_data(computation_result);

        // Proof hash: ties workload → miner → result cryptographically
        let proof_input = format!(
            "{}:{}:{}:{}:{}",
            workload_key, data_hash, result_hash, miner_address, computation_time
        );
        let proof_hash = sha3_512_hex(proof_input.as_bytes());

        // Challenge-response slice
        let challenge_response = if computation_result.len() >= 10 {
            let proof_bytes = hex::decode(&proof_hash[..16]).unwrap_or_default();
            if proof_bytes.len() >= 4 {
                let offset_raw = u32::from_be_bytes([
                    proof_bytes[0],
                    proof_bytes[1],
                    proof_bytes[2],
                    proof_bytes[3],
                ]);
                let max_offset = computation_result.len().saturating_sub(32).max(1);
                let offset = (offset_raw as usize) % max_offset;
                let length = 64.min(computation_result.len() - offset);
                let slice = &computation_result[offset..offset + length];
                let challenge_input = format!("{}:{}", proof_hash, slice);
                let ch = sha3_256_hex(challenge_input.as_bytes());
                Some(ch[..32].to_string())
            } else {
                None
            }
        } else {
            None
        };

        Self {
            proof_hash,
            workload_key: workload_key.to_string(),
            data_hash,
            result_hash,
            miner_address: miner_address.to_string(),
            computation_time,
            method: method.to_string(),
            timestamp: now,
            version: PROOF_VERSION.to_string(),
            device_type: device.device_type.clone(),
            gpu_backend: device.device_name.clone(),
            tflops_measured: device.effective_tflops(),
            hw_attestation_hash: hw_attestation_hash.to_string(),
            challenge_response,
        }
    }

    /// Verify a proof against the original workload + result.
    ///
    /// Returns `(is_valid, quality_factor, reason)`.
    pub fn verify(
        &self,
        workload_data: &str,
        computation_result: &str,
        expected_miner: &str,
    ) -> (bool, f64, String) {
        // 1. Miner identity
        if self.miner_address != expected_miner {
            return (false, 0.0, "Miner address mismatch".into());
        }

        // 2. Data hash
        let expected_data = hash_data(workload_data);
        if self.data_hash != expected_data {
            return (false, 0.0, "Data hash mismatch".into());
        }

        // 3. Result hash
        let expected_result = hash_data(computation_result);
        if self.result_hash != expected_result {
            return (false, 0.0, "Result hash mismatch".into());
        }

        // 4. Proof hash integrity
        let proof_input = format!(
            "{}:{}:{}:{}:{}",
            self.workload_key,
            self.data_hash,
            self.result_hash,
            self.miner_address,
            self.computation_time
        );
        let expected_proof_hash = sha3_512_hex(proof_input.as_bytes());
        if self.proof_hash != expected_proof_hash {
            return (false, 0.0, "Proof hash integrity check failed".into());
        }

        // 5. Computation time plausibility
        if self.computation_time < MIN_COMPUTATION_TIME {
            return (
                false,
                0.0,
                format!("Computation time too fast ({:.4}s)", self.computation_time),
            );
        }
        if self.computation_time > MAX_COMPUTATION_TIME {
            return (
                false,
                0.0,
                format!("Computation time too slow ({:.1}s)", self.computation_time),
            );
        }

        // 6. Non-trivial result
        if computation_result.len() < 2 {
            return (false, 0.0, "Trivial/empty result".into());
        }

        // 7. Challenge-response verification
        if !self.verify_challenge(computation_result) {
            return (false, 0.0, "Challenge-response verification failed".into());
        }

        // 8. TFLOPS fraud detection
        if self.tflops_measured > 0.0 && self.computation_time > 1.0 {
            let expected_min_chars =
                self.tflops_measured * TOKENS_PER_TFLOP_SECOND * self.computation_time * 0.005;
            if (computation_result.len() as f64) < expected_min_chars
                && computation_result.len() < 50
            {
                return (
                    false,
                    0.0,
                    format!(
                        "TFLOPS fraud: {:.1} TFLOPS declared, but only {} chars output",
                        self.tflops_measured,
                        computation_result.len()
                    ),
                );
            }
        }

        // 9. TFLOPS ceiling
        if self.tflops_measured > MAX_PLAUSIBLE_TFLOPS {
            return (
                false,
                0.0,
                format!(
                    "TFLOPS implausible: {:.1} exceeds maximum {}",
                    self.tflops_measured, MAX_PLAUSIBLE_TFLOPS
                ),
            );
        }

        // 10. Quality factor
        let quality = self.calculate_quality(computation_result);

        (true, quality, String::new())
    }

    /// Verify the challenge-response slice.
    fn verify_challenge(&self, result: &str) -> bool {
        let Some(expected_cr) = &self.challenge_response else {
            // No challenge present — pass if result is too short to challenge
            return result.len() < 10;
        };

        if result.len() < 10 {
            return false; // Challenge present but result too short
        }

        let proof_bytes = match hex::decode(&self.proof_hash[..16]) {
            Ok(b) if b.len() >= 4 => b,
            _ => return false,
        };

        let offset_raw = u32::from_be_bytes([
            proof_bytes[0],
            proof_bytes[1],
            proof_bytes[2],
            proof_bytes[3],
        ]);
        let max_offset = result.len().saturating_sub(32).max(1);
        let offset = (offset_raw as usize) % max_offset;
        let length = 64.min(result.len() - offset);
        let slice = &result[offset..offset + length];
        let challenge_input = format!("{}:{}", self.proof_hash, slice);
        let computed = sha3_256_hex(challenge_input.as_bytes());
        &computed[..32] == expected_cr.as_str()
    }

    /// Calculate quality factor (0.0–1.0).
    ///
    /// Scoring:
    ///   0.25 base
    ///   0.20 computation time
    ///   0.20 result richness
    ///   0.15 result entropy
    ///   0.20 device attestation
    fn calculate_quality(&self, result: &str) -> f64 {
        let mut quality = 0.25_f64; // Base

        // Time factor (max 0.20)
        if self.computation_time < 0.5 {
            quality += 0.04;
        } else if self.computation_time <= 30.0 {
            quality += 0.08 + (self.computation_time / 250.0).min(0.12);
        } else {
            quality += 0.16;
        }

        // Result richness (max 0.20)
        let result_len = result.len();
        if result_len > 50 {
            quality += (0.04 + result_len as f64 / 25000.0).min(0.20);
        }

        // Entropy check (max 0.15)
        if result_len > 10 {
            let sample: Vec<char> = result.chars().take(2000).collect();
            let unique: std::collections::HashSet<char> = sample.iter().copied().collect();
            let entropy_ratio = unique.len() as f64 / sample.len().min(2000) as f64;
            quality += (entropy_ratio * 0.4).min(0.15);
        }

        // Device attestation (max 0.20)
        if !self.hw_attestation_hash.is_empty() && self.hw_attestation_hash.len() >= 16 {
            let gpu_types = ["cuda", "rocm", "mps", "xpu"];
            if gpu_types.contains(&self.device_type.as_str()) {
                quality += 0.10;
                if (0.5..=500.0).contains(&self.tflops_measured) {
                    quality += (self.tflops_measured / BASELINE_TFLOPS * 0.05).min(0.10);
                }
            } else if self.device_type == "cpu" && self.tflops_measured > 0.0 {
                quality += 0.03;
            }
        }

        (quality.max(0.0).min(1.0) * 10000.0).round() / 10000.0
    }

    /// Convert to dict for serialisation / storage.
    pub fn to_dict(&self) -> BTreeMap<String, Value> {
        let mut d: BTreeMap<String, Value> = BTreeMap::new();
        d.insert("proof_hash".into(), Value::String(self.proof_hash.clone()));
        d.insert(
            "workload_key".into(),
            Value::String(self.workload_key.clone()),
        );
        d.insert("data_hash".into(), Value::String(self.data_hash.clone()));
        d.insert(
            "result_hash".into(),
            Value::String(self.result_hash.clone()),
        );
        d.insert(
            "miner_address".into(),
            Value::String(self.miner_address.clone()),
        );
        d.insert("computation_time".into(), json_f64(self.computation_time));
        d.insert("method".into(), Value::String(self.method.clone()));
        d.insert("timestamp".into(), json_f64(self.timestamp));
        d.insert("version".into(), Value::String(self.version.clone()));
        d.insert(
            "device_type".into(),
            Value::String(self.device_type.clone()),
        );
        d.insert(
            "gpu_backend".into(),
            Value::String(self.gpu_backend.clone()),
        );
        d.insert("tflops_measured".into(), json_f64(self.tflops_measured));
        d.insert(
            "hw_attestation_hash".into(),
            Value::String(self.hw_attestation_hash.clone()),
        );
        if let Some(cr) = &self.challenge_response {
            d.insert("challenge_response".into(), Value::String(cr.clone()));
        }
        d
    }
}

// ── Reward Calculation ───────────────────────────────────────────────────────

/// Calculate the dynamic mining reward for a workload proof.
///
/// `reward = base_reward × halving_factor × tflops_mult × quality`
pub fn calculate_reward(workload_tflops: f64, quality_factor: f64, block_height: u64) -> i64 {
    let halvings = block_height / HALVING_INTERVAL;
    let halving_factor = if halvings >= 64 {
        0.0
    } else {
        1.0 / (1u64 << halvings) as f64
    };

    // TFLOPS multiplier: capped at 2×
    let tflops_mult = (0.5 + workload_tflops / BASELINE_TFLOPS).clamp(0.5, 2.0);

    // Quality multiplier: floor at 0.1
    let quality_mult = quality_factor.max(0.1);

    let reward = BASE_REWARD_PLANCKS as f64 * halving_factor * tflops_mult * quality_mult;
    (reward as i64).max(1)
}

// ── Helpers ──────────────────────────────────────────────────────────────────

fn hash_data(data: &str) -> String {
    sha3_512_hex(data.as_bytes())
}

fn sha3_512_hex(data: &[u8]) -> String {
    let mut hasher = Sha3_512::new();
    hasher.update(data);
    hex::encode(hasher.finalize())
}

fn sha3_256_hex(data: &[u8]) -> String {
    let mut hasher = Sha3_256::new();
    hasher.update(data);
    hex::encode(hasher.finalize())
}

fn json_f64(v: f64) -> Value {
    serde_json::Number::from_f64(v)
        .map(Value::Number)
        .unwrap_or(Value::Null)
}

fn now_f64() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("time went backwards")
        .as_secs_f64()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_device() -> DeviceInfo {
        DeviceInfo {
            device_type: "cuda".into(),
            device_name: "Orin".into(),
            tflops_fp16: 5.4,
            tflops_fp32: 1.3,
            memory_gb: 7.4,
            benchmark_time_s: 0.5,
        }
    }

    #[test]
    fn test_generate_and_verify_proof() {
        let device = test_device();
        let proof = ProofOfPower::generate(
            "workload_001",
            "input data for the computation that is long enough to test",
            "result output from the computation that is also fairly long and varied with numbers 12345 and symbols !@#$%",
            "miner_abc",
            2.5,
            "deterministic",
            &device,
            "aabbccdd11223344aabbccdd11223344",
        );

        assert_eq!(proof.proof_hash.len(), 128); // SHA3-512
        assert_eq!(proof.miner_address, "miner_abc");
        assert_eq!(proof.device_type, "cuda");
        assert_eq!(proof.tflops_measured, 5.4);
        assert!(proof.challenge_response.is_some());

        let (valid, quality, reason) = proof.verify(
            "input data for the computation that is long enough to test",
            "result output from the computation that is also fairly long and varied with numbers 12345 and symbols !@#$%",
            "miner_abc",
        );
        assert!(valid, "Proof should be valid: {}", reason);
        assert!(
            quality > 0.25,
            "Quality should be > base 0.25, got {}",
            quality
        );
    }

    #[test]
    fn test_verify_rejects_wrong_miner() {
        let device = test_device();
        let proof = ProofOfPower::generate(
            "wk",
            "data_long_enough",
            "result_long_enough_here",
            "real_miner",
            1.0,
            "deterministic",
            &device,
            "",
        );
        let (valid, _, reason) =
            proof.verify("data_long_enough", "result_long_enough_here", "fake_miner");
        assert!(!valid);
        assert!(reason.contains("Miner address mismatch"));
    }

    #[test]
    fn test_verify_rejects_tampered_result() {
        let device = test_device();
        let proof = ProofOfPower::generate(
            "wk",
            "data_long_enough",
            "correct_result_here",
            "miner",
            1.0,
            "deterministic",
            &device,
            "",
        );
        let (valid, _, reason) = proof.verify("data_long_enough", "tampered_result", "miner");
        assert!(!valid);
        assert!(reason.contains("Result hash mismatch"));
    }

    #[test]
    fn test_verify_rejects_implausible_tflops() {
        let mut device = test_device();
        device.tflops_fp16 = 999.0; // Way over ceiling
        let proof = ProofOfPower::generate(
            "wk",
            "some_workload_data",
            "some_result_data_here",
            "miner",
            1.0,
            "deterministic",
            &device,
            "",
        );
        let (valid, _, reason) =
            proof.verify("some_workload_data", "some_result_data_here", "miner");
        assert!(!valid);
        assert!(reason.contains("TFLOPS implausible"));
    }

    #[test]
    fn test_reward_halving() {
        let r0 = calculate_reward(5.0, 0.5, 0);
        let r1 = calculate_reward(5.0, 0.5, 420_000);
        // After first halving, reward should be ~half
        assert!(r1 < r0);
        assert!((r1 as f64 / r0 as f64 - 0.5).abs() < 0.01);
    }

    #[test]
    fn test_reward_quality_scaling() {
        let r_low = calculate_reward(5.0, 0.1, 0);
        let r_high = calculate_reward(5.0, 1.0, 0);
        // Higher quality → higher reward
        assert!(r_high > r_low);
    }

    #[test]
    fn test_quality_factors() {
        let device = test_device();
        let proof = ProofOfPower::generate(
            "wk",
            "some big data input that is long enough",
            &"A".repeat(5000), // Rich result
            "miner",
            10.0, // Reasonable time
            "deterministic",
            &device,
            "aabbccdd11223344aabbccdd11223344", // HW attestation
        );
        let quality = proof.calculate_quality(&"A".repeat(5000));
        // Should have: base(0.25) + time(~0.12) + richness(0.20) + entropy(LOW, A repeated)
        // + attestation(0.10 GPU + ~0.01 tflops)
        assert!(quality > 0.4, "quality {} should be > 0.4", quality);
    }
}
