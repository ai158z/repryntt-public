//! Proof of Physical Device (PPD) — device registry and hardware verification.
//!
//! Ensures each mining node is backed by unique physical hardware.
//! Matches Python's `entity_verification.py` DeviceRegistry, GPUSiliconFingerprint,
//! LatencyVouch, NetworkPositionProof, and DeviceRegistration.

use sha3::{Digest, Sha3_256};
use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

// ── Constants ────────────────────────────────────────────────────────────────

/// Trust tier 1: GPU fingerprint + latency only.
pub const TRUST_TIER_1: u8 = 1;
/// Trust tier 2: Phone/TEE + GPU + latency.
pub const TRUST_TIER_2: u8 = 2;
/// Trust tier 3: TPM + GPU + latency.
pub const TRUST_TIER_3: u8 = 3;

/// Maximum nodes an entity may run per trust tier.
pub fn tier_max_nodes(tier: u8) -> usize {
    match tier {
        TRUST_TIER_1 => 1,
        TRUST_TIER_2 => 3,
        TRUST_TIER_3 => 5,
        _ => 0,
    }
}

/// Silicon fingerprint similarity tolerance (0.5%).
pub const SILICON_FINGERPRINT_TOLERANCE: f64 = 0.005;

/// Minimum peers needed for latency triangulation.
pub const MIN_LATENCY_PEERS: usize = 3;

/// Correlation threshold for colocation detection.
pub const LATENCY_CORRELATION_THRESHOLD: f64 = 0.95;

/// Blocks between forced re-verification (~19h at 69s/block).
pub const REVERIFICATION_INTERVAL_BLOCKS: u64 = 1000;

/// Registration bond: 0.01 CR in plancks (100M plancks = 1 CR).
pub const REGISTRATION_BOND_PLANCKS: i64 = 1_000_000;

// ── GPU Silicon Fingerprint ──────────────────────────────────────────────────

/// Unique timing fingerprint from GPU silicon manufacturing variance.
///
/// Two identical-model GPUs produce measurably different timing patterns
/// because of lithographic variation, enabling per-device identification.
#[derive(Debug, Clone)]
pub struct GPUSiliconFingerprint {
    /// Mean iteration time in nanoseconds.
    pub mean_ns: f64,
    /// Standard deviation.
    pub stddev_ns: f64,
    /// Coefficient of variation (stddev / mean).
    pub coeff_variation: f64,
    /// Percentiles: [p5, p25, p50, p75, p95].
    pub percentiles: [f64; 5],
    /// SHA3-256 of the full raw timing vector.
    pub timing_hash: String,
    /// GPU model string (for grouping).
    pub gpu_model: String,
    /// Number of benchmark iterations.
    pub iterations: u32,
    /// Timestamp of measurement.
    pub measured_at: f64,
}

impl GPUSiliconFingerprint {
    /// Similarity score between two fingerprints (0.0 – 1.0).
    ///
    /// Weighted: 75% percentile pattern + 25% coefficient-of-variation match.
    pub fn similarity(&self, other: &Self) -> f64 {
        // Percentile pattern similarity (normalized euclidean distance)
        let mut sq_diff_sum = 0.0_f64;
        let mut max_val = 1.0_f64;
        for i in 0..5 {
            let d = self.percentiles[i] - other.percentiles[i];
            sq_diff_sum += d * d;
            max_val = max_val
                .max(self.percentiles[i].abs())
                .max(other.percentiles[i].abs());
        }
        let norm_dist = (sq_diff_sum.sqrt()) / (max_val * 5.0_f64.sqrt());
        let pct_sim = (1.0 - norm_dist).max(0.0);

        // CV similarity
        let cv_max = self.coeff_variation.max(other.coeff_variation).max(1e-9);
        let cv_sim = 1.0 - ((self.coeff_variation - other.coeff_variation).abs() / cv_max);

        0.75 * pct_sim + 0.25 * cv_sim.max(0.0)
    }

    pub fn to_dict(&self) -> serde_json::Value {
        serde_json::json!({
            "mean_ns": self.mean_ns,
            "stddev_ns": self.stddev_ns,
            "coeff_variation": self.coeff_variation,
            "percentiles": self.percentiles,
            "timing_hash": self.timing_hash,
            "gpu_model": self.gpu_model,
            "iterations": self.iterations,
            "measured_at": self.measured_at,
        })
    }

    pub fn from_dict(v: &serde_json::Value) -> Result<Self, String> {
        let mean_ns = v["mean_ns"].as_f64().ok_or("missing mean_ns")?;
        let stddev_ns = v["stddev_ns"].as_f64().ok_or("missing stddev_ns")?;
        let coeff_variation = v["coeff_variation"]
            .as_f64()
            .ok_or("missing coeff_variation")?;
        let pct_arr = v["percentiles"].as_array().ok_or("missing percentiles")?;
        if pct_arr.len() != 5 {
            return Err("percentiles must have 5 elements".into());
        }
        let mut percentiles = [0.0; 5];
        for (i, val) in pct_arr.iter().enumerate() {
            percentiles[i] = val.as_f64().ok_or("non-numeric percentile")?;
        }
        Ok(Self {
            mean_ns,
            stddev_ns,
            coeff_variation,
            percentiles,
            timing_hash: v["timing_hash"].as_str().unwrap_or("").to_string(),
            gpu_model: v["gpu_model"].as_str().unwrap_or("").to_string(),
            iterations: v["iterations"].as_u64().unwrap_or(0) as u32,
            measured_at: v["measured_at"].as_f64().unwrap_or(0.0),
        })
    }
}

// ── Latency Vouch ────────────────────────────────────────────────────────────

/// A single peer's RTT attestation for network position proof.
#[derive(Debug, Clone)]
pub struct LatencyVouch {
    /// Wallet address of the vouching peer.
    pub peer_address: String,
    /// Wallet address of the node being vouched for.
    pub target_address: String,
    /// Round-trip time in milliseconds.
    pub rtt_ms: f64,
    /// Random nonce for freshness.
    pub nonce: String,
    /// When the vouch was created.
    pub timestamp: f64,
    /// Ed25519 signature by the peer.
    pub peer_signature: String,
}

