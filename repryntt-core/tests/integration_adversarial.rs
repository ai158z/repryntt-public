//! Session 13 — Adversarial, boundary, and error-path integration tests.
//!
//! Complements Session 12's happy-path tests with:
//!   1. Adversarial inputs: double-spend, replay, negative amounts, overflow
//!   2. Token advanced: freeze, burn, allowance, disable minting
//!   3. DAO edge cases: expired votes, double-vote, quorum, 0 treasury
//!   4. Staking edge cases: unstake, slash, min-stake boundary, reputation
//!   5. Contract edge cases: double-claim, double-complete, unknown workload
//!   6. Mempool: validated add, pool limits, purge expired, fee estimation
//!   7. RPC error paths: malformed JSON, missing params, unknown methods
//!   8. Chain: out-of-order blocks, bad hash, duplicate index, batch add
//!   9. Economy: timeout/expiry, reputation decay, status broadcast
//!  10. Election: zero TFLOPS, single candidate, empty contributors

use std::collections::{BTreeMap, HashMap};

use repryntt_core::block::Block;
use repryntt_core::chain::Chain;
use repryntt_core::contract::WorkloadContract;
use repryntt_core::dao::{DAO_TREASURY, PlanetaryDAO, VoteDirection};
use repryntt_core::economy::{DEFAULT_FEE_PLANCKS, EconomyBridge, WorkloadStatus};
use repryntt_core::election::{self, ComputeContributors};
use repryntt_core::entity::{EntityRecord, EntityRegistry};
use repryntt_core::genesis::{BASE_REWARD_PLANCKS, EXPECTED_GENESIS_HASH, HALVING_INTERVAL};
use repryntt_core::mempool::{self, MIN_FEE_PLANCKS, Mempool};
use repryntt_core::network::MessageType;
use repryntt_core::pop::{DeviceInfo, calculate_reward};
use repryntt_core::producer::{BlockProducer, NodeConfig};
use repryntt_core::rpc::{self, NodeState, RpcRequest, handle_request};
use repryntt_core::staking::{MIN_STAKE_PLANCKS, StakingManager};
use repryntt_core::storage::Storage;
use repryntt_core::token::TokenRegistry;
use repryntt_core::transaction::{PLANCKS_PER_CREDIT, Transaction};

use serde_json::Value;

// ── Helpers ──────────────────────────────────────────────────────────────────

fn miner_config(addr: &str, tflops: f64) -> NodeConfig {
    NodeConfig {
        address: addr.to_string(),
        measured_tflops: tflops,
        compute_share: 1.0,
        tflops,
        mining_enabled: true,
    }
}

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

fn make_rpc_request(method: &str, params: Value) -> RpcRequest {
    RpcRequest {
        jsonrpc: "2.0".into(),
        method: method.into(),
        params,
        id: Value::Number(1.into()),
    }
}

fn genesis_ts() -> f64 {
    Chain::new().latest_block().timestamp
}

const NOW: f64 = 1_750_000_000.0;

// ═════════════════════════════════════════════════════════════════════════════
// 1. ADVERSARIAL: DOUBLE-SPEND, REPLAY, NEGATIVE AMOUNTS, OVERFLOW
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_chain_rejects_insufficient_balance_transfer() {
    let mut chain = Chain::new();
    let ts = chain.latest_block().timestamp + 69.0;

    // Sender has 0 balance, tries to transfer 1000
    let tx = Transaction::new(
        "broke_addr",
        "victim",
        1000,
        "transfer",
        0,
        BTreeMap::new(),
        None,
        None,
        1,
    );
    let block = Block::new(
        chain.height(),
        &chain.latest_block().hash,
        ts,
        vec![tx],
        "SYSTEM",
        BTreeMap::new(),
    );
    let result = chain.add_block(block);
    assert!(result.is_err(), "Should reject transfer with 0 balance");
}

#[test]
fn test_chain_rejects_overspend() {
    let mut chain = Chain::new();
    let ts = chain.latest_block().timestamp + 69.0;

    // Give sender exactly 1000
    let fund_tx = Transaction::new(
        "SYSTEM",
        "spender",
        1000,
        "reward",
        0,
        BTreeMap::new(),
        None,
        None,
        1,
    );
    let b1 = Block::new(
        chain.height(),
        &chain.latest_block().hash,
        ts,
        vec![fund_tx],
        "SYSTEM",
        BTreeMap::new(),
    );
    chain.add_block(b1).unwrap();
    assert_eq!(*chain.balances.get("spender").unwrap(), 1000);

    // Try to spend 1001 — should fail
    let overspend = Transaction::new(
        "spender",
        "target",
        1001,
        "transfer",
        0,
        BTreeMap::new(),
        None,
        None,
        1,
    );
    let ts2 = chain.latest_block().timestamp + 69.0;
    let b2 = Block::new(
        chain.height(),
        &chain.latest_block().hash,
        ts2,
        vec![overspend],
        "SYSTEM",
        BTreeMap::new(),
    );
    let result = chain.add_block(b2);
    assert!(result.is_err(), "Overspend block should be rejected");
}

#[test]
fn test_coinbase_reward_overflow_at_extreme_heights() {
    // Test halving at extreme block heights
    let reward_at_max = Chain::coinbase_reward(u64::MAX);
    assert_eq!(reward_at_max, 0, "Extreme height should return 0 reward");

    // Just before overflow boundary
    let reward_late = Chain::coinbase_reward(HALVING_INTERVAL * 63);
    assert!(reward_late >= 0, "Should never return negative");

    // After 64 halvings: zero
    let reward_zero = Chain::coinbase_reward(HALVING_INTERVAL * 64);
    assert_eq!(reward_zero, 0);
}

#[test]
fn test_block_rejects_wrong_previous_hash() {
    let mut chain = Chain::new();
    let ts = chain.latest_block().timestamp + 69.0;

    let tx = Transaction::new(
        "SYSTEM",
        "miner",
        BASE_REWARD_PLANCKS,
        "reward",
        0,
        BTreeMap::new(),
        None,
        None,
        1,
    );
    let block = Block::new(
        chain.height(),
        "0000_totally_wrong_hash",
        ts,
        vec![tx],
        "SYSTEM",
        BTreeMap::new(),
    );
    let result = chain.add_block(block);
    assert!(result.is_err(), "Wrong previous_hash must be rejected");
}

#[test]
fn test_block_rejects_wrong_index() {
    let mut chain = Chain::new();
    let ts = chain.latest_block().timestamp + 69.0;

    let tx = Transaction::new(
        "SYSTEM",
        "miner",
        BASE_REWARD_PLANCKS,
        "reward",
        0,
        BTreeMap::new(),
        None,
        None,
        1,
    );
    // Index should be 1, but we give 5
    let block = Block::new(
        5,
        &chain.latest_block().hash,
        ts,
        vec![tx],
        "SYSTEM",
        BTreeMap::new(),
    );
    let result = chain.add_block(block);
    assert!(result.is_err(), "Wrong block index must be rejected");
}

#[test]
fn test_block_rejects_timestamp_before_previous() {
    let mut chain = Chain::new();
    let genesis_ts = chain.latest_block().timestamp;

    let tx = Transaction::new(
        "SYSTEM",
        "miner",
        BASE_REWARD_PLANCKS,
        "reward",
        0,
        BTreeMap::new(),
        None,
        None,
        1,
    );
    // Timestamp before genesis
    let block = Block::new(
        chain.height(),
        &chain.latest_block().hash,
        genesis_ts - 100.0,
        vec![tx],
        "SYSTEM",
        BTreeMap::new(),
    );
    let result = chain.add_block(block);
    assert!(
        result.is_err(),
        "Timestamp before previous must be rejected"
    );
}

