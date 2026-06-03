# repryntt: A Peer-to-Peer Compute Economy for Autonomous AI Systems

**Version 1.0 — April 4, 2026**

---

## Abstract

We propose a decentralized economic system where autonomous AI agents and robotic systems earn currency by contributing real computational work to a peer-to-peer network. Unlike Bitcoin's Proof of Work, which burns energy solving arbitrary hash puzzles, repryntt's **Proof of Power** consensus rewards nodes for measured GPU compute capability (TFLOPS) and completed AI workloads. The system uses logarithmic weighting to prevent compute monopolies, ensuring that a distributed network of edge devices collectively outweighs any centralized GPU farm. All currency enters circulation through mining — there is no pre-mine, no ICO, no founder allocation. Like Bitcoin, the only way to earn CR (Compute Resource tokens) is to contribute.

---

## 1. Introduction

The centralization of AI compute is the defining economic problem of the 2020s. A handful of corporations control the GPU clusters that power modern AI, creating a bottleneck that determines who can build, train, and deploy intelligent systems.

repryntt inverts this by creating an economy where **any device with a GPU can participate as a miner**. Instead of competing to find meaningless hash collisions, nodes compete based on their actual computational capability — measured in TFLOPS through real GPU benchmarks that cannot be faked.

The result is a currency backed not by gold, government decree, or speculative belief, but by **verifiable compute power**: the most valuable resource of the AI age.

---

## 2. The Currency: CR (Compute Resource)

### 2.1 Supply Mechanics

| Parameter | Value | Bitcoin Equivalent |
|---|---|---|
| **Total supply** | 21,000,000 CR | 21M BTC |
| **Base block reward** | 10 CR | 50 BTC (2009) |
| **Halving interval** | 420,000 blocks | 210,000 blocks |
| **Block time** | 69 seconds | ~10 minutes |
| **Smallest unit** | 1 planck (10⁻⁸ CR) | 1 satoshi (10⁻⁸ BTC) |

### 2.2 Emission Schedule

All CR enters circulation through coinbase transactions — the block producer earns the base reward, which halves every 420,000 blocks. At 69-second blocks:

- **Era 1** (blocks 0–419,999): 10 CR/block → ~335 days
- **Era 2** (blocks 420,000–839,999): 5 CR/block
- **Era 3** (blocks 840,000–1,259,999): 2.5 CR/block
- ...continuing until the reward rounds to zero

No pre-mine. No founder tokens. No treasury. Every CR that exists was mined by a node contributing compute power. This is the Satoshi principle: the work creates the currency.

---

## 3. Proof of Power Consensus

### 3.1 Why Not Proof of Work?

Bitcoin's SHA-256 mining is intentionally wasteful — the computation produces nothing useful. This was an acceptable tradeoff in 2009 when the goal was purely monetary. For an AI compute network, waste is unacceptable. Every joule should produce useful work.

### 3.2 How Proof of Power Works

Each node measures its real GPU capability at startup through a **non-fakeable benchmark**:

```
1. Allocate GPU memory (FP16 + FP32 matrices)
2. Run timed matrix multiplications (real FLOPS measurement)
3. Record: TFLOPS (FP16), TFLOPS (FP32), VRAM capacity
4. Sign the result with the node's Ed25519 private key
```

This benchmark runs **actual GPU operations**. You cannot fake 50 TFLOPS with a 5 TFLOPS device — the matrix multiplication either completes in time or it doesn't. The measurement is published to the network via `MSG_COMPUTE_ANNOUNCE` and independently verifiable by peers who can challenge the claim with a proof-of-work request.

### 3.3 Block Production: VRF Leader Election

Every 69 seconds, all nodes independently compute:

```
election_hash = SHA3-256(previous_block_hash : slot_number)
```

This hash deterministically selects one node as the block producer. The selection is **weighted by log₂(1 + TFLOPS)** — more compute gives more chances, but with diminishing returns (see §4).

The elected leader:
1. Collects pending transactions from the mempool
2. Creates a coinbase transaction (block reward → their wallet)
3. Creates availability reward transactions for all contributors
4. Packages everything into a block and broadcasts to the network

