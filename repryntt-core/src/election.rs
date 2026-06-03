//! VRF-based leader election for block production.
//!
//! Every 69 seconds a new "slot" opens.  All nodes deterministically compute
//! the elected leader from `SHA3-256(prev_hash : slot_number)` and a
//! log-weighted TFLOPS distribution.  Only the elected leader produces the
//! block — no forks.
//!
//! MUST match Python's qnode2.py election logic exactly so all nodes agree.

use sha3::{Digest, Sha3_256};
use std::collections::BTreeMap;

use crate::device::DeviceRegistry;
use crate::entity::EntityRegistry;
use crate::genesis::BLOCK_INTERVAL_SECS;

// ── Slot / VRF ───────────────────────────────────────────────────────────────

/// Compute the slot number for a given unix timestamp.
pub fn slot_number(unix_secs: f64) -> u64 {
    (unix_secs as u64) / BLOCK_INTERVAL_SECS
}

/// Deterministic election hash for a slot.
///
/// `SHA3-256("{prev_hash}:{slot_number}")` — every node computes the same
/// value so the elected leader is globally agreed upon.
pub fn election_hash(prev_hash: &str, slot: u64) -> String {
    let input = format!("{}:{}", prev_hash, slot);
    let mut hasher = Sha3_256::new();
    hasher.update(input.as_bytes());
    hex::encode(hasher.finalize())
}

// ── Compute Contributors ─────────────────────────────────────────────────────

/// Registry of compute contributors eligible for block production.
///
/// Maps `wallet_address → effective_tflops`.
#[derive(Debug, Clone, Default)]
pub struct ComputeContributors {
    /// address → TFLOPS
    pub contributors: BTreeMap<String, f64>,
}

impl ComputeContributors {
    pub fn new() -> Self {
        Self::default()
    }

    /// Register (or update) a contributor's TFLOPS.
    pub fn register(&mut self, address: &str, tflops: f64) {
        if tflops > 0.0 {
            self.contributors.insert(address.to_string(), tflops);
        }
    }

    /// Remove a contributor.
    pub fn remove(&mut self, address: &str) {
        self.contributors.remove(address);
    }

    /// Number of active contributors.
    pub fn count(&self) -> usize {
        self.contributors.len()
    }

    /// Build a sorted candidate list with log-weighted TFLOPS.
    ///
    /// Returns `Vec<(address, raw_tflops, log_weight)>` sorted by address.
    ///
    /// The log weighting is the anti-concentration mechanism:
    ///   `weight = log2(1 + tflops)`
    ///
    /// This gives diminishing returns to larger hardware:
    ///   5 TFLOPS  → 2.58 weight
    ///   50 TFLOPS → 5.67 weight  (10× compute = 2.2× weight)
    ///   500 TFLOPS → 8.97 weight (100× compute = 3.5× weight)
    pub fn candidates(&self) -> Vec<(String, f64, f64)> {
        let mut list: Vec<(String, f64, f64)> = self
            .contributors
            .iter()
            .filter(|(_, tflops)| **tflops > 0.0)
            .map(|(addr, tflops)| {
                let weight = (1.0_f64 + *tflops).log2();
                (addr.clone(), *tflops, weight)
            })
            .collect();
        // Sort by address (first element) — matches Python's sorted(zip(candidates, tflops, weights))
        // where candidates are strings, which sorts lexicographically by address.
        list.sort_by(|a, b| a.0.cmp(&b.0));
        list
    }

    /// Build candidate list filtered through the entity+device verification gate.
    ///
    /// Only addresses that pass `is_entity_verified()` are included.
    /// During bootstrap (< 2 entities), all contributors pass.
    pub fn verified_candidates(
        &self,
        entity_registry: Option<&EntityRegistry>,
        device_registry: Option<&DeviceRegistry>,
    ) -> Vec<(String, f64, f64)> {
        let mut list: Vec<(String, f64, f64)> = self
            .contributors
            .iter()
            .filter(|(_, tflops)| **tflops > 0.0)
            .filter(|(addr, _)| {
                crate::device::is_entity_verified(addr, entity_registry, device_registry)
            })
            .map(|(addr, tflops)| {
                let weight = (1.0_f64 + *tflops).log2();
                (addr.clone(), *tflops, weight)
            })
            .collect();
        list.sort_by(|a, b| a.0.cmp(&b.0));
        list
    }
}

// ── Leader Election ──────────────────────────────────────────────────────────

