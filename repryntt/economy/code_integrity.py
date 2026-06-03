"""
Code Integrity Protection for SAIGE Blockchain

Prevents malicious modification of blockchain node code.
Run this check on every node startup.
"""

import hashlib
import json
import os
import sys
from repryntt.economy.logging_config import blockchain_logger

# Files to protect
PROTECTED_FILES = [
    'qnode2.py',
    'transaction.py',
    'proof_of_power.py',
    'smartcontracts.py',
    'dao.py',
]

# Checksums file path (persisted alongside code)
_CHECKSUMS_FILE = os.path.join(os.path.dirname(__file__), 'OFFICIAL_CHECKSUMS.json')

# Known good blockchain checkpoints (block_height: block_hash)
# Populated at runtime via add_blockchain_checkpoint() after
# the network's genesis stabilizes.  Empty = no checkpoint enforcement.
BLOCKCHAIN_CHECKPOINTS = {}


def _load_checksums() -> dict:
    """Load persisted checksums from disk."""
    if os.path.exists(_CHECKSUMS_FILE):
        try:
            with open(_CHECKSUMS_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            blockchain_logger.warning(f"Could not load checksums file: {e}")
    return {}


def _save_checksums(checksums: dict):
    """Persist checksums to disk."""
    try:
        with open(_CHECKSUMS_FILE, 'w') as f:
            json.dump(checksums, f, indent=2)
    except OSError as e:
        blockchain_logger.error(f"Could not save checksums: {e}")


def calculate_file_checksum(filepath: str) -> str:
    """Calculate SHA256 checksum of a file"""
    try:
        with open(filepath, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception as e:
        blockchain_logger.error(f"Failed to calculate checksum for {filepath}: {e}")
        return None


def verify_code_integrity(strict_mode: bool = False) -> bool:
    """
    Verify blockchain code hasn't been tampered with.
    
    Checksums are loaded from OFFICIAL_CHECKSUMS.json on disk.
    On first run, records current checksums and persists them.
    
    Args:
        strict_mode: If True, exit on checksum mismatch. If False, just warn.
        
    Returns:
        True if all checksums match, False otherwise
    """
    robot_economy_dir = os.path.dirname(__file__)
    all_valid = True
    stored = _load_checksums()
    updated = False
    
    blockchain_logger.info("🔒 Verifying code integrity...")
    
    for filename in PROTECTED_FILES:
        filepath = os.path.join(robot_economy_dir, filename)
        
        if not os.path.exists(filepath):
            blockchain_logger.error(f"❌ CRITICAL: {filename} is MISSING!")
            all_valid = False
            continue
        
        actual_checksum = calculate_file_checksum(filepath)
        expected_checksum = stored.get(filename)
        
        if expected_checksum is None:
            # First run - store the checksum
            blockchain_logger.info(f"📝 Recording checksum for {filename}: {actual_checksum[:16]}...")
            stored[filename] = actual_checksum
            updated = True
            continue
        
        if actual_checksum != expected_checksum:
            blockchain_logger.error(f"❌ CODE TAMPERED: {filename}")
            blockchain_logger.error(f"   Expected: {expected_checksum[:16]}...")
            blockchain_logger.error(f"   Actual:   {actual_checksum[:16]}...")
            all_valid = False
            
            if strict_mode:
                blockchain_logger.error("❌ EXITING due to code tampering (strict mode)")
                sys.exit(1)
    
    if updated:
        _save_checksums(stored)
    
    if all_valid:
        blockchain_logger.info("✅ Code integrity verified - all files match official checksums")
    else:
        blockchain_logger.warning("⚠️  Code integrity check FAILED - running potentially modified code")
    
    return all_valid


def verify_blockchain_checkpoints(chain: list) -> bool:
    """
    Verify blockchain matches known good checkpoints.
    
    Args:
        chain: The blockchain to verify
        
    Returns:
        True if all checkpoints match, False otherwise
    """
    all_valid = True
    checkpoints_verified = 0
    
    blockchain_logger.info("🔒 Verifying blockchain checkpoints...")
    
    for block_height, expected_hash in BLOCKCHAIN_CHECKPOINTS.items():
        if expected_hash is None:
            continue  # Checkpoint not set yet
        
        if block_height >= len(chain):
            continue  # Haven't reached this checkpoint yet
        
        actual_hash = chain[block_height].hash if hasattr(chain[block_height], 'hash') else chain[block_height].get('hash')
        
        if actual_hash != expected_hash:
            blockchain_logger.error(f"❌ BLOCKCHAIN TAMPERED: Checkpoint {block_height} mismatch!")
            blockchain_logger.error(f"   Expected: {expected_hash[:16]}...")
            blockchain_logger.error(f"   Actual:   {actual_hash[:16]}...")
            all_valid = False
        else:
            checkpoints_verified += 1
    
    if checkpoints_verified == 0:
        blockchain_logger.info("✅ Blockchain checkpoints skipped (no checkpoints configured for this network)")
    elif all_valid:
        blockchain_logger.info(f"✅ Blockchain checkpoints verified ({checkpoints_verified} checkpoints)")
    else:
        blockchain_logger.error("❌ BLOCKCHAIN CHECKPOINT FAILURE - chain has diverged from official network")
    
    return all_valid


def set_checkpoint(block_height: int, block_hash: str):
    """
    Set a blockchain checkpoint (admin only).
    
    Args:
        block_height: Block number
        block_hash: Hash of that block
    """
    BLOCKCHAIN_CHECKPOINTS[block_height] = block_hash
    blockchain_logger.info(f"📌 Set checkpoint: Block {block_height} = {block_hash[:16]}...")


def enable_read_only_mode():
    """
    Make code files read-only (requires root).
    WARNING: You'll need root to update code after this!
    """
    import subprocess
    import sys as _sys_ci
    robot_economy_dir = os.path.dirname(__file__)
    
    try:
        for filename in _load_checksums().keys():
            filepath = os.path.join(robot_economy_dir, filename)
            if _sys_ci.platform == "win32":
                # Windows: set read-only attribute
                subprocess.run(['attrib', '+R', filepath], check=True)
            else:
                # Unix: make files read-only, owned by root
                subprocess.run(['sudo', 'chmod', '444', filepath], check=True)
                subprocess.run(['sudo', 'chown', 'root:root', filepath], check=True)
        
        blockchain_logger.info("🔒 Code files locked to read-only mode (root access required to modify)")
        return True
    except Exception as e:
        blockchain_logger.error(f"Failed to enable read-only mode: {e}")
        return False


def generate_official_checksums():
    """
    Generate checksums for the current code (run this on official release).
    Persists to OFFICIAL_CHECKSUMS.json.
    """
    robot_economy_dir = os.path.dirname(__file__)
    checksums = {}
    
    for filename in PROTECTED_FILES:
        filepath = os.path.join(robot_economy_dir, filename)
        checksum = calculate_file_checksum(filepath)
        checksums[filename] = checksum
        print(f"{filename}: {checksum}")
    
    _save_checksums(checksums)
    
    print(f"\n✅ Checksums saved to {_CHECKSUMS_FILE}")
    print("⚠️  SIGN THIS FILE with your private key and distribute with releases!")


if __name__ == "__main__":
    # Test the integrity check
    print("Testing code integrity verification...\n")
    
    if verify_code_integrity(strict_mode=False):
        print("✅ All code integrity checks passed")
    else:
        print("❌ Code integrity check FAILED")
    
    print("\n" + "="*60)
    print("To generate official checksums for distribution:")
    print("  python3 code_integrity.py --generate")
    print("\nTo enable read-only mode (requires root):")
    print("  python3 code_integrity.py --lock")
    print("="*60)