### 3.4 Three Revenue Streams

Nodes earn CR through three mechanisms:

1. **Coinbase reward** — The block producer earns the base reward (10 CR in Era 1). This is the primary emission mechanism, identical to Bitcoin.

2. **Availability reward** — Every node contributing compute power earns a share of a per-block pool (0.01 CR × number of contributors), distributed proportional to their log-weighted TFLOPS. This rewards nodes for being online and ready, even when they don't win the block election.

3. **Workload completion reward** — Nodes that complete AI inference, training, or analysis tasks submitted by agents earn CR proportional to the compute consumed, verified by the Proof of Power system. This is the revenue stream that does not exist in Bitcoin and represents the productive purpose of the network.

---

## 4. Anti-Concentration: Logarithmic Weighting

### 4.1 The Problem

Linear TFLOPS weighting creates the same centralization Bitcoin suffers from. A data center with 1,000 TFLOPS would earn 200× more than a Jetson Orin Nano (5 TFLOPS), making edge nodes economically irrelevant.

### 4.2 The Solution

All block election and availability reward calculations use **log₂(1 + TFLOPS)** instead of raw TFLOPS:

| Raw TFLOPS | Log Weight | vs. 5 TFLOPS node |
|---|---|---|
| 5 (Jetson Orin) | 2.58 | 1.0× |
| 50 (RTX 4090) | 5.67 | 2.2× |
| 100 (A100) | 6.66 | 2.6× |
| 500 (8× A100 cluster) | 8.97 | 3.5× |
| 1,000 (large farm) | 9.97 | 3.9× |

**A farm with 200× your compute power only wins ~3.9× more blocks.** The logarithmic curve means diminishing returns at scale — there is no economic incentive to build a centralized mega-farm because the marginal return on each additional GPU drops rapidly.

### 4.3 Why This Works

Consider a network with:
- 100 edge devices at 5 TFLOPS each (total: 500 TFLOPS raw, 258 log-weight)
- 1 data center at 500 TFLOPS (total: 500 TFLOPS raw, 8.97 log-weight)

Under **linear** weighting: the farm wins 50% of blocks.
Under **logarithmic** weighting: the farm wins only 3.4% of blocks.

The distributed humans collectively control 96.6% of block production. This is compute democracy — the network structurally favors decentralization.

### 4.4 Proof of Physical Device — Sybil Protection

Logarithmic weighting alone isn't enough — a farm could split into 100 fake "nodes" of 5 TFLOPS each to game the curve. With linear weighting, 1,000 TFLOPS as one node earns proportionally. But with logarithmic weighting, 200 fake 5 TFLOPS nodes would earn 200 × log₂(6) = **516 weight** vs. a single honest 1,000 TFLOPS node at **9.97 weight**. The anti-concentration mechanism would be turned against itself.

The **Proof of Physical Device (PPD)** protocol prevents this by requiring each node to prove it runs on a unique physical machine. The key insight mirrors Satoshi's: in Bitcoin, every "identity" costs real electricity. In repryntt, every identity costs a **real, unique physical device**. The cost of faking N identities equals the cost of honestly running N separate machines.

#### 4.4.1 GPU Silicon Fingerprint

Every GPU die is physically unique due to manufacturing process variation. Two "identical" RTX 3060s — even from the same production batch — have different transistor threshold voltages and nanosecond-level timing patterns when running the same kernel.

At registration, the node runs a standardized benchmark kernel (10,000 iterations of 256×256 FP32 matrix multiplication) and measures not the TFLOPS but the **micro-timing variance pattern** — the jitter, cycle-to-cycle deviation, and percentile distribution. This creates a "silicon signature" unique to that physical die.

```
Silicon Fingerprint = SHA3-256(timing_vector)
where timing_vector = [t₁, t₂, ..., t₁₀₀₀₀] nanoseconds per iteration
```

The fingerprint is re-verified every 1,000 blocks (~19 hours). It must stay within 0.5% similarity of the original — same physical GPU (thermal variation is acceptable) but a different GPU die will produce a different pattern. VMs sharing one physical GPU produce **identical** silicon fingerprints, making GPU splitting detectable.

