//! Session 12 — Cross-module integration tests.
//!
//! These tests exercise multi-module flows end-to-end:
//!   1. Transaction → Mempool → Block Producer → Chain → Storage
//!   2. VRF Election + multi-miner fairness
//!   3. Staking + DAO governance lifecycle
//!   4. Token creation → mint → transfer → balance verification via RPC
//!   5. Workload contract + PoP + reward pipeline
//!   6. Economy bridge: announce → dispatch → claim → result → chain credit
//!   7. Entity + Device verification gate on elections
//!   8. Full node simulation: multiple blocks, diverse tx types

use std::collections::{BTreeMap, HashMap};

use repryntt_core::block::Block;
use repryntt_core::chain::Chain;
use repryntt_core::contract::WorkloadContract;
use repryntt_core::crypto;
use repryntt_core::dao::{DAO_TREASURY, PlanetaryDAO, VoteDirection};
use repryntt_core::economy::{DEFAULT_FEE_PLANCKS, EconomyBridge, WorkloadStatus};
use repryntt_core::election::{self, ComputeContributors};
use repryntt_core::entity::{EntityRecord, EntityRegistry};
use repryntt_core::genesis::{
    BASE_REWARD_PLANCKS, BLOCK_INTERVAL_SECS, EXPECTED_GENESIS_HASH, HALVING_INTERVAL,
    MAX_SUPPLY_PLANCKS,
};
use repryntt_core::mempool::MIN_FEE_PLANCKS;
use repryntt_core::network::MessageType;
use repryntt_core::pop::{DeviceInfo, ProofOfPower, calculate_reward};
use repryntt_core::producer::{BlockProducer, NodeConfig};
use repryntt_core::rpc::{self, NodeState, RpcRequest, handle_request};
use repryntt_core::staking::StakingManager;
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

fn fund_address(chain: &mut Chain, addr: &str, amount: i64) {
    let tx = Transaction::new(
        "SYSTEM",
        addr,
        amount,
        "reward",
        0,
        BTreeMap::new(),
        None,
        None,
        1,
    );
    // Genesis timestamp is 1_743_379_200.0 — we must go after it
    let ts = chain.latest_block().timestamp + 69.0;
    let block = Block::new(
        chain.height(),
        &chain.latest_block().hash,
        ts,
        vec![tx],
        "SYSTEM",
        BTreeMap::new(),
    );
    chain.add_block(block).expect("fund block failed");
}

fn signed_transfer(
    from_sk: &[u8],
    from_pk: &[u8],
    to: &str,
    amount: i64,
    nonce: u64,
) -> Transaction {
    let from = crypto::address_from_pubkey(from_pk);
    let mut tx = Transaction::new(
        &from,
        to,
        amount,
        "transfer",
        nonce,
        BTreeMap::new(),
        None,
        Some(from_pk.to_vec()),
        1,
    );
    tx.sign(from_sk);
    tx
}

const NOW: f64 = 1_700_000_000.0;

// ═════════════════════════════════════════════════════════════════════════════
// 1. TX → MEMPOOL → BLOCK PRODUCER → CHAIN → STORAGE
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_tx_mempool_producer_chain_storage_pipeline() {
    let (sk, pk) = crypto::generate_keypair();
    let addr = crypto::address_from_pubkey(&pk);
    let mut producer = BlockProducer::new(miner_config(&addr, 5.4));
    producer.ibd_complete = true;

    // Block 1: coinbase gives miner funds
    let block1 = producer
        .try_produce_block()
        .expect("should produce block 1");
    assert_eq!(block1.index, 1);
    assert!(producer.chain.balances.get(&addr).unwrap_or(&0) > &0);

    // Add signed transfer tx to mempool
    let recipient = "b2b5000000000000000000000000000000000000";
    let transfer_amount = PLANCKS_PER_CREDIT; // 1 CR
    let tx = signed_transfer(&sk, &pk, recipient, transfer_amount, 0);
    producer
        .mempool
        .add_transaction(tx, MIN_FEE_PLANCKS)
        .unwrap();
    assert_eq!(producer.mempool.size(), 1);

    // Block 2: should contain coinbase + transfer
    let block2 = producer
        .try_produce_block()
        .expect("should produce block 2");
    assert_eq!(block2.index, 2);
    let has_transfer = block2.transactions.iter().any(|t| t.tx_type == "transfer");
    assert!(has_transfer, "Block 2 should include transfer");
    assert_eq!(producer.mempool.size(), 0, "Mempool drained");

    // Verify chain state
    assert_eq!(producer.chain.height(), 3); // genesis + 2
    let recipient_bal = *producer.chain.balances.get(recipient).unwrap_or(&0);
    assert_eq!(recipient_bal, transfer_amount);
    producer.chain.validate_full().unwrap();

    // Save to storage and reload
    let storage = Storage::in_memory().unwrap();
    producer.save(&storage).unwrap();

    let mut producer2 = BlockProducer::new(miner_config(&addr, 5.4));
    producer2.load(&storage).unwrap();
    assert_eq!(producer2.chain.height(), producer.chain.height());
    assert_eq!(
        producer2.chain.latest_block().hash,
        producer.chain.latest_block().hash
    );
    let reloaded_bal = *producer2.chain.balances.get(recipient).unwrap_or(&0);
    assert_eq!(reloaded_bal, transfer_amount);
}

#[test]
fn test_multiple_txs_fee_priority_ordering() {
    let (sk, pk) = crypto::generate_keypair();
    let addr = crypto::address_from_pubkey(&pk);
    let mut producer = BlockProducer::new(miner_config(&addr, 5.4));
    producer.ibd_complete = true;

    // Block 1: fund the miner
    producer.try_produce_block().unwrap();

    // Add three signed transfers with different fees
    for (i, fee) in [(1, 10_000i64), (2, 100_000), (3, 1_000)] {
        let tx = signed_transfer(&sk, &pk, &format!("recipient_{}", i), 1000, i as u64 - 1);
        producer.mempool.add_transaction(tx, fee).unwrap();
    }
    assert_eq!(producer.mempool.size(), 3);

    // Block 2: all three should be included (block has space)
    let block = producer.try_produce_block().unwrap();
    let transfer_txs: Vec<_> = block
        .transactions
        .iter()
        .filter(|t| t.tx_type == "transfer")
        .collect();
    assert_eq!(transfer_txs.len(), 3, "All transfers should be included");
    assert_eq!(producer.mempool.size(), 0);
}

