//! DAO governance — proposals, voting, execution, treasury management.
//!
//! Faithful port of Python `dao.py` `PlanetaryDAO`:
//! - Proposal creation with funding request
//! - Token-weighted voting (by stake)
//! - Quorum + approval threshold checks
//! - 24-hour voting window
//! - Treasury allocation on execution

use sha3::{Digest, Sha3_256};
use std::collections::HashMap;

// ── Constants ────────────────────────────────────────────────────────────────

/// DAO treasury address (in balances map).
pub const DAO_TREASURY: &str = "DAO";

/// Minimum votes for a proposal to pass.
pub const DEFAULT_QUORUM: u64 = 3;

/// Approval ratio required (>51%).
pub const DEFAULT_APPROVAL_THRESHOLD: f64 = 0.51;

/// Default voting period: 24 hours in seconds.
pub const DEFAULT_VOTING_PERIOD_SECS: f64 = 86400.0;

/// Maximum proposal title length.
pub const MAX_TITLE_LEN: usize = 120;

/// Maximum proposal description length.
pub const MAX_DESCRIPTION_LEN: usize = 2000;

// ── Proposal ─────────────────────────────────────────────────────────────────

/// Status of a DAO proposal.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProposalStatus {
    Active,
    Passed,
    Rejected,
    Executed,
    Expired,
}

impl std::fmt::Display for ProposalStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Active => write!(f, "active"),
            Self::Passed => write!(f, "passed"),
            Self::Rejected => write!(f, "rejected"),
            Self::Executed => write!(f, "executed"),
            Self::Expired => write!(f, "expired"),
        }
    }
}

impl ProposalStatus {
    fn from_str(s: &str) -> Self {
        match s {
            "passed" => Self::Passed,
            "rejected" => Self::Rejected,
            "executed" => Self::Executed,
            "expired" => Self::Expired,
            _ => Self::Active,
        }
    }
}

/// A single vote cast by a voter.
#[derive(Debug, Clone)]
pub struct Vote {
    pub voter: String,
    /// "for" or "against".
    pub direction: VoteDirection,
    /// Weight (typically the voter's stake in plancks, min 1).
    pub weight: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum VoteDirection {
    For,
    Against,
}

impl std::fmt::Display for VoteDirection {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::For => write!(f, "for"),
            Self::Against => write!(f, "against"),
        }
    }
}

/// A DAO funding proposal.
#[derive(Debug, Clone)]
pub struct Proposal {
    /// 16-char hex ID.
    pub id: String,
    /// Address of the proposer.
    pub proposer: String,
    /// Title (max 120 chars).
    pub title: String,
    /// Description.
    pub description: String,
    /// Requested amount in plancks.
    pub amount_plancks: i64,
    /// Recipient address.
    pub recipient: String,
    /// When created (unix timestamp).
    pub created_at: f64,
    /// Voting deadline (unix timestamp).
    pub voting_deadline: f64,
    /// Total weighted votes for.
    pub votes_for: u64,
    /// Total weighted votes against.
    pub votes_against: u64,
    /// Voters: address → Vote.
    pub voters: HashMap<String, Vote>,
    /// Current status.
    pub status: ProposalStatus,
}

impl Proposal {
    pub fn total_votes(&self) -> u64 {
        self.votes_for + self.votes_against
    }

    pub fn approval_ratio(&self) -> f64 {
        let total = self.total_votes();
        if total == 0 {
            return 0.0;
        }
        self.votes_for as f64 / total as f64
    }

    pub fn to_dict(&self) -> serde_json::Value {
        let voters: serde_json::Map<String, serde_json::Value> = self
            .voters
            .iter()
            .map(|(k, v)| {
                (
                    k.clone(),
                    serde_json::json!({
                        "vote": v.direction.to_string(),
                        "weight": v.weight,
                    }),
                )
            })
            .collect();

        serde_json::json!({
            "id": self.id,
            "proposer": self.proposer,
            "title": self.title,
            "description": self.description,
            "amount_plancks": self.amount_plancks,
            "recipient": self.recipient,
            "created_at": self.created_at,
            "voting_deadline": self.voting_deadline,
            "votes_for": self.votes_for,
            "votes_against": self.votes_against,
            "voters": voters,
            "status": self.status.to_string(),
        })
    }

