"""
Node Wallet — One canonical wallet per node, like Bitcoin's wallet.dat.

Design principles (Satoshi approach):
  1. ONE wallet per node, at ~/.repryntt/wallet/node_wallet.json
  2. Generated on first start — seed phrase displayed clearly
  3. Mnemonic stored encrypted (AES-256-GCM) in the wallet file —
     the operator can ALWAYS export their keys later
  4. Private key derived on load if password is available
  5. No silent key generation — every creation logs prominently

Like Bitcoin's wallet.dat: the keys live in the wallet file, encrypted.
The operator doesn't have to write anything down to keep using their
wallet — but they SHOULD back up their seed phrase for disaster recovery.

Encryption password priority:
  1. REPRYNTT_WALLET_PASSWORD env var
  2. Machine-derived default (deterministic, unique per install)

The machine-derived default means the wallet "just works" on the same
machine without any config, but is useless if the wallet file is copied
to a different machine without knowing the password.  For real security,
set REPRYNTT_WALLET_PASSWORD to your own passphrase.

Usage:
  from repryntt.economy.node_wallet import get_node_wallet
  wallet = get_node_wallet()
  print(wallet.address)  # 40-char hex
  wallet.sign(b"message")  # Ed25519 signature — always works
"""

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("repryntt.economy.node_wallet")

from repryntt.paths import get_data_dir as _get_data_dir

NODE_WALLET_PATH = _get_data_dir() / "wallet" / "node_wallet.json"
SEED_BACKUP_ENV = "REPRYNTT_WRITE_SEED_BACKUP"

# Banner for seed phrase display — impossible to miss
_SEED_BANNER = """
╔══════════════════════════════════════════════════════════════════════╗
║                    🔐 NODE WALLET CREATED 🔐                       ║
╠══════════════════════════════════════════════════════════════════════╣
║                                                                      ║
║  Your 24-word seed phrase (back this up for disaster recovery):      ║
║                                                                      ║
║  {phrase_lines}
║                                                                      ║
║  Address: {address}                                 ║
║                                                                      ║
║  Your keys are stored encrypted in the wallet file.                  ║
║  Export anytime with: repryntt-wallet export                         ║
║                                                                      ║
║  ⚠️  Anyone with this phrase controls your wallet.                   ║
║  ⚠️  Store the backup offline. Never share it.                       ║
║                                                                      ║
╚══════════════════════════════════════════════════════════════════════╝
"""


class NodeWallet:
    """Canonical wallet for this blockchain node.

    Attributes:
        address: 40-char hex wallet address
        public_key: Ed25519 public key bytes (32 bytes)
        private_key: Ed25519 private key bytes (32 bytes) — only available
                     if mnemonic was provided or wallet was just created
    """

    __slots__ = ("address", "public_key", "private_key", "_mnemonic")

    def __init__(
        self,
        address: str,
        public_key: bytes,
        private_key: Optional[bytes] = None,
        mnemonic: Optional[str] = None,
    ):
        self.address = address
        self.public_key = public_key
        self.private_key = private_key
        self._mnemonic = mnemonic  # held in memory only, never persisted

    def can_sign(self) -> bool:
        """True if this wallet has the private key loaded for signing."""
        return self.private_key is not None

    def sign(self, message: bytes) -> bytes:
        """Sign a message with the node's Ed25519 private key."""
        if not self.private_key:
            raise RuntimeError(
                "Node wallet private key not loaded. "
                "Recover with: REPRYNTT_WALLET_MNEMONIC='your 24 words'"
            )
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.from_private_bytes(self.private_key)
        return key.sign(message)


# ── Module-level singleton ──────────────────────────────────────────

_wallet: Optional[NodeWallet] = None
_lock = threading.Lock()


def get_node_wallet() -> Optional[NodeWallet]:
    """Return the canonical node wallet (lazy-loads or creates on first call).

    On first start:
      - Generates a new Ed25519 wallet
      - Displays the 24-word seed phrase in a banner
      - Saves address, public key, and encrypted mnemonic to disk
      - Private key is available immediately

    On subsequent starts:
      - Loads from disk, decrypts mnemonic, derives private key
      - Signing always works (no manual mnemonic entry needed)
    """
    global _wallet
    if _wallet is not None:
        return _wallet

    with _lock:
        if _wallet is not None:
            return _wallet

        if NODE_WALLET_PATH.exists():
            _wallet = _load_existing()
        else:
            _wallet = _create_new()
        _harden_wallet_permissions()

    return _wallet