impl LatencyVouch {
    pub fn to_dict(&self) -> serde_json::Value {
        serde_json::json!({
            "peer_address": self.peer_address,
            "target_address": self.target_address,
            "rtt_ms": self.rtt_ms,
            "nonce": self.nonce,
            "timestamp": self.timestamp,
            "peer_signature": self.peer_signature,
        })
    }

    pub fn from_dict(v: &serde_json::Value) -> Result<Self, String> {
        Ok(Self {
            peer_address: v["peer_address"]
                .as_str()
                .ok_or("missing peer_address")?
                .to_string(),
            target_address: v["target_address"]
                .as_str()
                .ok_or("missing target_address")?
                .to_string(),
            rtt_ms: v["rtt_ms"].as_f64().ok_or("missing rtt_ms")?,
            nonce: v["nonce"].as_str().unwrap_or("").to_string(),
            timestamp: v["timestamp"].as_f64().unwrap_or(0.0),
            peer_signature: v["peer_signature"].as_str().unwrap_or("").to_string(),
        })
    }
}

// ── Network Position Proof ───────────────────────────────────────────────────

/// Proof of network position via multi-peer latency triangulation.
#[derive(Debug, Clone)]
pub struct NetworkPositionProof {
    /// Wallet address of the target node.
    pub target_address: String,
    /// Individual latency vouches from peers.
    pub vouches: Vec<LatencyVouch>,
    /// Summary: peer_address → rtt_ms.
    pub latency_vector: HashMap<String, f64>,
    /// When the proof was created.
    pub created_at: f64,
}

impl NetworkPositionProof {
    /// Number of vouching peers.
    pub fn peer_count(&self) -> usize {
        self.vouches.len()
    }

    /// Valid if ≥ MIN_LATENCY_PEERS and all RTTs in [0.1, 2000] ms.
    pub fn is_valid(&self) -> bool {
        if self.vouches.len() < MIN_LATENCY_PEERS {
            return false;
        }
        self.vouches
            .iter()
            .all(|v| v.rtt_ms >= 0.1 && v.rtt_ms <= 2000.0)
    }

    pub fn to_dict(&self) -> serde_json::Value {
        let vouches: Vec<serde_json::Value> = self.vouches.iter().map(|v| v.to_dict()).collect();
        let lv: serde_json::Map<String, serde_json::Value> = self
            .latency_vector
            .iter()
            .map(|(k, v)| (k.clone(), serde_json::json!(*v)))
            .collect();
        serde_json::json!({
            "target_address": self.target_address,
            "vouches": vouches,
            "latency_vector": lv,
            "created_at": self.created_at,
        })
    }

    pub fn from_dict(v: &serde_json::Value) -> Result<Self, String> {
        let vouches: Vec<LatencyVouch> = v["vouches"]
            .as_array()
            .unwrap_or(&Vec::new())
            .iter()
            .filter_map(|vv| LatencyVouch::from_dict(vv).ok())
            .collect();
        let mut latency_vector = HashMap::new();
        if let Some(lv) = v["latency_vector"].as_object() {
            for (k, val) in lv {
                if let Some(f) = val.as_f64() {
                    latency_vector.insert(k.clone(), f);
                }
            }
        }
        Ok(Self {
            target_address: v["target_address"].as_str().unwrap_or("").to_string(),
            vouches,
            latency_vector,
            created_at: v["created_at"].as_f64().unwrap_or(0.0),
        })
    }
}

// ── Device Registration ──────────────────────────────────────────────────────

/// Full device registration record.
#[derive(Debug, Clone)]
pub struct DeviceRegistration {
    /// Wallet address of the node operator.
    pub wallet_address: String,
    /// Links to EntityRecord commitment.
    pub entity_commitment: String,
    /// Trust tier (1, 2, or 3).
    pub trust_tier: u8,
    /// Hardware attestation data (composite hash etc.).
    pub hardware_attestation: serde_json::Value,
    /// GPU silicon fingerprint.
    pub silicon_fingerprint: Option<GPUSiliconFingerprint>,
    /// Network position proof.
    pub network_position: Option<NetworkPositionProof>,
    /// TPM EK cert hash (Tier 3 only).
    pub tpm_attestation: Option<String>,
    /// Phone attestation hash (Tier 2 only).
    pub phone_attestation: Option<String>,
    /// When registered.
    pub registered_at: f64,
    /// Last re-verification timestamp.
    pub last_reverified_at: f64,
    /// Block height at last re-verification.
    pub reverification_block: u64,
    /// Hash of the bond transaction.
    pub bond_tx_hash: String,
}

impl DeviceRegistration {
    pub fn to_dict(&self) -> serde_json::Value {
        serde_json::json!({
            "wallet_address": self.wallet_address,
            "entity_commitment": self.entity_commitment,
            "trust_tier": self.trust_tier,
            "hardware_attestation": self.hardware_attestation,
            "silicon_fingerprint": self.silicon_fingerprint.as_ref().map(|f| f.to_dict()),
            "network_position": self.network_position.as_ref().map(|p| p.to_dict()),
            "tpm_attestation": self.tpm_attestation,
            "phone_attestation": self.phone_attestation,
            "registered_at": self.registered_at,
            "last_reverified_at": self.last_reverified_at,
            "reverification_block": self.reverification_block,
            "bond_tx_hash": self.bond_tx_hash,
        })
    }

