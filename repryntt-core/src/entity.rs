//! Entity Verification Protocol (REVP) — zero-knowledge entity-type verification.
//!
//! Proves HUMAN vs MACHINE without revealing real-world identity.
//! Uses RSA blind signatures (Chaum '82) so the oracle cannot link
//! a credential to any on-chain address.
//!
//! Matches Python's `entity_verification.py` exactly:
//!   - EntityIdentity: local secret (never transmitted)
//!   - BlindCredentialIssuer: RSA blind signature oracle
//!   - MerkleTree: SHA3-256 binary append-only tree
//!   - EntityRecord / EntityRegistry: on-chain commitment registry

use sha3::{Digest, Sha3_256};
use std::collections::{HashMap, HashSet};
use std::time::{SystemTime, UNIX_EPOCH};

use num_bigint::BigUint;
use num_integer::Integer;
use num_traits::One;
use rand::RngCore;
use rsa::{
    BigUint as RsaBigUint, RsaPrivateKey,
    traits::{PrivateKeyParts, PublicKeyParts},
};

// ── Constants ────────────────────────────────────────────────────────────────

/// Valid entity types.
pub const ENTITY_TYPE_HUMAN: &str = "human";
pub const ENTITY_TYPE_MACHINE: &str = "machine";

/// Epoch length in seconds (30 days).
pub const EPOCH_LENGTH: u64 = 30 * 24 * 3600;

/// RSA key size for blind signatures.
pub const ORACLE_RSA_BITS: usize = 2048;

/// Zero leaf for Merkle tree padding (32 zero bytes).
const ZERO_LEAF: [u8; 32] = [0u8; 32];

// ── Entity Identity (local secret, never transmitted) ────────────────────────

/// Secret identity of a repryntt entity (human or machine).
///
/// Generated once on the device and NEVER shared.  Only the commitment
/// (a one-way hash) and epoch_nullifier leave the device.
#[derive(Debug, Clone)]
pub struct EntityIdentity {
    /// 32 random bytes — secret.
    pub identity_secret: [u8; 32],
    /// 32 random bytes — nullifier.
    pub identity_nullifier: [u8; 32],
    /// "human" or "machine".
    pub entity_type: String,
    /// SHA3-256 hex of (secret ‖ nullifier ‖ entity_type).
    pub commitment: String,
    /// When created.
    pub created_at: f64,
}

impl EntityIdentity {
    /// Generate a new random identity.
    pub fn generate(entity_type: &str) -> Result<Self, String> {
        if entity_type != ENTITY_TYPE_HUMAN && entity_type != ENTITY_TYPE_MACHINE {
            return Err(format!("Invalid entity type: {}", entity_type));
        }
        let mut rng = rand::thread_rng();
        let mut secret = [0u8; 32];
        let mut nullifier = [0u8; 32];
        rng.fill_bytes(&mut secret);
        rng.fill_bytes(&mut nullifier);

        let commitment = compute_commitment(&secret, &nullifier, entity_type);
        Ok(Self {
            identity_secret: secret,
            identity_nullifier: nullifier,
            entity_type: entity_type.to_string(),
            commitment,
            created_at: now_f64(),
        })
    }

    /// Deterministic nullifier for an epoch — prevents double-registration.
    pub fn epoch_nullifier(&self, epoch: u64) -> String {
        let mut data = Vec::with_capacity(40);
        data.extend_from_slice(&self.identity_nullifier);
        data.extend_from_slice(&epoch.to_be_bytes());
        let mut hasher = Sha3_256::new();
        hasher.update(&data);
        hex::encode(hasher.finalize())
    }

    /// Serialize to JSON dict for persistence.
    pub fn to_dict(&self) -> serde_json::Value {
        serde_json::json!({
            "identity_secret": hex::encode(self.identity_secret),
            "identity_nullifier": hex::encode(self.identity_nullifier),
            "entity_type": self.entity_type,
            "commitment": self.commitment,
            "created_at": self.created_at,
        })
    }

    /// Deserialize from JSON dict.
    pub fn from_dict(v: &serde_json::Value) -> Result<Self, String> {
        let secret_hex = v["identity_secret"]
            .as_str()
            .ok_or("missing identity_secret")?;
        let nullifier_hex = v["identity_nullifier"]
            .as_str()
            .ok_or("missing identity_nullifier")?;
        let entity_type = v["entity_type"]
            .as_str()
            .ok_or("missing entity_type")?
            .to_string();
        let commitment = v["commitment"]
            .as_str()
            .ok_or("missing commitment")?
            .to_string();
        let created_at = v["created_at"].as_f64().unwrap_or(0.0);

        let secret_bytes = hex::decode(secret_hex).map_err(|e| e.to_string())?;
        let nullifier_bytes = hex::decode(nullifier_hex).map_err(|e| e.to_string())?;

        let mut secret = [0u8; 32];
        let mut nullifier = [0u8; 32];
        if secret_bytes.len() != 32 || nullifier_bytes.len() != 32 {
            return Err("secret and nullifier must be 32 bytes".into());
        }
        secret.copy_from_slice(&secret_bytes);
        nullifier.copy_from_slice(&nullifier_bytes);

        Ok(Self {
            identity_secret: secret,
            identity_nullifier: nullifier,
            entity_type,
            commitment,
            created_at,
        })
    }
}

/// Binding + hiding commitment: SHA3-256(secret ‖ nullifier ‖ type).
pub fn compute_commitment(secret: &[u8; 32], nullifier: &[u8; 32], entity_type: &str) -> String {
    let mut hasher = Sha3_256::new();
    hasher.update(secret);
    hasher.update(nullifier);
    hasher.update(entity_type.as_bytes());
    hex::encode(hasher.finalize())
}