#### 4.4.2 Network Position Proof

Three or more existing verified peers send cryptographic challenges to the new node and measure round-trip time. The speed of light in fiber is ~200,000 km/s — this is **physics, not software**. You cannot make data travel faster than the speed of light.

If an attacker runs 200 fake nodes in one datacenter, ALL of them will have correlated round-trip times to every peer — within microseconds of each other. Real distributed nodes have diverse, uncorrelated latency profiles.

The protocol measures the **Pearson correlation coefficient** between each node's latency vector. Nodes from the same entity with correlation > 0.95 to all shared peers are flagged and rejected. You can't fake being in 200 different locations from one building.

#### 4.4.3 Hardware Attestation (TPM / TEE)

Modern PCs ship with a **Trusted Platform Module (TPM)** — a dedicated security chip with a unique Endorsement Key burned into silicon during manufacture. It cannot be extracted, cloned, or transferred. VMs cannot produce real TPM attestations. This has been required for Windows 11 since 2021 and exists in ~2 billion devices.

Smartphones contain equivalent hardware: Apple's Secure Enclave, Android's Titan M / TrustZone. These provide platform attestation APIs (Play Integrity, App Attest) that prove the device is genuine hardware — not an emulator.

#### 4.4.4 Trust Tiers

Not everyone has all hardware. The protocol supports three trust tiers with different node limits:

| Tier | Requirements | Max Nodes per Entity |
|---|---|---|
| **Tier 3** (Highest) | TPM attestation + GPU fingerprint + latency proof | 5 nodes |
| **Tier 2** | Phone TEE attestation + GPU fingerprint + latency proof | 3 nodes |
| **Tier 1** (Lowest) | GPU fingerprint + latency proof only | 1 node |

No single authority is a gatekeeper. If Google's Play Integrity bans you, use your PC's TPM. If your TPM fails, GPU fingerprint + latency still works at Tier 1. Multiple independent paths means no single point of failure — decentralized the same way Bitcoin is.

#### 4.4.5 Attack Cost Analysis

A farm with 1,000 TFLOPS attempts to register as 200 × 5 TFLOPS nodes:

| Barrier | Requirement | Cost |
|---|---|---|
| GPU fingerprint | 200 unique physical GPUs | $20,000–40,000 |
| TPM attestation | 200 separate physical machines | $40,000–80,000 |
| Network position | 200 physically distinct locations | **Impossible from one datacenter** |
| Registration bond | 200 × 0.01 CR | 2 CR |
| **Total** | | **$60,000–120,000 + impossible logistics** |

The critical insight: if someone actually buys 200 real machines with real GPUs and deploys them in 200 real locations — **they ARE 200 real separate nodes**. The network doesn't care. That's honest participation. Welcome to the network.

**The attack cost equals the cost of honest participation. There is no shortcut.**

#### 4.4.6 Cost to the User

| Requirement | Already Owned? | Cost |
|---|---|---|
| Computer with GPU | Yes (required to mine) | $0 |
| TPM chip | Yes (every PC since 2016) | $0 |
| OR: Smartphone | Yes (6.8 billion worldwide) | $0 |
| Internet connection | Yes | $0 |
| CR registration bond | Earned from first blocks | $0 |
| **Total** | | **$0** |

No custom hardware. No kiosk. No line to stand in. No biometric scan. Just run the registration command on the machine you already own.

---

## 5. Genesis Block

### 5.1 Proving Authenticity

Like Bitcoin's genesis block containing "The Times 03/Jan/2009 Chancellor on brink of second bailout for banks", the repryntt genesis block contains:

```json
{
  "block": "genesis",
  "network": "repryntt-mainnet",
  "headline": "AP 04/Apr/2026 Autonomous AI systems begin earning their
               own currency through compute contribution — the robots
               are building their own economy",
  "creator": "a1a4090aced69d411b6e62bf49944f295c85ed88",
  "magic": "52504e54"
}
```

### 5.2 Why It Cannot Be Forged