    pub fn from_dict(v: &serde_json::Value) -> Result<Self, String> {
        let silicon_fingerprint = if v["silicon_fingerprint"].is_object() {
            Some(GPUSiliconFingerprint::from_dict(&v["silicon_fingerprint"])?)
        } else {
            None
        };
        let network_position = if v["network_position"].is_object() {
            Some(NetworkPositionProof::from_dict(&v["network_position"])?)
        } else {
            None
        };
        Ok(Self {
            wallet_address: v["wallet_address"]
                .as_str()
                .ok_or("missing wallet_address")?
                .to_string(),
            entity_commitment: v["entity_commitment"]
                .as_str()
                .ok_or("missing entity_commitment")?
                .to_string(),
            trust_tier: v["trust_tier"].as_u64().ok_or("missing trust_tier")? as u8,
            hardware_attestation: v["hardware_attestation"].clone(),
            silicon_fingerprint,
            network_position,
            tpm_attestation: v["tpm_attestation"].as_str().map(|s| s.to_string()),
            phone_attestation: v["phone_attestation"].as_str().map(|s| s.to_string()),
            registered_at: v["registered_at"].as_f64().unwrap_or(0.0),
            last_reverified_at: v["last_reverified_at"].as_f64().unwrap_or(0.0),
            reverification_block: v["reverification_block"].as_u64().unwrap_or(0),
            bond_tx_hash: v["bond_tx_hash"].as_str().unwrap_or("").to_string(),
        })
    }
}

// ── Latency Correlation ──────────────────────────────────────────────────────

/// Pearson correlation of latency vectors to shared peers.
///
/// Returns 0.0 if fewer than MIN_LATENCY_PEERS shared peers.
pub fn check_latency_correlation(a: &NetworkPositionProof, b: &NetworkPositionProof) -> f64 {
    // Find shared peers
    let shared: Vec<&String> = a
        .latency_vector
        .keys()
        .filter(|k| b.latency_vector.contains_key(*k))
        .collect();

    if shared.len() < MIN_LATENCY_PEERS {
        return 0.0;
    }

    let xs: Vec<f64> = shared.iter().map(|k| a.latency_vector[*k]).collect();
    let ys: Vec<f64> = shared.iter().map(|k| b.latency_vector[*k]).collect();

    pearson_correlation(&xs, &ys)
}

/// Pearson correlation coefficient for two equal-length vectors.
fn pearson_correlation(xs: &[f64], ys: &[f64]) -> f64 {
    let n = xs.len() as f64;
    if n < 2.0 {
        return 0.0;
    }
    let mean_x: f64 = xs.iter().sum::<f64>() / n;
    let mean_y: f64 = ys.iter().sum::<f64>() / n;

    let mut cov = 0.0;
    let mut var_x = 0.0;
    let mut var_y = 0.0;
    for (x, y) in xs.iter().zip(ys.iter()) {
        let dx = x - mean_x;
        let dy = y - mean_y;
        cov += dx * dy;
        var_x += dx * dx;
        var_y += dy * dy;
    }

    let denom = (var_x * var_y).sqrt();
    if denom < 1e-12 {
        return 0.0;
    }
    cov / denom
}

// ── Hardware Fingerprint ─────────────────────────────────────────────────────

/// Collect hardware fingerprint from the local system.
///
/// Gathers platform, hostname hash, board serial hash, machine_id hash,
/// GPU UUID hash, and a composite hash.
pub fn collect_hardware_fingerprint() -> serde_json::Value {
    let platform = std::env::consts::ARCH.to_string();

    let hostname_hash = sha3_hash_str(
        &std::fs::read_to_string("/etc/hostname")
            .unwrap_or_default()
            .trim()
            .to_string(),
    );

    let board_serial_hash = sha3_hash_str(
        &std::fs::read_to_string("/sys/firmware/devicetree/base/serial-number")
            .unwrap_or_default()
            .trim()
            .to_string(),
    );

    let machine_id_hash = sha3_hash_str(
        &std::fs::read_to_string("/etc/machine-id")
            .unwrap_or_default()
            .trim()
            .to_string(),
    );

    // GPU UUID via nvidia-smi (best-effort)
    let gpu_uuid = std::process::Command::new("nvidia-smi")
        .args(["--query-gpu=uuid", "--format=csv,noheader"])
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .unwrap_or_default();
    let gpu_uuid_hash = sha3_hash_str(gpu_uuid.trim());

    // Composite
    let mut parts: Vec<(String, String)> = vec![
        ("platform".into(), platform.clone()),
        (
            "hostname_hash".into(),
            hostname_hash[..16.min(hostname_hash.len())].to_string(),
        ),
        (
            "board_serial_hash".into(),
            board_serial_hash[..32.min(board_serial_hash.len())].to_string(),
        ),
        (
            "machine_id_hash".into(),
            machine_id_hash[..32.min(machine_id_hash.len())].to_string(),
        ),
        (
            "gpu_uuid_hash".into(),
            gpu_uuid_hash[..32.min(gpu_uuid_hash.len())].to_string(),
        ),
    ];
    parts.sort_by(|a, b| a.0.cmp(&b.0));
    let composite_input: String = parts
        .iter()
        .map(|(k, v)| format!("{}={}", k, v))
        .collect::<Vec<_>>()
        .join("|");
    let composite_hash = sha3_hash_str(&composite_input);

    serde_json::json!({
        "platform": platform,
        "hostname_hash": &hostname_hash[..16.min(hostname_hash.len())],
        "board_serial_hash": &board_serial_hash[..32.min(board_serial_hash.len())],
        "machine_id_hash": &machine_id_hash[..32.min(machine_id_hash.len())],
        "gpu_uuid_hash": &gpu_uuid_hash[..32.min(gpu_uuid_hash.len())],
        "composite_hash": composite_hash,
    })
}

// ── Device Registry ──────────────────────────────────────────────────────────

/// Registry of verified physical devices.
///
/// Validates uniqueness of GPU silicon fingerprints and network positions
/// to prevent Sybil attacks (one entity running multiple virtual nodes
/// on the same physical hardware).
pub struct DeviceRegistry {
    /// wallet_address → registration.
    pub devices: HashMap<String, DeviceRegistration>,
    /// entity_commitment → list of wallet addresses.
    pub entity_nodes: HashMap<String, Vec<String>>,
    /// wallet → fingerprint (for duplicate detection).
    fingerprints: HashMap<String, GPUSiliconFingerprint>,
    /// wallet → position (for colocation detection).
    positions: HashMap<String, NetworkPositionProof>,
}

impl DeviceRegistry {
    pub fn new() -> Self {
        Self {
            devices: HashMap::new(),
            entity_nodes: HashMap::new(),
            fingerprints: HashMap::new(),
            positions: HashMap::new(),
        }
    }