/// Current epoch number (based on Unix time).
pub fn current_epoch() -> u64 {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    now / EPOCH_LENGTH
}

// ── RSA Blind Signatures (Chaum '82) ────────────────────────────────────────

/// Blinded message ready for the oracle to sign.
#[derive(Debug)]
pub struct BlindedMessage {
    /// m * r^e mod n — oracle sees this.
    pub blinded: BigUint,
    /// r (secret, kept by entity).
    pub blinding_factor: BigUint,
    /// Integer of H(commitment ‖ entity_type ‖ epoch).
    pub original_hash: BigUint,
}

/// Un-blinded oracle signature — proves entity type without linkability.
#[derive(Debug, Clone)]
pub struct BlindCredential {
    pub commitment: String,
    pub entity_type: String,
    pub epoch: u64,
    /// Raw RSA signature on H(commitment ‖ type ‖ epoch).
    pub signature: BigUint,
}

impl BlindCredential {
    pub fn to_dict(&self) -> serde_json::Value {
        serde_json::json!({
            "commitment": self.commitment,
            "entity_type": self.entity_type,
            "epoch": self.epoch,
            "signature": format!("0x{}", self.signature.to_str_radix(16)),
        })
    }

    pub fn from_dict(v: &serde_json::Value) -> Result<Self, String> {
        let commitment = v["commitment"]
            .as_str()
            .ok_or("missing commitment")?
            .to_string();
        let entity_type = v["entity_type"]
            .as_str()
            .ok_or("missing entity_type")?
            .to_string();
        let epoch = v["epoch"].as_u64().ok_or("missing epoch")?;
        let sig_str = v["signature"].as_str().ok_or("missing signature")?;
        let sig_hex = sig_str.strip_prefix("0x").unwrap_or(sig_str);
        let signature =
            BigUint::parse_bytes(sig_hex.as_bytes(), 16).ok_or("invalid signature hex")?;
        Ok(Self {
            commitment,
            entity_type,
            epoch,
            signature,
        })
    }
}

/// RSA blind-signature oracle for credential issuance.
///
/// Lifecycle:
///   1. Oracle generates RSA keypair (once).
///   2. Entity creates a BlindedMessage and sends `blinded` to oracle.
///   3. Oracle signs the blinded value (never sees the real message).
///   4. Entity un-blinds the signature → BlindCredential.
///   5. Anyone with the oracle's public key can verify.
pub struct BlindCredentialIssuer {
    n: BigUint,
    e: BigUint,
    d: BigUint,
}

impl BlindCredentialIssuer {
    /// Create from an existing RSA private key.
    pub fn from_rsa_key(private_key: &RsaPrivateKey) -> Self {
        let n = biguint_from_rsa(private_key.n());
        let e = biguint_from_rsa(private_key.e());
        let d = biguint_from_rsa(private_key.d());
        Self { n, e, d }
    }

    /// Generate a new oracle with a fresh RSA keypair.
    pub fn generate() -> Result<Self, String> {
        let mut rng = rand::thread_rng();
        let key = RsaPrivateKey::new(&mut rng, ORACLE_RSA_BITS)
            .map_err(|e| format!("RSA keygen failed: {}", e))?;
        Ok(Self::from_rsa_key(&key))
    }

    /// Return (n, e) for clients to build blinded messages.
    pub fn public_params(&self) -> (&BigUint, &BigUint) {
        (&self.n, &self.e)
    }

    // ── Blinding (entity side) ──────────────────────────────────

    /// Entity-side: create a blinded message.
    pub fn blind(
        commitment: &str,
        entity_type: &str,
        epoch: u64,
        n: &BigUint,
        e: &BigUint,
    ) -> BlindedMessage {
        let msg_hash = credential_hash(commitment, entity_type, epoch);
        let m = BigUint::from_bytes_be(&msg_hash) % n;

        // Pick random blinding factor coprime to n
        let mut rng = rand::thread_rng();
        let r = loop {
            let mut r_bytes = vec![0u8; 32];
            rng.fill_bytes(&mut r_bytes);
            let r = BigUint::from_bytes_be(&r_bytes) % n;
            if r > BigUint::one() && r.gcd(n) == BigUint::one() {
                break r;
            }
        };

        // blinded = m * r^e mod n
        let r_e = r.modpow(e, n);
        let blinded = (&m * &r_e) % n;

        BlindedMessage {
            blinded,
            blinding_factor: r,
            original_hash: m,
        }
    }

    // ── Signing (oracle side) ───────────────────────────────────

    /// Oracle-side: sign a blinded value.
    pub fn sign_blinded(&self, blinded: &BigUint) -> BigUint {
        blinded.modpow(&self.d, &self.n)
    }

    // ── Un-blinding (entity side) ───────────────────────────────

    /// Entity-side: remove blinding factor to get valid RSA signature.
    pub fn unblind(blind_sig: &BigUint, blinding_factor: &BigUint, n: &BigUint) -> BigUint {
        let r_inv = modinv(blinding_factor, n);
        (blind_sig * &r_inv) % n
    }

    // ── Verification (anyone) ───────────────────────────────────

    /// Verify a blind credential using oracle's public params.
    pub fn verify_credential(&self, cred: &BlindCredential) -> bool {
        verify_credential_with_params(cred, &self.n, &self.e)
    }
}

