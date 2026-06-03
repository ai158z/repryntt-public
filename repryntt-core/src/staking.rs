//! Staking manager — stake/unstake, reputation, availability rewards, slashing.
//!
//! Faithfully ports the staking logic from Python `qnode2.py`:
//! - Stake/unstake with Ed25519 signature verification
//! - Availability rewards: 0.01 CR per block per qualifying staker
//! - Slashing: 10% stake penalty for invalid work
//! - Reputation: 0.0–1.0 score tracking miner quality
//! - Leaderboard: top miners by earnings

use std::collections::HashMap;

use crate::transaction::PLANCKS_PER_CREDIT;

// ── Constants ────────────────────────────────────────────────────────────────

/// Minimum stake to be a validator: 1 CR.
pub const MIN_STAKE_PLANCKS: i64 = PLANCKS_PER_CREDIT;

/// Availability reward per block per staked miner: 0.01 CR.
pub const AVAILABILITY_REWARD_PLANCKS: i64 = PLANCKS_PER_CREDIT / 100; // 1_000_000

/// Maximum supply in plancks (21M CR).
pub const MAX_SUPPLY_PLANCKS: i64 = 21_000_000 * PLANCKS_PER_CREDIT;

/// Default reputation for new miners.
pub const DEFAULT_REPUTATION: f64 = 0.5;

/// Reputation increase per successful action (scaled by quality).
pub const REP_SUCCESS_DELTA: f64 = 0.01;

/// Reputation decrease per failure.
pub const REP_FAILURE_DELTA: f64 = 0.05;

/// Slash rate: fraction of stake taken as penalty.
pub const SLASH_RATE: f64 = 0.10; // 10%

/// Minimum slash amount: 0.1 CR.
pub const MIN_SLASH_PLANCKS: i64 = PLANCKS_PER_CREDIT / 10; // 10_000_000

/// Stake pool address.
pub const STAKE_POOL: &str = "STAKE_POOL";

/// System address (minting source).
pub const SYSTEM_ADDR: &str = "SYSTEM";

/// DAO treasury address.
pub const DAO_ADDR: &str = "DAO";

// ── Staking Manager ──────────────────────────────────────────────────────────

/// Manages staking state: stakes, reputation, and availability rewards.
///
/// This sits on top of `Chain.stakes` and `Chain.balances`, providing
/// the higher-level operations Python's `qnode2.py` handles inline.
pub struct StakingManager {
    /// Miner reputation scores: address → score (0.0 to 1.0).
    pub reputation: HashMap<String, f64>,
    /// Cumulative earnings tracked for leaderboard: address → plancks earned.
    pub earnings: HashMap<String, i64>,
    /// Workloads completed per miner: address → count.
    pub workloads_completed: HashMap<String, u64>,
}

impl StakingManager {
    pub fn new() -> Self {
        Self {
            reputation: HashMap::new(),
            earnings: HashMap::new(),
            workloads_completed: HashMap::new(),
        }
    }

    // ── Stake / Unstake ─────────────────────────────────────────

    /// Stake `amount` plancks from `address`'s balance into the stake pool.
    ///
    /// `balances` and `stakes` are the chain's mutable state maps.
    pub fn stake(
        &self,
        address: &str,
        amount: i64,
        balances: &mut HashMap<String, i64>,
        stakes: &mut HashMap<String, i64>,
    ) -> Result<(), String> {
        if amount <= 0 {
            return Err("Stake amount must be positive".into());
        }
        if amount < MIN_STAKE_PLANCKS {
            return Err(format!(
                "Minimum stake is {} plancks (1 CR)",
                MIN_STAKE_PLANCKS
            ));
        }

        let balance = balances.get(address).copied().unwrap_or(0);
        if balance < amount {
            return Err(format!("Insufficient balance: {} < {}", balance, amount));
        }

        *balances.get_mut(address).unwrap() -= amount;
        *stakes.entry(address.to_string()).or_insert(0) += amount;
        Ok(())
    }

    /// Unstake `amount` plancks from `address`'s stake back to balance.
    pub fn unstake(
        &self,
        address: &str,
        amount: i64,
        balances: &mut HashMap<String, i64>,
        stakes: &mut HashMap<String, i64>,
    ) -> Result<UnstakeResult, String> {
        if amount <= 0 {
            return Err("Unstake amount must be positive".into());
        }

        let staked = stakes.get(address).copied().unwrap_or(0);
        if staked < amount {
            return Err(format!("Insufficient stake: {} < {}", staked, amount));
        }

        let new_stake = staked - amount;
        if new_stake > 0 {
            stakes.insert(address.to_string(), new_stake);
        } else {
            stakes.remove(address);
        }
        *balances.entry(address.to_string()).or_insert(0) += amount;

        Ok(UnstakeResult {
            unstaked: amount,
            remaining_stake: new_stake,
            new_balance: balances.get(address).copied().unwrap_or(0),
        })
    }

