"""
Personal wallet — human-only CR holdings.

Separate from the node wallet:
  - Node wallet: AI + system use for mining, workloads, fees
  - Personal wallet: Human-only, no AI tool access, your money

The private key is derived from the mnemonic at runtime (never stored
raw on disk).  The mnemonic is AES-256-GCM encrypted with a password
you choose — NOT the machine-derived default the node wallet uses.

Usage:
    repryntt wallet create          Create a new personal wallet
    repryntt wallet show            Show address + balance
    repryntt wallet withdraw <amt>  Transfer CR from node wallet → personal
    repryntt wallet send <to> <amt> Send CR from personal wallet
    repryntt wallet export          Show seed phrase (careful!)
"""

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("repryntt.economy.personal_wallet")

# Personal wallet lives alongside node wallet but separate file
from repryntt.paths import get_data_dir as _get_data_dir

PERSONAL_WALLET_DIR = _get_data_dir() / "wallet"
PERSONAL_WALLET_PATH = PERSONAL_WALLET_DIR / "personal_wallet.json"


class PersonalWallet:
    """Human-controlled wallet — AI agents cannot access this."""

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
        self._mnemonic = mnemonic

    def can_sign(self) -> bool:
        return self.private_key is not None

    def sign(self, message: bytes) -> bytes:
        if not self.private_key:
            raise RuntimeError(
                "Personal wallet private key not loaded. "
                "Unlock with your wallet password."
            )
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        key = Ed25519PrivateKey.from_private_bytes(self.private_key)
        return key.sign(message)


# ── Wallet creation ─────────────────────────────────────────────────


def _get_personal_password(prompt: str = "Wallet password: ") -> str:
    """Prompt the human for their wallet password.

    This is NOT the machine-derived default — the human must remember it.
    """
    import getpass
    return getpass.getpass(prompt)


def _derive_from_mnemonic(mnemonic: str) -> Tuple[Optional[bytes], Optional[str]]:
    """Derive Ed25519 keypair from a BIP39-style mnemonic."""
    try:
        from repryntt.economy.crypto_utils import crypto_utils
        priv_bytes, pub_bytes = crypto_utils.derive_private_key_from_mnemonic(
            mnemonic, kdf_version=3
        )
        if not pub_bytes:
            return None, None
        address = hashlib.sha3_256(pub_bytes).hexdigest()[:40]
        return priv_bytes, address
    except Exception as e:
        logger.error(f"Mnemonic derivation failed: {e}")
        return None, None


def create_personal_wallet(password: str) -> Optional[PersonalWallet]:
    """Generate a brand-new personal wallet with human-chosen password."""
    if PERSONAL_WALLET_PATH.exists():
        logger.error("Personal wallet already exists. Delete it first if you want a new one.")
        return None

    try:
        from repryntt.economy.crypto_utils import crypto_utils

        address, mnemonic = crypto_utils.generate_wallet_seed()
        priv_bytes, pub_bytes = crypto_utils.derive_private_key_from_mnemonic(
            mnemonic, kdf_version=3
        )
        if not priv_bytes or not pub_bytes:
            logger.error("Key derivation failed")
            return None

        # Verify round-trip
        verify_addr = hashlib.sha3_256(pub_bytes).hexdigest()[:40]
        if verify_addr != address:
            logger.error("Address verification failed")
            return None

        # Encrypt mnemonic with the HUMAN-CHOSEN password
        encrypted_mnemonic = crypto_utils.encrypt_data(
            mnemonic.encode(), password
        ).decode()

        # Save wallet
        PERSONAL_WALLET_DIR.mkdir(parents=True, exist_ok=True)
        wallet_data = {
            "address": address,
            "public_key": pub_bytes.hex(),
            "encrypted_mnemonic": encrypted_mnemonic,
            "created_at": time.time(),
            "format_version": 2,
            "wallet_type": "personal",
        }
        PERSONAL_WALLET_PATH.write_text(json.dumps(wallet_data, indent=2))

        logger.info(f"Personal wallet created: {address}")
        return PersonalWallet(
            address=address,
            public_key=pub_bytes,
            private_key=priv_bytes,
            mnemonic=mnemonic,
        )
    except Exception as e:
        logger.error(f"Failed to create personal wallet: {e}")
        return None


