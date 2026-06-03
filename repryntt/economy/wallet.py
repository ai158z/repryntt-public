#!/usr/bin/env python3
"""
Wallet — High-level wallet abstraction for the Robot Economy.

Wraps QuantumCryptoUtils to provide wallet creation, recovery, and key management.
Used by RobotEconomyManager for user-facing wallet operations.
"""

import os
import json
import logging
from datetime import datetime
from typing import Optional, Tuple, Dict, Any

from repryntt.economy.crypto_utils import crypto_utils  # global QuantumCryptoUtils instance


class Wallet:
    """
    Quantum-safe wallet backed by QuantumCryptoUtils (Ed25519 + ML-DSA-44).

    Each Wallet instance represents a single identity:
      - address           (hex string, first 40 chars of sha3-256 of master seed)
      - key_phrase        (24-word BIP-39 mnemonic)
      - private_key       (Ed25519 private key bytes, derived from mnemonic)
      - public_key        (Ed25519 public key bytes)
    """

    WALLET_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'robot_economy_data', 'wallets')

    def __init__(self, storage_dir: str = None):
        self.logger = logging.getLogger("wallet")
        self.address: Optional[str] = None
        self.key_phrase: Optional[str] = None
        self.private_key: Optional[bytes] = None
        self.public_key: Optional[bytes] = None
        self.wallet_type: str = "user"
        self.created_at: Optional[str] = None
        self.storage_dir = storage_dir or self.WALLET_DIR

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def create_wallet(self, wallet_type: str = "user") -> str:
        """
        Generate a brand-new quantum-safe wallet.

        Returns
        -------
        str
            The wallet address.
        """
        self.wallet_type = wallet_type

        # Generate address + mnemonic via crypto_utils
        address, mnemonic = crypto_utils.generate_wallet_seed()
        self.address = address
        self.key_phrase = mnemonic
        self.created_at = datetime.utcnow().isoformat()

        # Derive signing keys from mnemonic
        self._derive_keys(mnemonic)

        self.logger.info(f"Wallet created: {address[:16]}... (type={wallet_type})")
        return address

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def recover_wallet(self, key_phrase: str, kdf_version: int = 1) -> Optional[str]:
        """
        Recover wallet from a 24-word mnemonic phrase.

        Tries the requested *kdf_version* first.  If that yields no match
        and kdf_version==1, also tries v2 (PBKDF2) automatically so that
        wallets created with the newer KDF still recover.

        Returns
        -------
        str or None
            The recovered address, or ``None`` on failure.
        """
        try:
            address = crypto_utils.recover_wallet_from_mnemonic(key_phrase, kdf_version=kdf_version)

            if address is None and kdf_version == 1:
                # Retry with v2 KDF in case wallet was created after the upgrade
                address = crypto_utils.recover_wallet_from_mnemonic(key_phrase, kdf_version=2)

            if address:
                self.address = address
                self.key_phrase = key_phrase
                self._derive_keys(key_phrase, kdf_version=kdf_version)
                self.logger.info(f"Wallet recovered: {address[:16]}...")
                return address
            else:
                self.logger.warning("Wallet recovery failed: invalid mnemonic")
                return None

        except Exception as e:
            self.logger.error(f"Wallet recovery error: {e}")
            return None

    # ------------------------------------------------------------------
    # Key derivation
    # ------------------------------------------------------------------

    def _derive_keys(self, mnemonic: str, kdf_version: int = 1):
        """Derive Ed25519 signing keys from mnemonic via crypto_utils."""
        try:
            priv, pub = crypto_utils.derive_private_key_from_mnemonic(mnemonic, kdf_version=kdf_version)
            if priv and pub:
                self.private_key = priv
                self.public_key = pub
            else:
                self.logger.warning("Key derivation returned None — signing will be unavailable")
        except Exception as e:
            self.logger.warning(f"Key derivation failed (non-fatal): {e}")

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def save(self, directory: str = None) -> bool:
        """Persist wallet metadata to a JSON file (NEVER stores private key)."""
        try:
            save_dir = directory or self.storage_dir
            os.makedirs(save_dir, exist_ok=True)
            path = os.path.join(save_dir, f"{self.address}.json")
            data = {
                "address": self.address,
                "wallet_type": self.wallet_type,
                "created_at": self.created_at or datetime.utcnow().isoformat(),
            }
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            return True
        except Exception as e:
            self.logger.error(f"Wallet save error: {e}")
            return False

    def load(self, address: str, directory: str = None) -> bool:
        """Load wallet metadata from disk (keys are NOT stored — use recover_wallet)."""
        try:
            load_dir = directory or self.storage_dir
            path = os.path.join(load_dir, f"{address}.json")
            if not os.path.exists(path):
                return False
            with open(path, "r") as f:
                data = json.load(f)
            self.address = data.get("address")
            self.wallet_type = data.get("wallet_type", "user")
            self.created_at = data.get("created_at")
            return True
        except Exception as e:
            self.logger.error(f"Wallet load error: {e}")
            return False

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def get_info(self) -> Dict[str, Any]:
        """Return non-sensitive wallet info."""
        return {
            "address": self.address,
            "wallet_type": self.wallet_type,
            "created_at": self.created_at,
            "has_keys": self.private_key is not None,
        }

    def __repr__(self):
        addr = self.address[:16] + "..." if self.address else "None"
        return f"<Wallet address={addr} type={self.wallet_type}>"