    /// Check if an address qualifies as a validator (meets minimum stake).
    pub fn is_validator(&self, address: &str, stakes: &HashMap<String, i64>) -> bool {
        stakes.get(address).copied().unwrap_or(0) >= MIN_STAKE_PLANCKS
    }

    /// Get all qualifying validators (stake >= MIN_STAKE_PLANCKS).
    pub fn validators(&self, stakes: &HashMap<String, i64>) -> Vec<String> {
        stakes
            .iter()
            .filter(|(_, s)| **s >= MIN_STAKE_PLANCKS)
            .map(|(a, _)| a.clone())
            .collect()
    }

    // ── Availability Rewards ────────────────────────────────────

    /// Calculate availability rewards for all qualifying stakers.
    ///
    /// Returns a list of (address, reward_plancks) pairs, respecting the
    /// supply cap. `current_supply` is the sum of all balances + all stakes.
    pub fn calculate_availability_rewards(
        &self,
        stakes: &HashMap<String, i64>,
        current_supply: i64,
    ) -> Vec<(String, i64)> {
        let mut rewards = Vec::new();
        let mut supply = current_supply;

        // Deterministic ordering: sort by address
        let mut validators: Vec<_> = stakes
            .iter()
            .filter(|(_, s)| **s >= MIN_STAKE_PLANCKS)
            .map(|(a, _)| a.clone())
            .collect();
        validators.sort();

        for addr in validators {
            if supply + AVAILABILITY_REWARD_PLANCKS > MAX_SUPPLY_PLANCKS {
                break;
            }
            rewards.push((addr, AVAILABILITY_REWARD_PLANCKS));
            supply += AVAILABILITY_REWARD_PLANCKS;
        }
        rewards
    }

    // ── Slashing ────────────────────────────────────────────────

    /// Slash a miner's stake for invalid work.
    ///
    /// Penalty = max(min(10% of stake, stake), MIN_SLASH_PLANCKS).
    /// The slash amount is removed from the miner's stake (burned / sent to DAO).
    /// Returns the actual amount slashed, or 0 if no stake.
    pub fn slash(
        &mut self,
        miner_address: &str,
        reason: &str,
        stakes: &mut HashMap<String, i64>,
        balances: &mut HashMap<String, i64>,
    ) -> SlashResult {
        let staked = stakes.get(miner_address).copied().unwrap_or(0);
        if staked == 0 {
            return SlashResult {
                slashed: 0,
                remaining_stake: 0,
                reason: reason.to_string(),
            };
        }

        // 10% of stake, clamped to [MIN_SLASH, stake]
        let raw_penalty = (staked as f64 * SLASH_RATE) as i64;
        let penalty = raw_penalty.max(MIN_SLASH_PLANCKS).min(staked);

        let new_stake = staked - penalty;
        if new_stake > 0 {
            stakes.insert(miner_address.to_string(), new_stake);
        } else {
            stakes.remove(miner_address);
        }

        // Slashed funds go to DAO treasury
        *balances.entry(DAO_ADDR.to_string()).or_insert(0) += penalty;

        // Reputation hit
        self.update_reputation(miner_address, false, 1.0);

        SlashResult {
            slashed: penalty,
            remaining_stake: new_stake.max(0),
            reason: reason.to_string(),
        }
    }

    // ── Reputation ──────────────────────────────────────────────

    /// Update a miner's reputation based on performance.
    pub fn update_reputation(&mut self, address: &str, success: bool, quality: f64) {
        let current = self
            .reputation
            .get(address)
            .copied()
            .unwrap_or(DEFAULT_REPUTATION);

        let new_rep = if success {
            (current + REP_SUCCESS_DELTA * quality).min(1.0)
        } else {
            (current - REP_FAILURE_DELTA).max(0.0)
        };

        self.reputation.insert(address.to_string(), new_rep);
    }

    /// Get a miner's reputation.
    pub fn get_reputation(&self, address: &str) -> f64 {
        self.reputation
            .get(address)
            .copied()
            .unwrap_or(DEFAULT_REPUTATION)
    }