def load_personal_wallet(password: str) -> Optional[PersonalWallet]:
    """Load and unlock the personal wallet with the human's password."""
    if not PERSONAL_WALLET_PATH.exists():
        return None

    try:
        data = json.loads(PERSONAL_WALLET_PATH.read_text())
        address = data["address"]
        public_key = bytes.fromhex(data["public_key"])

        # Integrity check
        expected_addr = hashlib.sha3_256(public_key).hexdigest()[:40]
        if expected_addr != address:
            logger.error("Personal wallet integrity check FAILED")
            return None

        # Decrypt mnemonic with human's password
        encrypted_mnemonic = data.get("encrypted_mnemonic")
        private_key = None
        mnemonic = None

        if encrypted_mnemonic:
            from repryntt.economy.crypto_utils import crypto_utils
            try:
                decrypted = crypto_utils.decrypt_data(
                    encrypted_mnemonic.encode(), password
                )
                mnemonic = decrypted.decode()
                private_key, recovered_addr = _derive_from_mnemonic(mnemonic)
                if recovered_addr != address:
                    logger.error("Wrong password — decrypted mnemonic doesn't match wallet")
                    return None
            except Exception:
                logger.error("Wrong password or corrupted wallet")
                return None

        return PersonalWallet(
            address=address,
            public_key=public_key,
            private_key=private_key,
            mnemonic=mnemonic,
        )
    except Exception as e:
        logger.error(f"Failed to load personal wallet: {e}")
        return None


def get_personal_address() -> Optional[str]:
    """Get the personal wallet address without unlocking (no password needed)."""
    if not PERSONAL_WALLET_PATH.exists():
        return None
    try:
        data = json.loads(PERSONAL_WALLET_PATH.read_text())
        return data.get("address")
    except Exception:
        return None


def personal_wallet_exists() -> bool:
    return PERSONAL_WALLET_PATH.exists()


# ── Transfer helpers ────────────────────────────────────────────────


def withdraw_from_node(amount_cr: float, password: str) -> dict:
    """Transfer CR from the node wallet to the personal wallet.

    This creates a signed 'transfer' transaction and submits it
    to the local blockchain node.
    """
    personal = load_personal_wallet(password)
    if not personal:
        return {"success": False, "error": "Could not unlock personal wallet"}

    from repryntt.economy.node_wallet import get_node_wallet
    node = get_node_wallet()
    if not node or not node.can_sign():
        return {"success": False, "error": "Node wallet not available or locked"}

    amount_plancks = int(amount_cr * 100_000_000)
    if amount_plancks <= 0:
        return {"success": False, "error": "Amount must be positive"}

    return _submit_transfer(
        from_wallet=node,
        to_address=personal.address,
        amount_plancks=amount_plancks,
    )


def send_from_personal(to_address: str, amount_cr: float, password: str) -> dict:
    """Send CR from the personal wallet to any address."""
    personal = load_personal_wallet(password)
    if not personal:
        return {"success": False, "error": "Could not unlock personal wallet"}
    if not personal.can_sign():
        return {"success": False, "error": "Personal wallet has no signing key"}

    amount_plancks = int(amount_cr * 100_000_000)
    if amount_plancks <= 0:
        return {"success": False, "error": "Amount must be positive"}

    # Basic address validation
    if not to_address or len(to_address) != 40:
        return {"success": False, "error": "Invalid destination address (must be 40 hex chars)"}
    try:
        int(to_address, 16)
    except ValueError:
        return {"success": False, "error": "Invalid destination address (not valid hex)"}

    return _submit_transfer(
        from_wallet=personal,
        to_address=to_address,
        amount_plancks=amount_plancks,
    )


def _submit_transfer(from_wallet, to_address: str, amount_plancks: int) -> dict:
    """Submit a signed transfer transaction to the local blockchain node."""
    import socket as sock

    from repryntt.economy.transaction import Transaction

    # Create and sign the transaction
    tx = Transaction(
        from_address=from_wallet.address,
        to_address=to_address,
        amount=amount_plancks,
        tx_type="transfer",
        nonce=0,  # Node will validate
        public_key=from_wallet.public_key,
        metadata={"fee": 0},
    )
    tx.sign(from_wallet.private_key)

    # Submit to local node via TCP
    msg = json.dumps({
        "type": "transfer",
        "from_address": from_wallet.address,
        "to_address": to_address,
        "amount_plancks": amount_plancks,
        "signature": tx.signature.hex(),
        "public_key": from_wallet.public_key.hex(),
        "nonce": 0,
    }).encode()

    try:
        s = sock.socket(sock.AF_INET, sock.SOCK_STREAM)
        s.settimeout(10)
        s.connect(("127.0.0.1", 5001))

        # Send length-prefixed message
        s.sendall(len(msg).to_bytes(4, "big") + msg)

        # Read response
        header = b""
        while len(header) < 4:
            chunk = s.recv(4 - len(header))
            if not chunk:
                break
            header += chunk

        if len(header) == 4:
            resp_len = int.from_bytes(header, "big")
            resp_data = b""
            while len(resp_data) < resp_len:
                chunk = s.recv(min(4096, resp_len - len(resp_data)))
                if not chunk:
                    break
                resp_data += chunk
            result = json.loads(resp_data.decode())
        else:
            result = {"success": False, "error": "No response from node"}

        s.close()
        return result
    except ConnectionRefusedError:
        return {"success": False, "error": "Blockchain node not running (port 5001)"}
    except Exception as e:
        return {"success": False, "error": f"Transfer failed: {e}"}