def _chmod_best_effort(path: Path, mode: int) -> None:
    try:
        if path.exists():
            path.chmod(mode)
    except Exception as e:
        logger.warning(f"Could not set permissions on {path}: {e}")


def _harden_wallet_permissions() -> None:
    """Best-effort owner-only permissions for node wallet secrets."""
    _chmod_best_effort(NODE_WALLET_PATH.parent, 0o700)
    _chmod_best_effort(NODE_WALLET_PATH, 0o600)
    _chmod_best_effort(
        NODE_WALLET_PATH.parent / "SEED_PHRASE_DELETE_AFTER_SAVING.txt",
        0o600,
    )


def _get_wallet_password() -> str:
    """Get the wallet encryption password.

    Priority:
      1. REPRYNTT_WALLET_PASSWORD env var (operator-chosen, strongest)
      2. Machine-derived default (works automatically on this machine)

    The machine-derived default is a SHA3-256 hash of the machine-id +
    username + a domain separator.  This means:
      - Wallet works out of the box on the same machine (no config)
      - If someone copies the wallet file, they can't decrypt without
        knowing the password OR having access to this machine's identity
      - For real security, set your own REPRYNTT_WALLET_PASSWORD
    """
    explicit = os.environ.get("REPRYNTT_WALLET_PASSWORD", "").strip()
    if explicit:
        return explicit

    # Machine-derived default: deterministic, unique per install
    import getpass

    machine_id = "unknown"
    try:
        # Linux: /etc/machine-id is unique per install
        mid_path = Path("/etc/machine-id")
        if mid_path.exists():
            machine_id = mid_path.read_text().strip()
    except Exception:
        pass

    username = getpass.getuser()
    material = f"repryntt-node-wallet:{machine_id}:{username}:v1"
    return hashlib.sha3_256(material.encode()).hexdigest()


def _load_existing() -> Optional[NodeWallet]:
    """Load an existing node wallet from disk, decrypt mnemonic, derive keys."""
    try:
        data = json.loads(NODE_WALLET_PATH.read_text())
        address = data["address"]
        public_key = bytes.fromhex(data["public_key"])

        # Verify address matches public key (don't trust, verify)
        expected_addr = hashlib.sha3_256(public_key).hexdigest()[:40]
        if expected_addr != address:
            logger.error(
                "Node wallet integrity check FAILED: "
                f"address {address} does not match public key → {expected_addr}"
            )
            return None

        logger.info(f"🔐 Node wallet loaded: {address[:16]}...")

        # Decrypt mnemonic and derive private key
        private_key = None
        mnemonic = None
        encrypted_mnemonic = data.get("encrypted_mnemonic")

        if encrypted_mnemonic:
            password = _get_wallet_password()
            try:
                from repryntt.economy.crypto_utils import crypto_utils

                decrypted = crypto_utils.decrypt_data(
                    encrypted_mnemonic.encode(), password
                )
                mnemonic = decrypted.decode()
                private_key, recovered_addr = _derive_from_mnemonic(mnemonic)
                if recovered_addr != address:
                    logger.error(
                        "Decrypted mnemonic does not match wallet address! "
                        f"Expected {address[:16]}..., got "
                        f"{recovered_addr[:16] if recovered_addr else 'None'}..."
                    )
                    private_key = None
                    mnemonic = None
                else:
                    logger.info("🔐 Private key decrypted — signing enabled")
            except Exception as e:
                logger.warning(
                    f"Could not decrypt wallet mnemonic: {e} — "
                    "set REPRYNTT_WALLET_PASSWORD or REPRYNTT_WALLET_MNEMONIC"
                )

        # Fallback: explicit mnemonic via env var
        if not private_key:
            env_mnemonic = os.environ.get("REPRYNTT_WALLET_MNEMONIC", "").strip()
            if env_mnemonic:
                private_key, recovered_addr = _derive_from_mnemonic(env_mnemonic)
                if recovered_addr == address:
                    mnemonic = env_mnemonic
                    logger.info(
                        "🔐 Private key loaded from REPRYNTT_WALLET_MNEMONIC"
                    )
                else:
                    logger.error(
                        "REPRYNTT_WALLET_MNEMONIC does not match node wallet!"
                    )
                    private_key = None

        return NodeWallet(
            address=address,
            public_key=public_key,
            private_key=private_key,
            mnemonic=mnemonic,
        )
    except Exception as e:
        logger.error(f"Failed to load node wallet: {e}")
        return None