    pub fn from_dict(v: &serde_json::Value) -> Result<Self, String> {
        let mut voters = HashMap::new();
        if let Some(vmap) = v["voters"].as_object() {
            for (addr, vv) in vmap {
                let direction = match vv["vote"].as_str().unwrap_or("for") {
                    "against" => VoteDirection::Against,
                    _ => VoteDirection::For,
                };
                voters.insert(
                    addr.clone(),
                    Vote {
                        voter: addr.clone(),
                        direction,
                        weight: vv["weight"].as_u64().unwrap_or(1),
                    },
                );
            }
        }

        Ok(Self {
            id: v["id"].as_str().ok_or("missing id")?.to_string(),
            proposer: v["proposer"]
                .as_str()
                .ok_or("missing proposer")?
                .to_string(),
            title: v["title"].as_str().unwrap_or("").to_string(),
            description: v["description"].as_str().unwrap_or("").to_string(),
            amount_plancks: v["amount_plancks"].as_i64().unwrap_or(0),
            recipient: v["recipient"].as_str().unwrap_or("").to_string(),
            created_at: v["created_at"].as_f64().unwrap_or(0.0),
            voting_deadline: v["voting_deadline"].as_f64().unwrap_or(0.0),
            votes_for: v["votes_for"].as_u64().unwrap_or(0),
            votes_against: v["votes_against"].as_u64().unwrap_or(0),
            voters,
            status: ProposalStatus::from_str(v["status"].as_str().unwrap_or("active")),
        })
    }
}

// ── PlanetaryDAO ─────────────────────────────────────────────────────────────

/// On-chain DAO governing the robot economy treasury.
pub struct PlanetaryDAO {
    /// All proposals: id → proposal.
    pub proposals: HashMap<String, Proposal>,
    /// Cumulative token allocations per address.
    pub allocations: HashMap<String, i64>,
    /// Monotonically increasing proposal counter (for unique IDs).
    pub proposal_counter: u64,
}

impl PlanetaryDAO {
    pub fn new() -> Self {
        Self {
            proposals: HashMap::new(),
            allocations: HashMap::new(),
            proposal_counter: 0,
        }
    }

    // ── Direct Allocation ───────────────────────────────────────

    /// Transfer `amount_plancks` from DAO treasury to `recipient`.
    ///
    /// Used for admin/system allocations (not proposal-driven).
    pub fn allocate_tokens(
        &mut self,
        recipient: &str,
        amount_plancks: i64,
        purpose: &str,
        balances: &mut HashMap<String, i64>,
    ) -> Result<AllocationResult, String> {
        if recipient.is_empty() {
            return Err("Invalid recipient address".into());
        }
        if amount_plancks <= 0 {
            return Err("Amount must be positive".into());
        }
        if purpose.len() > MAX_DESCRIPTION_LEN {
            return Err("Purpose too long".into());
        }

        let dao_balance = balances.get(DAO_TREASURY).copied().unwrap_or(0);
        if dao_balance < amount_plancks {
            return Err(format!(
                "Insufficient DAO funds ({} < {})",
                dao_balance, amount_plancks
            ));
        }

        *balances.get_mut(DAO_TREASURY).unwrap() -= amount_plancks;
        *balances.entry(recipient.to_string()).or_insert(0) += amount_plancks;
        *self.allocations.entry(recipient.to_string()).or_insert(0) += amount_plancks;

        Ok(AllocationResult {
            amount_plancks,
            recipient: recipient.to_string(),
            dao_remaining: balances.get(DAO_TREASURY).copied().unwrap_or(0),
        })
    }

    // ── Proposals ───────────────────────────────────────────────