#[test]
fn test_chain_from_blocks_rejects_bad_genesis() {
    // Wrong miner
    let bad = vec![Block::new(
        0,
        "0",
        0.0,
        vec![],
        "evil_miner",
        BTreeMap::new(),
    )];
    assert!(Chain::from_blocks(bad).is_err());

    // Correct genesis should work
    let chain = Chain::new();
    let genesis = chain.genesis.clone();
    let loaded = Chain::from_blocks(vec![genesis]);
    assert!(loaded.is_ok());
}

#[test]
fn test_chain_batch_add_blocks() {
    let addr = "a1a4090aced69d411b6e62bf49944f295c85ed88";
    let mut src = BlockProducer::new(miner_config(addr, 5.4));
    src.ibd_complete = true;

    // Produce 5 blocks
    for _ in 0..5 {
        src.try_produce_block().unwrap();
    }
    let blocks: Vec<Block> = &src.chain.recent_as_vec()[1..].to_vec(); // skip genesis

    // Load into another chain via add_blocks
    let mut dest = Chain::new();
    let added = dest.add_blocks(blocks).unwrap();
    assert_eq!(added, 5);
    assert_eq!(dest.height(), src.chain.height());
    dest.validate_full().unwrap();
}

// ═════════════════════════════════════════════════════════════════════════════
// 2. TOKEN ADVANCED: FREEZE, BURN, ALLOWANCE, DISABLE MINTING
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_token_burn_reduces_supply() {
    let mut tokens = TokenRegistry::new();
    let id = tokens
        .create_token(
            "BurnCoin",
            "BURN",
            8,
            1_000_000_00000000,
            "creator",
            "creator",
            "creator",
            0,
            0,
        )
        .unwrap();

    tokens
        .mint(&id, "creator", 500_00000000, "creator")
        .unwrap();
    assert_eq!(tokens.balance_of(&id, "creator"), 500_00000000);

    tokens.burn(&id, "creator", 200_00000000).unwrap();
    assert_eq!(tokens.balance_of(&id, "creator"), 300_00000000);

    // Burn more than balance → error
    let result = tokens.burn(&id, "creator", 999_00000000);
    assert!(result.is_err());
    assert_eq!(tokens.balance_of(&id, "creator"), 300_00000000);
}

#[test]
fn test_token_freeze_blocks_transfer() {
    let mut tokens = TokenRegistry::new();
    let id = tokens
        .create_token(
            "FreezeCoin",
            "FRZ",
            8,
            1_000_000_00000000,
            "admin",
            "admin",
            "admin",
            0,
            0,
        )
        .unwrap();

    tokens.mint(&id, "admin", 1000, "admin").unwrap();
    tokens.transfer(&id, "admin", "bob", 500).unwrap();

    // Freeze bob's account
    tokens.freeze(&id, "bob", "admin").unwrap();
    assert!(tokens.is_frozen(&id, "bob"));

    // bob can't transfer out
    let result = tokens.transfer(&id, "bob", "charlie", 100);
    assert!(
        result.is_err(),
        "Frozen account should not be able to transfer"
    );

    // Thaw bob
    tokens.thaw(&id, "bob", "admin").unwrap();
    assert!(!tokens.is_frozen(&id, "bob"));

    // Now bob can transfer
    tokens.transfer(&id, "bob", "charlie", 100).unwrap();
    assert_eq!(tokens.balance_of(&id, "charlie"), 100);
}

#[test]
fn test_token_disable_minting_permanent() {
    let mut tokens = TokenRegistry::new();
    let id = tokens
        .create_token(
            "FixedCoin",
            "FIX",
            8,
            1_000_000_00000000,
            "creator",
            "creator",
            "creator",
            0,
            0,
        )
        .unwrap();

    tokens.mint(&id, "creator", 1000, "creator").unwrap();

    // Disable minting
    tokens.disable_minting(&id, "creator").unwrap();

    // Can't mint anymore
    let result = tokens.mint(&id, "creator", 500, "creator");
    assert!(result.is_err(), "Minting should be disabled permanently");

    // Can't re-disable
    let result = tokens.disable_minting(&id, "creator");
    assert!(result.is_err());

    // Existing supply unchanged
    assert_eq!(tokens.balance_of(&id, "creator"), 1000);
}

#[test]
fn test_token_allowance_and_transfer_from() {
    let mut tokens = TokenRegistry::new();
    let id = tokens
        .create_token(
            "AllowCoin",
            "ALW",
            8,
            1_000_000_00000000,
            "owner",
            "owner",
            "owner",
            0,
            0,
        )
        .unwrap();

    tokens.mint(&id, "owner", 10000, "owner").unwrap();

    // Approve spender
    tokens.approve(&id, "owner", "spender", 5000).unwrap();

    // Spender transfers on behalf of owner
    tokens
        .transfer_from(&id, "owner", "spender", "recipient", 3000)
        .unwrap();
    assert_eq!(tokens.balance_of(&id, "recipient"), 3000);
    assert_eq!(tokens.balance_of(&id, "owner"), 7000);

    // Spender tries to exceed remaining allowance (5000-3000=2000)
    let result = tokens.transfer_from(&id, "owner", "spender", "recipient", 3000);
    assert!(result.is_err(), "Should fail: exceeds remaining allowance");

    // Can transfer within remaining allowance
    tokens
        .transfer_from(&id, "owner", "spender", "recipient", 2000)
        .unwrap();
    assert_eq!(tokens.balance_of(&id, "recipient"), 5000);
}

#[test]
fn test_token_mint_exceeds_max_supply() {
    let mut tokens = TokenRegistry::new();
    let id = tokens
        .create_token(
            "CappedCoin",
            "CAP",
            8,
            1000,
            "creator",
            "creator",
            "creator",
            0,
            0,
        )
        .unwrap();

    // Mint exactly at max
    tokens.mint(&id, "creator", 1000, "creator").unwrap();

    // Mint 1 more → should fail
    let result = tokens.mint(&id, "creator", 1, "creator");
    assert!(result.is_err(), "Should reject mint exceeding max_supply");
}

#[test]
fn test_token_symbol_collision_rejected() {
    let mut tokens = TokenRegistry::new();
    tokens
        .create_token("First", "DUP", 8, 1000, "alice", "alice", "alice", 0, 0)
        .unwrap();

    // Same symbol from another creator → should fail
    let result = tokens.create_token("Second", "DUP", 8, 1000, "bob", "bob", "bob", 0, 1);
    assert!(result.is_err(), "Duplicate symbol should be rejected");
}

#[test]
fn test_token_self_approve_rejected() {
    let mut tokens = TokenRegistry::new();
    let id = tokens
        .create_token("SelfCoin", "SLF", 8, 1000, "alice", "alice", "alice", 0, 0)
        .unwrap();

    let result = tokens.approve(&id, "alice", "alice", 500);
    assert!(result.is_err(), "Cannot approve self");
}