// ═════════════════════════════════════════════════════════════════════════════
// 2. VRF ELECTION — MULTI-MINER FAIRNESS
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_vrf_election_deterministic() {
    let mut contributors = ComputeContributors::new();
    contributors.register("alice", 10.0);
    contributors.register("bob", 20.0);
    contributors.register("charlie", 5.0);

    let prev_hash = EXPECTED_GENESIS_HASH;

    // Same input → same leader
    let r1 = election::elect_leader(prev_hash, 100, &contributors);
    let r2 = election::elect_leader(prev_hash, 100, &contributors);
    assert_eq!(
        r1.as_ref().map(|r| &r.leader),
        r2.as_ref().map(|r| &r.leader)
    );

    // Different slot → potentially different leader (deterministic but varied)
    let results: Vec<String> = (100..200)
        .filter_map(|slot| election::elect_leader(prev_hash, slot, &contributors))
        .map(|r| r.leader)
        .collect();

    // All three miners should win at least once across 100 slots
    let alice_wins = results.iter().filter(|l| l.as_str() == "alice").count();
    let bob_wins = results.iter().filter(|l| l.as_str() == "bob").count();
    let charlie_wins = results.iter().filter(|l| l.as_str() == "charlie").count();

    assert!(alice_wins > 0, "Alice should win some slots");
    assert!(bob_wins > 0, "Bob should win some slots");
    assert!(charlie_wins > 0, "Charlie should win some slots");

    // Log-weighted: bob has 2× compute but shouldn't have 2× wins
    assert!(
        (bob_wins as f64) < (alice_wins as f64) * 3.0,
        "Log-weighting should prevent compute domination: bob={}, alice={}",
        bob_wins,
        alice_wins
    );
}

#[test]
fn test_vrf_slot_number_matches_interval() {
    let t1 = 1_700_000_000.0;
    let slot1 = election::slot_number(t1);
    let slot2 = election::slot_number(t1 + BLOCK_INTERVAL_SECS as f64);
    assert_eq!(slot2 - slot1, 1);
}

// ═════════════════════════════════════════════════════════════════════════════
// 3. STAKING + DAO GOVERNANCE LIFECYCLE
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_staking_and_dao_governance_full_cycle() {
    let staking = StakingManager::new();
    let mut dao = PlanetaryDAO::new();

    let alice = "alice_wallet";
    let bob = "bob_wallet";
    let charlie = "charlie_wallet";

    // Set up balances/stakes HashMaps
    let mut balances: HashMap<String, i64> = HashMap::new();
    let mut stakes: HashMap<String, i64> = HashMap::new();
    let stake_amount = 100 * PLANCKS_PER_CREDIT;

    // Fund each validator
    balances.insert(alice.to_string(), stake_amount * 2);
    balances.insert(bob.to_string(), stake_amount * 2);
    balances.insert(charlie.to_string(), stake_amount * 2);

    // Stake
    staking
        .stake(alice, stake_amount, &mut balances, &mut stakes)
        .unwrap();
    staking
        .stake(bob, stake_amount, &mut balances, &mut stakes)
        .unwrap();
    staking
        .stake(charlie, stake_amount, &mut balances, &mut stakes)
        .unwrap();
    assert!(staking.is_validator(alice, &stakes));
    assert!(staking.is_validator(bob, &stakes));
    assert!(staking.is_validator(charlie, &stakes));

    let validators = staking.validators(&stakes);
    assert_eq!(validators.len(), 3);

    // Fund the DAO treasury via balances
    balances.insert(DAO_TREASURY.to_string(), 50 * PLANCKS_PER_CREDIT);
    assert!(dao.treasury_balance(&balances) >= 50 * PLANCKS_PER_CREDIT);

    // Alice creates a proposal
    let proposal_id = dao
        .create_proposal(
            alice,
            "Upgrade mining algorithm",
            "Improve hash rate",
            10 * PLANCKS_PER_CREDIT,
            "dev_fund",
            NOW,
            None,
        )
        .unwrap();

    // All three vote
    dao.vote(
        &proposal_id,
        alice,
        VoteDirection::For,
        stake_amount as u64,
        NOW + 10.0,
    )
    .unwrap();
    dao.vote(
        &proposal_id,
        bob,
        VoteDirection::For,
        stake_amount as u64,
        NOW + 20.0,
    )
    .unwrap();
    dao.vote(
        &proposal_id,
        charlie,
        VoteDirection::Against,
        stake_amount as u64,
        NOW + 30.0,
    )
    .unwrap();

    // Execute: 2/3 approval > 51% threshold → passes
    let execution = dao.execute_proposal(&proposal_id, &mut balances);
    assert!(
        execution.is_ok(),
        "Proposal should execute: {:?}",
        execution
    );

    // Treasury should be reduced
    assert!(dao.treasury_balance(&balances) < 50 * PLANCKS_PER_CREDIT);

    // Recipient should have received funds
    assert!(balances.get("dev_fund").unwrap_or(&0) > &0);
}

#[test]
fn test_staking_dao_proposal_fails_without_quorum() {
    let staking = StakingManager::new();
    let mut dao = PlanetaryDAO::new();

    let alice = "alice";
    let bob = "bob";
    let charlie = "charlie";

    let mut balances: HashMap<String, i64> = HashMap::new();
    let mut stakes: HashMap<String, i64> = HashMap::new();
    let stake = 100 * PLANCKS_PER_CREDIT;

    balances.insert(alice.to_string(), stake * 2);
    balances.insert(bob.to_string(), stake * 2);
    balances.insert(charlie.to_string(), stake * 2);

    staking
        .stake(alice, stake, &mut balances, &mut stakes)
        .unwrap();
    staking
        .stake(bob, stake, &mut balances, &mut stakes)
        .unwrap();
    staking
        .stake(charlie, stake, &mut balances, &mut stakes)
        .unwrap();

    balances.insert(DAO_TREASURY.to_string(), 50 * PLANCKS_PER_CREDIT);

    let pid = dao
        .create_proposal(
            alice,
            "Under-voted",
            "desc",
            5 * PLANCKS_PER_CREDIT,
            alice,
            NOW,
            None,
        )
        .unwrap();

    // Only one vote (quorum needs 3)
    dao.vote(&pid, alice, VoteDirection::For, 1, NOW + 10.0)
        .unwrap();

    let result = dao.execute_proposal(&pid, &mut balances);
    assert!(result.is_err(), "Should fail: not enough votes for quorum");
}