    /// Create a new funding proposal.
    pub fn create_proposal(
        &mut self,
        proposer: &str,
        title: &str,
        description: &str,
        amount_plancks: i64,
        recipient: &str,
        now: f64,
        voting_period_secs: Option<f64>,
    ) -> Result<String, String> {
        if title.is_empty() || title.len() > MAX_TITLE_LEN {
            return Err(format!("Title required (max {} chars)", MAX_TITLE_LEN));
        }
        if description.len() > MAX_DESCRIPTION_LEN {
            return Err(format!(
                "Description too long (max {} chars)",
                MAX_DESCRIPTION_LEN
            ));
        }
        if amount_plancks <= 0 {
            return Err("Amount must be positive".into());
        }
        if recipient.is_empty() {
            return Err("Recipient required".into());
        }

        self.proposal_counter += 1;
        let proposal_id = generate_proposal_id(self.proposal_counter, proposer, title, now);

        let voting_period = voting_period_secs.unwrap_or(DEFAULT_VOTING_PERIOD_SECS);

        let proposal = Proposal {
            id: proposal_id.clone(),
            proposer: proposer.to_string(),
            title: title.to_string(),
            description: description.to_string(),
            amount_plancks,
            recipient: recipient.to_string(),
            created_at: now,
            voting_deadline: now + voting_period,
            votes_for: 0,
            votes_against: 0,
            voters: HashMap::new(),
            status: ProposalStatus::Active,
        };

        self.proposals.insert(proposal_id.clone(), proposal);
        Ok(proposal_id)
    }

    /// Cast a vote on an active proposal.
    ///
    /// `stake_weight` is the voter's stake in plancks (min 1 for unweighted).
    pub fn vote(
        &mut self,
        proposal_id: &str,
        voter: &str,
        direction: VoteDirection,
        stake_weight: u64,
        now: f64,
    ) -> Result<VoteResult, String> {
        let proposal = self
            .proposals
            .get_mut(proposal_id)
            .ok_or("Proposal not found")?;

        if proposal.status != ProposalStatus::Active {
            return Err(format!("Proposal is {}", proposal.status));
        }

        // Check deadline
        if now > proposal.voting_deadline {
            proposal.status = ProposalStatus::Expired;
            return Err("Voting period has ended".into());
        }

        // Check duplicate vote
        if proposal.voters.contains_key(voter) {
            return Err("Already voted".into());
        }

        let weight = stake_weight.max(1);
        let vote = Vote {
            voter: voter.to_string(),
            direction: direction.clone(),
            weight,
        };

        match direction {
            VoteDirection::For => proposal.votes_for += weight,
            VoteDirection::Against => proposal.votes_against += weight,
        }

        proposal.voters.insert(voter.to_string(), vote);

        Ok(VoteResult {
            votes_for: proposal.votes_for,
            votes_against: proposal.votes_against,
        })
    }

    /// Execute a passed proposal — transfers from DAO treasury.
    ///
    /// Checks quorum and approval threshold.
    pub fn execute_proposal(
        &mut self,
        proposal_id: &str,
        balances: &mut HashMap<String, i64>,
    ) -> Result<ExecutionResult, String> {
        let proposal = self
            .proposals
            .get_mut(proposal_id)
            .ok_or("Proposal not found")?;

        if proposal.status != ProposalStatus::Active {
            return Err(format!("Proposal is {}", proposal.status));
        }

        // Check quorum
        let total_votes = proposal.total_votes();
        if total_votes < DEFAULT_QUORUM {
            return Err(format!(
                "Quorum not met ({}/{})",
                total_votes, DEFAULT_QUORUM
            ));
        }

        // Check approval threshold
        let approval = proposal.approval_ratio();
        if approval < DEFAULT_APPROVAL_THRESHOLD {
            proposal.status = ProposalStatus::Rejected;
            return Err(format!("Approval too low ({:.0}%)", approval * 100.0));
        }

        // Check treasury
        let amount = proposal.amount_plancks;
        let dao_balance = balances.get(DAO_TREASURY).copied().unwrap_or(0);
        if dao_balance < amount {
            return Err("Insufficient DAO treasury funds".into());
        }

        // Execute transfer
        *balances.get_mut(DAO_TREASURY).unwrap() -= amount;
        let recipient = proposal.recipient.clone();
        *balances.entry(recipient.clone()).or_insert(0) += amount;
        *self.allocations.entry(recipient.clone()).or_insert(0) += amount;

        proposal.status = ProposalStatus::Executed;

        Ok(ExecutionResult {
            proposal_id: proposal_id.to_string(),
            amount_plancks: amount,
            recipient,
        })
    }

    // ── Queries ─────────────────────────────────────────────────

    /// Get the DAO treasury balance.
    pub fn treasury_balance(&self, balances: &HashMap<String, i64>) -> i64 {
        balances.get(DAO_TREASURY).copied().unwrap_or(0)
    }