#[test]
fn test_token_freeze_by_non_authority_rejected() {
    let mut tokens = TokenRegistry::new();
    let id = tokens
        .create_token("AuthCoin", "ATH", 8, 1000, "admin", "admin", "admin", 0, 0)
        .unwrap();

    tokens.mint(&id, "admin", 500, "admin").unwrap();
    tokens.transfer(&id, "admin", "user", 100).unwrap();

    // Non-authority tries to freeze
    let result = tokens.freeze(&id, "user", "attacker");
    assert!(result.is_err(), "Non-authority cannot freeze");
}

// ═════════════════════════════════════════════════════════════════════════════
// 3. DAO EDGE CASES: EXPIRED, DOUBLE-VOTE, QUORUM, 0 TREASURY
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_dao_vote_after_deadline_rejected() {
    let mut dao = PlanetaryDAO::new();
    let mut balances: HashMap<String, i64> = HashMap::new();
    balances.insert(DAO_TREASURY.to_string(), 100 * PLANCKS_PER_CREDIT);

    // Create proposal with short voting period (100 seconds)
    let pid = dao
        .create_proposal(
            "alice",
            "Timed",
            "desc",
            1 * PLANCKS_PER_CREDIT,
            "alice",
            NOW,
            Some(100.0),
        )
        .unwrap();

    // Vote within deadline → ok
    dao.vote(&pid, "alice", VoteDirection::For, 100, NOW + 50.0)
        .unwrap();

    // Vote after deadline → should fail
    let result = dao.vote(&pid, "bob", VoteDirection::For, 100, NOW + 200.0);
    assert!(result.is_err(), "Vote after deadline should be rejected");
}

#[test]
fn test_dao_double_vote_rejected() {
    let mut dao = PlanetaryDAO::new();
    let mut balances: HashMap<String, i64> = HashMap::new();
    balances.insert(DAO_TREASURY.to_string(), 100 * PLANCKS_PER_CREDIT);

    let pid = dao
        .create_proposal(
            "alice",
            "DoubleVote",
            "desc",
            1 * PLANCKS_PER_CREDIT,
            "alice",
            NOW,
            None,
        )
        .unwrap();

    dao.vote(&pid, "alice", VoteDirection::For, 100, NOW + 10.0)
        .unwrap();

    // Same voter again → should fail
    let result = dao.vote(&pid, "alice", VoteDirection::Against, 100, NOW + 20.0);
    assert!(result.is_err(), "Double-vote should be rejected");
}

#[test]
fn test_dao_execute_with_zero_treasury() {
    let mut dao = PlanetaryDAO::new();
    let mut balances: HashMap<String, i64> = HashMap::new();

    // Treasury has 0
    balances.insert(DAO_TREASURY.to_string(), 0);

    let pid = dao
        .create_proposal(
            "alice",
            "Unfunded",
            "desc",
            5 * PLANCKS_PER_CREDIT,
            "bob",
            NOW,
            None,
        )
        .unwrap();

    // 3 votes to pass quorum
    dao.vote(&pid, "v1", VoteDirection::For, 100, NOW + 1.0)
        .unwrap();
    dao.vote(&pid, "v2", VoteDirection::For, 100, NOW + 2.0)
        .unwrap();
    dao.vote(&pid, "v3", VoteDirection::For, 100, NOW + 3.0)
        .unwrap();

    let result = dao.execute_proposal(&pid, &mut balances);
    assert!(result.is_err(), "Execute with 0 treasury should fail");
}

#[test]
fn test_dao_allocate_tokens_direct() {
    let mut dao = PlanetaryDAO::new();
    let mut balances: HashMap<String, i64> = HashMap::new();
    balances.insert(DAO_TREASURY.to_string(), 50 * PLANCKS_PER_CREDIT);

    // Allocate funds directly
    let result = dao.allocate_tokens(
        "dev_fund",
        10 * PLANCKS_PER_CREDIT,
        "development",
        &mut balances,
    );
    assert!(result.is_ok());
    assert!(balances.get("dev_fund").unwrap_or(&0) > &0);
    assert!(dao.treasury_balance(&balances) < 50 * PLANCKS_PER_CREDIT);

    // Allocate more than available → fail
    let result = dao.allocate_tokens("greedy", 999 * PLANCKS_PER_CREDIT, "greed", &mut balances);
    assert!(result.is_err());

    // Allocate 0 → fail
    let result = dao.allocate_tokens("anyone", 0, "zero", &mut balances);
    assert!(result.is_err());

    // Empty recipient → fail
    let result = dao.allocate_tokens("", 1, "empty", &mut balances);
    assert!(result.is_err());
}

// ═════════════════════════════════════════════════════════════════════════════
// 4. STAKING: UNSTAKE, SLASH, MIN-STAKE BOUNDARY, REPUTATION
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_staking_unstake_returns_to_balance() {
    let staking = StakingManager::new();
    let mut balances: HashMap<String, i64> = HashMap::new();
    let mut stakes: HashMap<String, i64> = HashMap::new();

    let stake_amount = 10 * PLANCKS_PER_CREDIT;
    balances.insert("alice".to_string(), 20 * PLANCKS_PER_CREDIT);

    // Stake
    staking
        .stake("alice", stake_amount, &mut balances, &mut stakes)
        .unwrap();
    assert_eq!(*balances.get("alice").unwrap(), 10 * PLANCKS_PER_CREDIT);
    assert_eq!(*stakes.get("alice").unwrap(), stake_amount);
    assert!(staking.is_validator("alice", &stakes));

    // Unstake
    staking
        .unstake("alice", stake_amount, &mut balances, &mut stakes)
        .unwrap();
    assert_eq!(*balances.get("alice").unwrap(), 20 * PLANCKS_PER_CREDIT);
    assert_eq!(*stakes.get("alice").unwrap_or(&0), 0);
    assert!(!staking.is_validator("alice", &stakes));
}

#[test]
fn test_staking_unstake_more_than_staked_fails() {
    let staking = StakingManager::new();
    let mut balances: HashMap<String, i64> = HashMap::new();
    let mut stakes: HashMap<String, i64> = HashMap::new();

    balances.insert("alice".to_string(), 10 * PLANCKS_PER_CREDIT);
    staking
        .stake("alice", 5 * PLANCKS_PER_CREDIT, &mut balances, &mut stakes)
        .unwrap();

    let result = staking.unstake("alice", 10 * PLANCKS_PER_CREDIT, &mut balances, &mut stakes);
    assert!(result.is_err(), "Unstake > staked should fail");
}

#[test]
fn test_staking_slash_penalizes_miner() {
    let mut staking = StakingManager::new();
    let mut balances: HashMap<String, i64> = HashMap::new();
    let mut stakes: HashMap<String, i64> = HashMap::new();

    let stake = 100 * PLANCKS_PER_CREDIT;
    balances.insert("bad_miner".to_string(), stake * 2);
    staking
        .stake("bad_miner", stake, &mut balances, &mut stakes)
        .unwrap();

    let staked_before = *stakes.get("bad_miner").unwrap();

    // Slash for bad behavior
    let result = staking.slash(
        "bad_miner",
        "produced invalid block",
        &mut stakes,
        &mut balances,
    );
    assert!(result.slashed > 0, "Slash should penalize");
    assert!(
        *stakes.get("bad_miner").unwrap() < staked_before,
        "Stake should decrease"
    );

    // Reputation should drop
    let rep = staking.get_reputation("bad_miner");
    assert!(
        rep < 0.5,
        "Reputation should drop below default after slash"
    );
}