    /// Register a new device.  5-step validation matches Python exactly.
    ///
    /// Returns `Ok(())` on success or `Err(reason)` on rejection.
    pub fn register_device(&mut self, reg: &DeviceRegistration) -> Result<(), String> {
        // 1. Already registered?
        if self.devices.contains_key(&reg.wallet_address) {
            return Err("Device already registered".into());
        }

        // 2. Trust tier valid?
        let max = tier_max_nodes(reg.trust_tier);
        if max == 0 {
            return Err(format!("Invalid trust tier: {}", reg.trust_tier));
        }
        if reg.trust_tier >= TRUST_TIER_3 && reg.tpm_attestation.is_none() {
            return Err("Tier 3 requires TPM attestation".into());
        }
        if reg.trust_tier >= TRUST_TIER_2
            && reg.phone_attestation.is_none()
            && reg.tpm_attestation.is_none()
        {
            return Err("Tier 2 requires phone or TPM attestation".into());
        }

        // 3. Node-per-entity limit
        let current_count = self
            .entity_nodes
            .get(&reg.entity_commitment)
            .map(|v| v.len())
            .unwrap_or(0);
        if current_count >= max {
            return Err(format!(
                "Entity has {} nodes, tier {} allows max {}",
                current_count, reg.trust_tier, max
            ));
        }

        // 4. Silicon fingerprint uniqueness
        if let Some(fp) = &reg.silicon_fingerprint {
            let threshold = 1.0 - SILICON_FINGERPRINT_TOLERANCE; // 0.995
            for (existing_wallet, existing_fp) in &self.fingerprints {
                let sim = fp.similarity(existing_fp);
                if sim > threshold {
                    // Same entity splitting GPU or different entity sharing GPU
                    return Err(format!(
                        "Silicon fingerprint too similar to device {} (similarity: {:.4})",
                        existing_wallet, sim
                    ));
                }
            }
        }

        // 5. Network position colocation check (against same entity's other nodes)
        if let Some(pos) = &reg.network_position {
            if let Some(sibling_wallets) = self.entity_nodes.get(&reg.entity_commitment) {
                for sibling_addr in sibling_wallets {
                    if let Some(sibling_pos) = self.positions.get(sibling_addr) {
                        let corr = check_latency_correlation(pos, sibling_pos);
                        if corr > LATENCY_CORRELATION_THRESHOLD {
                            return Err(format!(
                                "Network position too correlated with {} (r={:.4})",
                                sibling_addr, corr
                            ));
                        }
                    }
                }
            }
        }

        // Accept
        self.devices.insert(reg.wallet_address.clone(), reg.clone());
        self.entity_nodes
            .entry(reg.entity_commitment.clone())
            .or_default()
            .push(reg.wallet_address.clone());
        if let Some(fp) = &reg.silicon_fingerprint {
            self.fingerprints
                .insert(reg.wallet_address.clone(), fp.clone());
        }
        if let Some(pos) = &reg.network_position {
            self.positions
                .insert(reg.wallet_address.clone(), pos.clone());
        }

        Ok(())
    }

    /// Check if a wallet has a verified device.
    pub fn is_device_verified(&self, wallet_address: &str) -> bool {
        self.devices.contains_key(wallet_address)
    }

    /// Check if a device needs re-verification.
    pub fn needs_reverification(&self, wallet_address: &str, current_block: u64) -> bool {
        match self.devices.get(wallet_address) {
            Some(dev) => current_block - dev.reverification_block >= REVERIFICATION_INTERVAL_BLOCKS,
            None => false,
        }
    }

    /// Update re-verification with a new fingerprint.
    ///
    /// Compares new fingerprint against stored; rejects if device changed
    /// (similarity below threshold means hardware swap → must re-register).
    pub fn update_reverification(
        &mut self,
        wallet_address: &str,
        new_fingerprint: &GPUSiliconFingerprint,
        current_block: u64,
    ) -> Result<(), String> {
        let threshold = 1.0 - SILICON_FINGERPRINT_TOLERANCE; // 0.995
        let old_fp = self
            .fingerprints
            .get(wallet_address)
            .ok_or("No fingerprint on record")?;
        let sim = new_fingerprint.similarity(old_fp);
        if sim < threshold {
            return Err(format!(
                "Device appears to have changed (similarity: {:.4}, need: {:.4}). Re-register.",
                sim, threshold
            ));
        }

        // Update
        if let Some(dev) = self.devices.get_mut(wallet_address) {
            dev.last_reverified_at = now_f64();
            dev.reverification_block = current_block;
        }
        self.fingerprints
            .insert(wallet_address.to_string(), new_fingerprint.clone());
        Ok(())
    }

    /// Remove a device registration.
    pub fn remove_device(&mut self, wallet_address: &str) -> bool {
        if let Some(dev) = self.devices.remove(wallet_address) {
            // Remove from entity_nodes
            if let Some(nodes) = self.entity_nodes.get_mut(&dev.entity_commitment) {
                nodes.retain(|w| w != wallet_address);
                if nodes.is_empty() {
                    self.entity_nodes.remove(&dev.entity_commitment);
                }
            }
            self.fingerprints.remove(wallet_address);
            self.positions.remove(wallet_address);
            true
        } else {
            false
        }
    }

    /// Get number of nodes for an entity.
    pub fn get_entity_node_count(&self, entity_commitment: &str) -> usize {
        self.entity_nodes
            .get(entity_commitment)
            .map(|v| v.len())
            .unwrap_or(0)
    }

    /// Get trust tier for a wallet (0 if not registered).
    pub fn get_trust_tier(&self, wallet_address: &str) -> u8 {
        self.devices
            .get(wallet_address)
            .map(|d| d.trust_tier)
            .unwrap_or(0)
    }