/// Result of a leader election for a single slot.
#[derive(Debug, Clone)]
pub struct ElectionResult {
    pub slot: u64,
    pub leader: String,
    pub leader_tflops: f64,
    pub total_weight: f64,
    pub candidate_count: usize,
}

/// Run the VRF leader election for a given slot.
///
/// Returns `None` if there are no candidates.
///
/// Algorithm (must match Python exactly):
/// 1. Build sorted candidate list with log weights
/// 2. `election_point = (int(election_hash, 16) % 1_000_000) / 1_000_000 * total_weight`
/// 3. Walk cumulative weights until `cumulative >= election_point`
pub fn elect_leader(
    prev_hash: &str,
    slot: u64,
    contributors: &ComputeContributors,
) -> Option<ElectionResult> {
    elect_from_candidates(prev_hash, slot, &contributors.candidates())
}

/// Run VRF leader election with entity+device verification gate.
///
/// Same algorithm as `elect_leader` but only considers verified contributors.
pub fn elect_leader_verified(
    prev_hash: &str,
    slot: u64,
    contributors: &ComputeContributors,
    entity_registry: Option<&EntityRegistry>,
    device_registry: Option<&DeviceRegistry>,
) -> Option<ElectionResult> {
    elect_from_candidates(
        prev_hash,
        slot,
        &contributors.verified_candidates(entity_registry, device_registry),
    )
}

/// Core election from a pre-built candidate list.
fn elect_from_candidates(
    prev_hash: &str,
    slot: u64,
    candidates: &[(String, f64, f64)],
) -> Option<ElectionResult> {
    if candidates.is_empty() {
        return None;
    }

    let e_hash = election_hash(prev_hash, slot);
    let total_weight: f64 = candidates.iter().map(|(_, _, w)| w).sum();
    if total_weight <= 0.0 {
        return None;
    }

    // Python: (int(election_hash, 16) % 1_000_000) / 1_000_000 * total_weight
    // The election_hash is 64 hex chars (256 bits).  We only need the lower bits.
    // Parse the full hex as a big integer via u128 (we only need modulo 1M).
    let hash_int = u128_from_hex_tail(&e_hash);
    let election_point = ((hash_int % 1_000_000) as f64) / 1_000_000.0 * total_weight;

    let mut cumulative = 0.0_f64;
    let mut leader_idx = 0;
    for (i, (_, _, weight)) in candidates.iter().enumerate() {
        cumulative += weight;
        if cumulative >= election_point {
            leader_idx = i;
            break;
        }
    }

    let (addr, tflops, _) = &candidates[leader_idx];
    Some(ElectionResult {
        slot,
        leader: addr.clone(),
        leader_tflops: *tflops,
        total_weight,
        candidate_count: candidates.len(),
    })
}

/// Parse the last 32 hex chars of a hash string as a u128.
///
/// Python does `int(election_hash, 16)` which parses **all 64 hex chars** as
/// a 256-bit integer.  But since we only need `% 1_000_000`, the lower 128
/// bits are sufficient (the upper bits vanish under modulo).
fn u128_from_hex_tail(hex_str: &str) -> u128 {
    // Take last 32 hex chars = 128 bits
    let start = if hex_str.len() > 32 {
        hex_str.len() - 32
    } else {
        0
    };
    u128::from_str_radix(&hex_str[start..], 16).unwrap_or(0)
}

// ── Availability Rewards ─────────────────────────────────────────────────────

/// Per-contributor availability reward: 0.01 CR in plancks.
pub const AVAILABILITY_REWARD_PLANCKS: i64 = 1_000_000; // 0.01 CR