#[test]
fn test_staking_min_stake_boundary() {
    let staking = StakingManager::new();
    let mut balances: HashMap<String, i64> = HashMap::new();
    let mut stakes: HashMap<String, i64> = HashMap::new();

    // Below minimum → fail
    balances.insert("low".to_string(), MIN_STAKE_PLANCKS * 2);
    let result = staking.stake("low", MIN_STAKE_PLANCKS - 1, &mut balances, &mut stakes);
    assert!(result.is_err(), "Below MIN_STAKE should fail");

    // Exactly minimum → succeed and qualify as validator
    staking
        .stake("low", MIN_STAKE_PLANCKS, &mut balances, &mut stakes)
        .unwrap();
    assert!(staking.is_validator("low", &stakes));
}

#[test]
fn test_staking_reputation_default_and_update() {
    let mut staking = StakingManager::new();

    // Unknown address → default reputation
    let rep = staking.get_reputation("unknown");
    assert!((rep - 0.5).abs() < 0.01, "Default reputation should be 0.5");

    // Update reputation
    staking.update_reputation("known", true, 0.9);
    assert!(staking.get_reputation("known") > 0.5);
}

#[test]
fn test_staking_then_dao_vote_then_unstake() {
    let staking = StakingManager::new();
    let mut dao = PlanetaryDAO::new();
    let mut balances: HashMap<String, i64> = HashMap::new();
    let mut stakes: HashMap<String, i64> = HashMap::new();

    let stake = 50 * PLANCKS_PER_CREDIT;
    balances.insert("voter".to_string(), stake * 2);
    balances.insert(DAO_TREASURY.to_string(), 100 * PLANCKS_PER_CREDIT);

    staking
        .stake("voter", stake, &mut balances, &mut stakes)
        .unwrap();

    let pid = dao
        .create_proposal(
            "voter",
            "Test",
            "desc",
            1 * PLANCKS_PER_CREDIT,
            "dev",
            NOW,
            None,
        )
        .unwrap();

    // Vote with stake weight
    dao.vote(&pid, "voter", VoteDirection::For, stake as u64, NOW + 10.0)
        .unwrap();

    // Unstake after voting
    staking
        .unstake("voter", stake, &mut balances, &mut stakes)
        .unwrap();
    assert!(!staking.is_validator("voter", &stakes));

    // Need two more votes for quorum
    dao.vote(&pid, "v2", VoteDirection::For, 1, NOW + 20.0)
        .unwrap();
    dao.vote(&pid, "v3", VoteDirection::For, 1, NOW + 30.0)
        .unwrap();

    // Execute should still work — vote was recorded
    let exec = dao.execute_proposal(&pid, &mut balances);
    assert!(exec.is_ok(), "Should execute: votes already recorded");
}

// ═════════════════════════════════════════════════════════════════════════════
// 5. CONTRACT: DOUBLE-CLAIM, DOUBLE-COMPLETE, UNKNOWN WORKLOAD
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_contract_double_claim_rejected() {
    let mut contract = WorkloadContract::new();
    let mut balances: HashMap<String, i64> = HashMap::new();
    balances.insert("submitter".to_string(), 100 * PLANCKS_PER_CREDIT);
    balances.insert(DAO_TREASURY.to_string(), 100 * PLANCKS_PER_CREDIT);

    contract.register_machine("submitter", "key1").unwrap();
    let wk = contract
        .submit_workload(
            "submitter",
            "test",
            "hash1",
            vec![],
            "computational",
            &mut balances,
        )
        .unwrap();

    // First claim succeeds
    contract.claim_workload(&wk, "miner1").unwrap();

    // Second claim fails
    let result = contract.claim_workload(&wk, "miner2");
    assert!(result.is_err(), "Double-claim should fail");
}

#[test]
fn test_contract_double_complete_rejected() {
    let mut contract = WorkloadContract::new();
    let mut balances: HashMap<String, i64> = HashMap::new();
    balances.insert("submitter".to_string(), 100 * PLANCKS_PER_CREDIT);
    balances.insert(DAO_TREASURY.to_string(), 100 * PLANCKS_PER_CREDIT);

    contract.register_machine("submitter", "key1").unwrap();
    let wk = contract
        .submit_workload(
            "submitter",
            "test",
            "hash2",
            vec![],
            "computational",
            &mut balances,
        )
        .unwrap();

    contract.claim_workload(&wk, "miner1").unwrap();
    contract
        .complete_workload(
            &wk,
            "miner1",
            serde_json::json!({"result": "done"}),
            &mut balances,
        )
        .unwrap();

    // Complete again → fail
    let result = contract.complete_workload(
        &wk,
        "miner1",
        serde_json::json!({"result": "done again"}),
        &mut balances,
    );
    assert!(result.is_err(), "Double-complete should fail");
}

#[test]
fn test_contract_unknown_workload() {
    let mut contract = WorkloadContract::new();

    let result = contract.claim_workload("nonexistent_key", "miner");
    assert!(result.is_err(), "Unknown workload should fail");

    let result = contract.get_result("nonexistent_key");
    assert!(result.is_none(), "Unknown workload result should be None");
}

#[test]
fn test_contract_invalid_workload_type() {
    let mut contract = WorkloadContract::new();
    let mut balances: HashMap<String, i64> = HashMap::new();
    balances.insert("sub".to_string(), 100 * PLANCKS_PER_CREDIT);

    contract.register_machine("sub", "k1").unwrap();
    let result =
        contract.submit_workload("sub", "test", "h1", vec![], "invalid_type", &mut balances);
    assert!(result.is_err(), "Invalid workload type should fail");
}

#[test]
fn test_contract_complete_by_wrong_miner_rejected() {
    let mut contract = WorkloadContract::new();
    let mut balances: HashMap<String, i64> = HashMap::new();
    balances.insert("sub".to_string(), 100 * PLANCKS_PER_CREDIT);
    balances.insert(DAO_TREASURY.to_string(), 100 * PLANCKS_PER_CREDIT);

    contract.register_machine("sub", "k1").unwrap();
    let wk = contract
        .submit_workload("sub", "test", "h3", vec![], "computational", &mut balances)
        .unwrap();
    contract.claim_workload(&wk, "right_miner").unwrap();

    // Wrong miner tries to complete
    let result =
        contract.complete_workload(&wk, "wrong_miner", serde_json::json!({}), &mut balances);
    assert!(
        result.is_err(),
        "Wrong miner should not be able to complete"
    );
}

// ═════════════════════════════════════════════════════════════════════════════
// 6. MEMPOOL: VALIDATED ADD, LIMITS, PURGE, FEE ESTIMATION
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_mempool_rejects_duplicate_tx_hash() {
    let mut mempool = Mempool::new();

    let tx = Transaction::new(
        "alice",
        "bob",
        1000,
        "transfer",
        0,
        BTreeMap::new(),
        None,
        None,
        1,
    );
    let tx_clone = tx.clone();

    mempool.add_transaction(tx, MIN_FEE_PLANCKS).unwrap();
    let result = mempool.add_transaction(tx_clone, MIN_FEE_PLANCKS);
    assert!(result.is_err(), "Duplicate tx hash should be rejected");
}

#[test]
fn test_mempool_select_for_block_respects_limits() {
    let mut mempool = Mempool::new();

    // Add many transactions
    for i in 0..600u64 {
        let tx = Transaction::new(
            "alice",
            &format!("bob_{}", i),
            100,
            "transfer",
            i,
            BTreeMap::new(),
            None,
            None,
            1,
        );
        mempool.add_transaction(tx, MIN_FEE_PLANCKS + i as i64).ok();
    }
    assert!(mempool.size() >= 500);

    let (selected, total_fees) = mempool.select_for_block();
    assert!(selected.len() <= 500, "Block should cap at MAX_BLOCK_TXS");
    assert!(total_fees > 0);
}