def _create_new() -> Optional[NodeWallet]:
    """Generate a brand-new node wallet, store encrypted, display seed phrase."""
    try:
        from repryntt.economy.crypto_utils import crypto_utils

        # Generate wallet
        address, mnemonic = crypto_utils.generate_wallet_seed()
        priv_bytes, pub_bytes = crypto_utils.derive_private_key_from_mnemonic(
            mnemonic, kdf_version=3
        )
        if not priv_bytes or not pub_bytes:
            logger.error("Wallet key derivation failed")
            return None

        # Verify round-trip
        verify_addr = hashlib.sha3_256(pub_bytes).hexdigest()[:40]
        if verify_addr != address:
            logger.error("Wallet address verification failed after generation")
            return None

        # Encrypt the mnemonic for persistent storage
        password = _get_wallet_password()
        encrypted_mnemonic = crypto_utils.encrypt_data(
            mnemonic.encode(), password
        ).decode()

        # Save to disk — address, public key, AND encrypted mnemonic
        NODE_WALLET_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        wallet_data = {
            "address": address,
            "public_key": pub_bytes.hex(),
            "encrypted_mnemonic": encrypted_mnemonic,
            "created_at": __import__("time").time(),
            "format_version": 2,
        }
        NODE_WALLET_PATH.write_text(json.dumps(wallet_data, indent=2))
        _harden_wallet_permissions()

        # Display the seed phrase — prominently, unmissably
        words = mnemonic.split()
        phrase_lines = ""
        for row in range(6):
            parts = []
            for col in range(4):
                idx = col * 6 + row
                if idx < len(words):
                    parts.append(f"{idx + 1:2d}. {words[idx]:<12s}")
            phrase_lines += "║  " + "  ".join(parts) + "\n"

        banner = _SEED_BANNER.format(
            phrase_lines=phrase_lines.rstrip(),
            address=address,
        )

        import sys

        print(banner, file=sys.stderr, flush=True)
        logger.warning(
            "Node wallet created: %s. Seed phrase displayed on stderr only.",
            address,
        )

        if os.environ.get(SEED_BACKUP_ENV) == "1":
            seed_once_path = NODE_WALLET_PATH.parent / "SEED_PHRASE_DELETE_AFTER_SAVING.txt"
            seed_once_path.write_text(
                f"Node Wallet Seed Phrase (24 words):\n\n"
                f"{mnemonic}\n\n"
                f"Address: {address}\n\n"
                f"Your keys are stored encrypted in the wallet file.\n"
                f"Export anytime: repryntt-wallet export\n\n"
                f"This file is an extra backup. Delete after saving offline.\n"
            )
            _chmod_best_effort(seed_once_path, 0o600)
            logger.warning(
                "Seed phrase backup written with owner-only permissions: %s",
                seed_once_path,
            )

        return NodeWallet(
            address=address,
            public_key=pub_bytes,
            private_key=priv_bytes,
            mnemonic=mnemonic,
        )
    except Exception as e:
        logger.error(f"Failed to create node wallet: {e}")
        return None


def _derive_from_mnemonic(mnemonic: str) -> Tuple[Optional[bytes], Optional[str]]:
    """Derive private key and address from a mnemonic phrase.

    Tries KDF v3 first (current), falls back to v2 and v1 for legacy wallets.
    Returns (private_key_bytes, address) or (None, None).
    """
    from repryntt.economy.crypto_utils import crypto_utils

    for kdf_version in (3, 2, 1):
        try:
            priv, pub = crypto_utils.derive_private_key_from_mnemonic(
                mnemonic, kdf_version=kdf_version
            )
            if priv and pub:
                address = hashlib.sha3_256(pub).hexdigest()[:40]
                return priv, address
        except Exception:
            continue
    return None, None


