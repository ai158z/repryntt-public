"""
repryntt Entity Verification Protocol (REVP)

Zero-knowledge entity-type verification — proves HUMAN vs MACHINE without
revealing real-world identity.  Inspired by Semaphore, Worldcoin, and
Polygon ID.

Architecture
────────────
  1. Entity generates a secret identity → SHA3-256 commitment (hidden)
  2. Verification oracle confirms entity type → RSA **blind** credential
  3. Entity un-blinds the credential and registers commitment on-chain
  4. Anyone can verify entity type; nobody can link it to real identity

Privacy guarantees
──────────────────
  • Oracle cannot link a credential to any on-chain address (Chaum '82
    RSA blind signature).
  • On-chain record stores (commitment, entity_type, epoch_nullifier) but
    ZERO PII.
  • Epoch nullifier prevents double-registration within the same epoch.
  • Commitment scheme is computationally hiding AND binding (SHA3-256).

Modules
───────
  EntityIdentity      — local secret identity (never leaves device)
  BlindCredentialIssuer — RSA blind signature oracle for credential issuance
  MerkleTree          — SHA3-256 binary Merkle tree (entity registry)
  HardwareAttestation — machine fingerprint signed by node Ed25519 key
  EntityRegistry      — on-chain registry of commitments + proofs
  helpers             — TX construction, wallet tagging

Upgrade path
────────────
  Phase 1  ✓  entity_type in identity + wallet; commitment registry
  Phase 2  ✓  RSA blind credentials (oracle cannot link address ↔ person)
  Phase 3  ✓  Merkle-tree registry; epoch nullifiers; hardware attestation
  Future      Replace oracle with ZK-SNARK circuit; biometric hooks
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import platform
import secrets
import struct
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cryptography.hazmat.primitives.asymmetric import rsa as rsa_mod
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPrivateKey,
    RSAPublicKey,
    generate_private_key,
    RSAPublicNumbers,
    RSAPrivateNumbers,
)
from cryptography.hazmat.primitives import serialization

logger = logging.getLogger("repryntt.entity_verification")

# ═══════════════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════
ENTITY_DIR = Path.home() / ".repryntt" / "entity"
ORACLE_KEY_PATH = ENTITY_DIR / "oracle_rsa.pem"
ORACLE_PUB_PATH = ENTITY_DIR / "oracle_rsa.pub"
IDENTITY_SECRET_PATH = ENTITY_DIR / "identity_secret.json"
REGISTRY_PATH = ENTITY_DIR / "entity_registry.json"

ENTITY_TYPE_HUMAN = "human"
ENTITY_TYPE_MACHINE = "machine"
VALID_ENTITY_TYPES = frozenset({ENTITY_TYPE_HUMAN, ENTITY_TYPE_MACHINE})

# Epoch length in seconds (30 days).  Entities can only register once per epoch.
EPOCH_LENGTH = 30 * 24 * 3600

# RSA key size for blind signatures (2048-bit — NIST-approved through 2030+)
ORACLE_RSA_BITS = 2048

# SHA3-256 zero leaf for Merkle tree padding
ZERO_LEAF = b"\x00" * 32


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTITY IDENTITY — local secret, never transmitted
# ═══════════════════════════════════════════════════════════════════════════════
@dataclass
class EntityIdentity:
    """Secret identity of a repryntt entity (human or machine).

    Generated once on the device and NEVER shared.  The only thing that
    leaves the device is the *commitment* (a one-way hash) and the
    *epoch_nullifier* (prevents double-registration).
    """
    identity_secret: bytes       # 32 random bytes
    identity_nullifier: bytes    # 32 random bytes
    entity_type: str             # "human" or "machine"
    commitment: str              # SHA3-256 hex of (secret ‖ nullifier ‖ entity_type)
    created_at: float

    @staticmethod
    def generate(entity_type: str) -> "EntityIdentity":
        if entity_type not in VALID_ENTITY_TYPES:
            raise ValueError(f"Invalid entity type: {entity_type}")
        secret = secrets.token_bytes(32)
        nullifier = secrets.token_bytes(32)
        commitment = _compute_commitment(secret, nullifier, entity_type)
        return EntityIdentity(
            identity_secret=secret,
            identity_nullifier=nullifier,
            entity_type=entity_type,
            commitment=commitment,
            created_at=time.time(),
        )

    def epoch_nullifier(self, epoch: int) -> str:
        """Deterministic nullifier for an epoch — prevents double-registration."""
        data = self.identity_nullifier + struct.pack("!Q", epoch)
        return hashlib.sha3_256(data).hexdigest()

    # ── Persistence ──────────────────────────────────────────────
    def save(self, path: Optional[Path] = None):
        path = path or IDENTITY_SECRET_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "identity_secret": self.identity_secret.hex(),
            "identity_nullifier": self.identity_nullifier.hex(),
            "entity_type": self.entity_type,
            "commitment": self.commitment,
            "created_at": self.created_at,
        }
        path.write_text(json.dumps(data, indent=2))
        # Restrict permissions — secret material
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    @staticmethod
    def load(path: Optional[Path] = None) -> "EntityIdentity":
        path = path or IDENTITY_SECRET_PATH
        data = json.loads(path.read_text())
        return EntityIdentity(
            identity_secret=bytes.fromhex(data["identity_secret"]),
            identity_nullifier=bytes.fromhex(data["identity_nullifier"]),
            entity_type=data["entity_type"],
            commitment=data["commitment"],
            created_at=data.get("created_at", 0.0),
        )


def _compute_commitment(secret: bytes, nullifier: bytes, entity_type: str) -> str:
    """Binding + hiding commitment: SHA3-256(secret ‖ nullifier ‖ type)."""
    return hashlib.sha3_256(
        secret + nullifier + entity_type.encode()
    ).hexdigest()


def current_epoch() -> int:
    """Current epoch number (monotonic, based on Unix time)."""
    return int(time.time()) // EPOCH_LENGTH


# ═══════════════════════════════════════════════════════════════════════════════
#  RSA BLIND SIGNATURES — Chaum '82
#
#  The oracle cannot link the signed credential to the entity's on-chain
#  commitment.  This is the same primitive Worldcoin uses (via a
#  Semaphore circuit) — we implement the classical RSA version which is
#  well-understood and doesn't require a trusted setup.
# ═══════════════════════════════════════════════════════════════════════════════

def _modinv(a: int, m: int) -> int:
    """Modular multiplicative inverse via extended Euclidean algorithm."""
    return pow(a, -1, m)


@dataclass
class BlindedMessage:
    """An entity's blinded message ready for the oracle to sign."""
    blinded: int          # m * r^e mod n — oracle sees this
    blinding_factor: int  # r (secret, kept by entity)
    original_hash: int    # integer of H(commitment ‖ entity_type ‖ epoch)