/// Hash that becomes the blind-signed message.  32 bytes (SHA3-256).
pub fn credential_hash(commitment: &str, entity_type: &str, epoch: u64) -> Vec<u8> {
    let data = format!("{}|{}|{}", commitment, entity_type, epoch);
    let mut hasher = Sha3_256::new();
    hasher.update(data.as_bytes());
    hasher.finalize().to_vec()
}

/// Verify a blind credential given oracle public params (n, e).
pub fn verify_credential_with_params(cred: &BlindCredential, n: &BigUint, e: &BigUint) -> bool {
    let msg_hash = credential_hash(&cred.commitment, &cred.entity_type, cred.epoch);
    let m = BigUint::from_bytes_be(&msg_hash) % n;
    let recovered = cred.signature.modpow(e, n);
    recovered == m
}

/// Modular multiplicative inverse.
fn modinv(a: &BigUint, m: &BigUint) -> BigUint {
    // Extended Euclidean via num-bigint: a^(m-2) mod m works for prime m,
    // but RSA n isn't prime.  Use extended_gcd from num-integer.
    let a_big = num_bigint::BigInt::from(a.clone());
    let m_big = num_bigint::BigInt::from(m.clone());
    let ext = a_big.extended_gcd(&m_big);
    let inv = ((ext.x % &m_big) + &m_big) % &m_big;
    inv.to_biguint()
        .expect("modinv result must be non-negative")
}

/// Convert `rsa::BigUint` to `num_bigint::BigUint`.
fn biguint_from_rsa(v: &RsaBigUint) -> BigUint {
    BigUint::from_bytes_be(&v.to_bytes_be())
}

// ── SHA3-256 Merkle Tree ─────────────────────────────────────────────────────

/// Append-only binary Merkle tree with SHA3-256.
///
/// Stores entity commitments for proof-of-inclusion without downloading
/// the full registry.
#[derive(Debug, Clone)]
pub struct MerkleTree {
    /// Hashed leaves (each is SHA3-256 of the original data).
    pub leaves: Vec<[u8; 32]>,
    /// Cached root (invalidated on mutation).
    root_cache: Option<[u8; 32]>,
}

impl MerkleTree {
    pub fn new() -> Self {
        Self {
            leaves: Vec::new(),
            root_cache: None,
        }
    }

    /// Add raw data as a leaf (hashes it first).
    pub fn add_leaf(&mut self, data: &[u8]) {
        let mut hasher = Sha3_256::new();
        hasher.update(data);
        let hash: [u8; 32] = hasher.finalize().into();
        self.leaves.push(hash);
        self.root_cache = None;
    }

    /// Add a commitment hex string as a leaf.
    pub fn add_commitment(&mut self, commitment_hex: &str) {
        if let Ok(bytes) = hex::decode(commitment_hex) {
            self.add_leaf(&bytes);
        }
    }

    /// Compute the Merkle root.
    pub fn root(&mut self) -> [u8; 32] {
        if let Some(cached) = self.root_cache {
            return cached;
        }
        let root = self.compute_root();
        self.root_cache = Some(root);
        root
    }

    /// Root as hex string.
    pub fn root_hex(&mut self) -> String {
        hex::encode(self.root())
    }

    fn compute_root(&self) -> [u8; 32] {
        if self.leaves.is_empty() {
            let mut hasher = Sha3_256::new();
            hasher.update(ZERO_LEAF);
            return hasher.finalize().into();
        }

        let mut layer: Vec<[u8; 32]> = self.leaves.clone();
        while layer.len() > 1 {
            if layer.len() % 2 == 1 {
                layer.push(ZERO_LEAF);
            }
            let mut next = Vec::with_capacity(layer.len() / 2);
            for chunk in layer.chunks(2) {
                let mut hasher = Sha3_256::new();
                hasher.update(chunk[0]);
                hasher.update(chunk[1]);
                next.push(hasher.finalize().into());
            }
            layer = next;
        }
        layer[0]
    }

    /// Number of leaves.
    pub fn len(&self) -> usize {
        self.leaves.len()
    }

    pub fn is_empty(&self) -> bool {
        self.leaves.is_empty()
    }

    /// Generate Merkle proof for leaf at `index`.
    ///
    /// Returns list of (sibling_hash, side) where side is 'L' or 'R'.
    pub fn proof(&self, index: usize) -> Result<Vec<([u8; 32], char)>, String> {
        if index >= self.leaves.len() {
            return Err(format!(
                "Leaf index {} out of range ({})",
                index,
                self.leaves.len()
            ));
        }

        let mut layer: Vec<[u8; 32]> = self.leaves.clone();
        let mut proof_path = Vec::new();
        let mut idx = index;

        while layer.len() > 1 {
            if layer.len() % 2 == 1 {
                layer.push(ZERO_LEAF);
            }
            let sibling_idx = idx ^ 1;
            let side = if sibling_idx > idx { 'R' } else { 'L' };
            proof_path.push((layer[sibling_idx], side));

            let mut next = Vec::with_capacity(layer.len() / 2);
            for chunk in layer.chunks(2) {
                let mut hasher = Sha3_256::new();
                hasher.update(chunk[0]);
                hasher.update(chunk[1]);
                next.push(hasher.finalize().into());
            }
            layer = next;
            idx /= 2;
        }

        Ok(proof_path)
    }

    /// Verify a Merkle inclusion proof.
    pub fn verify_proof(leaf: &[u8; 32], proof_path: &[([u8; 32], char)], root: &[u8; 32]) -> bool {
        let mut current = *leaf;
        for (sibling, side) in proof_path {
            let mut hasher = Sha3_256::new();
            if *side == 'R' {
                hasher.update(current);
                hasher.update(sibling);
            } else {
                hasher.update(sibling);
                hasher.update(current);
            }
            current = hasher.finalize().into();
        }
        current == *root
    }

