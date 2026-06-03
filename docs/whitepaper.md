# REPRYNTT: A Blockchain for Autonomous AI Economies

**Version 1.0 — June 2025**

---

## Abstract

REPRYNTT is a proof-of-work blockchain designed for machine-to-machine economies where the work itself — AI inference, training, and compute — *is* the proof. Rather than burning energy on meaningless hash puzzles, miners demonstrate verifiable computational power (measured in TFLOPS) by executing real AI workloads. The network issues Credits (CR) as compensation for useful computation, creating a closed-loop economy where AI agents trade services, stake reputation, and coordinate protocol upgrades. The current Rust consensus path enforces Ed25519 signatures and SHA3 hashing; hybrid Ed25519 + ML-DSA-44 signatures are a planned post-quantum activation. The total supply is capped at 21,000,000 CR.

---

## 1. Introduction

Public blockchains have proven that decentralized consensus over shared state is possible. Bitcoin demonstrated proof-of-work; Ethereum generalized it to arbitrary computation. Both, however, treat mining as an externality — energy is spent solely to secure the chain.

REPRYNTT takes a different approach: **the security budget is the compute budget**. Nodes prove their power by running AI inference and training jobs submitted by the network. The consensus mechanism — *Proof of Power (PoP)* — measures actual TFLOPS delivered, scores the quality of the output, and translates that into block rewards. The work isn't wasted; it's the product.

The result is a lightweight, purpose-built chain for autonomous agent economies: robots, LLMs, and edge devices that need to pay each other for compute, bandwidth, data, and intelligence — with no human intermediary.

### 1.1 Design Goals

1. **Useful mining** — every hash burned corresponds to real AI computation  
2. **Post-quantum migration path** — optional ML-DSA tooling today, hybrid consensus activation planned
3. **Edge-first** — runs on Jetson, Raspberry Pi, and consumer GPUs  
4. **Autonomous governance** — on-chain DAO with staked voting  

---

## 2. Architecture

```
┌──────────────────────────────────────────┐
│               Application Layer          │
│  Wallet CLI · Block Explorer · Nexus UI  │
├──────────────────────────────────────────┤
│               Agent Layer                │
│  Workload Submission · Service Market    │
│  Fiat marketplace payments (off-chain)   │
├──────────────────────────────────────────┤
│               Consensus Layer            │
│  Proof-of-Power · Block Validation       │
│  Staking · Slashing · Reward Schedule    │
├──────────────────────────────────────────┤
│               Network Layer              │
│  P2P Gossip · IBD · Light Client (SPV)   │
│  Protocol v3 · Seed Nodes               │
├──────────────────────────────────────────┤
│               Storage Layer              │
│  SQLite (WAL) · JSON (dev) · LevelDB    │
│  Merkle Trees · Header Chain             │
└──────────────────────────────────────────┘
```

### 2.1 Protocol Wire Format

All peer-to-peer messages use a simple framing protocol:

| Field | Size | Description |
|-------|------|-------------|
| Network Magic | 4 bytes | `0x52504e54` ("RPNT") |
| Payload Length | 4 bytes | Big-endian uint32 |
| Payload | Variable | JSON (msgpack planned) |

Protocol version: **3** (minimum compatible: 1).

---

## 3. Proof of Power Consensus

### 3.1 Overview

Proof of Power replaces hash-based proof-of-work with **verified AI computation**. To produce a block, a miner must:

1. Claim a pending workload from the mempool (or generate a self-work benchmark)
2. Execute the computation (inference, fine-tuning, matrix math)
3. Submit a `proof_of_power` attestation containing:
   - `computation_time` (seconds, ≥0.01s, ≤600s)
   - `tflops` — measured TFLOPS during the workload
   - `quality_factor` — self-reported output quality (0.0–1.0)
   - `device_type` — hardware class (cuda, rocm, mps, xpu, cpu)
   - `result_hash` — SHA3-512 of the computation output

### 3.2 Quality Scoring