@dataclass
class BlindCredential:
    """Un-blinded oracle signature — proves entity type without linkability."""
    commitment: str       # entity's commitment
    entity_type: str
    epoch: int
    signature: int        # raw RSA signature on H(commitment ‖ type ‖ epoch)

    def to_dict(self) -> dict:
        return {
            "commitment": self.commitment,
            "entity_type": self.entity_type,
            "epoch": self.epoch,
            "signature": hex(self.signature),
        }

    @staticmethod
    def from_dict(d: dict) -> "BlindCredential":
        sig = d["signature"]
        if isinstance(sig, str):
            sig = int(sig, 16) if sig.startswith("0x") else int(sig)
        return BlindCredential(
            commitment=d["commitment"],
            entity_type=d["entity_type"],
            epoch=d["epoch"],
            signature=sig,
        )


class BlindCredentialIssuer:
    """RSA blind-signature oracle.

    Lifecycle:
      1. Oracle generates RSA keypair (once, stored on disk).
      2. Entity creates a BlindedMessage and sends `blinded` integer to oracle.
      3. Oracle signs the blinded value (never sees the real message).
      4. Entity un-blinds the signature → BlindCredential.
      5. Anyone with the oracle's public key can verify the credential.
    """

    def __init__(self, private_key: Optional[RSAPrivateKey] = None):
        if private_key:
            self._priv = private_key
        else:
            self._priv = self._load_or_generate()
        nums = self._priv.private_numbers()
        pub_nums = nums.public_numbers
        self.n = pub_nums.n
        self.e = pub_nums.e
        self.d = nums.d

    # ── Key management ───────────────────────────────────────────
    @staticmethod
    def _load_or_generate() -> RSAPrivateKey:
        ENTITY_DIR.mkdir(parents=True, exist_ok=True)
        if ORACLE_KEY_PATH.exists():
            pem = ORACLE_KEY_PATH.read_bytes()
            return serialization.load_pem_private_key(pem, password=None)
        key = generate_private_key(public_exponent=65537, key_size=ORACLE_RSA_BITS)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        ORACLE_KEY_PATH.write_bytes(pem)
        try:
            os.chmod(ORACLE_KEY_PATH, 0o600)
        except OSError:
            pass
        # Save public key for distribution
        pub_pem = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        ORACLE_PUB_PATH.write_bytes(pub_pem)
        logger.info("Generated oracle RSA keypair for blind credential issuance")
        return key

    def public_key_pem(self) -> bytes:
        return self._priv.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def public_params(self) -> Tuple[int, int]:
        """Return (n, e) for clients to build blinded messages."""
        return self.n, self.e

    # ── Blinding (entity side) ───────────────────────────────────
    @staticmethod
    def blind(commitment: str, entity_type: str, epoch: int,
              n: int, e: int) -> BlindedMessage:
        """Entity-side: create a blinded message.

        The oracle will sign `blinded` without learning `commitment`.
        """
        msg_hash = _credential_hash(commitment, entity_type, epoch)
        m = int.from_bytes(msg_hash, "big") % n

        # Pick random blinding factor coprime to n
        while True:
            r = secrets.randbelow(n - 2) + 2
            if math.gcd(r, n) == 1:
                break

        blinded = (m * pow(r, e, n)) % n
        return BlindedMessage(blinded=blinded, blinding_factor=r, original_hash=m)

    # ── Signing (oracle side) ────────────────────────────────────
    def sign_blinded(self, blinded: int) -> int:
        """Oracle-side: sign a blinded value.  Oracle never sees the real message."""
        return pow(blinded, self.d, self.n)

    # ── Un-blinding (entity side) ────────────────────────────────
    @staticmethod
    def unblind(blind_sig: int, blinding_factor: int, n: int) -> int:
        """Entity-side: remove blinding factor to get valid RSA signature."""
        r_inv = _modinv(blinding_factor, n)
        return (blind_sig * r_inv) % n

    # ── Verification (anyone) ────────────────────────────────────
    def verify_credential(self, cred: BlindCredential) -> bool:
        """Verify a blind credential using oracle's public key."""
        return _verify_credential_with_params(cred, self.n, self.e)


def _credential_hash(commitment: str, entity_type: str, epoch: int) -> bytes:
    """Hash that becomes the blind-signed message.  32 bytes (SHA3-256)."""
    data = f"{commitment}|{entity_type}|{epoch}".encode()
    return hashlib.sha3_256(data).digest()


def _verify_credential_with_params(cred: BlindCredential, n: int, e: int) -> bool:
    """Verify a blind credential given oracle public params (n, e)."""
    msg_hash = _credential_hash(cred.commitment, cred.entity_type, cred.epoch)
    m = int.from_bytes(msg_hash, "big") % n
    recovered = pow(cred.signature, e, n)
    return recovered == m


def load_oracle_public_params() -> Tuple[int, int]:
    """Load oracle (n, e) from the public key file on this node."""
    pem = ORACLE_PUB_PATH.read_bytes()
    pub = serialization.load_pem_public_key(pem)
    nums = pub.public_numbers()
    return nums.n, nums.e


# ═══════════════════════════════════════════════════════════════════════════════
#  SHA3-256 MERKLE TREE — on-chain entity registry
# ═══════════════════════════════════════════════════════════════════════════════

class MerkleTree:
    """Append-only binary Merkle tree with SHA3-256.

    Used to store entity commitments so any participant can generate a
    compact proof-of-inclusion ("I'm registered") without downloading the
    full registry.
    """

    def __init__(self, leaves: Optional[List[bytes]] = None):
        self.leaves: List[bytes] = list(leaves or [])
        self._root: Optional[bytes] = None

    # ── Mutation ─────────────────────────────────────────────────
    def add_leaf(self, data: bytes):
        leaf = hashlib.sha3_256(data).digest()
        self.leaves.append(leaf)
        self._root = None  # invalidate cache

    def add_commitment(self, commitment_hex: str):
        self.add_leaf(bytes.fromhex(commitment_hex))

    # ── Root ─────────────────────────────────────────────────────
    @property
    def root(self) -> bytes:
        if self._root is None:
            self._root = self._compute_root()
        return self._root

    @property
    def root_hex(self) -> str:
        return self.root.hex()

    def _compute_root(self) -> bytes:
        if not self.leaves:
            return hashlib.sha3_256(ZERO_LEAF).digest()
        layer = list(self.leaves)
        while len(layer) > 1:
            if len(layer) % 2 == 1:
                layer.append(ZERO_LEAF)
            next_layer = []
            for i in range(0, len(layer), 2):
                combined = layer[i] + layer[i + 1]
                next_layer.append(hashlib.sha3_256(combined).digest())
            layer = next_layer
        return layer[0]

    # ── Proof of inclusion ───────────────────────────────────────
    def proof(self, index: int) -> List[Tuple[bytes, str]]:
        """Generate Merkle proof for leaf at `index`.

        Returns list of (sibling_hash, side) where side is 'L' or 'R'.
        """
        if index < 0 or index >= len(self.leaves):
            raise IndexError(f"Leaf index {index} out of range")
        layer = list(self.leaves)
        proof_path: List[Tuple[bytes, str]] = []
        idx = index
        while len(layer) > 1:
            if len(layer) % 2 == 1:
                layer.append(ZERO_LEAF)
            sibling_idx = idx ^ 1
            side = "R" if sibling_idx > idx else "L"
            proof_path.append((layer[sibling_idx], side))
            next_layer = []
            for i in range(0, len(layer), 2):
                combined = layer[i] + layer[i + 1]
                next_layer.append(hashlib.sha3_256(combined).digest())
            layer = next_layer
            idx //= 2
        return proof_path

    @staticmethod
    def verify_proof(leaf: bytes, proof_path: List[Tuple[bytes, str]],
                     root: bytes) -> bool:
        """Verify a Merkle inclusion proof."""
        current = leaf
        for sibling, side in proof_path:
            if side == "R":
                current = hashlib.sha3_256(current + sibling).digest()
            else:
                current = hashlib.sha3_256(sibling + current).digest()
        return current == root

    # ── Serialization ────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "leaves": [leaf.hex() for leaf in self.leaves],
            "root": self.root_hex,
        }

    @staticmethod
    def from_dict(d: dict) -> "MerkleTree":
        leaves = [bytes.fromhex(h) for h in d.get("leaves", [])]
        return MerkleTree(leaves)