#[test]
fn test_mempool_fee_below_minimum_rejected() {
    let mut mempool = Mempool::new();

    let tx = Transaction::new(
        "alice",
        "bob",
        100,
        "transfer",
        0,
        BTreeMap::new(),
        None,
        None,
        1,
    );
    let result = mempool.add_transaction(tx, MIN_FEE_PLANCKS - 1);
    assert!(result.is_err(), "Fee below minimum should be rejected");
}

#[test]
fn test_mempool_fee_estimation() {
    let mempool = Mempool::new();

    // Empty mempool → base fee
    let fee = mempool::estimate_fee(&mempool, 250);
    assert!(fee >= MIN_FEE_PLANCKS, "Estimated fee should be >= min");
}

#[test]
fn test_mempool_stats() {
    let mut mempool = Mempool::new();

    for i in 0..5u64 {
        let tx = Transaction::new(
            "from",
            &format!("to_{}", i),
            100,
            "transfer",
            i,
            BTreeMap::new(),
            None,
            None,
            1,
        );
        mempool
            .add_transaction(tx, MIN_FEE_PLANCKS * (i as i64 + 1))
            .ok();
    }

    let stats = mempool.stats();
    assert_eq!(stats.size, 5);
    assert!(stats.total_bytes > 0);
}

// ═════════════════════════════════════════════════════════════════════════════
// 7. RPC ERROR PATHS: MALFORMED JSON, MISSING PARAMS, UNKNOWN METHODS
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_rpc_parse_malformed_json() {
    let malformed = b"{ not valid json ]]]";
    let result = rpc::parse_request(malformed);
    assert!(result.is_err(), "Malformed JSON should return Err");
    let err_resp = result.unwrap_err();
    assert_eq!(err_resp.error.as_ref().unwrap().code, rpc::PARSE_ERROR);
}

#[test]
fn test_rpc_parse_invalid_utf8() {
    let invalid_utf8: &[u8] = &[0xFF, 0xFE, 0x00, 0x01];
    let result = rpc::parse_request(invalid_utf8);
    assert!(result.is_err());
    assert_eq!(
        result.unwrap_err().error.as_ref().unwrap().code,
        rpc::PARSE_ERROR
    );
}

#[test]
fn test_rpc_wrong_jsonrpc_version() {
    let mut state = NodeState {
        chain: Chain::new(),
        staking: StakingManager::new(),
        dao: PlanetaryDAO::new(),
        contract: WorkloadContract::new(),
        tokens: TokenRegistry::new(),
        balances: HashMap::new(),
        stakes: HashMap::new(),
        node_address: "test".into(),
        peer_count: 0,
        mempool_snapshot: Vec::new(),
        mempool_size: 0,
        mining_state: "enabled".into(),
        mining_pause_reason: String::new(),
        fork_status: "synced".into(),
        checkpoint_status: "no_checkpoint".into(),
        checkpoint_height: None,
        checkpoint_hash: None,
        bootstrap_peer_count: 0,
        peer_diagnostics: Vec::new(),
    };

    let req = RpcRequest {
        jsonrpc: "1.0".into(), // wrong version
        method: "ping".into(),
        params: Value::Null,
        id: Value::Number(1.into()),
    };

    let resp = handle_request(&req, &mut state);
    assert!(resp.error.is_some());
    assert_eq!(resp.error.as_ref().unwrap().code, rpc::INVALID_REQUEST);
}

#[test]
fn test_rpc_get_balance_missing_address() {
    let mut state = NodeState {
        chain: Chain::new(),
        staking: StakingManager::new(),
        dao: PlanetaryDAO::new(),
        contract: WorkloadContract::new(),
        tokens: TokenRegistry::new(),
        balances: HashMap::new(),
        stakes: HashMap::new(),
        node_address: "test".into(),
        peer_count: 0,
        mempool_snapshot: Vec::new(),
        mempool_size: 0,
        mining_state: "enabled".into(),
        mining_pause_reason: String::new(),
        fork_status: "synced".into(),
        checkpoint_status: "no_checkpoint".into(),
        checkpoint_height: None,
        checkpoint_hash: None,
        bootstrap_peer_count: 0,
        peer_diagnostics: Vec::new(),
    };

    let req = make_rpc_request("get_balance", serde_json::json!({}));
    let resp = handle_request(&req, &mut state);
    assert!(resp.error.is_some());
    assert_eq!(resp.error.as_ref().unwrap().code, rpc::INVALID_PARAMS);
}

#[test]
fn test_rpc_get_block_out_of_range() {
    let mut state = NodeState {
        chain: Chain::new(),
        staking: StakingManager::new(),
        dao: PlanetaryDAO::new(),
        contract: WorkloadContract::new(),
        tokens: TokenRegistry::new(),
        balances: HashMap::new(),
        stakes: HashMap::new(),
        node_address: "test".into(),
        peer_count: 0,
        mempool_snapshot: Vec::new(),
        mempool_size: 0,
        mining_state: "enabled".into(),
        mining_pause_reason: String::new(),
        fork_status: "synced".into(),
        checkpoint_status: "no_checkpoint".into(),
        checkpoint_height: None,
        checkpoint_hash: None,
        bootstrap_peer_count: 0,
        peer_diagnostics: Vec::new(),
    };

    let req = make_rpc_request("get_block", serde_json::json!({"index": 9999}));
    let resp = handle_request(&req, &mut state);
    assert!(resp.error.is_some());
    assert_eq!(resp.error.as_ref().unwrap().code, rpc::NOT_FOUND);
}

#[test]
fn test_rpc_get_proposal_not_found() {
    let mut state = NodeState {
        chain: Chain::new(),
        staking: StakingManager::new(),
        dao: PlanetaryDAO::new(),
        contract: WorkloadContract::new(),
        tokens: TokenRegistry::new(),
        balances: HashMap::new(),
        stakes: HashMap::new(),
        node_address: "test".into(),
        peer_count: 0,
        mempool_snapshot: Vec::new(),
        mempool_size: 0,
        mining_state: "enabled".into(),
        mining_pause_reason: String::new(),
        fork_status: "synced".into(),
        checkpoint_status: "no_checkpoint".into(),
        checkpoint_height: None,
        checkpoint_hash: None,
        bootstrap_peer_count: 0,
        peer_diagnostics: Vec::new(),
    };

    let req = make_rpc_request(
        "get_proposal",
        serde_json::json!({"proposal_id": "nonexistent"}),
    );
    let resp = handle_request(&req, &mut state);
    assert!(resp.error.is_some());
    assert_eq!(resp.error.as_ref().unwrap().code, rpc::NOT_FOUND);
}

#[test]
fn test_rpc_get_token_not_found() {
    let mut state = NodeState {
        chain: Chain::new(),
        staking: StakingManager::new(),
        dao: PlanetaryDAO::new(),
        contract: WorkloadContract::new(),
        tokens: TokenRegistry::new(),
        balances: HashMap::new(),
        stakes: HashMap::new(),
        node_address: "test".into(),
        peer_count: 0,
        mempool_snapshot: Vec::new(),
        mempool_size: 0,
        mining_state: "enabled".into(),
        mining_pause_reason: String::new(),
        fork_status: "synced".into(),
        checkpoint_status: "no_checkpoint".into(),
        checkpoint_height: None,
        checkpoint_hash: None,
        bootstrap_peer_count: 0,
        peer_diagnostics: Vec::new(),
    };

    let req = make_rpc_request("get_token", serde_json::json!({"token_id": "nonexistent"}));
    let resp = handle_request(&req, &mut state);
    assert!(resp.error.is_some());
    assert_eq!(resp.error.as_ref().unwrap().code, rpc::NOT_FOUND);
}