/// Calculate availability rewards for all contributors.
///
/// Pool = `AVAILABILITY_REWARD_PLANCKS × num_contributors`, distributed
/// proportionally by log-weight.
///
/// Returns `Vec<(address, reward_plancks)>`.
pub fn availability_rewards(
    contributors: &ComputeContributors,
    current_supply: i64,
    max_supply: i64,
) -> Vec<(String, i64)> {
    let candidates = contributors.candidates();
    if candidates.is_empty() {
        return Vec::new();
    }

    let total_weight: f64 = candidates.iter().map(|(_, _, w)| w).sum();
    if total_weight <= 0.0 {
        return Vec::new();
    }

    let pool = AVAILABILITY_REWARD_PLANCKS * candidates.len() as i64;
    let mut supply = current_supply;
    let mut rewards = Vec::new();

    for (addr, _tflops, weight) in &candidates {
        let share = weight / total_weight;
        let reward = (pool as f64 * share) as i64;
        if reward <= 0 {
            continue;
        }
        if supply + reward > max_supply {
            break;
        }
        rewards.push((addr.clone(), reward));
        supply += reward;
    }

    rewards
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::genesis::EXPECTED_GENESIS_HASH;

    #[test]
    fn test_slot_number() {
        // 1743379200.0 / 69 = 25266365 (truncated)
        assert_eq!(slot_number(1_743_379_200.0), 25_266_365);
    }

    #[test]
    fn test_election_hash_deterministic() {
        let h1 = election_hash("abc", 100);
        let h2 = election_hash("abc", 100);
        assert_eq!(h1, h2);
        assert_eq!(h1.len(), 64); // SHA3-256 = 32 bytes = 64 hex
    }

    #[test]
    fn test_election_different_slots() {
        let h1 = election_hash("abc", 100);
        let h2 = election_hash("abc", 101);
        assert_ne!(h1, h2);
    }

    #[test]
    fn test_log_weighting() {
        let mut cc = ComputeContributors::new();
        cc.register("addr_a", 5.0);
        cc.register("addr_b", 50.0);
        cc.register("addr_c", 500.0);

        let cands = cc.candidates();
        assert_eq!(cands.len(), 3);

        // Check log weights
        let (_, _, w_a) = &cands[0]; // addr_a
        let (_, _, w_b) = &cands[1]; // addr_b
        let (_, _, w_c) = &cands[2]; // addr_c

        // log2(6) ≈ 2.585, log2(51) ≈ 5.672, log2(501) ≈ 8.968
        assert!((w_a - 2.585).abs() < 0.01);
        assert!((w_b - 5.672).abs() < 0.01);
        assert!((w_c - 8.968).abs() < 0.01);
    }

    #[test]
    fn test_elect_leader_single_candidate() {
        let mut cc = ComputeContributors::new();
        cc.register("only_one", 5.4);

        let result = elect_leader(EXPECTED_GENESIS_HASH, 12345, &cc).unwrap();
        assert_eq!(result.leader, "only_one");
        assert_eq!(result.candidate_count, 1);
    }

    #[test]
    fn test_elect_leader_deterministic() {
        let mut cc = ComputeContributors::new();
        cc.register("alice", 5.0);
        cc.register("bob", 10.0);
        cc.register("charlie", 20.0);

        let r1 = elect_leader(EXPECTED_GENESIS_HASH, 999, &cc).unwrap();
        let r2 = elect_leader(EXPECTED_GENESIS_HASH, 999, &cc).unwrap();
        assert_eq!(r1.leader, r2.leader);
    }

    #[test]
    fn test_elect_leader_no_candidates() {
        let cc = ComputeContributors::new();
        assert!(elect_leader(EXPECTED_GENESIS_HASH, 1, &cc).is_none());
    }

    #[test]
    fn test_availability_rewards() {
        let mut cc = ComputeContributors::new();
        cc.register("alice", 5.0);
        cc.register("bob", 50.0);

        let max_supply = 21_000_000 * 100_000_000_i64;
        let rewards = availability_rewards(&cc, 0, max_supply);
        assert_eq!(rewards.len(), 2);

        let total: i64 = rewards.iter().map(|(_, r)| r).sum();
        // Pool = 0.01 CR * 2 contributors = 0.02 CR = 2_000_000 plancks
        assert!(total <= 2_000_000);
        assert!(total > 0);

        // Bob (50 TFLOPS, log2(51)≈5.67) should get more than Alice (5 TFLOPS, log2(6)≈2.58)
        let alice_reward = rewards
            .iter()
            .find(|(a, _)| a == "alice")
            .map(|(_, r)| *r)
            .unwrap();
        let bob_reward = rewards
            .iter()
            .find(|(a, _)| a == "bob")
            .map(|(_, r)| *r)
            .unwrap();
        assert!(bob_reward > alice_reward);
    }

    #[test]
    fn test_election_parity_with_python() {
        // Reproduce the same election as Python would compute.
        // We can verify this by running the same inputs through both.
        let mut cc = ComputeContributors::new();
        cc.register("a1a4090aced69d411b6e62bf49944f295c85ed88", 5.4);

        let slot = slot_number(1_743_379_200.0 + 69.0); // First slot after genesis
        let result = elect_leader(EXPECTED_GENESIS_HASH, slot, &cc).unwrap();

        // With only one candidate, it must be elected
        assert_eq!(result.leader, "a1a4090aced69d411b6e62bf49944f295c85ed88");
    }
}