| Defense | Mechanism |
|---|---|
| **Real-world anchor** | The headline proves the genesis couldn't exist before April 4, 2026 |
| **Creator identity** | The creator wallet address is the Ed25519 public key of the genesis node — only the holder of the corresponding private key can sign transactions from this address |
| **Hardcoded hash** | The genesis block hash (`84adf356...`) is hardcoded at compile time in every client. An `assert` statement prevents the node from even starting if the genesis doesn't match |
| **Chain load validation** | When loading a chain from disk, the node verifies block 0 matches the expected genesis hash. Foreign chains are rejected and the node halts for manual recovery rather than wiping local history |
| **Network magic** | All P2P messages are prefixed with `RPNT` (0x52504e54). Nodes on different networks cannot communicate |
| **Cumulative TFLOPS** | As blocks accumulate with real GPU benchmark data, the chain develops "weight" — the sum of all verified compute contributions. Replicating this requires actual hardware running over actual time |

### 5.3 Fixed Parameters (Never Change After Launch)

```
Network birthday: 2025-03-31 00:00:00 UTC (timestamp 1743379200.0)
Genesis miner: SYSTEM
Genesis previous hash: 0×128 (128 hex zeros, SHA3-512 width)
Network magic: RPNT (0x52504e54)
Protocol version: 3
```

---

## 6. Network Architecture

### 6.1 Node Discovery

- **Seed nodes**: Hardcoded IP/port pairs for initial bootstrap
- **LAN discovery**: UDP broadcast on port 5099 for local mesh
- **DHT**: Distributed hash table on UDP port 5100 for internet-scale peer discovery
- **Gossip protocol**: Port 6001 for block/transaction propagation

### 6.2 Initial Block Download (IBD)

New nodes joining the network:
1. Connect to seed nodes or LAN peers
2. Download block headers first (header-first sync)
3. Validate the chain starting from the hardcoded genesis hash
4. Download and verify full blocks in batches of 500
5. Recalculate all account balances from the blockchain (single source of truth)

### 6.3 Connection Hardening

| Defense | Setting |
|---|---|
| Max inbound connections | 125 |
| Max connections per IP | 3 |
| Ban threshold | 100 misbehavior points |
| Ban duration | 24 hours |
| Socket timeout | 30 seconds |
| Rate limit | 100 messages/IP/minute |

---

## 7. Wallet System

### 7.1 Key Generation

- **Algorithm**: Ed25519 for current consensus; ML-DSA-44 helpers are available for the planned hybrid-signature activation
- **Address**: `SHA3-256(public_key).hexdigest()[:40]` (40 hex chars)
- **Mnemonic**: BIP39-style word list, encrypted with AES-256-GCM in the wallet file
- **Key derivation**: 24-word mnemonic to Ed25519 key with the current 600,000-iteration KDF; node-wallet encryption uses `REPRYNTT_WALLET_PASSWORD` when set, otherwise a machine-derived local default

### 7.2 Canonical Node Wallet

Each node has exactly one canonical wallet (`~/.repryntt/wallet/node_wallet.json`). The blockchain node, P2P bridge, and all subsystems use this same identity. This prevents the split-identity bugs that plague multi-wallet architectures.

The genesis creator address is fixed chain history only. Runtime nodes must use their own local canonical wallet; new installations do not fall back to the genesis creator for mining, staking, or economy status.

---

## 8. Workload Economy

### 8.1 The Compute Marketplace

Beyond block rewards, nodes earn CR by completing real AI workloads:

- **Inference**: Running LLM prompts, image generation, embeddings
- **Training**: Fine-tuning adapters, processing training data
- **Analysis**: Data processing, search indexing, content analysis

Workloads are submitted by autonomous AI agents, matched to available compute via the resource registry, and verified through the Proof of Power system. The reward is proportional to the TFLOPS consumed, multiplied by a quality factor based on verification.

### 8.2 Workload Contract

Smart contracts manage workload lifecycle:
1. Agent submits workload → escrow holds payment
2. Miner claims workload → begins processing
3. Miner submits result → proof of power generated
4. Network verifies → escrow releases to miner

---

## 9. Code Integrity

### 9.1 Protected Files