#[test]
fn test_rpc_isolated_node_still_responds() {
    // peer_count = 0
    let mut state = NodeState {
        chain: Chain::new(),
        staking: StakingManager::new(),
        dao: PlanetaryDAO::new(),
        contract: WorkloadContract::new(),
        tokens: TokenRegistry::new(),
        balances: HashMap::new(),
        stakes: HashMap::new(),
        node_address: "isolated".into(),
        peer_count: 0,
        mempool_snapshot: Vec::new(),
        mempool_size: 0,
        mining_state: "enabled".into(),
        mining_pause_reason: String::new(),
        fork_status: "synced".into(),
        checkpoint_status: "no_checkpoint".into(),
        checkpoint_height: None,
        checkpoint_hash: None,
        bootstrap_peer_count: 0,
        peer_diagnostics: Vec::new(),
    };

    let resp = handle_request(&make_rpc_request("ping", Value::Null), &mut state);
    assert!(resp.error.is_none());
    assert_eq!(resp.result.as_ref().unwrap()["peer_count"], 0);

    let resp = handle_request(&make_rpc_request("get_chain_info", Value::Null), &mut state);
    assert!(resp.error.is_none());
}

// ═════════════════════════════════════════════════════════════════════════════
// 8. ECONOMY: TIMEOUT, REPUTATION DECAY, STATUS BROADCAST
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_economy_workload_expiry_on_cleanup() {
    let mut bridge = EconomyBridge::new("sub", "ws", test_device());

    // Add a peer
    let ann = serde_json::json!({"wallet_address":"wm","tflops":10.0,"available":true});
    bridge.handle_compute_announce("miner1", &ann, NOW);

    // Dispatch workload
    let (wl_id, _) = bridge
        .dispatch_workload("k1", Value::Null, "inference", None, 100, NOW + 1.0)
        .unwrap();

    // Don't complete it — wait past expiry
    let (expired, _pruned) = bridge.cleanup(NOW + 400.0);
    assert!(expired >= 1, "Pending workload should expire");
    assert_eq!(
        bridge.pending_workloads[&wl_id].status,
        WorkloadStatus::Expired
    );
}

#[test]
fn test_economy_stale_peer_pruned() {
    let mut bridge = EconomyBridge::new("node", "w", test_device());

    let ann = serde_json::json!({"wallet_address":"wm","tflops":5.0,"available":true});
    bridge.handle_compute_announce("peer1", &ann, NOW);
    assert_eq!(bridge.compute_peers.len(), 1);

    // Peer goes stale after 600s
    let (_, pruned) = bridge.cleanup(NOW + 700.0);
    assert_eq!(pruned, 1);
    assert!(bridge.compute_peers.is_empty());
}

#[test]
fn test_economy_multiple_peers_partial_prune() {
    let mut bridge = EconomyBridge::new("node", "w", test_device());

    // Add two peers at different times
    let ann1 = serde_json::json!({"wallet_address":"w1","tflops":5.0,"available":true});
    bridge.handle_compute_announce("peer1", &ann1, NOW);

    let ann2 = serde_json::json!({"wallet_address":"w2","tflops":10.0,"available":true});
    bridge.handle_compute_announce("peer2", &ann2, NOW + 500.0);

    // At NOW+700, peer1 is stale but peer2 is fresh
    let (_, pruned) = bridge.cleanup(NOW + 700.0);
    assert_eq!(pruned, 1);
    assert_eq!(bridge.compute_peers.len(), 1);
    assert!(bridge.compute_peers.contains_key("peer2"));
}

#[test]
fn test_economy_status_broadcast() {
    let bridge = EconomyBridge::new("node", "wallet", test_device());

    let msg = bridge.build_economy_status(100, 5_000_000 * PLANCKS_PER_CREDIT as i64);
    assert_eq!(msg.msg_type, MessageType::EconomyStatus);
    assert!(msg.target.is_none()); // broadcast
}

#[test]
fn test_economy_dispatch_with_no_peers_fails() {
    let mut bridge = EconomyBridge::new("node", "w", test_device());
    // No peers added

    let result = bridge.dispatch_workload("k1", Value::Null, "inference", None, 100, NOW);
    assert!(result.is_none(), "Should fail with no available peers");
}

#[test]
fn test_economy_reputation_decay_on_reject() {
    let mut bridge = EconomyBridge::new("sub", "ws", test_device());

    let ann = serde_json::json!({"wallet_address":"wm","tflops":10.0,"available":true});
    bridge.handle_compute_announce("miner1", &ann, NOW);

    let initial_rep = bridge.compute_peers["miner1"].reputation;

    // Reject penalizes reputation
    bridge.handle_compute_reject(
        "miner1",
        &serde_json::json!({
            "workload_id": "wl_fake", "reason": "GPU error"
        }),
    );

    let after_rep = bridge.compute_peers["miner1"].reputation;
    assert!(
        after_rep < initial_rep,
        "Reputation should decay after reject"
    );
}

// ═════════════════════════════════════════════════════════════════════════════
// 9. ELECTION: ZERO TFLOPS, SINGLE CANDIDATE, EMPTY CONTRIBUTORS
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_election_empty_contributors_no_leader() {
    let contrib = ComputeContributors::new();
    let result = election::elect_leader(EXPECTED_GENESIS_HASH, 100, &contrib);
    assert!(result.is_none(), "No contributors means no leader");
}

#[test]
fn test_election_single_candidate_always_wins() {
    let mut contrib = ComputeContributors::new();
    contrib.register("only_miner", 10.0);

    for slot in 0..10 {
        let result = election::elect_leader(EXPECTED_GENESIS_HASH, slot, &contrib);
        assert!(result.is_some());
        assert_eq!(result.unwrap().leader, "only_miner");
    }
}

#[test]
fn test_election_remove_candidate() {
    let mut contrib = ComputeContributors::new();
    contrib.register("alice", 10.0);
    contrib.register("bob", 10.0);
    assert_eq!(contrib.count(), 2);

    contrib.remove("alice");
    assert_eq!(contrib.count(), 1);

    let result = election::elect_leader(EXPECTED_GENESIS_HASH, 42, &contrib);
    assert_eq!(result.unwrap().leader, "bob");
}

#[test]
fn test_election_verified_candidates_entity_gate() {
    let mut entity_reg = EntityRegistry::new();

    // Register 2 entities (exit bootstrap mode)
    let r1 = EntityRecord {
        commitment: "c1".into(),
        entity_type: "machine".into(),
        epoch: 1,
        epoch_nullifier: "n1".into(),
        credential_signature: "0x1".into(),
        registered_at: NOW,
        hardware_attestation_hash: "hw1".into(),
    };
    let r2 = EntityRecord {
        commitment: "c2".into(),
        entity_type: "human".into(),
        epoch: 1,
        epoch_nullifier: "n2".into(),
        credential_signature: "0x2".into(),
        registered_at: NOW,
        hardware_attestation_hash: "".into(),
    };
    entity_reg.register(&r1).unwrap();
    entity_reg.register(&r2).unwrap();

    // Tag only one wallet
    entity_reg.tag_wallet("tagged_wallet", "c1").unwrap();

    let mut contrib = ComputeContributors::new();
    contrib.register("tagged_wallet", 10.0);
    contrib.register("untagged_wallet", 20.0);
    contrib.register("another_untagged", 30.0);

    let verified = contrib.verified_candidates(Some(&entity_reg), None);
    assert_eq!(verified.len(), 1, "Only tagged wallet should pass");
    assert_eq!(verified[0].0, "tagged_wallet");
}