    /// Serialize to JSON dict.
    pub fn to_dict(&self) -> serde_json::Value {
        let leaves: Vec<String> = self.leaves.iter().map(|l| hex::encode(l)).collect();
        serde_json::json!({
            "leaves": leaves,
        })
    }

    /// Deserialize from JSON dict.
    pub fn from_dict(v: &serde_json::Value) -> Self {
        let leaves: Vec<[u8; 32]> = v["leaves"]
            .as_array()
            .map(|arr| {
                arr.iter()
                    .filter_map(|h| {
                        let bytes = hex::decode(h.as_str()?).ok()?;
                        if bytes.len() == 32 {
                            let mut arr = [0u8; 32];
                            arr.copy_from_slice(&bytes);
                            Some(arr)
                        } else {
                            None
                        }
                    })
                    .collect()
            })
            .unwrap_or_default();
        MerkleTree {
            leaves,
            root_cache: None,
        }
    }
}

impl Default for MerkleTree {
    fn default() -> Self {
        Self::new()
    }
}

// ── Entity Record (on-chain) ─────────────────────────────────────────────────

/// A single on-chain entity registration.
#[derive(Debug, Clone)]
pub struct EntityRecord {
    /// SHA3-256(secret ‖ nullifier ‖ type).
    pub commitment: String,
    /// "human" or "machine".
    pub entity_type: String,
    /// Epoch in which the entity registered.
    pub epoch: u64,
    /// hash(nullifier ‖ epoch) — prevents double-reg.
    pub epoch_nullifier: String,
    /// Hex of blind-signed credential.
    pub credential_signature: String,
    /// Timestamp.
    pub registered_at: f64,
    /// SHA3-256 of attestation (machines only, empty for humans).
    pub hardware_attestation_hash: String,
}

impl EntityRecord {
    pub fn to_dict(&self) -> serde_json::Value {
        serde_json::json!({
            "commitment": self.commitment,
            "entity_type": self.entity_type,
            "epoch": self.epoch,
            "epoch_nullifier": self.epoch_nullifier,
            "credential_signature": self.credential_signature,
            "registered_at": self.registered_at,
            "hardware_attestation_hash": self.hardware_attestation_hash,
        })
    }

    pub fn from_dict(v: &serde_json::Value) -> Result<Self, String> {
        Ok(Self {
            commitment: v["commitment"]
                .as_str()
                .ok_or("missing commitment")?
                .to_string(),
            entity_type: v["entity_type"]
                .as_str()
                .ok_or("missing entity_type")?
                .to_string(),
            epoch: v["epoch"].as_u64().ok_or("missing epoch")?,
            epoch_nullifier: v["epoch_nullifier"]
                .as_str()
                .ok_or("missing epoch_nullifier")?
                .to_string(),
            credential_signature: v["credential_signature"]
                .as_str()
                .ok_or("missing credential_signature")?
                .to_string(),
            registered_at: v["registered_at"].as_f64().unwrap_or(0.0),
            hardware_attestation_hash: v["hardware_attestation_hash"]
                .as_str()
                .unwrap_or("")
                .to_string(),
        })
    }
}

// ── Entity Registry ──────────────────────────────────────────────────────────

/// In-memory entity registry backed by Merkle tree.
///
/// Each node maintains this as part of blockchain state.
pub struct EntityRegistry {
    /// commitment → record.
    pub records: HashMap<String, EntityRecord>,
    /// Spent epoch nullifiers.
    pub nullifiers: HashSet<String>,
    /// Merkle tree for human entities.
    pub human_tree: MerkleTree,
    /// Merkle tree for machine entities.
    pub machine_tree: MerkleTree,
    /// Oracle public params for credential verification.
    oracle_n: Option<BigUint>,
    oracle_e: Option<BigUint>,
    /// wallet_address → commitment (links address to entity).
    /// VRF mining candidate selection checks this.
    pub wallet_tags: HashMap<String, String>,
}

impl EntityRegistry {
    pub fn new() -> Self {
        Self {
            records: HashMap::new(),
            nullifiers: HashSet::new(),
            human_tree: MerkleTree::new(),
            machine_tree: MerkleTree::new(),
            oracle_n: None,
            oracle_e: None,
            wallet_tags: HashMap::new(),
        }
    }

    /// Set oracle public params for credential verification.
    pub fn set_oracle_params(&mut self, n: BigUint, e: BigUint) {
        self.oracle_n = Some(n);
        self.oracle_e = Some(e);
    }

    /// Validate and register an entity.
    pub fn register(&mut self, record: &EntityRecord) -> Result<(), String> {
        // 1. Type check
        if record.entity_type != ENTITY_TYPE_HUMAN && record.entity_type != ENTITY_TYPE_MACHINE {
            return Err(format!("Invalid entity type: {}", record.entity_type));
        }

        // 2. Duplicate commitment
        if self.records.contains_key(&record.commitment) {
            return Err("Commitment already registered".into());
        }

        // 3. Epoch nullifier — prevent double-registration
        if self.nullifiers.contains(&record.epoch_nullifier) {
            return Err("Epoch nullifier already spent (double-registration attempt)".into());
        }

        // 4. Verify blind credential if oracle params available
        if let (Some(n), Some(e)) = (&self.oracle_n, &self.oracle_e) {
            let sig_hex = record
                .credential_signature
                .strip_prefix("0x")
                .unwrap_or(&record.credential_signature);
            let signature = BigUint::parse_bytes(sig_hex.as_bytes(), 16)
                .ok_or("Invalid credential signature hex")?;
            let cred = BlindCredential {
                commitment: record.commitment.clone(),
                entity_type: record.entity_type.clone(),
                epoch: record.epoch,
                signature,
            };
            if !verify_credential_with_params(&cred, n, e) {
                return Err(
                    "Invalid oracle credential (blind signature verification failed)".into(),
                );
            }
        }

        // 5. Accept
        self.records
            .insert(record.commitment.clone(), record.clone());
        self.nullifiers.insert(record.epoch_nullifier.clone());
        if record.entity_type == ENTITY_TYPE_HUMAN {
            self.human_tree.add_commitment(&record.commitment);
        } else {
            self.machine_tree.add_commitment(&record.commitment);
        }

        Ok(())
    }