    /// Registry statistics.
    pub fn stats(&self) -> DeviceRegistryStats {
        let mut tier_counts = [0usize; 3];
        for dev in self.devices.values() {
            match dev.trust_tier {
                TRUST_TIER_1 => tier_counts[0] += 1,
                TRUST_TIER_2 => tier_counts[1] += 1,
                TRUST_TIER_3 => tier_counts[2] += 1,
                _ => {}
            }
        }
        DeviceRegistryStats {
            total_devices: self.devices.len(),
            tier_1: tier_counts[0],
            tier_2: tier_counts[1],
            tier_3: tier_counts[2],
            unique_entities: self.entity_nodes.len(),
        }
    }

    /// Serialize to JSON.
    pub fn to_dict(&self) -> serde_json::Value {
        let devices: serde_json::Map<String, serde_json::Value> = self
            .devices
            .iter()
            .map(|(k, v)| (k.clone(), v.to_dict()))
            .collect();
        let entity_nodes: serde_json::Map<String, serde_json::Value> = self
            .entity_nodes
            .iter()
            .map(|(k, v)| {
                (
                    k.clone(),
                    serde_json::Value::Array(
                        v.iter()
                            .map(|s| serde_json::Value::String(s.clone()))
                            .collect(),
                    ),
                )
            })
            .collect();
        serde_json::json!({
            "devices": devices,
            "entity_nodes": entity_nodes,
        })
    }

    /// Deserialize from JSON.
    pub fn from_dict(v: &serde_json::Value) -> Self {
        let mut reg = Self::new();

        if let Some(devs) = v["devices"].as_object() {
            for (wallet, dv) in devs {
                if let Ok(dev) = DeviceRegistration::from_dict(dv) {
                    // Rebuild fingerprint/position caches
                    if let Some(fp) = &dev.silicon_fingerprint {
                        reg.fingerprints.insert(wallet.clone(), fp.clone());
                    }
                    if let Some(pos) = &dev.network_position {
                        reg.positions.insert(wallet.clone(), pos.clone());
                    }
                    reg.devices.insert(wallet.clone(), dev);
                }
            }
        }

        if let Some(nodes) = v["entity_nodes"].as_object() {
            for (entity, wallets) in nodes {
                if let Some(arr) = wallets.as_array() {
                    let addrs: Vec<String> = arr
                        .iter()
                        .filter_map(|s| s.as_str().map(|s| s.to_string()))
                        .collect();
                    reg.entity_nodes.insert(entity.clone(), addrs);
                }
            }
        }

        reg
    }
}

impl Default for DeviceRegistry {
    fn default() -> Self {
        Self::new()
    }
}

/// Device registry statistics.
#[derive(Debug, Clone)]
pub struct DeviceRegistryStats {
    pub total_devices: usize,
    pub tier_1: usize,
    pub tier_2: usize,
    pub tier_3: usize,
    pub unique_entities: usize,
}

// ── Mining Gate ──────────────────────────────────────────────────────────────

/// Check if a wallet address passes the entity + device verification gate
/// for mining eligibility.
///
/// The gate is **soft**: enforcement only activates when the network has
/// ≥ 2 registered entities (bootstrap period allows free mining).
///
/// Matches Python qnode2.py `_is_entity_verified()` exactly.
pub fn is_entity_verified(
    wallet_address: &str,
    entity_registry: Option<&crate::entity::EntityRegistry>,
    device_registry: Option<&DeviceRegistry>,
) -> bool {
    // If no entity registry → bypass (pre-verification era)
    let entity_reg = match entity_registry {
        Some(r) => r,
        None => return true,
    };

    // Soft gate: only enforce when ≥ 2 entities registered
    if entity_reg.records.len() < 2 {
        return true;
    }

    // Check wallet is tagged to an entity
    if !entity_reg.is_wallet_tagged(wallet_address) {
        return false;
    }

    // Device verification (also soft: only when ≥ 2 devices)
    if let Some(dev_reg) = device_registry {
        if dev_reg.devices.len() >= 2 && !dev_reg.is_device_verified(wallet_address) {
            return false;
        }
    }

    true
}

// ── Helpers ──────────────────────────────────────────────────────────────────

