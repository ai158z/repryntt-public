# Verifiable AI Computation — Reference Guide

## Current System: Proof-of-Power Blockchain

### What It Already Verifies
- **Output integrity**: SHA3-512 hash of AI output (no tampering)
- **Non-triviality**: Output must be >2 chars
- **Computation time plausibility**: 10ms–600s window
- **Hardware type**: GPU/CPU (self-reported, not proven)
- **Quality scoring**: 0.0–1.0 based on result richness, entropy, device type

### What It Does NOT Verify (Gaps)
1. **Output correctness** — "An output was produced" ≠ "the output is good"
2. **Hardware honesty** — CPU miners can claim GPU TFLOPS for 1.5x multiplier
3. **Model integrity** — No proof of which LLM model was actually used
4. **Replay attacks** — Same output could theoretically be resubmitted

---

## ZK Proofs for AI (zkML) — Status

### What ZK Proofs Are
Zero-knowledge proofs let you prove a statement is true without revealing underlying data.
Example: "I can prove my bank balance is over $1000 without showing you my balance."

### Why They're Relevant
ZK proofs would let miners prove: "I ran this specific model on this input and got
this output, on real GPU hardware" — without exposing model weights or intermediate
computations. Makes the blockchain trustless instead of trust-based.

### Why They're NOT Practical Yet (2026)
- Proving even a small neural network in a ZK circuit is extremely expensive
- Proving full LLM inference in ZK is not practical for anything beyond toy models
- Projects working on it: EZKL, Giza, Modulus Labs — still bleeding-edge
- The computational overhead of generating ZK proofs ≫ the inference itself

---

## Practical Alternatives (What To Build Now)

### 1. TEE Attestation (Best Fit — Hardware Already Available)
- **What**: ARM TrustZone on Jetson Orin Nano provides hardware-signed attestations
- **Proves**: Specific code ran on specific hardware, tamper-proof
- **Closes**: GPU honesty gap + model integrity gap
- **Effort**: Moderate — need to integrate TrustZone attestation into proof_of_power.py

### 2. Optimistic Verification
- **What**: Run a second validator miner on same input, compare outputs
- **Proves**: Output correctness (if two independent miners agree, high confidence)
- **Closes**: Output correctness gap
- **Effort**: Low — add a "verify" workload type that re-runs existing workloads

### 3. Commitment Schemes
- **What**: Miners commit to model weights hash before mining
- **Proves**: Consistent model usage over time
- **Closes**: Model integrity gap
- **Effort**: Low — just hash model file and include in proof metadata

### 4. Fraud Proofs (Optimistic Rollup Style)
- **What**: Assume outputs correct, let anyone challenge within a window
- **Proves**: Correctness via economic incentives (challengers get reward)
- **Closes**: All gaps, but only when challenged
- **Effort**: Medium — need challenge/response protocol in blockchain

---

## Recommended Implementation Order
1. **Commitment schemes** (easiest — hash model file, include in proof)
2. **Optimistic verification** (add validator re-run, compare outputs)
3. **TEE attestation** (hardware-backed, strongest guarantee)
4. **ZK proofs** (future — when zkML matures enough for practical LLM circuits)

---

## Key Files
- `repryntt/repryntt/economy/proof_of_power.py` — Current verification logic
- `repryntt/repryntt/economy/secure_crypto.py` — Ed25519 + optional ML-DSA-44
- `repryntt/repryntt/economy/crypto_utils.py` — Crypto primitives (PQC optional)
- `repryntt/repryntt/economy/spaceminer.py` — Miner execution flow
- `repryntt/repryntt/economy/qnode2.py` — Blockchain node, block creation

## What Artemis's ZK Skills Actually Are
The 6 markdown "skill" files in `~/.repryntt/brain/skills/user/` (zk_proof_qualia_framework.md,
etc.) are conceptual frameworks only — no real code. Z3 SMT solver is not installed.
The "mock_signature" attestations in daily logs are just hardcoded strings, not computed.
The philosophical direction (proving AI decisions privately) aligns with the system goals,
but the implementation path should be TEE + optimistic verification, not raw ZK circuits.