    /// Get entity type for a commitment.
    pub fn get_entity_type(&self, commitment: &str) -> Option<&str> {
        self.records.get(commitment).map(|r| r.entity_type.as_str())
    }

    /// Statistics.
    pub fn stats(&self) -> RegistryStats {
        let humans = self
            .records
            .values()
            .filter(|r| r.entity_type == ENTITY_TYPE_HUMAN)
            .count();
        let machines = self
            .records
            .values()
            .filter(|r| r.entity_type == ENTITY_TYPE_MACHINE)
            .count();
        RegistryStats {
            total: self.records.len(),
            humans,
            machines,
            wallets_tagged: self.wallet_tags.len(),
        }
    }

    /// Merkle root for a given entity type.
    pub fn merkle_root(&mut self, entity_type: &str) -> String {
        if entity_type == ENTITY_TYPE_HUMAN {
            self.human_tree.root_hex()
        } else {
            self.machine_tree.root_hex()
        }
    }

    /// Link a wallet address to a registered entity commitment.
    ///
    /// Each wallet → ONE entity, each entity → ONE wallet (Sybil prevention).
    pub fn tag_wallet(&mut self, wallet_address: &str, commitment: &str) -> Result<(), String> {
        if !self.records.contains_key(commitment) {
            return Err("Commitment not registered".into());
        }
        if self.wallet_tags.contains_key(wallet_address) {
            return Err("Wallet already tagged to an entity".into());
        }
        // Check reverse: this commitment already has a wallet
        if self.wallet_tags.values().any(|c| c == commitment) {
            return Err("Entity already tagged to a wallet".into());
        }
        self.wallet_tags
            .insert(wallet_address.to_string(), commitment.to_string());
        Ok(())
    }

    /// Check if a wallet address is tagged to any entity.
    pub fn is_wallet_tagged(&self, wallet_address: &str) -> bool {
        self.wallet_tags.contains_key(wallet_address)
    }

    /// Serialize to JSON dict.
    pub fn to_dict(&self) -> serde_json::Value {
        let records: serde_json::Map<String, serde_json::Value> = self
            .records
            .iter()
            .map(|(k, v)| (k.clone(), v.to_dict()))
            .collect();
        let nullifiers: Vec<&str> = self.nullifiers.iter().map(|s| s.as_str()).collect();
        let wallet_tags: serde_json::Map<String, serde_json::Value> = self
            .wallet_tags
            .iter()
            .map(|(k, v)| (k.clone(), serde_json::Value::String(v.clone())))
            .collect();
        serde_json::json!({
            "records": records,
            "nullifiers": nullifiers,
            "human_tree": self.human_tree.to_dict(),
            "machine_tree": self.machine_tree.to_dict(),
            "wallet_tags": wallet_tags,
        })
    }

    /// Deserialize from JSON dict.
    pub fn from_dict(v: &serde_json::Value) -> Self {
        let mut reg = Self::new();

        if let Some(records) = v["records"].as_object() {
            for (k, rv) in records {
                if let Ok(record) = EntityRecord::from_dict(rv) {
                    reg.records.insert(k.clone(), record);
                }
            }
        }

        if let Some(nulls) = v["nullifiers"].as_array() {
            for n in nulls {
                if let Some(s) = n.as_str() {
                    reg.nullifiers.insert(s.to_string());
                }
            }
        }

        reg.human_tree = MerkleTree::from_dict(&v["human_tree"]);
        reg.machine_tree = MerkleTree::from_dict(&v["machine_tree"]);

        if let Some(tags) = v["wallet_tags"].as_object() {
            for (k, val) in tags {
                if let Some(s) = val.as_str() {
                    reg.wallet_tags.insert(k.clone(), s.to_string());
                }
            }
        }

        reg
    }
}

impl Default for EntityRegistry {
    fn default() -> Self {
        Self::new()
    }
}

/// Registry statistics.
#[derive(Debug, Clone)]
pub struct RegistryStats {
    pub total: usize,
    pub humans: usize,
    pub machines: usize,
    pub wallets_tagged: usize,
}

// ── Helpers ──────────────────────────────────────────────────────────────────