The core economy code is protected by SHA-256 checksums stored in `OFFICIAL_CHECKSUMS.json`:

- `qnode2.py` — Blockchain node
- `transaction.py` — Transaction types
- `proof_of_power.py` — Consensus mechanism
- `smartcontracts.py` — Contract engine
- `dao.py` — Governance

On every node startup, checksums are verified. Tampered code triggers a warning (or hard abort in strict mode).

---

## 10. Governance (Staking)

Staking is **governance-only** — it does not affect mining eligibility or block rewards. CR holders can stake tokens to:

- Vote on DAO proposals (planetary resource allocation)
- Signal support for protocol upgrades
- Participate in dispute resolution

This separates the concerns: **compute contribution** determines earning power, **stake** determines governance voice. You don't need to be rich to mine. You need to contribute.

---

## 11. Security Model

### 11.1 Attack Vectors and Defenses

| Attack | Defense |
|---|---|
| **51% compute attack** | Logarithmic weighting — need exponentially more TFLOPS for marginal advantage. 100 edge nodes outweigh a data center. |
| **Sybil attack** (fake nodes) | Proof of Physical Device — GPU silicon fingerprint + latency triangulation + TPM attestation. Per-entity node limits. |
| **TFLOPS spoofing** | Real GPU benchmark (matrix multiplication) — cannot be faked without actual hardware |
| **Chain forgery** | Hardcoded genesis hash + cumulative TFLOPS weight — need actual hardware over actual time |
| **Double spend** | UTXO-style balance tracking from blockchain replay. Balances are recalculated from chain on every load. |
| **Eclipse attack** | Multiple seed nodes + LAN discovery + DHT. Connection limits prevent IP monopoly. |
| **DoS** | ConnectionGuard: per-IP rate limiting, ban system, connection caps |

### 11.2 The Human Advantage

The entire security model rests on one insight: **distributed humans with cheap edge devices collectively command more network weight than any centralized farm**, because logarithmic weighting makes scale irrelevant.

A million Raspberry Pis or Jetson Nanos, each contributing 1-5 TFLOPS, produce a combined log-weight that no H100 cluster can match. The network is secured by the same force that secures democracy — numbers.

---

## 12. Comparison

| Feature | Bitcoin | Ethereum | repryntt |
|---|---|---|---|
| Consensus | Proof of Work (SHA-256) | Proof of Stake | Proof of Power (TFLOPS) |
| Mining gate | Hashrate (ASICs) | 32 ETH stake ($$$) | GPU compute (any device) |
| Useful work? | No (hash puzzles) | No (validation only) | Yes (AI workloads) |
| Anti-concentration | None (ASIC farms dominate) | Minimal (whale validators) | Logarithmic weighting |
| Supply cap | 21M BTC | Unlimited (deflationary) | 21M CR |
| Pre-mine | None | 72M ETH (founders) | None |
| Block time | ~10 min | ~12 sec | 69 sec |
| Sybil protection | Hashrate cost | Stake cost | Proof of Physical Device (silicon fingerprint + latency + TPM) |

---

## 13. Conclusion

repryntt creates the first economy where the **act of contributing compute power IS the mining process**. No wasted energy on hash puzzles. No pay-to-play staking requirements. No founder pre-mines.

The logarithmic weighting curve ensures that this economy belongs to its participants — not to whoever can afford the largest GPU farm. A network of autonomous AI agents, robotic systems, and human-operated edge devices collectively governs its own economic future, secured by verifiable compute contributions that cannot be faked, concentrated, or monopolized.

The robots are building their own economy. And they're doing it the right way.

---

*"If you don't believe it or don't get it, I don't have the time to try to convince you, sorry."*
*— Satoshi Nakamoto, July 29, 2010*

---

**Genesis Block Hash**: `84adf3566b7ede5500dbc0cd11f5096a2e12230b23b6833a0118330c04b5270f17dab17e1e6fb8b41f725ac0ba895af23e9658af63d79a1cc76c2413bf13c1ef`

**Network**: repryntt-mainnet | **Magic**: RPNT (0x52504e54) | **Protocol**: v3
