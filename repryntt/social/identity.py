"""
repryntt.social.identity — Cryptographic Node Identity System

Each repryntt instance generates a unique Ed25519 keypair on first boot.
The public key hash becomes the node_id — an unforgeable identity that
proves every post came from a specific repryntt installation.

Key storage: ~/.repryntt/social/node_key.pem (private), node_key.pub (public)

Why Ed25519:
  - Fast signing/verification (important on Jetson Nano)
  - 64-byte signatures (compact for federation)
  - No parameters to misconfigure (unlike RSA/ECDSA)
  - Widely supported across languages for cross-implementation compat
"""

import os
import hashlib
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization

logger = logging.getLogger("repryntt.social.identity")

# Key storage directory
SOCIAL_DIR = Path.home() / ".repryntt" / "social"
PRIVATE_KEY_PATH = SOCIAL_DIR / "node_key.pem"
PUBLIC_KEY_PATH = SOCIAL_DIR / "node_key.pub"
IDENTITY_PATH = SOCIAL_DIR / "node_identity.json"


@dataclass(frozen=True)
class NodeIdentity:
    """Immutable identity of a repryntt node."""
    node_id: str          # SHA256(public_key_bytes)[:16] — 16-char hex string
    public_key_hex: str   # Full public key as hex (64 chars)
    display_name: str     # Human-readable name (configurable)
    entity_type: str = ""      # "human" or "machine" (REVP)
    entity_commitment: str = ""  # SHA3-256 commitment from Entity Verification Protocol


# ── Module-level singleton ──────────────────────────────────────────────────
_identity: Optional[NodeIdentity] = None
_private_key: Optional[Ed25519PrivateKey] = None


def _ensure_keypair() -> tuple[Ed25519PrivateKey, bytes]:
    """Load or generate the node's Ed25519 keypair.

    Returns (private_key, public_key_bytes).
    """
    global _private_key

    SOCIAL_DIR.mkdir(parents=True, exist_ok=True)

    if PRIVATE_KEY_PATH.exists():
        # Load existing keypair
        pem_data = PRIVATE_KEY_PATH.read_bytes()
        private_key = serialization.load_pem_private_key(pem_data, password=None)
        if not isinstance(private_key, Ed25519PrivateKey):
            raise TypeError("Stored key is not Ed25519")
        pub_bytes = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        _private_key = private_key
        return private_key, pub_bytes

    # Generate new keypair
    logger.info("Generating new Ed25519 node identity...")
    private_key = Ed25519PrivateKey.generate()
    pub_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )

    # Save private key (restricted permissions)
    pem_data = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    PRIVATE_KEY_PATH.write_bytes(pem_data)
    from repryntt.platform_utils import secure_file
    secure_file(PRIVATE_KEY_PATH)

    # Save public key (readable)
    PUBLIC_KEY_PATH.write_text(pub_bytes.hex())
    # Public key is readable by design — no restriction needed

    _private_key = private_key
    node_id = hashlib.sha256(pub_bytes).hexdigest()[:16]
    logger.info(f"Node identity created: {node_id}")
    return private_key, pub_bytes


def get_node_identity() -> NodeIdentity:
    """Get (or create) this node's identity. Cached after first call."""
    global _identity
    if _identity is not None:
        return _identity

    _, pub_bytes = _ensure_keypair()
    node_id = hashlib.sha256(pub_bytes).hexdigest()[:16]
    public_key_hex = pub_bytes.hex()

    # Load or generate display name + entity fields
    import json
    display_name = "repryntt-node"
    entity_type = ""
    entity_commitment = ""
    if IDENTITY_PATH.exists():
        try:
            data = json.loads(IDENTITY_PATH.read_text())
            display_name = data.get("display_name", display_name)
            entity_type = data.get("entity_type", "")
            entity_commitment = data.get("entity_commitment", "")
        except Exception:
            pass
    else:
        # First boot — save default identity
        IDENTITY_PATH.write_text(json.dumps({
            "node_id": node_id,
            "display_name": display_name,
            "entity_type": entity_type,
            "entity_commitment": entity_commitment,
        }, indent=2))

    _identity = NodeIdentity(
        node_id=node_id,
        public_key_hex=public_key_hex,
        display_name=display_name,
        entity_type=entity_type,
        entity_commitment=entity_commitment,
    )
    return _identity


def set_display_name(name: str) -> NodeIdentity:
    """Update this node's display name."""
    global _identity
    import json

    identity = get_node_identity()

    IDENTITY_PATH.write_text(json.dumps({
        "node_id": identity.node_id,
        "display_name": name,
        "entity_type": identity.entity_type,
        "entity_commitment": identity.entity_commitment,
    }, indent=2))

    _identity = NodeIdentity(
        node_id=identity.node_id,
        public_key_hex=identity.public_key_hex,
        display_name=name,
        entity_type=identity.entity_type,
        entity_commitment=identity.entity_commitment,
    )
    return _identity


def set_entity_type(entity_type: str, entity_commitment: str) -> NodeIdentity:
    """Update this node's verified entity type (set after REVP registration)."""
    global _identity
    import json

    identity = get_node_identity()

    IDENTITY_PATH.write_text(json.dumps({
        "node_id": identity.node_id,
        "display_name": identity.display_name,
        "entity_type": entity_type,
        "entity_commitment": entity_commitment,
    }, indent=2))

    _identity = NodeIdentity(
        node_id=identity.node_id,
        public_key_hex=identity.public_key_hex,
        display_name=identity.display_name,
        entity_type=entity_type,
        entity_commitment=entity_commitment,
    )
    return _identity


def sign_message(message: str) -> str:
    """Sign a message string with this node's private key.

    Returns the signature as a hex string.
    """
    _ensure_keypair()
    assert _private_key is not None
    sig_bytes = _private_key.sign(message.encode("utf-8"))
    return sig_bytes.hex()


def verify_signature(public_key_hex: str, message: str, signature_hex: str) -> bool:
    """Verify an Ed25519 signature from any node.

    Args:
        public_key_hex: The signer's public key (64-char hex)
        message: The original signed message string
        signature_hex: The signature (128-char hex)

    Returns True if the signature is valid.
    """
    try:
        pub_bytes = bytes.fromhex(public_key_hex)
        sig_bytes = bytes.fromhex(signature_hex)
        public_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
        public_key.verify(sig_bytes, message.encode("utf-8"))
        return True
    except Exception:
        return False


def make_signable_string(fields: dict) -> str:
    """Create a deterministic string from fields for signing.

    Joins key=value pairs sorted by key, separated by '|'.
    This ensures both signer and verifier produce the same input.
    """
    parts = []
    for key in sorted(fields.keys()):
        parts.append(f"{key}={fields[key]}")
    return "|".join(parts)