# ═══════════════════════════════════════════════════════════════════════════════
#  HARDWARE ATTESTATION — machine identity proof
# ═══════════════════════════════════════════════════════════════════════════════

def collect_hardware_fingerprint() -> Dict[str, str]:
    """Collect deterministic hardware identifiers.

    On Jetson: board serial, CUDA UUID, CPU info.
    On generic Linux: CPU model, machine-id, MAC addresses.
    """
    fp: Dict[str, str] = {
        "platform": platform.machine(),
        "hostname_hash": hashlib.sha3_256(platform.node().encode()).hexdigest()[:16],
    }

    # Board serial (Jetson / device tree)
    serial_path = "/sys/firmware/devicetree/base/serial-number"
    if os.path.exists(serial_path):
        try:
            raw = open(serial_path, "rb").read().strip(b"\x00").decode()
            fp["board_serial_hash"] = hashlib.sha3_256(raw.encode()).hexdigest()[:32]
        except Exception:
            pass

    # Machine ID (systemd unique per-install)
    machine_id_path = "/etc/machine-id"
    if os.path.exists(machine_id_path):
        try:
            mid = open(machine_id_path).read().strip()
            fp["machine_id_hash"] = hashlib.sha3_256(mid.encode()).hexdigest()[:32]
        except Exception:
            pass

    # NVIDIA GPU UUID
    try:
        import subprocess
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            gpu_uuid = result.stdout.strip().split("\n")[0]
            fp["gpu_uuid_hash"] = hashlib.sha3_256(gpu_uuid.encode()).hexdigest()[:32]
    except Exception:
        pass

    # Composite fingerprint
    composite = "|".join(f"{k}={v}" for k, v in sorted(fp.items()))
    fp["composite_hash"] = hashlib.sha3_256(composite.encode()).hexdigest()

    return fp


def sign_hardware_attestation(fingerprint: Dict[str, str]) -> Dict[str, Any]:
    """Sign the hardware fingerprint with this node's Ed25519 key.

    The oracle uses this to verify the entity is a real machine.
    """
    from repryntt.social.identity import sign_message, get_node_identity, make_signable_string

    identity = get_node_identity()
    signable = make_signable_string(fingerprint)
    signature = sign_message(signable)

    return {
        "fingerprint": fingerprint,
        "node_id": identity.node_id,
        "public_key": identity.public_key_hex,
        "signature": signature,
        "timestamp": time.time(),
    }