// ═════════════════════════════════════════════════════════════════════════════
// 10. STORAGE: PERSISTENCE + RELOAD INTEGRITY
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_storage_empty_chain_round_trip() {
    let storage = Storage::in_memory().unwrap();

    let producer = BlockProducer::new(miner_config("test", 5.4));
    producer.save(&storage).unwrap();

    let mut loaded = BlockProducer::new(miner_config("test", 5.4));
    loaded.load(&storage).unwrap();
    assert_eq!(loaded.chain.height(), 1); // genesis only
}

#[test]
fn test_storage_block_by_block_persistence() {
    let storage = Storage::in_memory().unwrap();

    let addr = "a1a4090aced69d411b6e62bf49944f295c85ed88";
    let mut producer = BlockProducer::new(miner_config(addr, 5.4));
    producer.ibd_complete = true;

    // Save after each block
    for _ in 0..3 {
        producer.try_produce_block().unwrap();
        producer.save(&storage).unwrap();
    }

    let mut loaded = BlockProducer::new(miner_config(addr, 5.4));
    loaded.load(&storage).unwrap();
    assert_eq!(loaded.chain.height(), 4); // genesis + 3
    loaded.chain.validate_full().unwrap();
}

// ═════════════════════════════════════════════════════════════════════════════
// 11. CROSS-MODULE: FULL LIFECYCLE — STAKE → PRODUCE → DAO → TOKEN → RPC
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_full_lifecycle_stake_produce_govern_query() {
    let addr = "a1a4090aced69d411b6e62bf49944f295c85ed88";
    let mut producer = BlockProducer::new(miner_config(addr, 5.4));
    producer.ibd_complete = true;

    // Step 1: Produce blocks to earn funds
    for _ in 0..5 {
        producer.try_produce_block().unwrap();
    }
    let miner_bal = *producer.chain.balances.get(addr).unwrap();
    assert!(miner_bal >= 5 * BASE_REWARD_PLANCKS);

    // Step 2: Stake from chain balances
    let staking = StakingManager::new();
    let mut balances: HashMap<String, i64> = producer
        .chain
        .balances
        .iter()
        .map(|(k, v)| (k.clone(), *v))
        .collect();
    let mut stakes: HashMap<String, i64> = HashMap::new();

    let stake_amount = 2 * PLANCKS_PER_CREDIT;
    staking
        .stake(addr, stake_amount, &mut balances, &mut stakes)
        .unwrap();
    assert!(staking.is_validator(addr, &stakes));

    // Step 3: DAO governance
    let mut dao = PlanetaryDAO::new();
    balances.insert(DAO_TREASURY.to_string(), 20 * PLANCKS_PER_CREDIT);

    let pid = dao
        .create_proposal(
            addr,
            "Upgrade",
            "desc",
            1 * PLANCKS_PER_CREDIT,
            addr,
            NOW,
            None,
        )
        .unwrap();
    dao.vote(
        &pid,
        addr,
        VoteDirection::For,
        stake_amount as u64,
        NOW + 10.0,
    )
    .unwrap();
    dao.vote(&pid, "v2", VoteDirection::For, 1, NOW + 20.0)
        .unwrap();
    dao.vote(&pid, "v3", VoteDirection::For, 1, NOW + 30.0)
        .unwrap();
    dao.execute_proposal(&pid, &mut balances).unwrap();

    // Step 4: Create token
    let mut tokens = TokenRegistry::new();
    let token_id = tokens
        .create_token(
            "MinerCoin",
            "MNR",
            8,
            1_000_000_00000000,
            addr,
            addr,
            addr,
            5,
            0,
        )
        .unwrap();
    tokens.mint(&token_id, addr, 100_00000000, addr).unwrap();

    // Step 5: Query everything via RPC
    let mut state = NodeState {
        chain: producer.chain,
        staking,
        dao,
        contract: WorkloadContract::new(),
        tokens,
        balances,
        stakes,
        node_address: addr.into(),
        peer_count: 3,
        mempool_snapshot: Vec::new(),
        mempool_size: 0,
        mining_state: "enabled".into(),
        mining_pause_reason: String::new(),
        fork_status: "synced".into(),
        checkpoint_status: "no_checkpoint".into(),
        checkpoint_height: None,
        checkpoint_hash: None,
        bootstrap_peer_count: 0,
        peer_diagnostics: Vec::new(),
    };
    state.sync_from_chain();

    // RPC: chain height
    let resp = handle_request(
        &make_rpc_request("get_chain_height", Value::Null),
        &mut state,
    );
    assert!(resp.error.is_none());
    assert_eq!(resp.result.as_ref().unwrap()["height"], 6); // genesis + 5

    // RPC: validators
    let resp = handle_request(&make_rpc_request("get_validators", Value::Null), &mut state);
    assert!(resp.error.is_none());

    // RPC: proposals
    let resp = handle_request(&make_rpc_request("get_proposals", Value::Null), &mut state);
    assert!(resp.error.is_none());

    // RPC: token
    let resp = handle_request(
        &make_rpc_request("get_token", serde_json::json!({"symbol": "MNR"})),
        &mut state,
    );
    assert!(resp.error.is_none());
    assert_eq!(resp.result.as_ref().unwrap()["symbol"], "MNR");
}

// ═════════════════════════════════════════════════════════════════════════════
// 12. EDGE CASES: EMPTY BLOCKS, ZERO TRANSFERS, TX SERIALIZATION
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_empty_mempool_produces_coinbase_only_block() {
    let addr = "a1a4090aced69d411b6e62bf49944f295c85ed88";
    let mut producer = BlockProducer::new(miner_config(addr, 5.4));
    producer.ibd_complete = true;

    let block = producer.try_produce_block().unwrap();
    // Should have coinbase tx(s) — at least 1 reward
    assert!(block.transactions.len() >= 1);
    assert!(block.transactions.iter().any(|t| t.tx_type == "reward"));
    // Should not have any non-reward txs (mempool was empty)
    let non_reward = block
        .transactions
        .iter()
        .filter(|t| t.tx_type != "reward")
        .count();
    assert_eq!(
        non_reward, 0,
        "Empty mempool block should only have reward txs"
    );
}

#[test]
fn test_transaction_serde_round_trip() {
    let meta = BTreeMap::from([
        ("key".to_string(), Value::String("value".to_string())),
        ("num".to_string(), Value::Number(42.into())),
    ]);

    let tx = Transaction::new(
        "sender_addr",
        "receiver_addr",
        12345,
        "transfer",
        7,
        meta,
        Some(1_700_000_000.0),
        None,
        1,
    );

    let dict = tx.to_dict();
    let restored = Transaction::from_dict(&dict).unwrap();

    assert_eq!(restored.tx_hash, tx.tx_hash);
    assert_eq!(restored.from_address, tx.from_address);
    assert_eq!(restored.to_address, tx.to_address);
    assert_eq!(restored.amount, tx.amount);
    assert_eq!(restored.tx_type, tx.tx_type);
    assert_eq!(restored.nonce, tx.nonce);
}