    // ── Earnings Tracking ───────────────────────────────────────

    /// Record earnings for a miner (for leaderboard).
    pub fn record_earning(&mut self, address: &str, amount: i64) {
        *self.earnings.entry(address.to_string()).or_insert(0) += amount;
    }

    /// Record a workload completion.
    pub fn record_workload(&mut self, address: &str) {
        *self
            .workloads_completed
            .entry(address.to_string())
            .or_insert(0) += 1;
    }

    /// Get the leaderboard: top miners by total earnings.
    pub fn leaderboard(
        &self,
        stakes: &HashMap<String, i64>,
        top_n: usize,
    ) -> Vec<LeaderboardEntry> {
        let mut entries: Vec<_> = self
            .earnings
            .iter()
            .map(|(addr, &earned)| LeaderboardEntry {
                address: addr.clone(),
                total_earned_plancks: earned,
                workloads_completed: self.workloads_completed.get(addr).copied().unwrap_or(0),
                reputation: self.get_reputation(addr),
                stake_plancks: stakes.get(addr).copied().unwrap_or(0),
            })
            .collect();

        entries.sort_by(|a, b| b.total_earned_plancks.cmp(&a.total_earned_plancks));
        entries.truncate(top_n);
        entries
    }

    // ── Serialization ───────────────────────────────────────────

    pub fn to_dict(&self) -> serde_json::Value {
        let rep: serde_json::Map<String, serde_json::Value> = self
            .reputation
            .iter()
            .map(|(k, v)| (k.clone(), serde_json::json!(*v)))
            .collect();
        let earn: serde_json::Map<String, serde_json::Value> = self
            .earnings
            .iter()
            .map(|(k, v)| (k.clone(), serde_json::json!(*v)))
            .collect();
        let wc: serde_json::Map<String, serde_json::Value> = self
            .workloads_completed
            .iter()
            .map(|(k, v)| (k.clone(), serde_json::json!(*v)))
            .collect();

        serde_json::json!({
            "reputation": rep,
            "earnings": earn,
            "workloads_completed": wc,
        })
    }

    pub fn from_dict(v: &serde_json::Value) -> Self {
        let mut mgr = Self::new();

        if let Some(rep) = v["reputation"].as_object() {
            for (k, rv) in rep {
                if let Some(r) = rv.as_f64() {
                    mgr.reputation.insert(k.clone(), r);
                }
            }
        }

        if let Some(earn) = v["earnings"].as_object() {
            for (k, ev) in earn {
                if let Some(e) = ev.as_i64() {
                    mgr.earnings.insert(k.clone(), e);
                }
            }
        }

        if let Some(wc) = v["workloads_completed"].as_object() {
            for (k, wv) in wc {
                if let Some(w) = wv.as_u64() {
                    mgr.workloads_completed.insert(k.clone(), w);
                }
            }
        }

        mgr
    }
}

impl Default for StakingManager {
    fn default() -> Self {
        Self::new()
    }
}

// ── Result types ─────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct UnstakeResult {
    pub unstaked: i64,
    pub remaining_stake: i64,
    pub new_balance: i64,
}

#[derive(Debug, Clone)]
pub struct SlashResult {
    pub slashed: i64,
    pub remaining_stake: i64,
    pub reason: String,
}

#[derive(Debug, Clone)]
pub struct LeaderboardEntry {
    pub address: String,
    pub total_earned_plancks: i64,
    pub workloads_completed: u64,
    pub reputation: f64,
    pub stake_plancks: i64,
}

impl LeaderboardEntry {
    pub fn total_earned_cr(&self) -> f64 {
        self.total_earned_plancks as f64 / PLANCKS_PER_CREDIT as f64
    }

    pub fn stake_cr(&self) -> f64 {
        self.stake_plancks as f64 / PLANCKS_PER_CREDIT as f64
    }
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn setup() -> (StakingManager, HashMap<String, i64>, HashMap<String, i64>) {
        let mgr = StakingManager::new();
        let mut balances = HashMap::new();
        balances.insert("alice".to_string(), 100 * PLANCKS_PER_CREDIT);
        balances.insert("bob".to_string(), 50 * PLANCKS_PER_CREDIT);
        balances.insert(DAO_ADDR.to_string(), 0);
        let stakes = HashMap::new();
        (mgr, balances, stakes)
    }

    // ── Staking ─────────────────────────────────────────────────