def dump_wallet_info() -> dict:
    """Return wallet info for export/display (never includes private key)."""
    wallet = get_node_wallet()
    if not wallet:
        return {"error": "No node wallet"}
    return {
        "address": wallet.address,
        "public_key": wallet.public_key.hex(),
        "can_sign": wallet.can_sign(),
        "wallet_file": str(NODE_WALLET_PATH),
    }


def export_seed_phrase(password: Optional[str] = None) -> Optional[str]:
    """Decrypt and return the seed phrase from the wallet file.

    Args:
        password: Explicit password.  If None, uses the same resolution as
                  normal wallet loading (_get_wallet_password).

    Returns:
        The 24-word mnemonic string, or None if decryption fails.
    """
    if not NODE_WALLET_PATH.exists():
        logger.error("No wallet file to export from")
        return None

    try:
        data = json.loads(NODE_WALLET_PATH.read_text())
        encrypted = data.get("encrypted_mnemonic")
        if not encrypted:
            logger.error(
                "Wallet file has no encrypted_mnemonic (format_version 1). "
                "Run migrate_v1_wallet() first."
            )
            return None

        from repryntt.economy.crypto_utils import crypto_utils

        pw = password or _get_wallet_password()
        return crypto_utils.decrypt_data(encrypted.encode(), pw).decode()
    except Exception as e:
        logger.error(f"Failed to export seed phrase: {e}")
        return None


def migrate_v1_wallet() -> bool:
    """Migrate a format_version 1 wallet to v2 (add encrypted mnemonic).

    Looks for the seed phrase in:
      1. SEED_PHRASE_DELETE_AFTER_SAVING.txt (the temp backup file)
      2. REPRYNTT_WALLET_MNEMONIC env var

    Returns True if migration succeeded or wallet is already v2+.
    """
    if not NODE_WALLET_PATH.exists():
        return False

    try:
        data = json.loads(NODE_WALLET_PATH.read_text())
        if data.get("format_version", 1) >= 2 and data.get("encrypted_mnemonic"):
            return True  # Already migrated

        address = data["address"]

        # Find the mnemonic
        mnemonic = None
        seed_file = NODE_WALLET_PATH.parent / "SEED_PHRASE_DELETE_AFTER_SAVING.txt"
        if seed_file.exists():
            text = seed_file.read_text()
            # Extract the mnemonic line (second non-empty line after header)
            for line in text.splitlines():
                words = line.strip().split()
                if len(words) >= 20:  # 24-word phrase
                    mnemonic = line.strip()
                    break

        if not mnemonic:
            mnemonic = os.environ.get("REPRYNTT_WALLET_MNEMONIC", "").strip()

        if not mnemonic:
            logger.error(
                "Cannot migrate v1 wallet: no mnemonic source found. "
                "Set REPRYNTT_WALLET_MNEMONIC or restore the seed phrase file."
            )
            return False

        # Verify mnemonic matches this wallet
        _, recovered_addr = _derive_from_mnemonic(mnemonic)
        if recovered_addr != address:
            logger.error(
                f"Mnemonic does not match wallet address {address[:16]}... "
                f"(derived {recovered_addr[:16] if recovered_addr else 'None'}...)"
            )
            return False

        # Encrypt and store
        from repryntt.economy.crypto_utils import crypto_utils

        password = _get_wallet_password()
        encrypted = crypto_utils.encrypt_data(mnemonic.encode(), password).decode()

        data["encrypted_mnemonic"] = encrypted
        data["format_version"] = 2
        NODE_WALLET_PATH.write_text(json.dumps(data, indent=2))

        logger.info(
            f"✅ Migrated node wallet to v2 — encrypted mnemonic stored. "
            f"Signing now works automatically."
        )
        return True
    except Exception as e:
        logger.error(f"Wallet migration failed: {e}")
        return False