    /// Get proposals, optionally filtered by status.
    pub fn get_proposals(&self, status: Option<ProposalStatus>) -> Vec<&Proposal> {
        match status {
            Some(s) => self.proposals.values().filter(|p| p.status == s).collect(),
            None => self.proposals.values().collect(),
        }
    }

    /// Get a single proposal by ID.
    pub fn get_proposal(&self, id: &str) -> Option<&Proposal> {
        self.proposals.get(id)
    }

    /// Get cumulative allocation for an address (or all).
    pub fn get_allocation(&self, address: &str) -> i64 {
        self.allocations.get(address).copied().unwrap_or(0)
    }

    /// Get all allocations.
    pub fn all_allocations(&self) -> &HashMap<String, i64> {
        &self.allocations
    }

    /// DAO stats.
    pub fn stats(&self) -> DaoStats {
        let active = self
            .proposals
            .values()
            .filter(|p| p.status == ProposalStatus::Active)
            .count();
        let executed = self
            .proposals
            .values()
            .filter(|p| p.status == ProposalStatus::Executed)
            .count();
        let rejected = self
            .proposals
            .values()
            .filter(|p| p.status == ProposalStatus::Rejected)
            .count();
        let total_allocated: i64 = self.allocations.values().sum();

        DaoStats {
            total_proposals: self.proposals.len(),
            active,
            executed,
            rejected,
            total_allocated_plancks: total_allocated,
        }
    }

    // ── Serialization ───────────────────────────────────────────

    pub fn to_dict(&self) -> serde_json::Value {
        let proposals: serde_json::Map<String, serde_json::Value> = self
            .proposals
            .iter()
            .map(|(k, v)| (k.clone(), v.to_dict()))
            .collect();
        let allocs: serde_json::Map<String, serde_json::Value> = self
            .allocations
            .iter()
            .map(|(k, v)| (k.clone(), serde_json::json!(*v)))
            .collect();

        serde_json::json!({
            "proposals": proposals,
            "allocations": allocs,
            "proposal_counter": self.proposal_counter,
        })
    }

    pub fn from_dict(v: &serde_json::Value) -> Self {
        let mut dao = Self::new();

        dao.proposal_counter = v["proposal_counter"].as_u64().unwrap_or(0);

        if let Some(props) = v["proposals"].as_object() {
            for (k, pv) in props {
                if let Ok(p) = Proposal::from_dict(pv) {
                    dao.proposals.insert(k.clone(), p);
                }
            }
        }

        if let Some(allocs) = v["allocations"].as_object() {
            for (k, av) in allocs {
                if let Some(a) = av.as_i64() {
                    dao.allocations.insert(k.clone(), a);
                }
            }
        }

        dao
    }
}

impl Default for PlanetaryDAO {
    fn default() -> Self {
        Self::new()
    }
}

// ── Result types ─────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct AllocationResult {
    pub amount_plancks: i64,
    pub recipient: String,
    pub dao_remaining: i64,
}

#[derive(Debug, Clone)]
pub struct VoteResult {
    pub votes_for: u64,
    pub votes_against: u64,
}

#[derive(Debug, Clone)]
pub struct ExecutionResult {
    pub proposal_id: String,
    pub amount_plancks: i64,
    pub recipient: String,
}

#[derive(Debug, Clone)]
pub struct DaoStats {
    pub total_proposals: usize,
    pub active: usize,
    pub executed: usize,
    pub rejected: usize,
    pub total_allocated_plancks: i64,
}

// ── Helpers ──────────────────────────────────────────────────────────────────