    #[test]
    fn test_stake_basic() {
        let (mgr, mut balances, mut stakes) = setup();
        let initial_bal = balances["alice"];

        mgr.stake("alice", 10 * PLANCKS_PER_CREDIT, &mut balances, &mut stakes)
            .unwrap();

        assert_eq!(stakes["alice"], 10 * PLANCKS_PER_CREDIT);
        assert_eq!(balances["alice"], initial_bal - 10 * PLANCKS_PER_CREDIT);
    }

    #[test]
    fn test_stake_insufficient_balance() {
        let (mgr, mut balances, mut stakes) = setup();
        assert!(
            mgr.stake(
                "alice",
                200 * PLANCKS_PER_CREDIT,
                &mut balances,
                &mut stakes
            )
            .is_err()
        );
    }

    #[test]
    fn test_stake_below_minimum() {
        let (mgr, mut balances, mut stakes) = setup();
        assert!(
            mgr.stake("alice", MIN_STAKE_PLANCKS - 1, &mut balances, &mut stakes)
                .is_err()
        );
    }

    #[test]
    fn test_stake_zero() {
        let (mgr, mut balances, mut stakes) = setup();
        assert!(mgr.stake("alice", 0, &mut balances, &mut stakes).is_err());
    }

    #[test]
    fn test_stake_negative() {
        let (mgr, mut balances, mut stakes) = setup();
        assert!(mgr.stake("alice", -1, &mut balances, &mut stakes).is_err());
    }

    #[test]
    fn test_stake_multiple_times() {
        let (mgr, mut balances, mut stakes) = setup();
        mgr.stake("alice", 5 * PLANCKS_PER_CREDIT, &mut balances, &mut stakes)
            .unwrap();
        mgr.stake("alice", 5 * PLANCKS_PER_CREDIT, &mut balances, &mut stakes)
            .unwrap();
        assert_eq!(stakes["alice"], 10 * PLANCKS_PER_CREDIT);
    }

    // ── Unstaking ───────────────────────────────────────────────

    #[test]
    fn test_unstake_basic() {
        let (mgr, mut balances, mut stakes) = setup();
        mgr.stake("alice", 10 * PLANCKS_PER_CREDIT, &mut balances, &mut stakes)
            .unwrap();

        let result = mgr
            .unstake("alice", 5 * PLANCKS_PER_CREDIT, &mut balances, &mut stakes)
            .unwrap();

        assert_eq!(result.unstaked, 5 * PLANCKS_PER_CREDIT);
        assert_eq!(result.remaining_stake, 5 * PLANCKS_PER_CREDIT);
        assert_eq!(stakes["alice"], 5 * PLANCKS_PER_CREDIT);
    }

    #[test]
    fn test_unstake_all() {
        let (mgr, mut balances, mut stakes) = setup();
        mgr.stake("alice", 10 * PLANCKS_PER_CREDIT, &mut balances, &mut stakes)
            .unwrap();

        let result = mgr
            .unstake("alice", 10 * PLANCKS_PER_CREDIT, &mut balances, &mut stakes)
            .unwrap();

        assert_eq!(result.remaining_stake, 0);
        assert!(!stakes.contains_key("alice")); // removed when zero
    }

    #[test]
    fn test_unstake_insufficient() {
        let (mgr, mut balances, mut stakes) = setup();
        mgr.stake("alice", 5 * PLANCKS_PER_CREDIT, &mut balances, &mut stakes)
            .unwrap();
        assert!(
            mgr.unstake("alice", 10 * PLANCKS_PER_CREDIT, &mut balances, &mut stakes)
                .is_err()
        );
    }

    #[test]
    fn test_unstake_no_stake() {
        let (mgr, mut balances, mut stakes) = setup();
        assert!(
            mgr.unstake("alice", PLANCKS_PER_CREDIT, &mut balances, &mut stakes)
                .is_err()
        );
    }

    // ── Validator checks ────────────────────────────────────────

    #[test]
    fn test_is_validator() {
        let (mgr, mut balances, mut stakes) = setup();
        assert!(!mgr.is_validator("alice", &stakes));

        mgr.stake("alice", MIN_STAKE_PLANCKS, &mut balances, &mut stakes)
            .unwrap();
        assert!(mgr.is_validator("alice", &stakes));
    }