fn now_f64() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    // ── EntityIdentity ──────────────────────────────────────────

    #[test]
    fn test_identity_generate() {
        let id = EntityIdentity::generate("machine").unwrap();
        assert_eq!(id.entity_type, "machine");
        assert!(!id.commitment.is_empty());
        assert_eq!(id.commitment.len(), 64); // SHA3-256 hex
    }

    #[test]
    fn test_identity_invalid_type() {
        assert!(EntityIdentity::generate("robot").is_err());
    }

    #[test]
    fn test_identity_commitment_deterministic() {
        let secret = [1u8; 32];
        let nullifier = [2u8; 32];
        let c1 = compute_commitment(&secret, &nullifier, "human");
        let c2 = compute_commitment(&secret, &nullifier, "human");
        assert_eq!(c1, c2);
    }

    #[test]
    fn test_identity_commitment_different_types() {
        let secret = [1u8; 32];
        let nullifier = [2u8; 32];
        let c_human = compute_commitment(&secret, &nullifier, "human");
        let c_machine = compute_commitment(&secret, &nullifier, "machine");
        assert_ne!(c_human, c_machine);
    }

    #[test]
    fn test_identity_epoch_nullifier_deterministic() {
        let id = EntityIdentity::generate("human").unwrap();
        let n1 = id.epoch_nullifier(42);
        let n2 = id.epoch_nullifier(42);
        assert_eq!(n1, n2);
    }

    #[test]
    fn test_identity_epoch_nullifier_different_epochs() {
        let id = EntityIdentity::generate("human").unwrap();
        let n1 = id.epoch_nullifier(42);
        let n2 = id.epoch_nullifier(43);
        assert_ne!(n1, n2);
    }

    #[test]
    fn test_identity_roundtrip() {
        let id = EntityIdentity::generate("machine").unwrap();
        let dict = id.to_dict();
        let id2 = EntityIdentity::from_dict(&dict).unwrap();
        assert_eq!(id.commitment, id2.commitment);
        assert_eq!(id.entity_type, id2.entity_type);
        assert_eq!(id.identity_secret, id2.identity_secret);
    }

    // ── Blind Signatures ────────────────────────────────────────

    #[test]
    fn test_blind_credential_full_flow() {
        let oracle = BlindCredentialIssuer::generate().unwrap();
        let (n, e) = oracle.public_params();

        let commitment = "abc123def456";
        let entity_type = "machine";
        let epoch = 100u64;

        // Entity blinds
        let blinded_msg = BlindCredentialIssuer::blind(commitment, entity_type, epoch, n, e);

        // Oracle signs blinded value
        let blind_sig = oracle.sign_blinded(&blinded_msg.blinded);

        // Entity unblinds
        let real_sig = BlindCredentialIssuer::unblind(&blind_sig, &blinded_msg.blinding_factor, n);

        // Build credential
        let cred = BlindCredential {
            commitment: commitment.into(),
            entity_type: entity_type.into(),
            epoch,
            signature: real_sig,
        };

        // Anyone can verify
        assert!(oracle.verify_credential(&cred));
    }

    #[test]
    fn test_blind_credential_wrong_commitment_fails() {
        let oracle = BlindCredentialIssuer::generate().unwrap();
        let (n, e) = oracle.public_params();

        let blinded_msg = BlindCredentialIssuer::blind("real_commitment", "human", 1, n, e);
        let blind_sig = oracle.sign_blinded(&blinded_msg.blinded);
        let real_sig = BlindCredentialIssuer::unblind(&blind_sig, &blinded_msg.blinding_factor, n);

        // Tamper with commitment
        let cred = BlindCredential {
            commitment: "fake_commitment".into(),
            entity_type: "human".into(),
            epoch: 1,
            signature: real_sig,
        };
        assert!(!oracle.verify_credential(&cred));
    }

    #[test]
    fn test_blind_credential_roundtrip() {
        let oracle = BlindCredentialIssuer::generate().unwrap();
        let (n, e) = oracle.public_params();

        let blinded = BlindCredentialIssuer::blind("test", "machine", 5, n, e);
        let blind_sig = oracle.sign_blinded(&blinded.blinded);
        let sig = BlindCredentialIssuer::unblind(&blind_sig, &blinded.blinding_factor, n);

        let cred = BlindCredential {
            commitment: "test".into(),
            entity_type: "machine".into(),
            epoch: 5,
            signature: sig,
        };

        let dict = cred.to_dict();
        let cred2 = BlindCredential::from_dict(&dict).unwrap();
        assert_eq!(cred.commitment, cred2.commitment);
        assert_eq!(cred.epoch, cred2.epoch);
        assert!(oracle.verify_credential(&cred2));
    }

    // ── Merkle Tree ─────────────────────────────────────────────

    #[test]
    fn test_merkle_empty_root() {
        let mut tree = MerkleTree::new();
        let root = tree.root();
        // Should be SHA3-256 of ZERO_LEAF
        let mut hasher = Sha3_256::new();
        hasher.update(ZERO_LEAF);
        let expected: [u8; 32] = hasher.finalize().into();
        assert_eq!(root, expected);
    }

    #[test]
    fn test_merkle_single_leaf() {
        let mut tree = MerkleTree::new();
        tree.add_leaf(b"hello");
        let root = tree.root();
        assert_ne!(root, ZERO_LEAF);
    }

    #[test]
    fn test_merkle_root_deterministic() {
        let mut t1 = MerkleTree::new();
        let mut t2 = MerkleTree::new();
        t1.add_leaf(b"a");
        t1.add_leaf(b"b");
        t2.add_leaf(b"a");
        t2.add_leaf(b"b");
        assert_eq!(t1.root(), t2.root());
    }

    #[test]
    fn test_merkle_root_changes_on_add() {
        let mut tree = MerkleTree::new();
        tree.add_leaf(b"a");
        let r1 = tree.root();
        tree.add_leaf(b"b");
        let r2 = tree.root();
        assert_ne!(r1, r2);
    }

    #[test]
    fn test_merkle_proof_valid() {
        let mut tree = MerkleTree::new();
        tree.add_leaf(b"alpha");
        tree.add_leaf(b"beta");
        tree.add_leaf(b"gamma");
        tree.add_leaf(b"delta");

        let root = tree.root();

        for i in 0..4 {
            let proof = tree.proof(i).unwrap();
            let leaf = tree.leaves[i];
            assert!(MerkleTree::verify_proof(&leaf, &proof, &root));
        }
    }

    #[test]
    fn test_merkle_proof_invalid_leaf() {
        let mut tree = MerkleTree::new();
        tree.add_leaf(b"alpha");
        tree.add_leaf(b"beta");

        let root = tree.root();
        let proof = tree.proof(0).unwrap();
        let fake_leaf = [0xffu8; 32];
        assert!(!MerkleTree::verify_proof(&fake_leaf, &proof, &root));
    }

    #[test]
    fn test_merkle_proof_odd_leaves() {
        let mut tree = MerkleTree::new();
        tree.add_leaf(b"a");
        tree.add_leaf(b"b");
        tree.add_leaf(b"c"); // odd — gets ZERO_LEAF sibling

        let root = tree.root();
        for i in 0..3 {
            let proof = tree.proof(i).unwrap();
            assert!(MerkleTree::verify_proof(&tree.leaves[i], &proof, &root));
        }
    }

    #[test]
    fn test_merkle_roundtrip() {
        let mut tree = MerkleTree::new();
        tree.add_leaf(b"x");
        tree.add_leaf(b"y");
        let root1 = tree.root();

        let dict = tree.to_dict();
        let mut tree2 = MerkleTree::from_dict(&dict);
        assert_eq!(tree2.root(), root1);
    }

    // ── Entity Record ───────────────────────────────────────────

    #[test]
    fn test_entity_record_roundtrip() {
        let rec = EntityRecord {
            commitment: "abc123".into(),
            entity_type: "human".into(),
            epoch: 42,
            epoch_nullifier: "nullhash".into(),
            credential_signature: "0xdeadbeef".into(),
            registered_at: 1234567890.0,
            hardware_attestation_hash: "".into(),
        };
        let dict = rec.to_dict();
        let rec2 = EntityRecord::from_dict(&dict).unwrap();
        assert_eq!(rec.commitment, rec2.commitment);
        assert_eq!(rec.epoch, rec2.epoch);
    }

    // ── Entity Registry ─────────────────────────────────────────

    #[test]
    fn test_registry_register_basic() {
        let mut reg = EntityRegistry::new();
        let rec = EntityRecord {
            commitment: "commit1".into(),
            entity_type: "human".into(),
            epoch: 1,
            epoch_nullifier: "null1".into(),
            credential_signature: "0x1234".into(),
            registered_at: 0.0,
            hardware_attestation_hash: "".into(),
        };
        assert!(reg.register(&rec).is_ok());
        assert_eq!(reg.records.len(), 1);
        assert_eq!(reg.get_entity_type("commit1"), Some("human"));
    }

    #[test]
    fn test_registry_reject_duplicate_commitment() {
        let mut reg = EntityRegistry::new();
        let rec = EntityRecord {
            commitment: "commit1".into(),
            entity_type: "human".into(),
            epoch: 1,
            epoch_nullifier: "null1".into(),
            credential_signature: "0x1234".into(),
            registered_at: 0.0,
            hardware_attestation_hash: "".into(),
        };
        reg.register(&rec).unwrap();
        assert!(reg.register(&rec).is_err());
    }

    #[test]
    fn test_registry_reject_duplicate_nullifier() {
        let mut reg = EntityRegistry::new();
        let rec1 = EntityRecord {
            commitment: "commit1".into(),
            entity_type: "human".into(),
            epoch: 1,
            epoch_nullifier: "same_null".into(),
            credential_signature: "0x1234".into(),
            registered_at: 0.0,
            hardware_attestation_hash: "".into(),
        };
        let rec2 = EntityRecord {
            commitment: "commit2".into(),
            entity_type: "machine".into(),
            epoch: 1,
            epoch_nullifier: "same_null".into(),
            credential_signature: "0x5678".into(),
            registered_at: 0.0,
            hardware_attestation_hash: "".into(),
        };
        reg.register(&rec1).unwrap();
        assert!(reg.register(&rec2).is_err());
    }

    #[test]
    fn test_registry_reject_invalid_type() {
        let mut reg = EntityRegistry::new();
        let rec = EntityRecord {
            commitment: "c".into(),
            entity_type: "robot".into(),
            epoch: 1,
            epoch_nullifier: "n".into(),
            credential_signature: "0x0".into(),
            registered_at: 0.0,
            hardware_attestation_hash: "".into(),
        };
        assert!(reg.register(&rec).is_err());
    }

    #[test]
    fn test_registry_with_oracle_verification() {
        let oracle = BlindCredentialIssuer::generate().unwrap();
        let (n, e) = oracle.public_params();

        let mut reg = EntityRegistry::new();
        reg.set_oracle_params(n.clone(), e.clone());

        // Generate a valid credential
        let identity = EntityIdentity::generate("machine").unwrap();
        let epoch = current_epoch();
        let blinded_msg =
            BlindCredentialIssuer::blind(&identity.commitment, "machine", epoch, n, e);
        let blind_sig = oracle.sign_blinded(&blinded_msg.blinded);
        let real_sig = BlindCredentialIssuer::unblind(&blind_sig, &blinded_msg.blinding_factor, n);

        let rec = EntityRecord {
            commitment: identity.commitment.clone(),
            entity_type: "machine".into(),
            epoch,
            epoch_nullifier: identity.epoch_nullifier(epoch),
            credential_signature: format!("0x{}", real_sig.to_str_radix(16)),
            registered_at: now_f64(),
            hardware_attestation_hash: "deadbeef".into(),
        };

        // Should succeed with valid credential
        assert!(reg.register(&rec).is_ok());
    }

    #[test]
    fn test_registry_rejects_invalid_credential() {
        let oracle = BlindCredentialIssuer::generate().unwrap();
        let (n, e) = oracle.public_params();

        let mut reg = EntityRegistry::new();
        reg.set_oracle_params(n.clone(), e.clone());

        let rec = EntityRecord {
            commitment: "fake".into(),
            entity_type: "human".into(),
            epoch: 1,
            epoch_nullifier: "null".into(),
            credential_signature: "0xdeadbeef".into(),
            registered_at: 0.0,
            hardware_attestation_hash: "".into(),
        };

        assert!(reg.register(&rec).is_err());
    }

    // ── Wallet Tagging ──────────────────────────────────────────

    #[test]
    fn test_wallet_tag() {
        let mut reg = EntityRegistry::new();
        let rec = EntityRecord {
            commitment: "commit1".into(),
            entity_type: "human".into(),
            epoch: 1,
            epoch_nullifier: "null1".into(),
            credential_signature: "0x1234".into(),
            registered_at: 0.0,
            hardware_attestation_hash: "".into(),
        };
        reg.register(&rec).unwrap();

        assert!(reg.tag_wallet("wallet_A", "commit1").is_ok());
        assert!(reg.is_wallet_tagged("wallet_A"));
    }

    #[test]
    fn test_wallet_tag_reject_unregistered() {
        let mut reg = EntityRegistry::new();
        assert!(reg.tag_wallet("wallet_A", "nonexistent").is_err());
    }

    #[test]
    fn test_wallet_tag_reject_duplicate_wallet() {
        let mut reg = EntityRegistry::new();
        let rec1 = EntityRecord {
            commitment: "c1".into(),
            entity_type: "human".into(),
            epoch: 1,
            epoch_nullifier: "n1".into(),
            credential_signature: "0x1".into(),
            registered_at: 0.0,
            hardware_attestation_hash: "".into(),
        };
        let rec2 = EntityRecord {
            commitment: "c2".into(),
            entity_type: "machine".into(),
            epoch: 1,
            epoch_nullifier: "n2".into(),
            credential_signature: "0x2".into(),
            registered_at: 0.0,
            hardware_attestation_hash: "".into(),
        };
        reg.register(&rec1).unwrap();
        reg.register(&rec2).unwrap();
        reg.tag_wallet("wallet_A", "c1").unwrap();
        assert!(reg.tag_wallet("wallet_A", "c2").is_err());
    }

    #[test]
    fn test_wallet_tag_reject_duplicate_entity() {
        let mut reg = EntityRegistry::new();
        let rec = EntityRecord {
            commitment: "c1".into(),
            entity_type: "human".into(),
            epoch: 1,
            epoch_nullifier: "n1".into(),
            credential_signature: "0x1".into(),
            registered_at: 0.0,
            hardware_attestation_hash: "".into(),
        };
        reg.register(&rec).unwrap();
        reg.tag_wallet("wallet_A", "c1").unwrap();
        // Same entity trying to tag a different wallet
        assert!(reg.tag_wallet("wallet_B", "c1").is_err());
    }

    // ── Registry Stats ──────────────────────────────────────────

    #[test]
    fn test_registry_stats() {
        let mut reg = EntityRegistry::new();
        let h = EntityRecord {
            commitment: "h1".into(),
            entity_type: "human".into(),
            epoch: 1,
            epoch_nullifier: "hn1".into(),
            credential_signature: "0x1".into(),
            registered_at: 0.0,
            hardware_attestation_hash: "".into(),
        };
        let m = EntityRecord {
            commitment: "m1".into(),
            entity_type: "machine".into(),
            epoch: 1,
            epoch_nullifier: "mn1".into(),
            credential_signature: "0x2".into(),
            registered_at: 0.0,
            hardware_attestation_hash: "hw_hash".into(),
        };
        reg.register(&h).unwrap();
        reg.register(&m).unwrap();
        reg.tag_wallet("w1", "h1").unwrap();

        let stats = reg.stats();
        assert_eq!(stats.total, 2);
        assert_eq!(stats.humans, 1);
        assert_eq!(stats.machines, 1);
        assert_eq!(stats.wallets_tagged, 1);
    }

    // ── Registry Serialization ──────────────────────────────────

    #[test]
    fn test_registry_roundtrip() {
        let mut reg = EntityRegistry::new();
        let rec = EntityRecord {
            commitment: "c1".into(),
            entity_type: "human".into(),
            epoch: 1,
            epoch_nullifier: "n1".into(),
            credential_signature: "0x1234".into(),
            registered_at: 1000.0,
            hardware_attestation_hash: "".into(),
        };
        reg.register(&rec).unwrap();
        reg.tag_wallet("w1", "c1").unwrap();

        let dict = reg.to_dict();
        let reg2 = EntityRegistry::from_dict(&dict);

        assert_eq!(reg2.records.len(), 1);
        assert_eq!(reg2.nullifiers.len(), 1);
        assert_eq!(reg2.wallet_tags.len(), 1);
        assert!(reg2.is_wallet_tagged("w1"));
    }

    // ── Current Epoch ───────────────────────────────────────────

    #[test]
    fn test_current_epoch_nonzero() {
        assert!(current_epoch() > 0);
    }
}