fn sha3_hash_str(input: &str) -> String {
    let mut hasher = Sha3_256::new();
    hasher.update(input.as_bytes());
    hex::encode(hasher.finalize())
}

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

    fn make_fingerprint(mean: f64, percentiles: [f64; 5]) -> GPUSiliconFingerprint {
        GPUSiliconFingerprint {
            mean_ns: mean,
            stddev_ns: mean * 0.05,
            coeff_variation: 0.05,
            percentiles,
            timing_hash: sha3_hash_str(&format!("{}", mean)),
            gpu_model: "test-gpu".into(),
            iterations: 10000,
            measured_at: 0.0,
        }
    }

    fn make_position(target: &str, peers: &[(&str, f64)]) -> NetworkPositionProof {
        let vouches: Vec<LatencyVouch> = peers
            .iter()
            .map(|(p, rtt)| LatencyVouch {
                peer_address: p.to_string(),
                target_address: target.to_string(),
                rtt_ms: *rtt,
                nonce: "test".into(),
                timestamp: 0.0,
                peer_signature: "sig".into(),
            })
            .collect();
        let latency_vector: HashMap<String, f64> =
            peers.iter().map(|(p, rtt)| (p.to_string(), *rtt)).collect();
        NetworkPositionProof {
            target_address: target.to_string(),
            vouches,
            latency_vector,
            created_at: 0.0,
        }
    }

    fn make_registration(
        wallet: &str,
        entity: &str,
        tier: u8,
        fp: Option<GPUSiliconFingerprint>,
        pos: Option<NetworkPositionProof>,
    ) -> DeviceRegistration {
        DeviceRegistration {
            wallet_address: wallet.to_string(),
            entity_commitment: entity.to_string(),
            trust_tier: tier,
            hardware_attestation: serde_json::json!({}),
            silicon_fingerprint: fp,
            network_position: pos,
            tpm_attestation: if tier >= 3 {
                Some("tpm_hash".into())
            } else {
                None
            },
            phone_attestation: if tier == 2 {
                Some("phone_hash".into())
            } else {
                None
            },
            registered_at: 0.0,
            last_reverified_at: 0.0,
            reverification_block: 0,
            bond_tx_hash: "bond_abc".into(),
        }
    }

    // ── Fingerprint Similarity ──────────────────────────────────

    #[test]
    fn test_fingerprint_identical() {
        let fp = make_fingerprint(100.0, [80.0, 90.0, 100.0, 110.0, 120.0]);
        let sim = fp.similarity(&fp);
        assert!((sim - 1.0).abs() < 0.001);
    }

    #[test]
    fn test_fingerprint_different() {
        let fp1 = make_fingerprint(100.0, [80.0, 90.0, 100.0, 110.0, 120.0]);
        let fp2 = make_fingerprint(500.0, [400.0, 450.0, 500.0, 550.0, 600.0]);
        let sim = fp1.similarity(&fp2);
        assert!(sim < 0.9); // Very different hardware
    }

    #[test]
    fn test_fingerprint_roundtrip() {
        let fp = make_fingerprint(123.4, [100.0, 110.0, 123.0, 135.0, 150.0]);
        let dict = fp.to_dict();
        let fp2 = GPUSiliconFingerprint::from_dict(&dict).unwrap();
        assert!((fp.mean_ns - fp2.mean_ns).abs() < 1e-6);
        assert_eq!(fp.percentiles, fp2.percentiles);
    }

    // ── Network Position ────────────────────────────────────────

    #[test]
    fn test_position_valid() {
        let pos = make_position("wallet_a", &[("p1", 10.0), ("p2", 20.0), ("p3", 30.0)]);
        assert!(pos.is_valid());
    }

    #[test]
    fn test_position_too_few_peers() {
        let pos = make_position("wallet_a", &[("p1", 10.0), ("p2", 20.0)]);
        assert!(!pos.is_valid());
    }

    #[test]
    fn test_position_invalid_rtt() {
        let pos = make_position("wallet_a", &[("p1", 10.0), ("p2", 20.0), ("p3", -1.0)]);
        assert!(!pos.is_valid());
    }

    // ── Latency Correlation ─────────────────────────────────────

    #[test]
    fn test_correlation_identical() {
        let a = make_position("a", &[("p1", 10.0), ("p2", 20.0), ("p3", 30.0)]);
        let b = make_position("b", &[("p1", 10.0), ("p2", 20.0), ("p3", 30.0)]);
        let corr = check_latency_correlation(&a, &b);
        assert!((corr - 1.0).abs() < 0.001); // Perfect correlation
    }

    #[test]
    fn test_correlation_different() {
        let a = make_position("a", &[("p1", 10.0), ("p2", 20.0), ("p3", 30.0)]);
        let b = make_position("b", &[("p1", 50.0), ("p2", 5.0), ("p3", 100.0)]);
        let corr = check_latency_correlation(&a, &b);
        assert!(corr < 0.95); // Not colocated
    }

    #[test]
    fn test_correlation_too_few_shared() {
        let a = make_position("a", &[("p1", 10.0), ("p2", 20.0), ("p3", 30.0)]);
        let b = make_position("b", &[("p4", 50.0), ("p5", 60.0), ("p6", 70.0)]);
        let corr = check_latency_correlation(&a, &b);
        assert!((corr - 0.0).abs() < 0.001); // 0 shared peers
    }

    // ── Device Registry ─────────────────────────────────────────

    #[test]
    fn test_register_device_basic() {
        let mut reg = DeviceRegistry::new();
        let dev = make_registration("wallet_a", "entity_1", TRUST_TIER_1, None, None);
        assert!(reg.register_device(&dev).is_ok());
        assert!(reg.is_device_verified("wallet_a"));
        assert_eq!(reg.get_trust_tier("wallet_a"), TRUST_TIER_1);
    }

    #[test]
    fn test_register_device_duplicate_rejected() {
        let mut reg = DeviceRegistry::new();
        let dev = make_registration("wallet_a", "entity_1", TRUST_TIER_1, None, None);
        reg.register_device(&dev).unwrap();
        assert!(reg.register_device(&dev).is_err());
    }

    #[test]
    fn test_register_device_invalid_tier() {
        let mut reg = DeviceRegistry::new();
        let dev = make_registration("wallet_a", "entity_1", 99, None, None);
        assert!(reg.register_device(&dev).is_err());
    }

    #[test]
    fn test_register_device_tier2_requires_attestation() {
        let mut reg = DeviceRegistry::new();
        let mut dev = make_registration("wallet_a", "entity_1", TRUST_TIER_2, None, None);
        dev.phone_attestation = None; // Remove attestation
        dev.tpm_attestation = None;
        assert!(reg.register_device(&dev).is_err());
    }

    #[test]
    fn test_register_device_tier3_requires_tpm() {
        let mut reg = DeviceRegistry::new();
        let mut dev = make_registration("wallet_a", "entity_1", TRUST_TIER_3, None, None);
        dev.tpm_attestation = None; // Remove TPM
        assert!(reg.register_device(&dev).is_err());
    }

    #[test]
    fn test_register_device_node_limit_tier1() {
        let mut reg = DeviceRegistry::new();
        // Tier 1 allows max 1 node per entity
        let dev1 = make_registration("wallet_a", "entity_1", TRUST_TIER_1, None, None);
        reg.register_device(&dev1).unwrap();

        let dev2 = make_registration("wallet_b", "entity_1", TRUST_TIER_1, None, None);
        assert!(reg.register_device(&dev2).is_err());
    }

    #[test]
    fn test_register_device_fingerprint_uniqueness() {
        let mut reg = DeviceRegistry::new();
        let fp1 = make_fingerprint(100.0, [80.0, 90.0, 100.0, 110.0, 120.0]);
        let fp2 = make_fingerprint(100.0, [80.0, 90.0, 100.0, 110.0, 120.0]); // Identical

        // Different entities: tier 3 to allow >1 node
        let dev1 = make_registration("wallet_a", "entity_1", TRUST_TIER_3, Some(fp1), None);
        reg.register_device(&dev1).unwrap();

        let dev2 = make_registration("wallet_b", "entity_2", TRUST_TIER_3, Some(fp2), None);
        assert!(reg.register_device(&dev2).is_err()); // Same GPU rejected
    }

    #[test]
    fn test_register_device_colocation_rejected() {
        let mut reg = DeviceRegistry::new();
        // Same entity, tier 3 (allows up to 5 nodes)
        let pos1 = make_position("wallet_a", &[("p1", 10.0), ("p2", 20.0), ("p3", 30.0)]);
        let pos2 = make_position(
            "wallet_b",
            &[
                ("p1", 10.0),
                ("p2", 20.0),
                ("p3", 30.0), // Identical = colocated
            ],
        );

        let fp1 = make_fingerprint(100.0, [80.0, 90.0, 100.0, 110.0, 120.0]);
        let fp2 = make_fingerprint(500.0, [400.0, 450.0, 500.0, 550.0, 600.0]); // Different GPU

        let dev1 = make_registration("wallet_a", "entity_1", TRUST_TIER_3, Some(fp1), Some(pos1));
        reg.register_device(&dev1).unwrap();

        let dev2 = make_registration("wallet_b", "entity_1", TRUST_TIER_3, Some(fp2), Some(pos2));
        assert!(reg.register_device(&dev2).is_err()); // Colocated
    }

    #[test]
    fn test_remove_device() {
        let mut reg = DeviceRegistry::new();
        let dev = make_registration("wallet_a", "entity_1", TRUST_TIER_1, None, None);
        reg.register_device(&dev).unwrap();
        assert!(reg.is_device_verified("wallet_a"));

        assert!(reg.remove_device("wallet_a"));
        assert!(!reg.is_device_verified("wallet_a"));
        assert_eq!(reg.get_entity_node_count("entity_1"), 0);
    }

    #[test]
    fn test_remove_nonexistent() {
        let mut reg = DeviceRegistry::new();
        assert!(!reg.remove_device("ghost"));
    }

    #[test]
    fn test_needs_reverification() {
        let mut reg = DeviceRegistry::new();
        let dev = make_registration("wallet_a", "entity_1", TRUST_TIER_1, None, None);
        reg.register_device(&dev).unwrap();

        assert!(!reg.needs_reverification("wallet_a", 500)); // Block 500, registered at 0
        assert!(reg.needs_reverification("wallet_a", 1000)); // Block 1000 = interval
        assert!(reg.needs_reverification("wallet_a", 2000)); // Past due
    }

    #[test]
    fn test_update_reverification_success() {
        let mut reg = DeviceRegistry::new();
        let fp = make_fingerprint(100.0, [80.0, 90.0, 100.0, 110.0, 120.0]);
        let dev = make_registration("wallet_a", "entity_1", TRUST_TIER_1, Some(fp.clone()), None);
        reg.register_device(&dev).unwrap();

        // Re-verify with very similar fingerprint
        let fp2 = make_fingerprint(100.1, [80.0, 90.0, 100.0, 110.0, 120.0]);
        assert!(reg.update_reverification("wallet_a", &fp2, 1000).is_ok());
    }

    #[test]
    fn test_update_reverification_device_changed() {
        let mut reg = DeviceRegistry::new();
        let fp = make_fingerprint(100.0, [80.0, 90.0, 100.0, 110.0, 120.0]);
        let dev = make_registration("wallet_a", "entity_1", TRUST_TIER_1, Some(fp), None);
        reg.register_device(&dev).unwrap();

        // Completely different fingerprint = device swap
        let fp_new = make_fingerprint(999.0, [800.0, 900.0, 999.0, 1100.0, 1200.0]);
        assert!(
            reg.update_reverification("wallet_a", &fp_new, 1000)
                .is_err()
        );
    }

    #[test]
    fn test_registry_stats() {
        let mut reg = DeviceRegistry::new();
        let d1 = make_registration("w1", "e1", TRUST_TIER_1, None, None);
        let d2 = make_registration("w2", "e2", TRUST_TIER_2, None, None);
        let d3 = make_registration("w3", "e3", TRUST_TIER_3, None, None);
        reg.register_device(&d1).unwrap();
        reg.register_device(&d2).unwrap();
        reg.register_device(&d3).unwrap();

        let stats = reg.stats();
        assert_eq!(stats.total_devices, 3);
        assert_eq!(stats.tier_1, 1);
        assert_eq!(stats.tier_2, 1);
        assert_eq!(stats.tier_3, 1);
        assert_eq!(stats.unique_entities, 3);
    }

    // ── Device Registration Roundtrip ───────────────────────────

    #[test]
    fn test_device_registration_roundtrip() {
        let fp = make_fingerprint(200.0, [150.0, 180.0, 200.0, 220.0, 250.0]);
        let pos = make_position("wallet_x", &[("p1", 10.0), ("p2", 20.0), ("p3", 30.0)]);
        let dev = make_registration("wallet_x", "entity_x", TRUST_TIER_3, Some(fp), Some(pos));
        let dict = dev.to_dict();
        let dev2 = DeviceRegistration::from_dict(&dict).unwrap();
        assert_eq!(dev.wallet_address, dev2.wallet_address);
        assert_eq!(dev.trust_tier, dev2.trust_tier);
        assert!(dev2.silicon_fingerprint.is_some());
        assert!(dev2.network_position.is_some());
    }

    #[test]
    fn test_registry_roundtrip() {
        let mut reg = DeviceRegistry::new();
        let d = make_registration("w1", "e1", TRUST_TIER_1, None, None);
        reg.register_device(&d).unwrap();

        let dict = reg.to_dict();
        let reg2 = DeviceRegistry::from_dict(&dict);
        assert_eq!(reg2.devices.len(), 1);
        assert!(reg2.is_device_verified("w1"));
        assert_eq!(reg2.entity_nodes.len(), 1);
    }

    // ── Mining Gate ─────────────────────────────────────────────

    #[test]
    fn test_mining_gate_no_registry() {
        // No registry = free mining
        assert!(is_entity_verified("anyone", None, None));
    }

    #[test]
    fn test_mining_gate_bootstrap_period() {
        // < 2 entities = free mining (bootstrap)
        let mut entity_reg = crate::entity::EntityRegistry::new();
        let rec = crate::entity::EntityRecord {
            commitment: "c1".into(),
            entity_type: "human".into(),
            epoch: 1,
            epoch_nullifier: "n1".into(),
            credential_signature: "0x1".into(),
            registered_at: 0.0,
            hardware_attestation_hash: "".into(),
        };
        entity_reg.register(&rec).unwrap();
        assert!(is_entity_verified("anyone", Some(&entity_reg), None));
    }

    #[test]
    fn test_mining_gate_enforced() {
        let mut entity_reg = crate::entity::EntityRegistry::new();
        // Register 2 entities to activate gate
        let rec1 = crate::entity::EntityRecord {
            commitment: "c1".into(),
            entity_type: "human".into(),
            epoch: 1,
            epoch_nullifier: "n1".into(),
            credential_signature: "0x1".into(),
            registered_at: 0.0,
            hardware_attestation_hash: "".into(),
        };
        let rec2 = crate::entity::EntityRecord {
            commitment: "c2".into(),
            entity_type: "machine".into(),
            epoch: 1,
            epoch_nullifier: "n2".into(),
            credential_signature: "0x2".into(),
            registered_at: 0.0,
            hardware_attestation_hash: "".into(),
        };
        entity_reg.register(&rec1).unwrap();
        entity_reg.register(&rec2).unwrap();

        // Untagged wallet should fail
        assert!(!is_entity_verified(
            "untagged_wallet",
            Some(&entity_reg),
            None
        ));

        // Tagged wallet should pass
        entity_reg.tag_wallet("tagged_wallet", "c1").unwrap();
        assert!(is_entity_verified("tagged_wallet", Some(&entity_reg), None));
    }

    #[test]
    fn test_mining_gate_device_check() {
        let mut entity_reg = crate::entity::EntityRegistry::new();
        let rec1 = crate::entity::EntityRecord {
            commitment: "c1".into(),
            entity_type: "human".into(),
            epoch: 1,
            epoch_nullifier: "n1".into(),
            credential_signature: "0x1".into(),
            registered_at: 0.0,
            hardware_attestation_hash: "".into(),
        };
        let rec2 = crate::entity::EntityRecord {
            commitment: "c2".into(),
            entity_type: "machine".into(),
            epoch: 1,
            epoch_nullifier: "n2".into(),
            credential_signature: "0x2".into(),
            registered_at: 0.0,
            hardware_attestation_hash: "".into(),
        };
        entity_reg.register(&rec1).unwrap();
        entity_reg.register(&rec2).unwrap();
        entity_reg.tag_wallet("wallet_a", "c1").unwrap();
        entity_reg.tag_wallet("wallet_b", "c2").unwrap();

        let mut dev_reg = DeviceRegistry::new();
        let d1 = make_registration("wallet_a", "c1", TRUST_TIER_1, None, None);
        let d2 = make_registration("wallet_b", "c2", TRUST_TIER_1, None, None);
        dev_reg.register_device(&d1).unwrap();
        dev_reg.register_device(&d2).unwrap();

        // Both tagged + device verified → pass
        assert!(is_entity_verified(
            "wallet_a",
            Some(&entity_reg),
            Some(&dev_reg)
        ));
        assert!(is_entity_verified(
            "wallet_b",
            Some(&entity_reg),
            Some(&dev_reg)
        ));
    }

    #[test]
    fn test_mining_gate_device_missing() {
        let mut entity_reg = crate::entity::EntityRegistry::new();
        let rec1 = crate::entity::EntityRecord {
            commitment: "c1".into(),
            entity_type: "human".into(),
            epoch: 1,
            epoch_nullifier: "n1".into(),
            credential_signature: "0x1".into(),
            registered_at: 0.0,
            hardware_attestation_hash: "".into(),
        };
        let rec2 = crate::entity::EntityRecord {
            commitment: "c2".into(),
            entity_type: "machine".into(),
            epoch: 1,
            epoch_nullifier: "n2".into(),
            credential_signature: "0x2".into(),
            registered_at: 0.0,
            hardware_attestation_hash: "".into(),
        };
        entity_reg.register(&rec1).unwrap();
        entity_reg.register(&rec2).unwrap();
        entity_reg.tag_wallet("wallet_a", "c1").unwrap();

        let mut dev_reg = DeviceRegistry::new();
        // Register 2 devices (activates device gate) but NOT for wallet_a
        let d1 = make_registration("wallet_x", "c1", TRUST_TIER_1, None, None);
        // Need a different entity for second device since c1 tier 1 only allows 1
        let d2 = make_registration("wallet_y", "c2", TRUST_TIER_1, None, None);
        dev_reg.register_device(&d1).unwrap();
        dev_reg.register_device(&d2).unwrap();

        // wallet_a is tagged but has no device → should fail
        assert!(!is_entity_verified(
            "wallet_a",
            Some(&entity_reg),
            Some(&dev_reg)
        ));
    }

    // ── Latency Vouch Roundtrip ─────────────────────────────────

    #[test]
    fn test_latency_vouch_roundtrip() {
        let vouch = LatencyVouch {
            peer_address: "peer1".into(),
            target_address: "me".into(),
            rtt_ms: 15.5,
            nonce: "abc123".into(),
            timestamp: 1000.0,
            peer_signature: "sig_hex".into(),
        };
        let dict = vouch.to_dict();
        let v2 = LatencyVouch::from_dict(&dict).unwrap();
        assert_eq!(v2.peer_address, "peer1");
        assert!((v2.rtt_ms - 15.5).abs() < 1e-6);
    }

    #[test]
    fn test_position_roundtrip() {
        let pos = make_position("target", &[("p1", 10.0), ("p2", 20.0), ("p3", 30.0)]);
        let dict = pos.to_dict();
        let pos2 = NetworkPositionProof::from_dict(&dict).unwrap();
        assert_eq!(pos2.target_address, "target");
        assert_eq!(pos2.vouches.len(), 3);
        assert_eq!(pos2.latency_vector.len(), 3);
    }
}