    #[test]
    fn test_validators_list() {
        let (mgr, mut balances, mut stakes) = setup();
        mgr.stake("alice", 5 * PLANCKS_PER_CREDIT, &mut balances, &mut stakes)
            .unwrap();
        mgr.stake("bob", 2 * PLANCKS_PER_CREDIT, &mut balances, &mut stakes)
            .unwrap();

        let vals = mgr.validators(&stakes);
        assert_eq!(vals.len(), 2);
        assert!(vals.contains(&"alice".to_string()));
        assert!(vals.contains(&"bob".to_string()));
    }

    // ── Availability Rewards ────────────────────────────────────

    #[test]
    fn test_availability_rewards() {
        let (mgr, mut balances, mut stakes) = setup();
        mgr.stake("alice", 5 * PLANCKS_PER_CREDIT, &mut balances, &mut stakes)
            .unwrap();
        mgr.stake("bob", 3 * PLANCKS_PER_CREDIT, &mut balances, &mut stakes)
            .unwrap();

        let supply = balances.values().sum::<i64>() + stakes.values().sum::<i64>();
        let rewards = mgr.calculate_availability_rewards(&stakes, supply);

        assert_eq!(rewards.len(), 2);
        for (_, amount) in &rewards {
            assert_eq!(*amount, AVAILABILITY_REWARD_PLANCKS);
        }
    }

    #[test]
    fn test_availability_rewards_supply_cap() {
        let mgr = StakingManager::new();
        let mut stakes = HashMap::new();
        stakes.insert("miner1".to_string(), MIN_STAKE_PLANCKS);
        stakes.insert("miner2".to_string(), MIN_STAKE_PLANCKS);

        // Almost at cap — only room for 1 reward
        let near_cap = MAX_SUPPLY_PLANCKS - AVAILABILITY_REWARD_PLANCKS;
        let rewards = mgr.calculate_availability_rewards(&stakes, near_cap);
        assert_eq!(rewards.len(), 1);
    }

    #[test]
    fn test_availability_rewards_at_cap() {
        let mgr = StakingManager::new();
        let mut stakes = HashMap::new();
        stakes.insert("miner1".to_string(), MIN_STAKE_PLANCKS);

        let rewards = mgr.calculate_availability_rewards(&stakes, MAX_SUPPLY_PLANCKS);
        assert_eq!(rewards.len(), 0);
    }

    // ── Slashing ────────────────────────────────────────────────

    #[test]
    fn test_slash_basic() {
        let (mut mgr, mut balances, mut stakes) = setup();
        mgr.stake("alice", 10 * PLANCKS_PER_CREDIT, &mut balances, &mut stakes)
            .unwrap();

        let result = mgr.slash("alice", "invalid PoP", &mut stakes, &mut balances);
        // 10% of 10 CR = 1 CR
        assert_eq!(result.slashed, PLANCKS_PER_CREDIT);
        assert_eq!(result.remaining_stake, 9 * PLANCKS_PER_CREDIT);
        assert_eq!(stakes["alice"], 9 * PLANCKS_PER_CREDIT);
        assert!(balances[DAO_ADDR] >= PLANCKS_PER_CREDIT);
    }

    #[test]
    fn test_slash_minimum() {
        let (mut mgr, mut balances, mut stakes) = setup();
        // Stake exactly 1 CR: 10% = 0.1 CR = MIN_SLASH
        mgr.stake("alice", PLANCKS_PER_CREDIT, &mut balances, &mut stakes)
            .unwrap();

        let result = mgr.slash("alice", "bad work", &mut stakes, &mut balances);
        assert_eq!(result.slashed, MIN_SLASH_PLANCKS);
    }

    #[test]
    fn test_slash_no_stake() {
        let (mut mgr, mut balances, mut stakes) = setup();
        let result = mgr.slash("nobody", "err", &mut stakes, &mut balances);
        assert_eq!(result.slashed, 0);
    }

    #[test]
    fn test_slash_reduces_reputation() {
        let (mut mgr, mut balances, mut stakes) = setup();
        mgr.stake("alice", 5 * PLANCKS_PER_CREDIT, &mut balances, &mut stakes)
            .unwrap();

        let rep_before = mgr.get_reputation("alice");
        mgr.slash("alice", "invalid", &mut stakes, &mut balances);
        let rep_after = mgr.get_reputation("alice");
        assert!(rep_after < rep_before);
    }

    // ── Reputation ──────────────────────────────────────────────

    #[test]
    fn test_reputation_default() {
        let mgr = StakingManager::new();
        assert!((mgr.get_reputation("unknown") - DEFAULT_REPUTATION).abs() < f64::EPSILON);
    }

