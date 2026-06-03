"""
SAIGE Machine Identity
══════════════════════
Persistent, auto-generated identity for every SAIGE installation.

- Created ONCE on first run, never changes
- Shared across all subsystems (P2P, daemon, blockchain, brain)
- Survives state resets, reinstalls (unless explicitly deleted)
- Human-readable name + cryptographic ID

Identity file: SAIGE_ROOT/saige_identity.json
"""

import hashlib
import json
import os
import platform
import time
import uuid
from pathlib import Path

# Identity lives in the SAIGE root directory, NOT in transient state dirs
_IDENTITY_FILE = Path(__file__).parent / "saige_identity.json"

# Cached identity so we only read from disk once per process
_cached_identity = None


def _generate_machine_fingerprint() -> str:
    """
    Generate a deterministic fingerprint from hardware characteristics.
    Uses platform-specific identity sources via platform_utils.
    """
    from repryntt.platform_utils import get_machine_fingerprint_parts
    parts = get_machine_fingerprint_parts()
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _auto_name() -> str:
    """Generate a human-friendly node name."""
    hostname = platform.node() or "unknown"
    from repryntt.platform_utils import detect_device_type
    device_type = detect_device_type()
    return f"saige-{device_type}-{hostname}"


def get_identity(identity_file: Path = None) -> dict:
    """
    Get or create the machine identity.
    
    Returns dict with:
      - machine_id: str — permanent UUID for this installation
      - fingerprint: str — hardware-derived fingerprint (secondary)
      - node_name: str — human-readable name
      - created_at: float — timestamp of first creation
      - created_at_human: str — ISO timestamp
      - role: str — "bootstrap" for genesis nodes, "node" for others
      - platform: dict — hardware info snapshot
      - version: int — identity schema version
    
    Thread-safe: reads file once, caches in memory.
    """
    global _cached_identity
    if _cached_identity is not None:
        return _cached_identity
    
    fpath = identity_file or _IDENTITY_FILE
    
    # Try to load existing identity
    if fpath.exists():
        try:
            with open(fpath) as f:
                identity = json.load(f)
            # Validate it has the essential fields
            if "machine_id" in identity and "node_name" in identity:
                _cached_identity = identity
                return identity
        except Exception:
            pass  # Corrupted file — regenerate
    
    # Generate new identity
    identity = {
        "machine_id": str(uuid.uuid4()),
        "fingerprint": _generate_machine_fingerprint(),
        "node_name": _auto_name(),
        "created_at": time.time(),
        "created_at_human": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "role": "node",  # Default; set to "bootstrap" for seed nodes
        "version": 1,
        "platform": {
            "system": platform.system(),
            "arch": platform.machine(),
            "hostname": platform.node(),
            "python": platform.python_version(),
        },
    }
    
    # Detect if this is likely the first/genesis node
    # (has existing agents = existing installation = bootstrap candidate)
    daemon_state = fpath.parent / "brain" / "daemon_state.json"
    if daemon_state.exists():
        try:
            with open(daemon_state) as f:
                data = json.load(f)
            if len(data.get("agents", [])) > 10:
                identity["role"] = "bootstrap"
        except Exception:
            pass
    
    # Save permanently
    try:
        fpath.parent.mkdir(parents=True, exist_ok=True)
        with open(fpath, "w") as f:
            json.dump(identity, f, indent=2)
    except Exception as e:
        import logging
        logging.getLogger("saige.identity").warning(f"Could not persist identity: {e}")
    
    _cached_identity = identity
    return identity


def get_machine_id() -> str:
    """Shortcut: get just the machine UUID."""
    return get_identity()["machine_id"]


def get_node_name() -> str:
    """Shortcut: get just the human-readable node name."""
    return get_identity()["node_name"]


def is_bootstrap_node() -> bool:
    """Check if this node is a bootstrap/seed node."""
    return get_identity().get("role") == "bootstrap"


def set_role(role: str, identity_file: Path = None):
    """Update this node's role (bootstrap, node, relay, etc.)."""
    global _cached_identity
    fpath = identity_file or _IDENTITY_FILE
    identity = get_identity(fpath)
    identity["role"] = role
    try:
        with open(fpath, "w") as f:
            json.dump(identity, f, indent=2)
        _cached_identity = identity
    except Exception:
        pass


def set_node_name(name: str, identity_file: Path = None):
    """Update this node's human-readable name."""
    global _cached_identity
    fpath = identity_file or _IDENTITY_FILE
    identity = get_identity(fpath)
    identity["node_name"] = name
    try:
        with open(fpath, "w") as f:
            json.dump(identity, f, indent=2)
        _cached_identity = identity
    except Exception:
        pass


# ─── CLI ───────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    
    identity = get_identity()
    
    if "--json" in sys.argv:
        print(json.dumps(identity, indent=2))
    else:
        print(f"SAIGE Machine Identity")
        print(f"══════════════════════")
        print(f"  Machine ID:   {identity['machine_id']}")
        print(f"  Fingerprint:  {identity['fingerprint']}")
        print(f"  Node Name:    {identity['node_name']}")
        print(f"  Role:         {identity['role']}")
        print(f"  Created:      {identity['created_at_human']}")
        print(f"  Platform:     {identity['platform']['system']} {identity['platform']['arch']}")
        print(f"  File:         {_IDENTITY_FILE}")