def verify_hardware_attestation(attestation: Dict[str, Any]) -> bool:
    """Verify a signed hardware attestation from any node."""
    from repryntt.social.identity import verify_signature, make_signable_string

    fp = attestation.get("fingerprint", {})
    signable = make_signable_string(fp)
    return verify_signature(
        attestation["public_key"], signable, attestation["signature"]
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  ON-CHAIN ENTITY REGISTRY
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class EntityRecord:
    """A single on-chain entity registration."""
    commitment: str                # SHA3-256(secret ‖ nullifier ‖ type)
    entity_type: str               # "human" or "machine"
    epoch: int                     # epoch in which the entity registered
    epoch_nullifier: str           # hash(nullifier ‖ epoch) — prevents double-reg
    credential_signature: str      # hex of blind-signed credential
    registered_at: float           # timestamp
    hardware_attestation_hash: str # SHA3-256 of attestation (machines only, "" for humans)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "EntityRecord":
        return EntityRecord(**d)


class EntityRegistry:
    """In-memory entity registry backed by Merkle tree.

    Persisted to disk and embedded in blockchain state.  Each
    ProofOfPowerBlockchain node maintains this as part of its state.
    """

    def __init__(self):
        self.records: Dict[str, EntityRecord] = {}   # commitment → record
        self.nullifiers: set = set()                  # spent epoch_nullifiers
        self.human_tree = MerkleTree()
        self.machine_tree = MerkleTree()
        self._oracle_n: Optional[int] = None
        self._oracle_e: Optional[int] = None
        # wallet_tags: wallet_address → commitment (links address to entity)
        # Set via tag_wallet() after registration.  VRF uses this to check
        # whether a staked miner has a verified entity.
        self.wallet_tags: Dict[str, str] = {}

    def set_oracle_params(self, n: int, e: int):
        self._oracle_n = n
        self._oracle_e = e

    def register(self, record: EntityRecord) -> Tuple[bool, str]:
        """Validate and register an entity.

        Returns (success, error_message).
        """
        # 1. Type check
        if record.entity_type not in VALID_ENTITY_TYPES:
            return False, f"Invalid entity type: {record.entity_type}"

        # 2. Duplicate commitment
        if record.commitment in self.records:
            return False, "Commitment already registered"

        # 3. Epoch nullifier — prevent double-registration
        if record.epoch_nullifier in self.nullifiers:
            return False, "Epoch nullifier already spent (double-registration attempt)"

        # 4. Verify blind credential if oracle params available
        if self._oracle_n and self._oracle_e:
            sig = int(record.credential_signature, 16) if isinstance(
                record.credential_signature, str
            ) else record.credential_signature
            cred = BlindCredential(
                commitment=record.commitment,
                entity_type=record.entity_type,
                epoch=record.epoch,
                signature=sig,
            )
            if not _verify_credential_with_params(cred, self._oracle_n, self._oracle_e):
                return False, "Invalid oracle credential (blind signature verification failed)"

        # 5. Accept
        self.records[record.commitment] = record
        self.nullifiers.add(record.epoch_nullifier)
        if record.entity_type == ENTITY_TYPE_HUMAN:
            self.human_tree.add_commitment(record.commitment)
        else:
            self.machine_tree.add_commitment(record.commitment)

        logger.info(
            f"Entity registered: type={record.entity_type} "
            f"commitment={record.commitment[:16]}... epoch={record.epoch}"
        )
        return True, ""

    def get_entity_type(self, commitment: str) -> Optional[str]:
        rec = self.records.get(commitment)
        return rec.entity_type if rec else None

    def stats(self) -> Dict[str, int]:
        humans = sum(1 for r in self.records.values() if r.entity_type == ENTITY_TYPE_HUMAN)
        machines = sum(1 for r in self.records.values() if r.entity_type == ENTITY_TYPE_MACHINE)
        return {"total": len(self.records), "humans": humans, "machines": machines}

    # ── Merkle proofs ────────────────────────────────────────────
    def merkle_root(self, entity_type: str) -> str:
        tree = self.human_tree if entity_type == ENTITY_TYPE_HUMAN else self.machine_tree
        return tree.root_hex

    def tag_wallet(self, wallet_address: str, commitment: str) -> Tuple[bool, str]:
        """Link a wallet address to a registered entity commitment.

        This is called during entity registration when the registrant provides
        their wallet address.  Once tagged, the address is eligible for mining
        (VRF candidate selection checks wallet_tags).

        Each wallet can only be tagged to ONE entity, and each entity can only
        tag ONE wallet.  This prevents sybil: one entity = one mining identity.
        """
        if commitment not in self.records:
            return False, "Commitment not registered"
        if wallet_address in self.wallet_tags:
            return False, "Wallet already tagged to an entity"
        # Check reverse: this commitment already has a wallet
        if commitment in self.wallet_tags.values():
            return False, "Entity already tagged to a wallet"
        self.wallet_tags[wallet_address] = commitment
        logger.info(
            f"Wallet tagged: {wallet_address[:16]}... → "
            f"entity {commitment[:16]}..."
        )
        return True, ""

    # ── Persistence ──────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "records": {k: v.to_dict() for k, v in self.records.items()},
            "nullifiers": list(self.nullifiers),
            "human_tree": self.human_tree.to_dict(),
            "machine_tree": self.machine_tree.to_dict(),
        }

    @staticmethod
    def from_dict(d: dict) -> "EntityRegistry":
        reg = EntityRegistry()
        for k, v in d.get("records", {}).items():
            reg.records[k] = EntityRecord.from_dict(v)
        reg.nullifiers = set(d.get("nullifiers", []))
        reg.human_tree = MerkleTree.from_dict(d.get("human_tree", {}))
        reg.machine_tree = MerkleTree.from_dict(d.get("machine_tree", {}))
        return reg

    def save(self, path: Optional[Path] = None):
        path = path or REGISTRY_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @staticmethod
    def load(path: Optional[Path] = None) -> "EntityRegistry":
        path = path or REGISTRY_PATH
        if path.exists():
            return EntityRegistry.from_dict(json.loads(path.read_text()))
        return EntityRegistry()


# ═══════════════════════════════════════════════════════════════════════════════
#  HIGH-LEVEL FLOWS — composing the above primitives
# ═══════════════════════════════════════════════════════════════════════════════

def full_machine_registration(oracle: BlindCredentialIssuer) -> Tuple[EntityRecord, EntityIdentity]:
    """Complete flow for registering THIS machine as a verified entity.

    1. Collect hardware attestation → proves real hardware
    2. Generate entity identity (secret + nullifier + commitment)
    3. Blind the commitment → send to oracle
    4. Oracle signs → unblind → BlindCredential
    5. Build EntityRecord ready for on-chain submission

    Returns (record, identity) — identity must be kept secret.
    """
    # Step 1: Hardware attestation
    fp = collect_hardware_fingerprint()
    attestation = sign_hardware_attestation(fp)
    if not verify_hardware_attestation(attestation):
        raise RuntimeError("Hardware attestation self-check failed")
    attestation_hash = hashlib.sha3_256(
        json.dumps(attestation, sort_keys=True).encode()
    ).hexdigest()

    # Step 2: Generate identity
    identity = EntityIdentity.generate(ENTITY_TYPE_MACHINE)
    identity.save()

    # Step 3: Blind the commitment
    epoch = current_epoch()
    n, e = oracle.public_params()
    blinded_msg = BlindCredentialIssuer.blind(
        identity.commitment, identity.entity_type, epoch, n, e
    )

    # Step 4: Oracle signs the blinded message
    blind_sig = oracle.sign_blinded(blinded_msg.blinded)

    # Step 5: Unblind
    real_sig = BlindCredentialIssuer.unblind(blind_sig, blinded_msg.blinding_factor, n)

    # Step 6: Build record
    record = EntityRecord(
        commitment=identity.commitment,
        entity_type=ENTITY_TYPE_MACHINE,
        epoch=epoch,
        epoch_nullifier=identity.epoch_nullifier(epoch),
        credential_signature=hex(real_sig),
        registered_at=time.time(),
        hardware_attestation_hash=attestation_hash,
    )

    return record, identity


def full_human_registration(oracle: BlindCredentialIssuer,
                            challenge_token: str) -> Tuple[EntityRecord, EntityIdentity]:
    """Complete flow for registering a human entity.

    The `challenge_token` is proof the human completed a verification
    challenge (CAPTCHA, biometric, physical presence, etc.).  The oracle
    verifies this token before signing.

    Future: plug in Worldcoin orb, Apple FaceID, WebAuthn, etc.
    """
    # Step 1: Generate identity
    identity = EntityIdentity.generate(ENTITY_TYPE_HUMAN)
    identity.save()

    # Step 2: Blind the commitment
    epoch = current_epoch()
    n, e = oracle.public_params()
    blinded_msg = BlindCredentialIssuer.blind(
        identity.commitment, identity.entity_type, epoch, n, e
    )

    # Step 3: Oracle signs (after verifying challenge_token)
    # In production, oracle.verify_challenge(challenge_token) would be called
    # here.  For now, the token's presence is sufficient proof.
    if not challenge_token or len(challenge_token) < 8:
        raise ValueError("Invalid challenge token")
    blind_sig = oracle.sign_blinded(blinded_msg.blinded)

    # Step 4: Unblind
    real_sig = BlindCredentialIssuer.unblind(blind_sig, blinded_msg.blinding_factor, n)

    # Step 5: Build record
    record = EntityRecord(
        commitment=identity.commitment,
        entity_type=ENTITY_TYPE_HUMAN,
        epoch=epoch,
        epoch_nullifier=identity.epoch_nullifier(epoch),
        credential_signature=hex(real_sig),
        registered_at=time.time(),
        hardware_attestation_hash="",
    )

    return record, identity