    #[test]
    fn test_reputation_success() {
        let mut mgr = StakingManager::new();
        mgr.update_reputation("alice", true, 1.0);
        assert!(mgr.get_reputation("alice") > DEFAULT_REPUTATION);
    }

    #[test]
    fn test_reputation_failure() {
        let mut mgr = StakingManager::new();
        mgr.update_reputation("alice", false, 1.0);
        assert!(mgr.get_reputation("alice") < DEFAULT_REPUTATION);
    }

    #[test]
    fn test_reputation_clamped() {
        let mut mgr = StakingManager::new();
        for _ in 0..200 {
            mgr.update_reputation("alice", true, 1.0);
        }
        assert!((mgr.get_reputation("alice") - 1.0).abs() < f64::EPSILON);

        for _ in 0..200 {
            mgr.update_reputation("alice", false, 1.0);
        }
        assert!((mgr.get_reputation("alice") - 0.0).abs() < f64::EPSILON);
    }

    // ── Leaderboard ─────────────────────────────────────────────

    #[test]
    fn test_leaderboard() {
        let (mut mgr, mut balances, mut stakes) = setup();
        mgr.stake("alice", 5 * PLANCKS_PER_CREDIT, &mut balances, &mut stakes)
            .unwrap();
        mgr.stake("bob", 3 * PLANCKS_PER_CREDIT, &mut balances, &mut stakes)
            .unwrap();

        mgr.record_earning("alice", 100 * PLANCKS_PER_CREDIT);
        mgr.record_earning("bob", 200 * PLANCKS_PER_CREDIT);
        mgr.record_workload("bob");
        mgr.record_workload("bob");
        mgr.record_workload("alice");

        let board = mgr.leaderboard(&stakes, 10);
        assert_eq!(board.len(), 2);
        assert_eq!(board[0].address, "bob"); // Most earnings
        assert_eq!(board[0].workloads_completed, 2);
        assert_eq!(board[1].address, "alice");
    }

    #[test]
    fn test_leaderboard_top_n() {
        let mut mgr = StakingManager::new();
        let stakes = HashMap::new();
        for i in 0..20 {
            mgr.record_earning(&format!("miner_{}", i), (i + 1) as i64 * PLANCKS_PER_CREDIT);
        }
        let board = mgr.leaderboard(&stakes, 5);
        assert_eq!(board.len(), 5);
        assert_eq!(board[0].address, "miner_19"); // Highest earner
    }

    // ── Serialization ───────────────────────────────────────────

    #[test]
    fn test_staking_roundtrip() {
        let mut mgr = StakingManager::new();
        mgr.update_reputation("alice", true, 1.0);
        mgr.record_earning("alice", 50 * PLANCKS_PER_CREDIT);
        mgr.record_workload("alice");

        let dict = mgr.to_dict();
        let mgr2 = StakingManager::from_dict(&dict);

        assert!((mgr2.get_reputation("alice") - mgr.get_reputation("alice")).abs() < f64::EPSILON);
        assert_eq!(mgr2.earnings["alice"], mgr.earnings["alice"]);
        assert_eq!(
            mgr2.workloads_completed["alice"],
            mgr.workloads_completed["alice"]
        );
    }

    // ── Integration: stake → slash → unstake ────────────────────

    #[test]
    fn test_full_staking_lifecycle() {
        let (mut mgr, mut balances, mut stakes) = setup();
        let initial_balance = balances["alice"];

        // 1. Stake 20 CR
        mgr.stake("alice", 20 * PLANCKS_PER_CREDIT, &mut balances, &mut stakes)
            .unwrap();
        assert_eq!(stakes["alice"], 20 * PLANCKS_PER_CREDIT);
        assert_eq!(balances["alice"], initial_balance - 20 * PLANCKS_PER_CREDIT);

        // 2. Slash 10% = 2 CR
        let slash = mgr.slash("alice", "invalid work", &mut stakes, &mut balances);
        assert_eq!(slash.slashed, 2 * PLANCKS_PER_CREDIT);
        assert_eq!(stakes["alice"], 18 * PLANCKS_PER_CREDIT);

        // 3. Unstake remaining
        let result = mgr
            .unstake("alice", 18 * PLANCKS_PER_CREDIT, &mut balances, &mut stakes)
            .unwrap();
        assert_eq!(result.remaining_stake, 0);
        assert!(!stakes.contains_key("alice"));

        // Net loss = 2 CR (slashed)
        assert_eq!(balances["alice"], initial_balance - 2 * PLANCKS_PER_CREDIT);
    }
}