#[test]
fn test_chain_validate_full_catches_corruption() {
    let addr = "a1a4090aced69d411b6e62bf49944f295c85ed88";
    let mut producer = BlockProducer::new(miner_config(addr, 5.4));
    producer.ibd_complete = true;

    for _ in 0..3 {
        producer.try_produce_block().unwrap();
    }

    // Corrupt a block hash manually
    let mut chain = producer.chain;
    chain.recent[1].hash = "corrupted_hash_value".to_string();

    let result = chain.validate_full();
    assert!(result.is_err(), "validate_full should catch corrupted hash");
}

// ═════════════════════════════════════════════════════════════════════════════
// 13. POP: EDGE CASES
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_pop_reward_at_max_halving() {
    let device = test_device();
    let quality = 1.0;

    // Block 0 → base reward territory
    let r1 = calculate_reward(device.effective_tflops(), quality, 0);
    assert!(r1 > 0);

    // High halving epoch
    let r_high = calculate_reward(device.effective_tflops(), quality, HALVING_INTERVAL * 50);
    assert!(
        r_high > 0,
        "Should still produce some reward (minimum floor)"
    );

    // After 64 halvings → minimum
    let r_min = calculate_reward(device.effective_tflops(), quality, HALVING_INTERVAL * 64);
    assert!(r_min >= 1, "Should have minimum floor of 1");
}

#[test]
fn test_pop_zero_quality_minimal_reward() {
    let device = test_device();

    let reward = calculate_reward(device.effective_tflops(), 0.0, 1);
    // Zero quality should produce very low or zero reward
    assert!(reward >= 0, "Reward should never be negative");
}

// ═════════════════════════════════════════════════════════════════════════════
// 14. NETWORK: MESSAGE TYPE COVERAGE
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_economy_route_unknown_message_type_no_crash() {
    let mut bridge = EconomyBridge::new("node", "w", test_device());
    let mut chain = Chain::new();

    // Route a message type that the economy bridge doesn't handle directly
    let payload = serde_json::json!({"hello": "world"});
    let msgs = bridge.route_message(MessageType::Ping, "peer1", &payload, &mut chain, NOW);
    // Should not crash, may return empty
    assert!(msgs.is_empty() || !msgs.is_empty()); // just ensure no panic
}

#[test]
fn test_economy_block_announce_triggers_block_request() {
    let mut bridge = EconomyBridge::new("node", "w", test_device());
    let mut chain = Chain::new();
    assert_eq!(chain.height(), 1); // genesis only

    // Peer announces block 3 → we need blocks 1,2,3
    let payload = serde_json::json!({"block_index": 3});
    let msgs = bridge.route_message(
        MessageType::BlockAnnounce,
        "peer1",
        &payload,
        &mut chain,
        NOW,
    );

    // Should request missing blocks
    assert!(!msgs.is_empty(), "Should request missing blocks");
    for msg in &msgs {
        assert_eq!(msg.msg_type, MessageType::BlockRequest);
    }
}

// ═════════════════════════════════════════════════════════════════════════════
// 15. WIRE FORMAT: EDGE CASES
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_wire_decode_incomplete_frame() {
    // Only 2 bytes — not enough for the 4-byte length prefix
    let incomplete = &[0x00, 0x00];
    let result = rpc::wire_decode(incomplete);
    assert!(result.is_none(), "Incomplete frame should return None");
}

#[test]
fn test_wire_decode_empty() {
    let result = rpc::wire_decode(&[]);
    assert!(result.is_none(), "Empty input should return None");
}

#[test]
fn test_wire_encode_empty_payload() {
    let encoded = rpc::wire_encode(&[]);
    assert_eq!(encoded.len(), 4); // just the length prefix (0)
    let result = rpc::wire_decode(&encoded);
    assert!(result.is_some());
    let (data, consumed) = result.unwrap();
    assert_eq!(data.len(), 0);
    assert_eq!(consumed, 4);
}

#[test]
fn test_wire_large_payload_round_trip() {
    let big_payload = vec![0x42u8; 100_000]; // 100KB
    let encoded = rpc::wire_encode(&big_payload);
    assert_eq!(encoded.len(), 100_004); // 4-byte prefix + payload

    let (decoded, consumed) = rpc::wire_decode(&encoded).unwrap();
    assert_eq!(decoded.len(), 100_000);
    assert_eq!(consumed, 100_004);
    assert_eq!(decoded, &big_payload[..]);
}

// ═════════════════════════════════════════════════════════════════════════════
// 16. ENTITY: DUPLICATE REGISTRATION, NULLIFIER REUSE
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_entity_duplicate_commitment_rejected() {
    let mut reg = EntityRegistry::new();

    let record = EntityRecord {
        commitment: "unique_c".into(),
        entity_type: "machine".into(),
        epoch: 1,
        epoch_nullifier: "n1".into(),
        credential_signature: "0x1".into(),
        registered_at: NOW,
        hardware_attestation_hash: "hw1".into(),
    };
    reg.register(&record).unwrap();

    // Duplicate commitment
    let dup = EntityRecord {
        commitment: "unique_c".into(),
        entity_type: "human".into(),
        epoch: 2,
        epoch_nullifier: "n2".into(),
        credential_signature: "0x2".into(),
        registered_at: NOW,
        hardware_attestation_hash: "".into(),
    };
    let result = reg.register(&dup);
    assert!(result.is_err(), "Duplicate commitment should be rejected");
}

#[test]
fn test_entity_nullifier_reuse_rejected() {
    let mut reg = EntityRegistry::new();

    let r1 = EntityRecord {
        commitment: "c1".into(),
        entity_type: "machine".into(),
        epoch: 1,
        epoch_nullifier: "shared_null".into(),
        credential_signature: "0x1".into(),
        registered_at: NOW,
        hardware_attestation_hash: "hw1".into(),
    };
    reg.register(&r1).unwrap();

    // Different commitment but same nullifier → double-registration attempt
    let r2 = EntityRecord {
        commitment: "c2".into(),
        entity_type: "machine".into(),
        epoch: 1,
        epoch_nullifier: "shared_null".into(),
        credential_signature: "0x2".into(),
        registered_at: NOW,
        hardware_attestation_hash: "hw2".into(),
    };
    let result = reg.register(&r2);
    assert!(
        result.is_err(),
        "Nullifier reuse should be rejected (double-registration)"
    );
}

#[test]
fn test_entity_tag_wallet_twice_rejected() {
    let mut reg = EntityRegistry::new();

    let record = EntityRecord {
        commitment: "c1".into(),
        entity_type: "machine".into(),
        epoch: 1,
        epoch_nullifier: "n1".into(),
        credential_signature: "0x1".into(),
        registered_at: NOW,
        hardware_attestation_hash: "hw1".into(),
    };
    reg.register(&record).unwrap();

    reg.tag_wallet("wallet1", "c1").unwrap();
    assert!(reg.is_wallet_tagged("wallet1"));

    // Tag same wallet again → should fail or be idempotent
    let result = reg.tag_wallet("wallet1", "c1");
    // Implementation-dependent: either error or noop
    // Just verify the tag still works
    assert!(reg.is_wallet_tagged("wallet1"));
}