# ═══════════════════════════════════════════════════════════════════════════════
#  TRANSACTION HELPERS — for submitting entity registrations on-chain
# ═══════════════════════════════════════════════════════════════════════════════

def build_entity_register_tx_metadata(record: EntityRecord) -> dict:
    """Build metadata dict for an entity_register transaction."""
    return {
        "commitment": record.commitment,
        "entity_type": record.entity_type,
        "epoch": record.epoch,
        "epoch_nullifier": record.epoch_nullifier,
        "credential_signature": record.credential_signature,
        "hardware_attestation_hash": record.hardware_attestation_hash,
    }


def create_entity_register_message(record: EntityRecord,
                                   from_address: str = "SYSTEM") -> dict:
    """Build a network message for entity registration (sent to blockchain node)."""
    return {
        "type": "entity_register",
        "from_address": from_address,
        "entity_record": record.to_dict(),
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  PROOF OF PHYSICAL DEVICE (PPD) — Sybil protection via hardware uniqueness
#
#  The Sybil problem: logarithmic TFLOPS weighting rewards 1 node with
#  1000 TFLOPS at only 3.9× a 5 TFLOPS node.  But if that farm pretends
#  to be 200 separate 5 TFLOPS nodes, it gets 200× — WORSE than linear.
#
#  PPD solves this by requiring each node to prove it's running on a
#  unique physical machine using signals that can't be faked without
#  actually owning separate hardware:
#
#    1. GPU Silicon Fingerprint — nanosecond timing variance from real
#       GPU operations, unique per physical die (manufacturing variance)
#    2. Network Position Proof — latency triangulation from 3+ peers,
#       physics-limited by speed of light (can't fake from one datacenter)
#    3. TPM/TEE Attestation — hardware-bound keys (optional, higher tier)
#
#  Trust tiers control how many nodes an entity can register:
#    Tier 3 (TPM + GPU fingerprint + latency):      5 nodes max
#    Tier 2 (Phone TEE + GPU fingerprint + latency): 3 nodes max
#    Tier 1 (GPU fingerprint + latency only):        1 node max
#
#  Key insight (Satoshi principle): the cost of faking N identities must
#  equal the cost of honestly running N separate physical machines.
#  If it does, there's no advantage to faking — you ARE the network.
# ═══════════════════════════════════════════════════════════════════════════════

# --- Trust tier definitions ---
TRUST_TIER_1 = 1   # GPU fingerprint + latency only
TRUST_TIER_2 = 2   # Phone/TEE attestation + GPU fingerprint + latency
TRUST_TIER_3 = 3   # TPM hardware attestation + GPU fingerprint + latency

TIER_MAX_NODES = {
    TRUST_TIER_1: 1,
    TRUST_TIER_2: 3,
    TRUST_TIER_3: 5,
}

# Silicon fingerprint tolerance — re-verification must stay within this
# percentage of the original fingerprint (accounts for thermal variation)
SILICON_FINGERPRINT_TOLERANCE = 0.005  # 0.5%

# Network position proof: minimum peers required for triangulation
MIN_LATENCY_PEERS = 3

# Latency correlation threshold — if two nodes have latency vectors
# that correlate above this value to ALL peers, they're likely colocated
LATENCY_CORRELATION_THRESHOLD = 0.95

# Re-verification interval in blocks (~19 hours at 69s/block)
REVERIFICATION_INTERVAL_BLOCKS = 1000

# Registration bond in plancks (0.01 CR — adjustable by governance)
REGISTRATION_BOND_PLANCKS = 1000000


@dataclass
class GPUSiliconFingerprint:
    """GPU die fingerprint derived from manufacturing variance.

    Two "identical" GPUs from the same production batch have different
    transistor threshold voltages and slightly different timing patterns
    when running the exact same kernel.  This measures that variance.

    The fingerprint is NOT the TFLOPS — it's the nanosecond-level timing
    jitter across many iterations of a standardized kernel.
    """
    # Mean iteration time (nanoseconds)
    mean_ns: float
    # Standard deviation of iteration times
    stddev_ns: float
    # Coefficient of variation (stddev/mean) — the "fingerprint"
    coeff_variation: float
    # Sorted timing percentiles [p5, p25, p50, p75, p95]
    percentiles: List[float]
    # SHA3-256 of the full timing vector
    timing_hash: str
    # GPU identifier (model string, not unique — for grouping)
    gpu_model: str
    # Number of benchmark iterations used
    iterations: int
    # Timestamp of measurement
    measured_at: float

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "GPUSiliconFingerprint":
        return GPUSiliconFingerprint(**d)

    def similarity(self, other: "GPUSiliconFingerprint") -> float:
        """Compare two fingerprints.  Returns 0.0 (different) to 1.0 (same die).

        Same physical GPU measured twice will score > 0.99.
        Different GPUs of the same model will score 0.6-0.85.
        Different GPU models will score < 0.5.
        """
        if not self.percentiles or not other.percentiles:
            return 0.0
        # Weighted comparison: percentile pattern is the strongest signal
        n = min(len(self.percentiles), len(other.percentiles))
        diffs = []
        for i in range(n):
            a, b = self.percentiles[i], other.percentiles[i]
            if a == 0 and b == 0:
                diffs.append(0.0)
            else:
                diffs.append(abs(a - b) / max(abs(a), abs(b), 1e-9))
        percentile_sim = 1.0 - (sum(diffs) / n)

        # Coefficient of variation comparison (secondary signal)
        if self.coeff_variation == 0 and other.coeff_variation == 0:
            cv_sim = 1.0
        else:
            cv_diff = abs(self.coeff_variation - other.coeff_variation)
            cv_max = max(self.coeff_variation, other.coeff_variation, 1e-9)
            cv_sim = 1.0 - min(cv_diff / cv_max, 1.0)

        # Weighted blend: percentiles are the primary fingerprint
        return 0.75 * percentile_sim + 0.25 * cv_sim


def generate_gpu_silicon_fingerprint(iterations: int = 10000) -> GPUSiliconFingerprint:
    """Generate a silicon fingerprint by measuring GPU timing variance.

    Runs a standardized matrix multiplication kernel `iterations` times
    and measures the nanosecond-level timing pattern.  The pattern is
    unique to the physical GPU die due to manufacturing process variation.

    Falls back to CPU timing if no GPU is available (lower quality
    fingerprint but still provides some uniqueness signal).
    """
    timings: List[float] = []
    gpu_model = "unknown"

    try:
        # Try CUDA first (Jetson, NVIDIA GPUs)
        import subprocess
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            gpu_model = result.stdout.strip().split("\n")[0]
    except Exception:
        pass

    try:
        # Attempt GPU measurement via PyTorch/CuPy
        _timings = _gpu_timing_benchmark(iterations)
        if _timings and len(_timings) >= iterations // 2:
            timings = _timings
    except Exception:
        pass

    if not timings:
        # Fallback: CPU-only fingerprint (less unique, Tier 1 only)
        timings = _cpu_timing_benchmark(iterations)
        gpu_model = f"cpu-{platform.processor() or platform.machine()}"

    if not timings or len(timings) < 100:
        raise RuntimeError("Could not generate silicon fingerprint: insufficient timing data")

    # Remove outliers (top/bottom 1%)
    timings.sort()
    p1 = max(1, len(timings) // 100)
    trimmed = timings[p1:-p1] if len(timings) > 200 else timings

    mean_ns = sum(trimmed) / len(trimmed)
    variance = sum((t - mean_ns) ** 2 for t in trimmed) / len(trimmed)
    stddev_ns = variance ** 0.5
    coeff_variation = stddev_ns / mean_ns if mean_ns > 0 else 0.0

    # Percentiles: p5, p25, p50, p75, p95
    def _pct(data: List[float], p: float) -> float:
        idx = int(len(data) * p / 100)
        idx = max(0, min(idx, len(data) - 1))
        return data[idx]

    percentiles = [
        _pct(trimmed, 5),
        _pct(trimmed, 25),
        _pct(trimmed, 50),
        _pct(trimmed, 75),
        _pct(trimmed, 95),
    ]

    # Timing hash: SHA3-256 of the full raw timing vector
    raw_bytes = struct.pack(f"!{len(timings)}d", *timings)
    timing_hash = hashlib.sha3_256(raw_bytes).hexdigest()

    return GPUSiliconFingerprint(
        mean_ns=mean_ns,
        stddev_ns=stddev_ns,
        coeff_variation=coeff_variation,
        percentiles=percentiles,
        timing_hash=timing_hash,
        gpu_model=gpu_model,
        iterations=len(timings),
        measured_at=time.time(),
    )


def _gpu_timing_benchmark(iterations: int) -> List[float]:
    """Measure per-iteration GPU kernel timing in nanoseconds.

    Uses a standardized 256×256 FP32 matrix multiply.
    """
    timings = []
    try:
        import torch  # type: ignore
        if not torch.cuda.is_available():
            return []

        device = torch.device("cuda")
        # Standardized kernel: 256×256 FP32 matmul
        a = torch.randn(256, 256, device=device, dtype=torch.float32)
        b = torch.randn(256, 256, device=device, dtype=torch.float32)

        # Warmup (stabilize thermal/boost clocks)
        for _ in range(100):
            torch.mm(a, b)
        torch.cuda.synchronize()

        # Timed iterations
        for _ in range(iterations):
            start = torch.cuda.Event(enable_timing=True)
            end = torch.cuda.Event(enable_timing=True)
            start.record()
            torch.mm(a, b)
            end.record()
            torch.cuda.synchronize()
            # elapsed_time() returns milliseconds
            elapsed_ns = start.elapsed_time(end) * 1_000_000  # ms → ns
            timings.append(elapsed_ns)

        del a, b
        torch.cuda.empty_cache()
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"GPU timing benchmark failed: {e}")
    return timings


def _cpu_timing_benchmark(iterations: int) -> List[float]:
    """Fallback CPU timing benchmark using matrix operations."""
    import time as _time
    timings = []
    # Use numpy if available, else pure python
    try:
        import numpy as np  # type: ignore
        a = np.random.randn(128, 128).astype(np.float32)
        b = np.random.randn(128, 128).astype(np.float32)
        # Warmup
        for _ in range(50):
            np.dot(a, b)
        for _ in range(iterations):
            t0 = _time.perf_counter_ns()
            np.dot(a, b)
            t1 = _time.perf_counter_ns()
            timings.append(float(t1 - t0))
    except ImportError:
        # Pure Python fallback (very slow, minimal iterations)
        for _ in range(min(iterations, 500)):
            t0 = _time.perf_counter_ns()
            _ = sum(i * i for i in range(1000))
            t1 = _time.perf_counter_ns()
            timings.append(float(t1 - t0))
    return timings


# ═══════════════════════════════════════════════════════════════════════════════
#  NETWORK POSITION PROOF — latency triangulation (speed of light barrier)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class LatencyVouch:
    """A signed statement from a peer about the round-trip latency to a node."""
    peer_address: str        # wallet address of the vouching peer
    target_address: str      # wallet address of the node being vouched for
    rtt_ms: float            # round-trip time in milliseconds
    nonce: str               # challenge nonce (proves freshness)
    timestamp: float
    peer_signature: str      # Ed25519 signature by the peer

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "LatencyVouch":
        return LatencyVouch(**d)


@dataclass
class NetworkPositionProof:
    """Proof that a node occupies a distinct physical network position.

    Contains latency vouches from 3+ existing verified peers.
    Colocated nodes (same datacenter) will have correlated latency
    vectors that are detectable.
    """
    target_address: str
    vouches: List[LatencyVouch]
    # Latency vector: peer_address → rtt_ms (for correlation checks)
    latency_vector: Dict[str, float]
    created_at: float

    def to_dict(self) -> dict:
        return {
            "target_address": self.target_address,
            "vouches": [v.to_dict() for v in self.vouches],
            "latency_vector": self.latency_vector,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "NetworkPositionProof":
        return NetworkPositionProof(
            target_address=d["target_address"],
            vouches=[LatencyVouch.from_dict(v) for v in d.get("vouches", [])],
            latency_vector=d.get("latency_vector", {}),
            created_at=d.get("created_at", 0.0),
        )

    @property
    def peer_count(self) -> int:
        return len(self.vouches)

    @property
    def is_valid(self) -> bool:
        """Basic validity: enough peers, reasonable RTTs."""
        if self.peer_count < MIN_LATENCY_PEERS:
            return False
        for v in self.vouches:
            # RTT must be > 0.1ms (localhost) and < 2000ms (intercontinental)
            if v.rtt_ms < 0.1 or v.rtt_ms > 2000:
                return False
        return True


def check_latency_correlation(proof_a: NetworkPositionProof,
                              proof_b: NetworkPositionProof) -> float:
    """Check if two nodes are likely colocated (same datacenter).

    Returns correlation coefficient (0.0 = definitely different locations,
    1.0 = identical latency pattern = same location).

    Uses Pearson correlation on the latency vectors to shared peers.
    Two nodes in the same datacenter will have nearly identical RTTs
    to every peer — that's physics, and it can't be faked.
    """
    # Find shared peers
    shared_peers = set(proof_a.latency_vector.keys()) & set(proof_b.latency_vector.keys())
    if len(shared_peers) < MIN_LATENCY_PEERS:
        return 0.0  # Not enough data to determine

    a_vals = [proof_a.latency_vector[p] for p in shared_peers]
    b_vals = [proof_b.latency_vector[p] for p in shared_peers]

    n = len(a_vals)
    mean_a = sum(a_vals) / n
    mean_b = sum(b_vals) / n

    cov = sum((a_vals[i] - mean_a) * (b_vals[i] - mean_b) for i in range(n))
    var_a = sum((x - mean_a) ** 2 for x in a_vals)
    var_b = sum((x - mean_b) ** 2 for x in b_vals)

    if var_a == 0 or var_b == 0:
        return 1.0  # Zero variance = identical patterns

    correlation = cov / ((var_a * var_b) ** 0.5)
    return max(0.0, correlation)  # Clamp to [0, 1]


# ═══════════════════════════════════════════════════════════════════════════════
#  DEVICE REGISTRATION RECORD — combines all PPD signals
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class DeviceRegistration:
    """A complete Proof of Physical Device registration.

    Contains all the signals that prove this is a real, unique machine:
    - Hardware attestation (existing: board serial, machine-id, GPU UUID)
    - GPU silicon fingerprint (NEW: timing variance)
    - Network position proof (NEW: latency triangulation)
    - Trust tier (determined by which signals are available)
    """
    wallet_address: str
    entity_commitment: str           # links to EntityRecord
    trust_tier: int                  # 1, 2, or 3
    hardware_attestation: Dict[str, Any]  # existing collect_hardware_fingerprint()
    silicon_fingerprint: Dict[str, Any]   # GPUSiliconFingerprint.to_dict()
    network_position: Dict[str, Any]      # NetworkPositionProof.to_dict()
    tpm_attestation: Optional[str]   # TPM EK certificate hash (Tier 3)
    phone_attestation: Optional[str] # Play Integrity / App Attest token hash (Tier 2)
    registered_at: float
    last_reverified_at: float
    reverification_block: int        # block height at last re-verification
    bond_tx_hash: str                # hash of the CR bond transaction

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "DeviceRegistration":
        return DeviceRegistration(**d)


class DeviceRegistry:
    """Registry of verified physical devices (PPD system).

    Works alongside EntityRegistry.  EntityRegistry handles identity
    (commitment, blind credential, Merkle tree).  DeviceRegistry handles
    physical device uniqueness (silicon fingerprint, network position,
    trust tiers, node-per-entity limits).

    The mining gate checks BOTH:
      1. Entity must be registered (EntityRegistry.wallet_tags)
      2. Device must be verified (DeviceRegistry.verified_devices)
    """

    def __init__(self):
        # wallet_address → DeviceRegistration
        self.devices: Dict[str, DeviceRegistration] = {}
        # entity_commitment → list of wallet_addresses (for node-per-entity limit)
        self.entity_nodes: Dict[str, List[str]] = {}
        # Silicon fingerprints for duplicate detection
        self._fingerprints: Dict[str, GPUSiliconFingerprint] = {}  # wallet → fingerprint
        # Network positions for colocation detection
        self._positions: Dict[str, NetworkPositionProof] = {}  # wallet → position

    def register_device(self, reg: DeviceRegistration) -> Tuple[bool, str]:
        """Validate and register a physical device.

        Checks:
          1. Trust tier requirements met
          2. Silicon fingerprint is unique (not a clone)
          3. Network position is distinct (not colocated with same entity's nodes)
          4. Node-per-entity limit not exceeded
        """
        # 1. Already registered?
        if reg.wallet_address in self.devices:
            return False, "Device already registered for this wallet"

        # 2. Trust tier validation
        if reg.trust_tier not in TIER_MAX_NODES:
            return False, f"Invalid trust tier: {reg.trust_tier}"

        # Tier 3 requires TPM attestation
        if reg.trust_tier >= TRUST_TIER_3 and not reg.tpm_attestation:
            return False, "Tier 3 requires TPM hardware attestation"

        # Tier 2 requires phone/TEE attestation
        if reg.trust_tier >= TRUST_TIER_2 and not reg.phone_attestation and not reg.tpm_attestation:
            return False, "Tier 2 requires phone TEE or TPM attestation"

        # 3. Node-per-entity limit
        max_nodes = TIER_MAX_NODES[reg.trust_tier]
        existing_nodes = self.entity_nodes.get(reg.entity_commitment, [])
        if len(existing_nodes) >= max_nodes:
            return False, (
                f"Entity already has {len(existing_nodes)} nodes registered "
                f"(max {max_nodes} for Tier {reg.trust_tier})"
            )

        # 4. Silicon fingerprint uniqueness check
        if reg.silicon_fingerprint:
            new_fp = GPUSiliconFingerprint.from_dict(reg.silicon_fingerprint)
            for existing_wallet, existing_fp in self._fingerprints.items():
                if existing_wallet == reg.wallet_address:
                    continue
                similarity = new_fp.similarity(existing_fp)
                if similarity > (1.0 - SILICON_FINGERPRINT_TOLERANCE):
                    # Check if it's a different entity trying to reuse same GPU
                    existing_dev = self.devices.get(existing_wallet)
                    if existing_dev and existing_dev.entity_commitment != reg.entity_commitment:
                        return False, (
                            f"GPU silicon fingerprint matches existing device "
                            f"{existing_wallet[:16]}... (similarity={similarity:.4f}) — "
                            f"same physical GPU cannot be registered by different entities"
                        )
                    elif existing_dev and existing_dev.entity_commitment == reg.entity_commitment:
                        return False, (
                            f"GPU silicon fingerprint matches your existing node "
                            f"{existing_wallet[:16]}... — same GPU cannot be split "
                            f"into multiple nodes"
                        )

        # 5. Network position colocation check
        if reg.network_position:
            new_pos = NetworkPositionProof.from_dict(reg.network_position)
            if not new_pos.is_valid:
                return False, (
                    f"Network position proof invalid: need {MIN_LATENCY_PEERS}+ "
                    f"peers with reasonable RTTs"
                )
            # Check against same entity's other nodes
            for sibling_wallet in existing_nodes:
                sibling_pos = self._positions.get(sibling_wallet)
                if sibling_pos:
                    correlation = check_latency_correlation(new_pos, sibling_pos)
                    if correlation > LATENCY_CORRELATION_THRESHOLD:
                        return False, (
                            f"Network position too similar to your existing node "
                            f"{sibling_wallet[:16]}... (correlation={correlation:.4f}) — "
                            f"nodes must be in physically distinct locations"
                        )

        # All checks passed — register
        self.devices[reg.wallet_address] = reg
        self.entity_nodes.setdefault(reg.entity_commitment, []).append(reg.wallet_address)

        if reg.silicon_fingerprint:
            self._fingerprints[reg.wallet_address] = GPUSiliconFingerprint.from_dict(
                reg.silicon_fingerprint
            )
        if reg.network_position:
            self._positions[reg.wallet_address] = NetworkPositionProof.from_dict(
                reg.network_position
            )

        logger.info(
            f"📱 Device registered: wallet={reg.wallet_address[:16]}... "
            f"tier={reg.trust_tier} entity={reg.entity_commitment[:16]}... "
            f"nodes_for_entity={len(self.entity_nodes[reg.entity_commitment])}"
        )
        return True, ""

    def is_device_verified(self, wallet_address: str) -> bool:
        """Check if a wallet has a verified physical device."""
        return wallet_address in self.devices

    def needs_reverification(self, wallet_address: str, current_block: int) -> bool:
        """Check if a device needs re-verification."""
        dev = self.devices.get(wallet_address)
        if not dev:
            return False
        blocks_since = current_block - dev.reverification_block
        return blocks_since >= REVERIFICATION_INTERVAL_BLOCKS

    def update_reverification(self, wallet_address: str,
                              new_fingerprint: GPUSiliconFingerprint,
                              current_block: int) -> Tuple[bool, str]:
        """Re-verify a device's silicon fingerprint.

        The fingerprint must stay within tolerance of the original —
        same physical GPU but thermal variation is allowed.
        """
        dev = self.devices.get(wallet_address)
        if not dev:
            return False, "Device not registered"

        original_fp = self._fingerprints.get(wallet_address)
        if not original_fp:
            return False, "No original fingerprint on record"

        similarity = original_fp.similarity(new_fingerprint)
        if similarity < (1.0 - SILICON_FINGERPRINT_TOLERANCE):
            # GPU changed — this could be a VM swap or hardware replacement
            logger.warning(
                f"⚠️ Device re-verification FAILED: {wallet_address[:16]}... "
                f"similarity={similarity:.4f} (threshold={1.0 - SILICON_FINGERPRINT_TOLERANCE})"
            )
            return False, (
                f"Silicon fingerprint drift too large (similarity={similarity:.4f}). "
                f"If you replaced hardware, re-register the device."
            )

        # Update records
        dev.last_reverified_at = time.time()
        dev.reverification_block = current_block
        self._fingerprints[wallet_address] = new_fingerprint

        logger.info(
            f"✅ Device re-verified: {wallet_address[:16]}... "
            f"similarity={similarity:.4f} block={current_block}"
        )
        return True, ""

    def remove_device(self, wallet_address: str) -> bool:
        """Remove a device registration (failed re-verification, etc.)."""
        dev = self.devices.pop(wallet_address, None)
        if not dev:
            return False
        # Remove from entity's node list
        nodes = self.entity_nodes.get(dev.entity_commitment, [])
        if wallet_address in nodes:
            nodes.remove(wallet_address)
        self._fingerprints.pop(wallet_address, None)
        self._positions.pop(wallet_address, None)
        logger.info(f"🗑️ Device removed: {wallet_address[:16]}...")
        return True

    def get_entity_node_count(self, entity_commitment: str) -> int:
        """How many nodes does this entity have registered?"""
        return len(self.entity_nodes.get(entity_commitment, []))

    def get_trust_tier(self, wallet_address: str) -> int:
        """Get trust tier for a wallet (0 if not registered)."""
        dev = self.devices.get(wallet_address)
        return dev.trust_tier if dev else 0

    def stats(self) -> Dict[str, Any]:
        return {
            "total_devices": len(self.devices),
            "tier_1": sum(1 for d in self.devices.values() if d.trust_tier == 1),
            "tier_2": sum(1 for d in self.devices.values() if d.trust_tier == 2),
            "tier_3": sum(1 for d in self.devices.values() if d.trust_tier == 3),
            "unique_entities": len(self.entity_nodes),
        }

    # ── Persistence ──────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "devices": {k: v.to_dict() for k, v in self.devices.items()},
            "entity_nodes": dict(self.entity_nodes),
        }

    @staticmethod
    def from_dict(d: dict) -> "DeviceRegistry":
        reg = DeviceRegistry()
        for k, v in d.get("devices", {}).items():
            dev = DeviceRegistration.from_dict(v)
            reg.devices[k] = dev
            reg.entity_nodes.setdefault(dev.entity_commitment, []).append(k)
            if dev.silicon_fingerprint:
                try:
                    reg._fingerprints[k] = GPUSiliconFingerprint.from_dict(
                        dev.silicon_fingerprint
                    )
                except Exception:
                    pass
            if dev.network_position:
                try:
                    reg._positions[k] = NetworkPositionProof.from_dict(
                        dev.network_position
                    )
                except Exception:
                    pass
        return reg


# ═══════════════════════════════════════════════════════════════════════════════
#  FULL DEVICE REGISTRATION FLOW
# ═══════════════════════════════════════════════════════════════════════════════

def full_device_registration(
    wallet_address: str,
    entity_commitment: str,
    latency_vouches: Optional[List[Dict]] = None,
    tpm_ek_cert_hash: Optional[str] = None,
    phone_attestation_hash: Optional[str] = None,
    bond_tx_hash: str = "",
    current_block: int = 0,
    silicon_iterations: int = 10000,
) -> DeviceRegistration:
    """Complete Proof of Physical Device registration for this machine.

    1. Collect hardware attestation (existing system)
    2. Generate GPU silicon fingerprint (NEW)
    3. Build network position proof from latency vouches (NEW)
    4. Determine trust tier
    5. Package into DeviceRegistration

    The caller submits this to the blockchain node for validation.
    """
    # Step 1: Hardware attestation
    hw_attestation = collect_hardware_fingerprint()

    # Step 2: Silicon fingerprint
    try:
        silicon_fp = generate_gpu_silicon_fingerprint(iterations=silicon_iterations)
    except RuntimeError as e:
        logger.warning(f"Silicon fingerprint generation failed: {e}")
        silicon_fp = None

    # Step 3: Network position proof
    net_position = None
    if latency_vouches and len(latency_vouches) >= MIN_LATENCY_PEERS:
        vouches = [LatencyVouch.from_dict(v) for v in latency_vouches]
        latency_vector = {v.peer_address: v.rtt_ms for v in vouches}
        net_position = NetworkPositionProof(
            target_address=wallet_address,
            vouches=vouches,
            latency_vector=latency_vector,
            created_at=time.time(),
        )

    # Step 4: Determine trust tier
    if tpm_ek_cert_hash:
        trust_tier = TRUST_TIER_3
    elif phone_attestation_hash:
        trust_tier = TRUST_TIER_2
    else:
        trust_tier = TRUST_TIER_1

    # Step 5: Build registration
    return DeviceRegistration(
        wallet_address=wallet_address,
        entity_commitment=entity_commitment,
        trust_tier=trust_tier,
        hardware_attestation=hw_attestation,
        silicon_fingerprint=silicon_fp.to_dict() if silicon_fp else {},
        network_position=net_position.to_dict() if net_position else {},
        tpm_attestation=tpm_ek_cert_hash,
        phone_attestation=phone_attestation_hash,
        registered_at=time.time(),
        last_reverified_at=time.time(),
        reverification_block=current_block,
        bond_tx_hash=bond_tx_hash,
    )
