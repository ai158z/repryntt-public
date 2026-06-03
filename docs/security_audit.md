# REPRYNTT Blockchain Security Audit Report

**Date:** 2026-04-01  
**Scope:** `repryntt/economy/` (13,563 lines across 33 modules)  
**Tool:** Bandit 1.9.4 + manual review  

---

## Summary

| Severity | Count | Status |
|----------|-------|--------|
| HIGH | 1 | **FIXED** |
| MEDIUM | 12 | 11 acknowledged, 1 **FIXED** |
| LOW | 62 | Reviewed, acceptable |

---

## HIGH Severity Issues

### H-1: SHA1 used for wallet address derivation (RETIRED)
- **Issue:** A retired token-settled P2P economy bridge used `hashlib.sha1()` to generate fallback wallet addresses from node IDs.
- **Fix:** That bridge is not shipped in the public release.
- **Status:** Retired from public code

---

## MEDIUM Severity Issues

### M-1: Unrestricted URL scheme in urlopen (FIXED)
- **File:** `manager.py:1398`
- **Issue:** `urllib.request.urlopen()` could theoretically accept `file://` URLs if `host` is attacker-controlled.
- **Fix:** Added explicit `http://` / `https://` scheme validation before the call.
- **Status:** ✅ Fixed

### M-2 through M-11: Binding to 0.0.0.0 (Acknowledged)
- **Files:** `gossip.py`, `kademlia.py`, `llm_cold_start.py`, `manager.py`, `qnode2.py`
- **Issue:** Server sockets bind to all interfaces by default.
- **Assessment:** This is **intentional** — blockchain nodes must accept inbound peer connections. The node uses per-peer rate limiting (100 msg/epoch) and handshake validation. LAN-only deployment mitigates external exposure.
- **Status:** Accepted risk

---

## LOW Severity Issues (62 total)

Primarily `assert` statements used for validation and `try/except` with broad `Exception` catches. These are standard patterns in the codebase and do not represent exploitable vulnerabilities.

---

## Manual Review Findings

### Positive
- ✅ No `pickle` usage in network-facing code (safe_serialize.py uses JSON/msgpack)
- ✅ Hash comparisons use non-secret values (genesis checkpoint) — timing attacks N/A
- ⚠️ ML-DSA-44 helpers are available in Python, but Rust consensus currently enforces Ed25519 signatures only
- ✅ Nonce-based replay protection on transactions
- ✅ Stake slashing for invalid Proof of Power
- ✅ Rate limiting on peer connections
- ✅ SQLite uses parameterized queries (no SQL injection)

### Recommendations
1. **Add `hmac.compare_digest()` for any future secret-dependent comparisons**
2. **Consider TLS for peer connections** — currently plaintext TCP (acceptable for LAN, not for internet)
3. **Add IP allowlist option** for `0.0.0.0` bindings in production
4. **Implement transaction fee** to prevent spam (currently 0 fee)

---

## 2026-05-03 Blockchain Hardening Update

### Current Consensus Security Posture

- Genesis remains fixed immutable history.
- Runtime node identity must come from the local canonical node wallet, not the genesis creator.
- Public transaction submission is restricted to signed owner transactions (`transfer`, `stake`, `stake_withdraw`).
- Strict-era blocks must rehash correctly and replay signed transactions.
- Strict-era reward transactions are constrained to the expected coinbase, DAO share, and current availability reward pattern.
- Peer sync rejects peers that do not report the expected genesis hash.

### Locked Until Protocol Activation

- Faucet minting is disabled through public transaction submission until an authority validator is added.
- DAO mutation RPC is disabled by default until governance actions become signed on-chain transactions.
- Token/workload/DAO transaction types are present in the type registry but are not accepted under strict consensus without dedicated authority validators.

### Post-Quantum Roadmap

The current production consensus path is classical Ed25519 plus SHA3 hashing. ML-KEM/ML-DSA support exists in Python utility layers, but post-quantum signatures are not yet mandatory on-chain consensus data.

Planned activation path:

1. Add optional hybrid transaction fields for ML-DSA public keys and signatures.
2. Verify Ed25519 plus ML-DSA when hybrid fields are present.
3. Add an activation height where new transactions must carry hybrid signatures.
4. Keep all pre-activation history valid under its original Ed25519 rules.