/// Generate a deterministic 16-char hex proposal ID.
fn generate_proposal_id(counter: u64, proposer: &str, title: &str, timestamp: f64) -> String {
    let mut hasher = Sha3_256::new();
    hasher.update(counter.to_be_bytes());
    hasher.update(proposer.as_bytes());
    hasher.update(title.as_bytes());
    hasher.update(timestamp.to_be_bytes());
    hex::encode(hasher.finalize())[..16].to_string()
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::transaction::PLANCKS_PER_CREDIT;

    fn setup() -> (PlanetaryDAO, HashMap<String, i64>) {
        let dao = PlanetaryDAO::new();
        let mut balances = HashMap::new();
        balances.insert(DAO_TREASURY.to_string(), 1000 * PLANCKS_PER_CREDIT);
        balances.insert("alice".to_string(), 50 * PLANCKS_PER_CREDIT);
        balances.insert("bob".to_string(), 30 * PLANCKS_PER_CREDIT);
        (dao, balances)
    }

    const NOW: f64 = 1_700_000_000.0;

    // ── Direct Allocation ───────────────────────────────────────

    #[test]
    fn test_allocate_tokens() {
        let (mut dao, mut balances) = setup();
        let result = dao
            .allocate_tokens("robot_1", 10 * PLANCKS_PER_CREDIT, "compute", &mut balances)
            .unwrap();

        assert_eq!(result.amount_plancks, 10 * PLANCKS_PER_CREDIT);
        assert_eq!(result.recipient, "robot_1");
        assert_eq!(balances["robot_1"], 10 * PLANCKS_PER_CREDIT);
        assert_eq!(balances[DAO_TREASURY], 990 * PLANCKS_PER_CREDIT);
        assert_eq!(dao.get_allocation("robot_1"), 10 * PLANCKS_PER_CREDIT);
    }

    #[test]
    fn test_allocate_insufficient_funds() {
        let (mut dao, mut balances) = setup();
        assert!(
            dao.allocate_tokens(
                "robot_1",
                2000 * PLANCKS_PER_CREDIT,
                "too much",
                &mut balances
            )
            .is_err()
        );
    }

    #[test]
    fn test_allocate_zero() {
        let (mut dao, mut balances) = setup();
        assert!(
            dao.allocate_tokens("robot_1", 0, "nothing", &mut balances)
                .is_err()
        );
    }

    #[test]
    fn test_allocate_empty_recipient() {
        let (mut dao, mut balances) = setup();
        assert!(
            dao.allocate_tokens("", PLANCKS_PER_CREDIT, "test", &mut balances)
                .is_err()
        );
    }

    // ── Proposal Creation ───────────────────────────────────────

    #[test]
    fn test_create_proposal() {
        let (mut dao, _) = setup();
        let id = dao
            .create_proposal(
                "alice",
                "Fund AI research",
                "Need 100 CR for GPU time",
                100 * PLANCKS_PER_CREDIT,
                "alice",
                NOW,
                None,
            )
            .unwrap();

        assert_eq!(id.len(), 16);
        let p = dao.get_proposal(&id).unwrap();
        assert_eq!(p.status, ProposalStatus::Active);
        assert_eq!(p.amount_plancks, 100 * PLANCKS_PER_CREDIT);
        assert_eq!(p.proposer, "alice");
    }

    #[test]
    fn test_create_proposal_empty_title() {
        let (mut dao, _) = setup();
        assert!(
            dao.create_proposal("alice", "", "desc", PLANCKS_PER_CREDIT, "alice", NOW, None)
                .is_err()
        );
    }

    #[test]
    fn test_create_proposal_title_too_long() {
        let (mut dao, _) = setup();
        let long_title = "x".repeat(MAX_TITLE_LEN + 1);
        assert!(
            dao.create_proposal(
                "alice",
                &long_title,
                "desc",
                PLANCKS_PER_CREDIT,
                "alice",
                NOW,
                None
            )
            .is_err()
        );
    }

    #[test]
    fn test_create_proposal_zero_amount() {
        let (mut dao, _) = setup();
        assert!(
            dao.create_proposal("alice", "title", "desc", 0, "alice", NOW, None)
                .is_err()
        );
    }

    // ── Voting ──────────────────────────────────────────────────

    #[test]
    fn test_vote_for() {
        let (mut dao, _) = setup();
        let id = dao
            .create_proposal(
                "alice",
                "Test",
                "desc",
                PLANCKS_PER_CREDIT,
                "alice",
                NOW,
                None,
            )
            .unwrap();

        let result = dao
            .vote(
                &id,
                "bob",
                VoteDirection::For,
                5 * PLANCKS_PER_CREDIT as u64,
                NOW + 100.0,
            )
            .unwrap();

        assert_eq!(result.votes_for, 5 * PLANCKS_PER_CREDIT as u64);
        assert_eq!(result.votes_against, 0);
    }

    #[test]
    fn test_vote_against() {
        let (mut dao, _) = setup();
        let id = dao
            .create_proposal(
                "alice",
                "Test",
                "desc",
                PLANCKS_PER_CREDIT,
                "alice",
                NOW,
                None,
            )
            .unwrap();

        let result = dao
            .vote(&id, "bob", VoteDirection::Against, 1, NOW + 100.0)
            .unwrap();

        assert_eq!(result.votes_for, 0);
        assert_eq!(result.votes_against, 1);
    }

    #[test]
    fn test_vote_duplicate() {
        let (mut dao, _) = setup();
        let id = dao
            .create_proposal(
                "alice",
                "Test",
                "desc",
                PLANCKS_PER_CREDIT,
                "alice",
                NOW,
                None,
            )
            .unwrap();

        dao.vote(&id, "bob", VoteDirection::For, 1, NOW + 100.0)
            .unwrap();
        assert!(
            dao.vote(&id, "bob", VoteDirection::Against, 1, NOW + 200.0)
                .is_err()
        );
    }

    #[test]
    fn test_vote_expired() {
        let (mut dao, _) = setup();
        let id = dao
            .create_proposal(
                "alice",
                "Test",
                "desc",
                PLANCKS_PER_CREDIT,
                "alice",
                NOW,
                Some(100.0), // 100s voting period
            )
            .unwrap();

        // Vote after deadline
        assert!(
            dao.vote(&id, "bob", VoteDirection::For, 1, NOW + 200.0)
                .is_err()
        );
    }

    #[test]
    fn test_vote_nonexistent() {
        let (mut dao, _) = setup();
        assert!(
            dao.vote("ghost", "bob", VoteDirection::For, 1, NOW)
                .is_err()
        );
    }

    // ── Execution ───────────────────────────────────────────────

    #[test]
    fn test_execute_proposal_passed() {
        let (mut dao, mut balances) = setup();
        let id = dao
            .create_proposal(
                "alice",
                "Fund project",
                "desc",
                50 * PLANCKS_PER_CREDIT,
                "robot_1",
                NOW,
                None,
            )
            .unwrap();

        // 3 votes for (meets quorum)
        dao.vote(&id, "v1", VoteDirection::For, 1, NOW + 10.0)
            .unwrap();
        dao.vote(&id, "v2", VoteDirection::For, 1, NOW + 20.0)
            .unwrap();
        dao.vote(&id, "v3", VoteDirection::For, 1, NOW + 30.0)
            .unwrap();

        let result = dao.execute_proposal(&id, &mut balances).unwrap();
        assert_eq!(result.amount_plancks, 50 * PLANCKS_PER_CREDIT);
        assert_eq!(result.recipient, "robot_1");
        assert_eq!(balances["robot_1"], 50 * PLANCKS_PER_CREDIT);
        assert_eq!(
            dao.get_proposal(&id).unwrap().status,
            ProposalStatus::Executed
        );
    }

    #[test]
    fn test_execute_quorum_not_met() {
        let (mut dao, mut balances) = setup();
        let id = dao
            .create_proposal("alice", "Test", "d", PLANCKS_PER_CREDIT, "alice", NOW, None)
            .unwrap();

        // Only 2 votes (quorum = 3)
        dao.vote(&id, "v1", VoteDirection::For, 1, NOW + 10.0)
            .unwrap();
        dao.vote(&id, "v2", VoteDirection::For, 1, NOW + 20.0)
            .unwrap();

        assert!(dao.execute_proposal(&id, &mut balances).is_err());
    }

    #[test]
    fn test_execute_approval_too_low() {
        let (mut dao, mut balances) = setup();
        let id = dao
            .create_proposal("alice", "Test", "d", PLANCKS_PER_CREDIT, "alice", NOW, None)
            .unwrap();

        // 1 for, 3 against — below 51%
        dao.vote(&id, "v1", VoteDirection::For, 1, NOW + 10.0)
            .unwrap();
        dao.vote(&id, "v2", VoteDirection::Against, 1, NOW + 20.0)
            .unwrap();
        dao.vote(&id, "v3", VoteDirection::Against, 1, NOW + 30.0)
            .unwrap();
        dao.vote(&id, "v4", VoteDirection::Against, 1, NOW + 40.0)
            .unwrap();

        assert!(dao.execute_proposal(&id, &mut balances).is_err());
        assert_eq!(
            dao.get_proposal(&id).unwrap().status,
            ProposalStatus::Rejected
        );
    }

    #[test]
    fn test_execute_insufficient_treasury() {
        let (mut dao, mut balances) = setup();
        balances.insert(DAO_TREASURY.to_string(), 0); // empty treasury

        let id = dao
            .create_proposal("alice", "Test", "d", PLANCKS_PER_CREDIT, "alice", NOW, None)
            .unwrap();

        dao.vote(&id, "v1", VoteDirection::For, 1, NOW + 10.0)
            .unwrap();
        dao.vote(&id, "v2", VoteDirection::For, 1, NOW + 20.0)
            .unwrap();
        dao.vote(&id, "v3", VoteDirection::For, 1, NOW + 30.0)
            .unwrap();

        assert!(dao.execute_proposal(&id, &mut balances).is_err());
    }

    #[test]
    fn test_execute_already_executed() {
        let (mut dao, mut balances) = setup();
        let id = dao
            .create_proposal("alice", "Test", "d", PLANCKS_PER_CREDIT, "alice", NOW, None)
            .unwrap();

        dao.vote(&id, "v1", VoteDirection::For, 1, NOW + 10.0)
            .unwrap();
        dao.vote(&id, "v2", VoteDirection::For, 1, NOW + 20.0)
            .unwrap();
        dao.vote(&id, "v3", VoteDirection::For, 1, NOW + 30.0)
            .unwrap();

        dao.execute_proposal(&id, &mut balances).unwrap();
        assert!(dao.execute_proposal(&id, &mut balances).is_err());
    }

    // ── Weighted Voting ─────────────────────────────────────────

    #[test]
    fn test_stake_weighted_voting() {
        let (mut dao, mut balances) = setup();
        let id = dao
            .create_proposal(
                "alice",
                "Big fund",
                "d",
                100 * PLANCKS_PER_CREDIT,
                "alice",
                NOW,
                None,
            )
            .unwrap();

        // Whale votes for with 50 CR stake weight
        dao.vote(
            &id,
            "whale",
            VoteDirection::For,
            50 * PLANCKS_PER_CREDIT as u64,
            NOW + 10.0,
        )
        .unwrap();
        // Two small voters against with 1 weight each
        dao.vote(&id, "small1", VoteDirection::Against, 1, NOW + 20.0)
            .unwrap();
        dao.vote(&id, "small2", VoteDirection::Against, 1, NOW + 30.0)
            .unwrap();

        // Whale's weight dominates → should pass
        let result = dao.execute_proposal(&id, &mut balances).unwrap();
        assert_eq!(result.amount_plancks, 100 * PLANCKS_PER_CREDIT);
    }

    // ── Queries ─────────────────────────────────────────────────

    #[test]
    fn test_treasury_balance() {
        let (dao, balances) = setup();
        assert_eq!(dao.treasury_balance(&balances), 1000 * PLANCKS_PER_CREDIT);
    }

    #[test]
    fn test_get_proposals_filtered() {
        let (mut dao, mut balances) = setup();
        dao.create_proposal("a", "P1", "d", PLANCKS_PER_CREDIT, "a", NOW, None)
            .unwrap();
        let id2 = dao
            .create_proposal("a", "P2", "d", PLANCKS_PER_CREDIT, "a", NOW + 1.0, None)
            .unwrap();

        // Execute P2
        dao.vote(&id2, "v1", VoteDirection::For, 1, NOW + 10.0)
            .unwrap();
        dao.vote(&id2, "v2", VoteDirection::For, 1, NOW + 20.0)
            .unwrap();
        dao.vote(&id2, "v3", VoteDirection::For, 1, NOW + 30.0)
            .unwrap();
        dao.execute_proposal(&id2, &mut balances).unwrap();

        let active = dao.get_proposals(Some(ProposalStatus::Active));
        assert_eq!(active.len(), 1);
        let executed = dao.get_proposals(Some(ProposalStatus::Executed));
        assert_eq!(executed.len(), 1);
        let all = dao.get_proposals(None);
        assert_eq!(all.len(), 2);
    }

    #[test]
    fn test_dao_stats() {
        let (mut dao, mut balances) = setup();
        dao.create_proposal("a", "P1", "d", PLANCKS_PER_CREDIT, "a", NOW, None)
            .unwrap();
        let id2 = dao
            .create_proposal("a", "P2", "d", PLANCKS_PER_CREDIT, "a", NOW + 1.0, None)
            .unwrap();

        dao.vote(&id2, "v1", VoteDirection::For, 1, NOW + 10.0)
            .unwrap();
        dao.vote(&id2, "v2", VoteDirection::For, 1, NOW + 20.0)
            .unwrap();
        dao.vote(&id2, "v3", VoteDirection::For, 1, NOW + 30.0)
            .unwrap();
        dao.execute_proposal(&id2, &mut balances).unwrap();

        let stats = dao.stats();
        assert_eq!(stats.total_proposals, 2);
        assert_eq!(stats.active, 1);
        assert_eq!(stats.executed, 1);
        assert_eq!(stats.total_allocated_plancks, PLANCKS_PER_CREDIT);
    }

    // ── Serialization ───────────────────────────────────────────

    #[test]
    fn test_dao_roundtrip() {
        let (mut dao, mut balances) = setup();
        let id = dao
            .create_proposal(
                "alice",
                "Test",
                "desc",
                PLANCKS_PER_CREDIT,
                "alice",
                NOW,
                None,
            )
            .unwrap();
        dao.vote(&id, "bob", VoteDirection::For, 10, NOW + 10.0)
            .unwrap();
        dao.allocate_tokens("robot_1", 5 * PLANCKS_PER_CREDIT, "test", &mut balances)
            .unwrap();

        let dict = dao.to_dict();
        let dao2 = PlanetaryDAO::from_dict(&dict);

        assert_eq!(dao2.proposal_counter, 1);
        assert_eq!(dao2.proposals.len(), 1);
        let p = dao2.get_proposal(&id).unwrap();
        assert_eq!(p.votes_for, 10);
        assert_eq!(p.voters.len(), 1);
        assert_eq!(dao2.get_allocation("robot_1"), 5 * PLANCKS_PER_CREDIT);
    }

    #[test]
    fn test_proposal_roundtrip() {
        let p = Proposal {
            id: "abcdef1234567890".to_string(),
            proposer: "alice".into(),
            title: "Test".into(),
            description: "Desc".into(),
            amount_plancks: 100 * PLANCKS_PER_CREDIT,
            recipient: "bob".into(),
            created_at: NOW,
            voting_deadline: NOW + DEFAULT_VOTING_PERIOD_SECS,
            votes_for: 5,
            votes_against: 2,
            voters: HashMap::new(),
            status: ProposalStatus::Active,
        };

        let dict = p.to_dict();
        let p2 = Proposal::from_dict(&dict).unwrap();
        assert_eq!(p2.id, p.id);
        assert_eq!(p2.votes_for, 5);
        assert_eq!(p2.votes_against, 2);
        assert_eq!(p2.status, ProposalStatus::Active);
    }

    // ── Full Lifecycle ──────────────────────────────────────────

    #[test]
    fn test_full_dao_lifecycle() {
        let (mut dao, mut balances) = setup();

        // 1. Create proposal
        let id = dao
            .create_proposal(
                "alice",
                "Fund ML training cluster",
                "Need 200 CR for GPU rental",
                200 * PLANCKS_PER_CREDIT,
                "ml_cluster",
                NOW,
                None,
            )
            .unwrap();

        // 2. Vote (3 for, 1 against → 75% approval)
        dao.vote(&id, "v1", VoteDirection::For, 1, NOW + 10.0)
            .unwrap();
        dao.vote(&id, "v2", VoteDirection::For, 1, NOW + 20.0)
            .unwrap();
        dao.vote(&id, "v3", VoteDirection::For, 1, NOW + 30.0)
            .unwrap();
        dao.vote(&id, "v4", VoteDirection::Against, 1, NOW + 40.0)
            .unwrap();

        // 3. Execute
        let result = dao.execute_proposal(&id, &mut balances).unwrap();
        assert_eq!(result.amount_plancks, 200 * PLANCKS_PER_CREDIT);
        assert_eq!(balances["ml_cluster"], 200 * PLANCKS_PER_CREDIT);
        assert_eq!(balances[DAO_TREASURY], 800 * PLANCKS_PER_CREDIT);

        // 4. Verify state
        let p = dao.get_proposal(&id).unwrap();
        assert_eq!(p.status, ProposalStatus::Executed);
        assert_eq!(p.approval_ratio(), 0.75);
    }
}
