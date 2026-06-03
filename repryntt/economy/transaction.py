"""
Transaction System for Proof of Power Blockchain (PRODUCTION-GRADE)

Provides formal transaction structure with:
- Full transaction history
- Audit trail
- Transaction validation
- Multiple transaction types (reward, fee, transfer, stake)
- Digital signatures (Ed25519 + post-quantum ML-DSA-44)
- Nonce system (prevents replay attacks)
- Public key cryptography
"""

import hashlib
import json
import time
from datetime import datetime
from typing import Optional, Dict, Any

# Import secure cryptography
from repryntt.economy.secure_crypto import SecureCrypto

# ── Chain identifier ─────────────────────────────────────────────────────
# Included in tx_version>=2 hashes to prevent cross-chain TX replay.
# A fork MUST change this value; otherwise signed TXs are valid on both chains.
CHAIN_ID = "RPNT-mainnet-1"


class Transaction:
    """
    Represents a single transaction in the blockchain (PRODUCTION-GRADE).
    
    Transaction Types:
    - reward: Block reward for successful Proof of Power
    - fee: Workload submission fee
    - transfer: Direct token transfer between addresses
    - stake: Stake deposit/withdrawal for mining eligibility
    - penalty: Stake slashing for invalid work
    
    Security Features:
    - Digital signatures (Ed25519)
    - Nonce (prevents replay attacks)
    - Public key verification
    """
    
    def __init__(
        self,
        from_address: str,
        to_address: str,
        amount: int,  # In Plancks (1 Credit = 100,000,000 Plancks)
        tx_type: str,
        nonce: int = 0,  # NEW: Nonce for replay protection
        metadata: Optional[Dict[str, Any]] = None,
        timestamp: Optional[float] = None,
        public_key: Optional[bytes] = None,  # NEW: Sender's public key
        tx_version: int = 1,  # 1=legacy (no chain_id), 2=chain_id in hash
    ):
        self.from_address = from_address
        self.to_address = to_address
        self.amount = amount
        self.tx_type = tx_type
        self.nonce = nonce  # NEW: Prevents replay attacks
        self.timestamp = timestamp or time.time()
        self.metadata = metadata or {}
        self.public_key = public_key  # NEW: For signature verification
        self.tx_version = tx_version  # Cross-chain replay protection
        self.signature = None  # NEW: Digital signature (set by sign() method)
        self.tx_hash = self.calculate_hash()
    
    def calculate_hash(self) -> str:
        """Calculate SHA3-512 hash of transaction data (excludes signature).
        
        tx_version >= 2 includes CHAIN_ID in the hash, making signed TXs
        invalid on any fork that uses a different CHAIN_ID.
        """
        tx_data = {
            'from': self.from_address,
            'to': self.to_address,
            'amount': self.amount,
            'type': self.tx_type,
            'nonce': self.nonce,  # NEW: Include nonce in hash
            'timestamp': self.timestamp,
            'metadata': self.metadata
        }
        if self.tx_version >= 2:
            tx_data['chain_id'] = CHAIN_ID
        tx_string = json.dumps(tx_data, sort_keys=True)
        return hashlib.sha3_512(tx_string.encode()).hexdigest()
    
    def sign(self, private_key: bytes):
        """
        Sign transaction with sender's private key.
        
        Args:
            private_key: Private key bytes (32 bytes for Ed25519)
        """
        # Hash must be calculated before signing
        if not self.tx_hash:
            self.tx_hash = self.calculate_hash()
        
        # Sign the transaction hash
        tx_hash_bytes = bytes.fromhex(self.tx_hash)
        self.signature = SecureCrypto.sign(tx_hash_bytes, private_key)
        
        print(f"[{datetime.now()}] Transaction signed by {self.from_address[:16]}...")
    
    def verify_signature(self) -> bool:
        """
        Verify transaction signature using public key.
        
        Returns:
            True if signature is valid, False otherwise
        """
        if not self.signature:
            print(f"[{datetime.now()}] Transaction has no signature")
            return False
        
        if not self.public_key:
            print(f"[{datetime.now()}] Transaction has no public key")
            return False
        
        # Verify signature
        tx_hash_bytes = bytes.fromhex(self.tx_hash)
        is_valid = SecureCrypto.verify(tx_hash_bytes, self.signature, self.public_key)
        
        if not is_valid:
            print(f"[{datetime.now()}] Invalid signature from {self.from_address[:16]}...")
        
        return is_valid
    
    def verify_address_matches_pubkey(self) -> bool:
        """
        Verify that from_address matches the public key.
        
        Prevents address spoofing.
        
        Returns:
            True if address matches public key, False otherwise
        """
        if not self.public_key:
            return False
        
        # Derive address from public key
        derived_address = hashlib.sha3_256(self.public_key).hexdigest()[:40]
        
        return self.from_address == derived_address
    
    def validate(self, balances: Dict[str, int], nonces: Optional[Dict[str, int]] = None, require_signature: bool = True) -> tuple[bool, str]:
        """
        Validate transaction against current balances (PRODUCTION-GRADE).
        
        Args:
            balances: Current account balances
            nonces: Current nonces for replay protection (optional for backward compat)
            require_signature: Whether to require digital signature (True for public chain)
        
        Returns:
            (is_valid, error_message)
        """
        # Type validation
        valid_types = ['reward', 'fee', 'transfer', 'stake', 'stake_withdraw', 'penalty', 'faucet', 'workload_completion', 'entity_register']
        if self.tx_type not in valid_types:
            return False, f"Invalid transaction type: {self.tx_type}"
        
        # Amount validation
        if self.amount < 0:
            return False, "Transaction amount cannot be negative"
        
        # Address validation
        if not self.from_address or not self.to_address:
            return False, "Missing from_address or to_address"
        
        # ===== NEW: SIGNATURE VALIDATION (Production Security) =====
        if require_signature and self.tx_type not in ['reward', 'entity_register']:  # Rewards and entity_register use their own crypto proofs
            # Check signature exists
            if not self.signature:
                return False, "Missing transaction signature (required for public chain)"
            
            # Check public key exists
            if not self.public_key:
                return False, "Missing public key (required for signature verification)"
            
            # Verify signature
            if not self.verify_signature():
                return False, "Invalid transaction signature"
            
            # Verify address matches public key (prevents spoofing)
            if not self.verify_address_matches_pubkey():
                return False, "Address does not match public key (possible spoofing attempt)"
        
        # ===== NEW: NONCE VALIDATION (Replay Protection) =====
        if nonces is not None and self.tx_type not in ['reward', 'faucet', 'entity_register']:  # Rewards, faucet, entity_register don't need nonces
            expected_nonce = nonces.get(self.from_address, 0)
            if self.nonce != expected_nonce:
                return False, f"Invalid nonce: expected {expected_nonce}, got {self.nonce} (replay attack prevention)"
        
        # Balance validation (for non-reward and non-faucet transactions)
        if self.tx_type not in ['reward', 'faucet', 'entity_register']:
            sender_balance = balances.get(self.from_address, 0)
            if sender_balance < self.amount:
                return False, f"Insufficient balance: {sender_balance/100000000:.8f} CR < {self.amount/100000000:.8f} CR"
        
        # Specific validations by type
        if self.tx_type == 'stake':
            # Minimum stake requirement: 1 Credit
            if self.amount < 100000000:
                return False, "Minimum stake is 1.0 CR"
        
        return True, ""
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert transaction to dictionary for serialization (PRODUCTION-GRADE)"""
        tx_dict = {
            'from_address': self.from_address,
            'to_address': self.to_address,
            'amount': self.amount,
            'tx_type': self.tx_type,
            'nonce': self.nonce,  # NEW
            'timestamp': self.timestamp,
            'metadata': self.metadata,
            'tx_hash': self.tx_hash,
            'tx_version': self.tx_version,
        }
        
        # Add signature if present (NEW)
        if self.signature:
            tx_dict['signature'] = self.signature.hex()
        
        # Add public key if present (NEW)
        if self.public_key:
            tx_dict['public_key'] = self.public_key.hex()
        
        return tx_dict
    
    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'Transaction':
        """Create Transaction from dictionary (PRODUCTION-GRADE)"""
        # Convert hex strings back to bytes if present
        public_key = None
        if 'public_key' in data and data['public_key']:
            public_key = bytes.fromhex(data['public_key'])
        
        tx = Transaction(
            from_address=data['from_address'],
            to_address=data['to_address'],
            amount=data['amount'],
            tx_type=data['tx_type'],
            nonce=data.get('nonce', 0),  # NEW: Default to 0 for backward compat
            metadata=data.get('metadata', {}),
            timestamp=data.get('timestamp', time.time()),
            public_key=public_key,  # NEW
            tx_version=data.get('tx_version', 1),  # Legacy TXs default to v1 (no chain_id)
        )
        
        # Restore signature if present (NEW)
        if 'signature' in data and data['signature']:
            tx.signature = bytes.fromhex(data['signature'])
        
        # Restore original hash if provided
        if 'tx_hash' in data:
            tx.tx_hash = data['tx_hash']
        
        return tx
    
    def __repr__(self) -> str:
        return (f"Transaction({self.tx_type}: {self.amount/100000000:.8f} CR "
                f"from {self.from_address[:8]}... to {self.to_address[:8]}...)")


class TransactionPool:
    """
    Memory pool for pending transactions waiting to be included in blocks.
    """
    
    def __init__(self):
        self.pending_transactions: Dict[str, Transaction] = {}
    
    def add_transaction(self, tx: Transaction, balances: Dict[str, int], require_signature: bool = True) -> tuple[bool, str]:
        """
        Add transaction to pool if valid.
        
        Args:
            tx: Transaction to add
            balances: Current account balances
            require_signature: Whether to require digital signature (False ONLY for system txns)
        
        Returns:
            (success, message)
        """
        # SECURITY: Only system transaction types may bypass signature validation
        SYSTEM_TX_TYPES = ('reward', 'fee', 'penalty', 'faucet')
        if not require_signature and tx.tx_type not in SYSTEM_TX_TYPES:
            import logging
            logging.getLogger(__name__).warning(
                f"Signature bypass attempted for non-system tx type '{tx.tx_type}' — forcing signature requirement"
            )
            require_signature = True
        
        # Validate transaction
        is_valid, error = tx.validate(balances, require_signature=require_signature)
        if not is_valid:
            return False, f"Invalid transaction: {error}"
        
        # Check for duplicate
        if tx.tx_hash in self.pending_transactions:
            return False, "Transaction already in pool"
        
        # Add to pool
        self.pending_transactions[tx.tx_hash] = tx
        print(f"[{datetime.now()}] Transaction added to pool: {tx}")
        return True, "Transaction added to pool"
    
    def get_transactions(self, max_count: int = 100) -> list[Transaction]:
        """Get transactions from pool for inclusion in next block"""
        transactions = list(self.pending_transactions.values())[:max_count]
        return transactions
    
    def remove_transactions(self, tx_hashes: list[str]):
        """Remove transactions from pool (after inclusion in block)"""
        for tx_hash in tx_hashes:
            self.pending_transactions.pop(tx_hash, None)
    
    def clear(self):
        """Clear all pending transactions"""
        self.pending_transactions.clear()
    
    def size(self) -> int:
        """Get number of pending transactions"""
        return len(self.pending_transactions)


def create_reward_transaction(miner_address: str, amount: int, metadata: Optional[Dict] = None) -> Transaction:
    """Create a reward transaction for successful mining"""
    return Transaction(
        from_address="SYSTEM",
        to_address=miner_address,
        amount=amount,
        tx_type="reward",
        metadata=metadata or {},
        tx_version=2,
    )


def create_fee_transaction(from_address: str, amount: int, metadata: Optional[Dict] = None) -> Transaction:
    """Create a fee transaction for workload submission"""
    return Transaction(
        from_address=from_address,
        to_address="DAO",
        amount=amount,
        tx_type="fee",
        metadata=metadata or {},
        tx_version=2,
    )


def create_transfer_transaction(from_address: str, to_address: str, amount: int) -> Transaction:
    """Create a transfer transaction between addresses"""
    return Transaction(
        from_address=from_address,
        to_address=to_address,
        amount=amount,
        tx_type="transfer",
        metadata={},
        tx_version=2,
    )


def create_stake_transaction(from_address: str, amount: int) -> Transaction:
    """Create a stake deposit transaction"""
    return Transaction(
        from_address=from_address,
        to_address="STAKE_POOL",
        amount=amount,
        tx_type="stake",
        metadata={},
        tx_version=2,
    )


def create_penalty_transaction(from_address: str, amount: int, reason: str) -> Transaction:
    """Create a penalty transaction (stake slashing)"""
    return Transaction(
        from_address=from_address,
        to_address="DAO",
        amount=amount,
        tx_type="penalty",
        metadata={'reason': reason},
        tx_version=2,
    )


if __name__ == "__main__":
    # Test transaction creation and validation
    print("Testing Transaction System...")
    
    # Create test balances
    balances = {
        "miner_abc123": 500000000,  # 5 CR
        "miner_def456": 1000000000,  # 10 CR
        "DAO": 0
    }
    
    # Test reward transaction
    reward_tx = create_reward_transaction("miner_abc123", 1000000000, {'block_height': 100})
    print(f"\n1. Reward Transaction: {reward_tx}")
    print(f"   Valid: {reward_tx.validate(balances)}")
    
    # Test fee transaction
    fee_tx = create_fee_transaction("miner_def456", 1000000, {'workload_key': 'test123'})
    print(f"\n2. Fee Transaction: {fee_tx}")
    print(f"   Valid: {fee_tx.validate(balances)}")
    
    # Test transfer transaction
    transfer_tx = create_transfer_transaction("miner_def456", "miner_abc123", 200000000)
    print(f"\n3. Transfer Transaction: {transfer_tx}")
    print(f"   Valid: {transfer_tx.validate(balances)}")
    
    # Test stake transaction
    stake_tx = create_stake_transaction("miner_abc123", 100000000)
    print(f"\n4. Stake Transaction: {stake_tx}")
    print(f"   Valid: {stake_tx.validate(balances)}")
    
    # Test invalid transaction (insufficient balance)
    invalid_tx = create_transfer_transaction("miner_abc123", "miner_def456", 999999999999)
    print(f"\n5. Invalid Transaction: {invalid_tx}")
    print(f"   Valid: {invalid_tx.validate(balances)}")
    
    # Test transaction pool
    print("\n\nTesting Transaction Pool...")
    pool = TransactionPool()
    pool.add_transaction(reward_tx, balances)
    pool.add_transaction(fee_tx, balances)
    pool.add_transaction(transfer_tx, balances)
    print(f"Pool size: {pool.size()}")
    print(f"Transactions in pool: {pool.get_transactions()}")