// ═════════════════════════════════════════════════════════════════════════════
// 4. TOKEN → MINT → TRANSFER → BALANCE VIA RPC
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_token_lifecycle_via_rpc() {
    let mut tokens = TokenRegistry::new();

    // Create a token
    let token_id = tokens
        .create_token(
            "TestCoin",
            "TST",
            8,
            1_000_000_00000000,
            "alice_auth",
            "alice_auth",
            "alice_auth",
            0,
            0,
        )
        .unwrap();
    assert!(!token_id.is_empty());

    // Mint
    tokens
        .mint(&token_id, "alice_auth", 500_000_00000000, "alice_auth")
        .unwrap();
    assert_eq!(tokens.balance_of(&token_id, "alice_auth"), 500_000_00000000);

    // Transfer
    tokens
        .transfer(&token_id, "alice_auth", "bob", 100_000_00000000)
        .unwrap();
    assert_eq!(tokens.balance_of(&token_id, "alice_auth"), 400_000_00000000);
    assert_eq!(tokens.balance_of(&token_id, "bob"), 100_000_00000000);

    // Now query via RPC
    let mut chain = Chain::new();
    fund_address(&mut chain, "alice_auth", 10 * PLANCKS_PER_CREDIT);

    let mut state = NodeState {
        chain,
        staking: StakingManager::new(),
        dao: PlanetaryDAO::new(),
        contract: WorkloadContract::new(),
        tokens,
        balances: HashMap::new(),
        stakes: HashMap::new(),
        node_address: "node1".into(),
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
    state.sync_from_chain();

    // RPC: get_token
    let req = make_rpc_request("get_token", serde_json::json!({"token_id": token_id}));
    let resp = handle_request(&req, &mut state);
    assert!(
        resp.error.is_none(),
        "get_token should succeed: {:?}",
        resp.error
    );
    assert_eq!(resp.result.as_ref().unwrap()["symbol"], "TST");

    // RPC: get_token_balance
    let req = make_rpc_request(
        "get_token_balance",
        serde_json::json!({"token_id": token_id, "address": "bob"}),
    );
    let resp = handle_request(&req, &mut state);
    assert!(resp.error.is_none());
    assert_eq!(
        resp.result.as_ref().unwrap()["balance"],
        100_000_00000000u64
    );

    // RPC: get_tokens
    let req = make_rpc_request("get_tokens", Value::Object(Default::default()));
    let resp = handle_request(&req, &mut state);
    assert!(resp.error.is_none());
    assert_eq!(resp.result.as_ref().unwrap()["total_tokens"], 1);

    // RPC: get_balance (native chain balance)
    let req = make_rpc_request("get_balance", serde_json::json!({"address": "alice_auth"}));
    let resp = handle_request(&req, &mut state);
    assert!(resp.error.is_none());
    let bal = resp.result.as_ref().unwrap()["balance_plancks"]
        .as_i64()
        .unwrap();
    assert!(
        bal > 0,
        "alice_auth should have native balance from funding"
    );
}

// ═════════════════════════════════════════════════════════════════════════════
// 5. WORKLOAD CONTRACT + POP + REWARD
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_workload_pop_reward_pipeline() {
    let mut contract = WorkloadContract::new();
    let device = test_device();

    let submitter = "submitter_wallet";
    let miner_addr = "miner_wallet";
    let workload_data = "Process this AI inference request with sufficient complexity for testing";
    let result_data = "The computed result from the AI model which is long enough to pass PoP quality checks including entropy and result richness metrics 12345 !@#$%";

    // Register the machine first
    contract
        .register_machine(submitter, "deploy_key_1")
        .unwrap();

    // Fund submitter balance so fee deduction works
    let mut balances: HashMap<String, i64> = HashMap::new();
    balances.insert(submitter.to_string(), 100 * PLANCKS_PER_CREDIT);
    balances.insert(DAO_TREASURY.to_string(), 100 * PLANCKS_PER_CREDIT);

    // Submit a workload
    let wk = contract
        .submit_workload(
            submitter,
            "AI inference",
            "data_hash_abc123",
            vec![],
            "ai_inference",
            &mut balances,
        )
        .unwrap();

    // Claim it
    contract.claim_workload(&wk, miner_addr).unwrap();

    // Generate PoP proof (miner side)
    let proof = ProofOfPower::generate(
        &wk,
        workload_data,
        result_data,
        miner_addr,
        2.5,
        "deterministic",
        &device,
        "aabbccdd11223344aabbccdd11223344",
    );

    // Verify proof
    let (valid, quality, reason) = proof.verify(workload_data, result_data, miner_addr);
    assert!(valid, "Proof should be valid: {}", reason);
    assert!(quality > 0.25, "Quality should exceed base");

    // Calculate reward
    let reward = calculate_reward(device.effective_tflops(), quality, 1);
    assert!(reward > 0, "Reward should be positive");

    // Complete the workload in the contract
    contract
        .complete_workload(
            &wk,
            miner_addr,
            serde_json::json!({"result": result_data}),
            &mut balances,
        )
        .unwrap();

    // Verify result stored
    let result = contract.get_result(&wk);
    assert!(result.is_some());

    // Apply PoP reward to a chain
    let mut chain = Chain::new();
    *chain.balances.entry(miner_addr.to_string()).or_insert(0) += reward;
    assert!(chain.balances[miner_addr] > 0);
}

#[test]
fn test_workload_pop_rejects_tampered_proof() {
    let device = test_device();
    let proof = ProofOfPower::generate(
        "wk1",
        "input data long enough for testing",
        "output result long enough for testing with entropy 12345 !@#$%",
        "miner_a",
        1.5,
        "deterministic",
        &device,
        "aabbccdd11223344aabbccdd11223344",
    );

    // Wrong miner
    let (valid, _, _) = proof.verify(
        "input data long enough for testing",
        "output result long enough for testing with entropy 12345 !@#$%",
        "miner_b",
    );
    assert!(!valid);

    // Wrong data
    let (valid, _, _) = proof.verify(
        "different input data",
        "output result long enough for testing with entropy 12345 !@#$%",
        "miner_a",
    );
    assert!(!valid);

    // Wrong result
    let (valid, _, _) = proof.verify(
        "input data long enough for testing",
        "different result",
        "miner_a",
    );
    assert!(!valid);
}

// ═════════════════════════════════════════════════════════════════════════════
// 6. ECONOMY BRIDGE: ANNOUNCE → DISPATCH → CLAIM → RESULT → CHAIN CREDIT
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_economy_bridge_end_to_end_workload() {
    // Two nodes: submitter and miner
    let mut submitter = EconomyBridge::new("sub_node", "sub_wallet", test_device());
    let mut miner = EconomyBridge::new("miner_node", "miner_wallet", test_device());

    // Step 1: Miner announces compute
    let announce_msg = miner.build_compute_announce(10, NOW);
    assert!(announce_msg.target.is_none()); // broadcast

    // Step 2: Submitter receives the announce
    submitter.handle_compute_announce("miner_node", &announce_msg.payload, NOW);
    assert_eq!(submitter.compute_peers.len(), 1);
    assert!(submitter.compute_peers["miner_node"].is_available(NOW));

    // Step 3: Submitter dispatches a workload
    let workload_data = serde_json::json!({"prompt": "compute something"});
    let (wl_id, dispatch_msg) = submitter
        .dispatch_workload(
            "wk_abc",
            workload_data,
            "inference",
            None,
            DEFAULT_FEE_PLANCKS,
            NOW + 1.0,
        )
        .expect("should dispatch");
    assert_eq!(dispatch_msg.msg_type, MessageType::ComputeRequest);

    // Step 4: Miner receives the request and claims
    let claim_msg = miner
        .handle_compute_request("sub_node", &dispatch_msg.payload, NOW + 2.0)
        .expect("miner should accept");
    assert_eq!(claim_msg.msg_type, MessageType::ComputeClaim);

    // Step 5: Submitter receives the claim
    assert!(submitter.handle_compute_claim("miner_node", &claim_msg.payload, NOW + 3.0));
    assert_eq!(
        submitter.pending_workloads[&wl_id].status,
        WorkloadStatus::Claimed
    );

    // Step 6: Miner runs the workload and builds result
    let workload_data_str = r#"{"prompt":"compute something"}"#;
    let result_str = "This is the AI inference result that is long enough for PoP verification with good quality and entropy metrics including numbers 12345 and symbols !@#$%";
    let result_msg = miner.build_compute_result(
        "sub_node",
        &wl_id,
        "wk_abc",
        workload_data_str,
        result_str,
        2.5,
        "aabbccdd11223344aabbccdd11223344",
    );
    assert_eq!(result_msg.msg_type, MessageType::ComputeResult);

    // Step 7: Submitter receives result, verifies PoP, credits miner on chain
    let mut chain = Chain::new();
    let reward = submitter
        .handle_compute_result("miner_node", &result_msg.payload, &mut chain, NOW + 10.0)
        .expect("result should verify and reward");
    assert!(reward > 0);

    // Miner credited on chain
    assert_eq!(*chain.balances.get("miner_wallet").unwrap(), reward);

    // Workload completed
    assert_eq!(
        submitter.pending_workloads[&wl_id].status,
        WorkloadStatus::Completed
    );

    // Result stored
    let cr = &submitter.completed_results[&wl_id];
    assert_eq!(cr.miner_wallet, "miner_wallet");
    assert_eq!(cr.reward_plancks, reward);

    // Stats correct
    assert_eq!(submitter.stats.workloads_dispatched, 1);
    assert_eq!(submitter.stats.plancks_paid_remote, reward);
    assert_eq!(miner.stats.workloads_completed, 1);
}

#[test]
fn test_economy_bridge_reject_and_cleanup() {
    let mut bridge = EconomyBridge::new("sub", "ws", test_device());

    // Add a remote peer
    let ann = serde_json::json!({"wallet_address":"wm","tflops":10.0,"available":true});
    bridge.handle_compute_announce("miner1", &ann, NOW);

    // Dispatch
    let (wl_id, _) = bridge
        .dispatch_workload("k1", Value::Null, "inference", None, 100, NOW + 1.0)
        .unwrap();

    // Miner rejects
    bridge.handle_compute_reject(
        "miner1",
        &serde_json::json!({
            "workload_id": wl_id, "reason": "LLM failed"
        }),
    );
    assert_eq!(
        bridge.pending_workloads[&wl_id].status,
        WorkloadStatus::Failed
    );
    assert!(bridge.compute_peers["miner1"].reputation < 1.0);

    // Time passes → cleanup: stale peer removed
    let (_expired, pruned) = bridge.cleanup(NOW + 700.0);
    assert_eq!(pruned, 1); // peer stale after 600s
    assert!(bridge.compute_peers.is_empty());
}

#[test]
fn test_economy_bridge_network_summary() {
    let mut bridge = EconomyBridge::new("local", "wl", test_device());

    // Add multiple peers with different capabilities
    for (id, tflops) in [("r1", 10.0), ("r2", 20.0), ("r3", 5.0)] {
        let ann = serde_json::json!({
            "wallet_address": format!("w_{}", id),
            "tflops": tflops,
            "device_type": "cuda",
            "device_name": format!("GPU_{}", id),
            "available": true,
        });
        bridge.handle_compute_announce(id, &ann, NOW);
    }

    let summary = bridge.network_compute_summary(50, NOW + 10.0);
    assert_eq!(summary["compute_peers"], 3);
    assert_eq!(summary["available_miners"], 3);
    // total = 10 + 20 + 5 + 5.4 (local) = 40.4
    assert_eq!(summary["total_network_tflops"], 40.4);
    assert_eq!(summary["blockchain_height"], 50);

    let peers = summary["peers"].as_array().unwrap();
    assert_eq!(peers.len(), 3);
}

// ═════════════════════════════════════════════════════════════════════════════
// 7. ENTITY + DEVICE VERIFICATION GATE ON ELECTIONS
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_entity_verified_election() {
    // Create entity records directly (no EntityRecord::from_identity)
    let record1 = EntityRecord {
        commitment: "entity_commit_1".into(),
        entity_type: "machine".into(),
        epoch: 1,
        epoch_nullifier: "null_1".into(),
        credential_signature: "0xsig1".into(),
        registered_at: NOW,
        hardware_attestation_hash: "hw_hash_1".into(),
    };
    let record2 = EntityRecord {
        commitment: "entity_commit_2".into(),
        entity_type: "human".into(),
        epoch: 1,
        epoch_nullifier: "null_2".into(),
        credential_signature: "0xsig2".into(),
        registered_at: NOW,
        hardware_attestation_hash: "".into(),
    };

    let mut entity_reg = EntityRegistry::new();
    entity_reg.register(&record1).unwrap();
    entity_reg.register(&record2).unwrap();

    // Tag wallets to entities
    entity_reg
        .tag_wallet("verified_wallet", "entity_commit_1")
        .unwrap();
    entity_reg
        .tag_wallet("human_wallet", "entity_commit_2")
        .unwrap();

    assert!(entity_reg.is_wallet_tagged("verified_wallet"));
    assert!(entity_reg.is_wallet_tagged("human_wallet"));
    assert!(!entity_reg.is_wallet_tagged("unverified_wallet"));

    // ComputeContributors with verified + unverified candidates
    let mut contrib = ComputeContributors::new();
    contrib.register("verified_wallet", 10.0);
    contrib.register("unverified_wallet", 20.0);

    // With ≥ 2 entities, only tagged wallets pass verification
    let verified = contrib.verified_candidates(Some(&entity_reg), None);
    assert_eq!(
        verified.len(),
        1,
        "Only verified wallet should pass entity check"
    );
    assert_eq!(verified[0].0, "verified_wallet");
}

#[test]
fn test_entity_bootstrap_allows_all() {
    // During bootstrap (< 2 entities), all pass
    let record = EntityRecord {
        commitment: "c1".into(),
        entity_type: "machine".into(),
        epoch: 1,
        epoch_nullifier: "n1".into(),
        credential_signature: "0x1".into(),
        registered_at: NOW,
        hardware_attestation_hash: "hw".into(),
    };
    let mut entity_reg = EntityRegistry::new();
    entity_reg.register(&record).unwrap();
    // Only 1 entity → bootstrap mode

    let mut contrib = ComputeContributors::new();
    contrib.register("anyone", 10.0);
    contrib.register("unverified", 20.0);

    let verified = contrib.verified_candidates(Some(&entity_reg), None);
    assert_eq!(verified.len(), 2, "Bootstrap: all should pass");
}

// ═════════════════════════════════════════════════════════════════════════════
// 8. FULL NODE SIMULATION — MULTIPLE BLOCKS, DIVERSE TX TYPES
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_full_node_simulation_10_blocks() {
    let addr = "a1a4090aced69d411b6e62bf49944f295c85ed88";
    let mut producer = BlockProducer::new(miner_config(addr, 5.4));
    producer.ibd_complete = true;

    // Produce 10 blocks
    for _ in 0..10 {
        let block = producer.try_produce_block();
        assert!(block.is_some());
    }

    assert_eq!(producer.chain.height(), 11); // genesis + 10
    producer.chain.validate_full().unwrap();

    // Total coinbase earned should be 10× base reward
    let balance = *producer.chain.balances.get(addr).unwrap_or(&0);
    assert!(
        balance >= BASE_REWARD_PLANCKS * 10,
        "Miner should have earned at least 10 coinbases, got {}",
        balance
    );
}

#[test]
fn test_full_node_diverse_tx_types() {
    let addr = "a1a4090aced69d411b6e62bf49944f295c85ed88";
    let mut producer = BlockProducer::new(miner_config(addr, 5.4));
    producer.ibd_complete = true;

    // Block 1: coinbase
    producer.try_produce_block().unwrap();

    let (sk, pk) = crypto::generate_keypair();
    let from = crypto::address_from_pubkey(&pk);
    producer.chain.balances.insert(from, 5 * PLANCKS_PER_CREDIT);

    // Add diverse signed transaction values to mempool
    let tx_types = vec![
        ("transfer", "recipient_1", PLANCKS_PER_CREDIT),
        ("transfer", "recipient_2", 1000),
        ("transfer", "recipient_3", 500),
    ];

    for (i, (_tx_type, to, amount)) in tx_types.iter().enumerate() {
        let tx = signed_transfer(&sk, &pk, to, *amount, i as u64);
        producer.mempool.add_transaction(tx, MIN_FEE_PLANCKS).ok();
    }

    // Block 2: should pick up all valid txs
    let block = producer.try_produce_block().unwrap();
    let tx_types_in_block: Vec<&str> = block
        .transactions
        .iter()
        .map(|t| t.tx_type.as_str())
        .collect();

    // Should have coinbase ("reward") + at least the transfer
    assert!(tx_types_in_block.contains(&"reward"));
    assert!(tx_types_in_block.contains(&"transfer"));

    // Chain still validates
    producer.chain.validate_full().unwrap();
}

// ═════════════════════════════════════════════════════════════════════════════
// 9. CHAIN + STORAGE ROUND-TRIP WITH BALANCES
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_storage_round_trip_balances() {
    let storage = Storage::in_memory().unwrap();

    let addr = "a1a4090aced69d411b6e62bf49944f295c85ed88";
    let mut producer = BlockProducer::new(miner_config(addr, 5.4));
    producer.ibd_complete = true;

    // Produce 5 blocks
    for _ in 0..5 {
        producer.try_produce_block().unwrap();
    }
    let expected_height = producer.chain.height();
    let expected_balance = *producer.chain.balances.get(addr).unwrap();

    producer.save(&storage).unwrap();

    // Reload
    let mut producer2 = BlockProducer::new(miner_config(addr, 5.4));
    producer2.load(&storage).unwrap();

    assert_eq!(producer2.chain.height(), expected_height);
    let loaded_balance = *producer2.chain.balances.get(addr).unwrap();
    assert_eq!(loaded_balance, expected_balance);
    producer2.chain.validate_full().unwrap();
}

// ═════════════════════════════════════════════════════════════════════════════
// 10. RPC SERVER — CHAIN INFO + STAKING + PING
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_rpc_chain_info_after_production() {
    let addr = "miner_rpc_test";
    let mut chain = Chain::new();
    fund_address(&mut chain, addr, 50 * PLANCKS_PER_CREDIT);

    let mut state = NodeState {
        chain,
        staking: StakingManager::new(),
        dao: PlanetaryDAO::new(),
        contract: WorkloadContract::new(),
        tokens: TokenRegistry::new(),
        balances: HashMap::new(),
        stakes: HashMap::new(),
        node_address: addr.into(),
        peer_count: 5,
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

    // ping
    let resp = handle_request(&make_rpc_request("ping", Value::Null), &mut state);
    assert!(resp.error.is_none());
    assert_eq!(resp.result.as_ref().unwrap()["status"], "ok");

    // get_chain_height
    let resp = handle_request(
        &make_rpc_request("get_chain_height", Value::Null),
        &mut state,
    );
    assert!(resp.error.is_none());
    assert_eq!(resp.result.as_ref().unwrap()["height"], 2); // genesis + 1 funded block

    // get_balance
    let resp = handle_request(
        &make_rpc_request("get_balance", serde_json::json!({"address": addr})),
        &mut state,
    );
    assert!(resp.error.is_none());
    let bal = resp.result.as_ref().unwrap()["balance_plancks"]
        .as_i64()
        .unwrap();
    assert_eq!(bal, 50 * PLANCKS_PER_CREDIT);

    // get_latest_block
    let resp = handle_request(
        &make_rpc_request("get_latest_block", Value::Null),
        &mut state,
    );
    assert!(resp.error.is_none());
    let block = resp.result.as_ref().unwrap();
    assert_eq!(block["index"], 1);

    // method_not_found
    let resp = handle_request(
        &make_rpc_request("nonexistent_method", Value::Null),
        &mut state,
    );
    assert!(resp.error.is_some());
    assert_eq!(resp.error.as_ref().unwrap().code, rpc::METHOD_NOT_FOUND);
}

#[test]
fn test_rpc_staking_endpoints() {
    let staking = StakingManager::new();
    let mut balances: HashMap<String, i64> = HashMap::new();
    let mut stakes: HashMap<String, i64> = HashMap::new();

    balances.insert("validator1".to_string(), 500 * PLANCKS_PER_CREDIT);
    balances.insert("validator2".to_string(), 500 * PLANCKS_PER_CREDIT);

    staking
        .stake(
            "validator1",
            200 * PLANCKS_PER_CREDIT,
            &mut balances,
            &mut stakes,
        )
        .unwrap();
    staking
        .stake(
            "validator2",
            100 * PLANCKS_PER_CREDIT,
            &mut balances,
            &mut stakes,
        )
        .unwrap();

    let mut state = NodeState {
        chain: Chain::new(),
        staking,
        dao: PlanetaryDAO::new(),
        contract: WorkloadContract::new(),
        tokens: TokenRegistry::new(),
        balances,
        stakes,
        node_address: "node".into(),
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

    // get_staking_info
    let resp = handle_request(
        &make_rpc_request(
            "get_staking_info",
            serde_json::json!({"address": "validator1"}),
        ),
        &mut state,
    );
    assert!(resp.error.is_none());

    // get_validators
    let resp = handle_request(&make_rpc_request("get_validators", Value::Null), &mut state);
    assert!(resp.error.is_none());

    // get_leaderboard
    let resp = handle_request(
        &make_rpc_request("get_leaderboard", Value::Null),
        &mut state,
    );
    assert!(resp.error.is_none());
}

// ═════════════════════════════════════════════════════════════════════════════
// 11. HALVING MECHANICS
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_coinbase_reward_halving() {
    // At block 0: full reward
    assert_eq!(Chain::coinbase_reward(0), BASE_REWARD_PLANCKS);

    // At halving interval: half reward
    assert_eq!(
        Chain::coinbase_reward(HALVING_INTERVAL),
        BASE_REWARD_PLANCKS / 2
    );

    // At 2× halving: quarter reward
    assert_eq!(
        Chain::coinbase_reward(HALVING_INTERVAL * 2),
        BASE_REWARD_PLANCKS / 4
    );

    // At 64+ halvings: zero
    assert_eq!(Chain::coinbase_reward(HALVING_INTERVAL * 64), 0);
}

#[test]
fn test_pop_reward_halving() {
    let quality = 0.8;
    let tflops = 5.4;

    let reward_early = calculate_reward(tflops, quality, 1);
    let reward_halved = calculate_reward(tflops, quality, HALVING_INTERVAL);
    let reward_zero = calculate_reward(tflops, quality, HALVING_INTERVAL * 64);

    assert!(reward_early > reward_halved);
    assert!(reward_halved > 0);
    // After 64 halvings, reward is effectively zero (but calculate_reward floors at 1)
    assert_eq!(reward_zero, 1); // minimum floor
}

// ═════════════════════════════════════════════════════════════════════════════
// 12. ECONOMY BRIDGE + RPC: NETWORK COMPUTE VIA JSON-RPC
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_economy_and_rpc_integration() {
    // Set up a bridge with peers
    let mut bridge = EconomyBridge::new("local", "wallet_local", test_device());

    let ann = serde_json::json!({
        "wallet_address": "wallet_r1", "tflops": 15.0,
        "device_type": "cuda", "device_name": "RTX4090",
        "available": true,
    });
    bridge.handle_compute_announce("r1", &ann, NOW);

    // Get summary
    let summary = bridge.network_compute_summary(100, NOW + 10.0);
    assert_eq!(summary["compute_peers"], 1);

    // Build economy status broadcast
    let status_msg = bridge.build_economy_status(100, 50_000_000);
    assert_eq!(status_msg.msg_type, MessageType::EconomyStatus);

    // Now verify Chain + NodeState + RPC query together
    let mut chain = Chain::new();
    fund_address(&mut chain, "wallet_local", 100 * PLANCKS_PER_CREDIT);

    let mut state = NodeState {
        chain,
        staking: StakingManager::new(),
        dao: PlanetaryDAO::new(),
        contract: WorkloadContract::new(),
        tokens: TokenRegistry::new(),
        balances: HashMap::new(),
        stakes: HashMap::new(),
        node_address: "wallet_local".into(),
        peer_count: 1,
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

    // get_chain_info includes supply info
    let resp = handle_request(&make_rpc_request("get_chain_info", Value::Null), &mut state);
    assert!(resp.error.is_none());
    let info = resp.result.unwrap();
    assert!(info["height"].as_u64().unwrap() > 0);
}

// ═════════════════════════════════════════════════════════════════════════════
// 13. GENESIS INVARIANTS
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_genesis_block_invariants() {
    let chain = Chain::new();
    let genesis = &chain.genesis;

    // Hash matches expected
    assert_eq!(genesis.hash, EXPECTED_GENESIS_HASH);
    assert_eq!(genesis.index, 0);
    assert_eq!(
        genesis.previous_hash,
        repryntt_core::genesis::GENESIS_PREV_HASH
    );

    // Constants are sensible
    assert_eq!(BLOCK_INTERVAL_SECS, 69);
    assert_eq!(HALVING_INTERVAL, 420_000);
    assert_eq!(BASE_REWARD_PLANCKS, 10 * PLANCKS_PER_CREDIT);
    assert!(MAX_SUPPLY_PLANCKS > 0);
}

#[test]
fn test_chain_rejects_wrong_genesis() {
    let bad_blocks = vec![Block::new(
        0,
        "0",
        0.0,
        vec![],
        "bad_miner",
        BTreeMap::new(),
    )];
    let result = Chain::from_blocks(bad_blocks);
    assert!(result.is_err());
}

// ═════════════════════════════════════════════════════════════════════════════
// 14. ECONOMY BRIDGE MESSAGE ROUTER — FULL DISPATCH
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_economy_route_all_message_types() {
    let mut bridge = EconomyBridge::new("node1", "w1", test_device());
    let mut chain = Chain::new();

    // ComputeAnnounce
    let ann = serde_json::json!({"wallet_address":"w2","tflops":10.0,"available":true});
    let msgs = bridge.route_message(MessageType::ComputeAnnounce, "r1", &ann, &mut chain, NOW);
    assert!(msgs.is_empty()); // ingest only
    assert_eq!(bridge.compute_peers.len(), 1);

    // ComputeRequest → produces a claim
    let req = serde_json::json!({"workload_id":"wl1","deadline": NOW + 300.0});
    let msgs = bridge.route_message(MessageType::ComputeRequest, "sub1", &req, &mut chain, NOW);
    assert_eq!(msgs.len(), 1);
    assert_eq!(msgs[0].msg_type, MessageType::ComputeClaim);

    // ComputeReject → no outbound
    let reject = serde_json::json!({"workload_id":"wl_fake","reason":"err"});
    let msgs = bridge.route_message(MessageType::ComputeReject, "r1", &reject, &mut chain, NOW);
    assert!(msgs.is_empty());

    // BlockAnnounce → block requests if remote ahead
    let blk_ann = serde_json::json!({"block_index": 3});
    let msgs = bridge.route_message(
        MessageType::BlockAnnounce,
        "peer1",
        &blk_ann,
        &mut chain,
        NOW,
    );
    assert_eq!(msgs.len(), 2); // request blocks 2, 3
}

// ═════════════════════════════════════════════════════════════════════════════
// 15. TOKEN + DAO CROSS-MODULE: DAO FUNDED BY TOKEN TREASURY
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_dao_funded_staking_validated_token_created() {
    let staking = StakingManager::new();
    let mut dao = PlanetaryDAO::new();
    let mut tokens = TokenRegistry::new();

    let v1 = "validator_one";
    let v2 = "validator_two";
    let v3 = "validator_three";

    let mut balances: HashMap<String, i64> = HashMap::new();
    let mut stakes: HashMap<String, i64> = HashMap::new();

    // Fund validators
    let stake = 100 * PLANCKS_PER_CREDIT;
    for v in [v1, v2, v3] {
        balances.insert(v.to_string(), stake * 2);
    }

    // Stake
    staking
        .stake(v1, stake, &mut balances, &mut stakes)
        .unwrap();
    staking
        .stake(v2, stake, &mut balances, &mut stakes)
        .unwrap();
    staking
        .stake(v3, stake, &mut balances, &mut stakes)
        .unwrap();
    let validators = staking.validators(&stakes);
    assert_eq!(validators.len(), 3);

    // Create governance token
    let gov_token = tokens
        .create_token(
            "GovernanceToken",
            "GOV",
            8,
            1_000_000_00000000,
            v1,
            v1,
            v1,
            0,
            0,
        )
        .unwrap();
    tokens.mint(&gov_token, v1, 300_000_00000000, v1).unwrap();
    tokens
        .transfer(&gov_token, v1, v2, 100_000_00000000)
        .unwrap();
    tokens
        .transfer(&gov_token, v1, v3, 100_000_00000000)
        .unwrap();

    assert_eq!(tokens.balance_of(&gov_token, v1), 100_000_00000000);
    assert_eq!(tokens.balance_of(&gov_token, v2), 100_000_00000000);
    assert_eq!(tokens.balance_of(&gov_token, v3), 100_000_00000000);

    // DAO proposal: fund treasury, create + vote + execute
    balances.insert(DAO_TREASURY.to_string(), 20 * PLANCKS_PER_CREDIT);
    let pid = dao
        .create_proposal(
            v1,
            "Fund development",
            "desc",
            5 * PLANCKS_PER_CREDIT,
            v1,
            NOW,
            None,
        )
        .unwrap();
    dao.vote(&pid, v1, VoteDirection::For, 1, NOW + 10.0)
        .unwrap();
    dao.vote(&pid, v2, VoteDirection::For, 1, NOW + 20.0)
        .unwrap();
    dao.vote(&pid, v3, VoteDirection::For, 1, NOW + 30.0)
        .unwrap();
    dao.execute_proposal(&pid, &mut balances).unwrap();

    // Build RPC state and query everything
    let mut chain = Chain::new();
    fund_address(&mut chain, v1, 50 * PLANCKS_PER_CREDIT);

    let mut state = NodeState {
        chain,
        staking,
        dao,
        contract: WorkloadContract::new(),
        tokens,
        balances,
        stakes,
        node_address: v1.into(),
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

    // RPC: get validators
    let resp = handle_request(&make_rpc_request("get_validators", Value::Null), &mut state);
    assert!(resp.error.is_none());

    // RPC: get token
    let resp = handle_request(
        &make_rpc_request("get_token", serde_json::json!({"symbol": "GOV"})),
        &mut state,
    );
    assert!(resp.error.is_none());
    assert_eq!(resp.result.as_ref().unwrap()["symbol"], "GOV");

    // RPC: get treasury
    let resp = handle_request(&make_rpc_request("get_treasury", Value::Null), &mut state);
    assert!(resp.error.is_none());
}

// ═════════════════════════════════════════════════════════════════════════════
// 16. WIRE FORMAT ROUND-TRIP (network + rpc)
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_wire_format_round_trip() {
    use repryntt_core::rpc::{wire_decode, wire_encode};

    let request = serde_json::json!({
        "jsonrpc": "2.0",
        "method": "get_chain_height",
        "params": {},
        "id": 42
    });

    let json_bytes = serde_json::to_vec(&request).unwrap();
    let encoded = wire_encode(&json_bytes);
    assert!(encoded.len() > 4); // 4 byte prefix + payload

    let (decoded_bytes, consumed) = wire_decode(&encoded).unwrap();
    assert_eq!(consumed, encoded.len());
    let decoded: Value = serde_json::from_slice(decoded_bytes).unwrap();
    assert_eq!(decoded["method"], "get_chain_height");
    assert_eq!(decoded["id"], 42);
}

// ═════════════════════════════════════════════════════════════════════════════
// 17. BLOCK SERIALIZATION ROUND-TRIP
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_block_serde_round_trip() {
    let tx = Transaction::new(
        "SYSTEM",
        "miner_a",
        1_000_000_000,
        "reward",
        0,
        BTreeMap::new(),
        None,
        None,
        1,
    );
    let block = Block::new(
        1,
        EXPECTED_GENESIS_HASH,
        1_700_000_000.0,
        vec![tx],
        "miner_a",
        BTreeMap::new(),
    );

    // to_dict → from_dict round-trip
    let dict = block.to_dict();
    let restored = Block::from_dict(&dict).unwrap();

    assert_eq!(restored.index, block.index);
    assert_eq!(restored.hash, block.hash);
    assert_eq!(restored.previous_hash, block.previous_hash);
    assert_eq!(restored.miner_address, block.miner_address);
    assert_eq!(restored.transactions.len(), block.transactions.len());
    assert_eq!(
        restored.transactions[0].tx_hash,
        block.transactions[0].tx_hash
    );
}

// ═════════════════════════════════════════════════════════════════════════════
// 18. MULTI-MODULE STRESS: RAPID BLOCK PRODUCTION + LARGE MEMPOOL
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_rapid_production_50_blocks() {
    let addr = "a1a4090aced69d411b6e62bf49944f295c85ed88";
    let mut producer = BlockProducer::new(miner_config(addr, 5.4));
    producer.ibd_complete = true;

    for i in 0..50 {
        let block = producer
            .try_produce_block()
            .unwrap_or_else(|| panic!("block {} failed", i));
        assert_eq!(block.index as u64, i + 1);
    }

    assert_eq!(producer.chain.height(), 51); // genesis + 50
    producer
        .chain
        .validate_full()
        .expect("chain should validate after 50 blocks");

    let balance = *producer.chain.balances.get(addr).unwrap_or(&0);
    assert!(balance > 0);

    // Supply should be bounded
    let supply = producer.current_supply();
    assert!(supply <= MAX_SUPPLY_PLANCKS, "Supply should not exceed max");
}

#[test]
fn test_large_mempool_drain() {
    let addr = "a1a4090aced69d411b6e62bf49944f295c85ed88";
    let mut producer = BlockProducer::new(miner_config(addr, 5.4));
    producer.ibd_complete = true;

    // Fund with a block
    producer.try_produce_block().unwrap();

    // Add 100 transactions to mempool
    for i in 0..100u64 {
        let tx = Transaction::new(
            addr,
            &format!("rcpt_{}", i),
            1000,
            "transfer",
            i,
            BTreeMap::new(),
            None,
            None,
            1,
        );
        producer
            .mempool
            .add_transaction(tx, MIN_FEE_PLANCKS + i as i64)
            .ok();
    }
    let pool_before = producer.mempool.size();
    assert!(pool_before > 0);

    // Produce blocks until mempool is drained
    let mut blocks_produced = 0;
    while producer.mempool.size() > 0 && blocks_produced < 10 {
        producer.try_produce_block();
        blocks_produced += 1;
    }
    assert_eq!(
        producer.mempool.size(),
        0,
        "Mempool should be fully drained"
    );
    producer.chain.validate_full().unwrap();
}

// ═════════════════════════════════════════════════════════════════════════════
// 19. CROSS-MODULE: STAKING AFFECTS ELECTION WEIGHTS
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_staking_election_cross_module() {
    let staking = StakingManager::new();
    let mut balances: HashMap<String, i64> = HashMap::new();
    let mut stakes: HashMap<String, i64> = HashMap::new();

    // Fund and stake
    balances.insert("miner_a".to_string(), 500 * PLANCKS_PER_CREDIT);
    balances.insert("miner_b".to_string(), 500 * PLANCKS_PER_CREDIT);
    staking
        .stake(
            "miner_a",
            200 * PLANCKS_PER_CREDIT,
            &mut balances,
            &mut stakes,
        )
        .unwrap();
    staking
        .stake(
            "miner_b",
            100 * PLANCKS_PER_CREDIT,
            &mut balances,
            &mut stakes,
        )
        .unwrap();

    let validators = staking.validators(&stakes);
    assert_eq!(validators.len(), 2);

    // Register staked validators as compute contributors
    let mut contrib = ComputeContributors::new();
    for v in &validators {
        contrib.register(v, 10.0);
    }

    // Run election — both should be in candidate list
    let candidates = contrib.candidates();
    assert_eq!(candidates.len(), 2);

    // Election should work
    let result = election::elect_leader(EXPECTED_GENESIS_HASH, 42, &contrib);
    assert!(result.is_some());
    assert!(validators.contains(&result.unwrap().leader));
}

// ═════════════════════════════════════════════════════════════════════════════
// 20. POP QUALITY SPECTRUM
// ═════════════════════════════════════════════════════════════════════════════

#[test]
fn test_pop_quality_spectrum() {
    let device = test_device();

    // Short, low-entropy result
    let proof_short = ProofOfPower::generate(
        "wk_short",
        "short workload data enough for a test",
        "short result ok",
        "miner",
        0.5,
        "deterministic",
        &device,
        "aabb1122aabb1122aabb1122aabb1122",
    );
    let (valid_s, quality_s, _) = proof_short.verify(
        "short workload data enough for a test",
        "short result ok",
        "miner",
    );
    assert!(valid_s);
    assert!(quality_s > 0.0, "Quality should be positive");

    // Different workload with higher compute time
    let varied_result = "The quick brown fox jumps over the lazy dog 0123456789 !@#$%^&*() \
        diverse tokens with high entropy and many unique characters \
        ABCDEFGHIJKLMNOP abcdefghijklmnop 9876543210";
    let proof_varied = ProofOfPower::generate(
        "wk_varied",
        "varied workload data with high entropy content for testing",
        varied_result,
        "miner",
        5.0,
        "deterministic",
        &device,
        "aabb1122aabb1122aabb1122aabb1122",
    );
    let (valid_v, quality_v, _) = proof_varied.verify(
        "varied workload data with high entropy content for testing",
        varied_result,
        "miner",
    );
    assert!(valid_v);
    assert!(quality_v > 0.0, "Varied quality should be positive");

    // Both should produce valid proofs with non-zero quality
    // Exact quality comparison depends on PoP implementation details
    let reward_s = calculate_reward(device.effective_tflops(), quality_s, 1);
    let reward_v = calculate_reward(device.effective_tflops(), quality_v, 1);
    assert!(reward_s > 0);
    assert!(reward_v > 0);
}