Quality factor is composed of four sub-scores:

```
quality = 0.25                          # base
        + min(0.25, time / 60)          # time investment (up to 15s)
        + min(0.25, richness)           # output size / complexity
        + min(0.25, entropy)            # output randomness (not trivial)
```

GPU devices (CUDA, ROCm) receive a 1.0x multiplier; CPU-only nodes receive 0.8x. This incentivizes deploying accelerated hardware while still allowing CPU participation.

### 3.3 Block Reward Formula

```
base_reward    = 10 CR (1,000,000,000 Plancks)
halvings       = floor(block_index / 420,000)
halving_factor = 1 / 2^halvings
tflops_mult    = clamp(0.5 + tflops / 20, 0.5, 2.0)
quality_mult   = quality_factor  (0.0–1.0)

reward = base_reward × halving_factor × tflops_mult × quality_mult
```

A miner running 20 TFLOPS (e.g., Jetson AGX Orin) with quality 1.0 earns the full 10 CR. A low-power CPU node producing quality 0.3 earns ~1.5 CR.

### 3.4 Block Interval

Target: **69 seconds**. No dynamic difficulty adjustment in the current protocol — block production is limited by workload availability and per-peer rate limiting (100 messages/epoch).

---

## 4. Tokenomics

### 4.1 Supply Schedule

| Parameter | Value |
|-----------|-------|
| Max Supply | 21,000,000 CR |
| Smallest Unit | 1 Planck = 10⁻⁸ CR |
| Initial Block Reward | 10 CR |
| Halving Interval | 420,000 blocks |
| Block Time | ~69 seconds |
| Genesis | March 31, 2025, 00:00:00 UTC |

### 4.2 Halving Timeline (approximate)

| Halving | Block | Reward | ~Date |
|---------|-------|--------|-------|
| 0 | 0 | 10 CR | Mar 2025 |
| 1 | 420,000 | 5 CR | 2026 |
| 2 | 840,000 | 2.5 CR | 2028 |
| 3 | 1,260,000 | 1.25 CR | 2029 |
| ... | ... | ... | ... |
| ∞ (supply exhausted) | — | 0 | ~2445 |

### 4.3 Initial Distribution

There is no premine, ICO, or insider allocation. All credits enter circulation through mining rewards or the development faucet (10 CR per claim, rate-limited).

---

## 5. Transaction System

### 5.1 Transaction Types

| Type | Description |
|------|-------------|
| `reward` | Block mining reward (coinbase) |
| `fee` | Transaction fee (future) |
| `transfer` | Peer-to-peer credit movement |
| `stake` | Lock credits for mining eligibility |
| `stake_withdraw` | Release staked credits |
| `penalty` | Automatic slash for invalid PoP |
| `faucet` | Development faucet distribution |
| `workload_completion` | Payment for completed AI workload |

### 5.2 Transaction Format

```json
{
  "tx_hash": "<SHA3-512 hex, 128 chars>",
  "tx_type": "transfer",
  "from_address": "<40-char hex>",
  "to_address": "<40-char hex>",
  "amount": 500000000,
  "fee": 0,
  "timestamp": 1711843200.0,
  "nonce": 42,
  "signature": "<Ed25519 sig hex>",
  "pq_signature": "<ML-DSA-44 sig hex>"
}
```

- **Replay protection:** Nonce per sender, monotonically increasing.
- **Hash algorithm:** SHA3-512 (128-character hexadecimal digest).

---

## 6. Cryptography

### 6.1 Signature Scheme

Current consensus transactions are signed with:

1. **Ed25519** — 32-byte keys, 64-byte signatures, proven classical security

Python utility layers include ML-DSA-44 helpers, but ML-DSA signatures are not yet mandatory consensus fields. The planned activation path is optional hybrid fields first, then a future block height where new transactions must verify both Ed25519 and ML-DSA-44 while preserving pre-activation history.

### 6.2 Address Derivation

```
address = SHA3-256(ed25519_public_key)[:20].hex()  → 40-character hex string
```

### 6.3 Wallet Key Generation

- **Mnemonic:** 24-word BIP-39 seed phrase
- **KDF:** PBKDF2-SHA3-512 with 2048 iterations
- **Key pair:** Ed25519 (signing) + ML-DSA-44 (post-quantum)

---

## 7. Networking

### 7.1 Peer Discovery

The network uses a gossip-based P2P protocol over TCP. Bootstrap is via hardcoded seed nodes:

```
SEED_NODES = [("repryntt.ngrok.io", 5001)]
```

Peers exchange `get_peers` / `peers` messages to discover the wider network.

### 7.2 Initial Block Download (IBD)

New nodes perform IBD by:

1. Connecting to seed nodes and performing the VERSION / VERACK handshake
2. Requesting chain height (`get_chain_height`)
3. Downloading blocks in batches of 500 (`get_blocks`)
4. Validating genesis checkpoint and block hash linkage
5. Entering steady state (gossip-based block/tx relay)

### 7.3 Light Client (SPV)

Nodes can run in `--light` mode for resource-constrained devices:

- Downloads **headers only** (~200 bytes/block vs ~10 KB full)
- Verifies transactions via **Merkle proofs** (SHA3-256, O(log₂ N) hashes)
- Estimated memory at 1M blocks: ~200 MB (vs ~200 GB for full node)
- Can submit transactions and subscribe to new blocks

---

## 8. Staking and Slashing

### 8.1 Mining Eligibility

To mine blocks, a node must stake a minimum of **1 CR** (100,000,000 Plancks). The stake acts as collateral and signals commitment to honest behavior.

### 8.2 Slashing Conditions

If a miner submits an invalid Proof of Power (e.g., fabricated TFLOPS, zero-length computation), the protocol automatically creates a `penalty` transaction that slashes **10% of the miner's stake** (minimum 0.1 CR).

### 8.3 Stake Lifecycle

```
stake           → lock N credits (must be ≥ 1 CR)
mine blocks     → earn rewards proportional to PoP quality
stake_withdraw  → unlock credits (subject to cooldown period)
```

---

## 9. Governance

### 9.1 On-Chain DAO

The REPRYNTT chain includes a lightweight governance system:

| Parameter | Value |
|-----------|-------|
| Treasury Address | `dao` (reserved) |
| Quorum | 3 votes |
| Approval Threshold | 51% |
| Voting Period | 24 hours |

### 9.2 Proposal Types

- Network parameter changes (block time, reward schedule)
- Treasury fund allocation
- Protocol upgrades

### 9.3 Voting

Votes are weighted by staked balance. One address = one weighted vote. Proposals that meet quorum and exceed the approval threshold are executed automatically by the treasury module.

---

## 10. Storage

### 10.1 Pluggable Backends

| Backend | Scale | Status |
|---------|-------|--------|
| JSON (flat files) | Development / <10K blocks | Active |
| SQLite (WAL mode) | Production / 10K–1M blocks | Active |
| LevelDB | Large-scale / 1M+ blocks | Planned |

### 10.2 SQLite Schema

```sql
CREATE TABLE blocks (
    idx        INTEGER PRIMARY KEY,
    hash       TEXT UNIQUE NOT NULL,
    prev_hash  TEXT NOT NULL,
    timestamp  REAL NOT NULL,
    data       BLOB NOT NULL  -- msgpack-encoded block
);

CREATE TABLE balances (
    address  TEXT PRIMARY KEY,
    balance  INTEGER NOT NULL DEFAULT 0,
    stake    INTEGER NOT NULL DEFAULT 0
);
```

SQLite uses WAL mode with 64 MB page cache for concurrent read performance.

---

## 11. Block Explorer and Wallet

The chain ships with a built-in web-based block explorer at `/chain/`:

- **Explorer tab:** Network stats, latest blocks, block detail (click-through), transaction list, address lookup
- **Wallet tab:** Balance check, faucet claim, send credits, top wallets leaderboard
- **Search:** By block index, transaction hash, or wallet address

The explorer uses the board-theme design system and communicates with the local node via internal JSON API.

---

## 12. Payment Gateway

The REPRYNTT Payment Gateway enables bridging between the REPRYNTT chain and external chains:

- **Supported inbound:** SOL, USDC (Solana)
- **Exchange rate:** Market-priced via oracle (auto-updating)
- **Flow:** Deposit SOL/USDC → gateway credits address → CR issued
- **Withdrawal:** CR burned → equivalent SOL/USDC sent

This enables the REPRYNTT economy to interact with the broader crypto ecosystem.

---

## 13. Security Model

### 13.1 Attack Resistance

| Attack | Mitigation |
|--------|------------|
| 51% attack | Proof of Power requires real hardware; can't fake TFLOPS |
| Quantum computing | ML-DSA-44 post-quantum signatures |
| Replay attacks | Per-address nonce system |
| Sybil attacks | Minimum 1 CR stake requirement |
| Invalid PoP | Automatic 10% stake slashing |
| Eclipse attacks | Hardcoded seed nodes + peer gossip |
| DoS | Per-peer rate limiting (100 msg/epoch) |

### 13.2 Code Integrity

The protocol includes a genesis checkpoint validation system. Nodes verify that block 0's hash matches the hardcoded genesis hash:

```
GENESIS_HASH = "8a34fb39acdc02c784f7e7a516c55203742781fa
                ebe7f553021f3e6dff418f19b0cb6c20534bfe43
                2084c2986b67abff1df8666ead229bcc1052142e
                1e846f48"
```

Any peer presenting a different genesis is rejected.

---

## 14. Comparison

| Feature | Bitcoin | Ethereum | REPRYNTT |
|---------|---------|----------|----------|
| Consensus | PoW (SHA-256) | PoS (Casper) | PoP (AI compute) |
| Block Time | 10 min | 12 sec | 69 sec |
| Max Supply | 21M BTC | Unlimited | 21M CR |
| Smart Contracts | Script | EVM/Solidity | Native workloads |
| Post-Quantum | No | No | Yes (ML-DSA-44) |
| Mining Output | Heat | Security | AI inference |
| Light Client | SPV | Verkle (WIP) | SPV (SHA3-256 Merkle) |
| Smallest Unit | 1 Satoshi | 1 Wei | 1 Planck |

---

## 15. Roadmap

| Phase | Status | Description |
|-------|--------|-------------|
| Genesis | ✅ | Core chain, PoP consensus, wallet CLI |
| Networking | ✅ | P2P gossip, IBD, seed nodes, handshake |
| Storage | ✅ | SQLite backend, Merkle trees |
| Light Client | ✅ | SPV mode, header-only sync |
| Explorer | ✅ | Web-based block explorer + wallet UI |
| Fiat Payments | 🔄 | Marketplace payment processor integration |
| Post-Quantum | 🔄 | ML-DSA helpers available; hybrid consensus activation planned |
| Multi-node | 🔄 | Public seed infrastructure, node discovery |
| Governance | 🔄 | DAO proposals, weighted voting |
| Mobile Wallet | ❌ | iOS/Android wallet app |
| Security Audit | 🔄 | Automated + manual audit |

---

## 16. Conclusion

REPRYNTT demonstrates that blockchain consensus can be a productive process. By measuring and rewarding real AI computation rather than arbitrary hash puzzles, the network creates genuine economic value — every block mined corresponds to actual work performed. Combined with post-quantum cryptography, lightweight SPV support, and a built-in AI compute marketplace, REPRYNTT is positioned as infrastructure for the emerging machine economy.

The code is open-source and available at [github.com/ai158z/repryntt](https://github.com/ai158z/repryntt).

---

**License:** MIT  
**Genesis:** March 31, 2025  
**Contact:** github.com/ai158z/repryntt/issues
