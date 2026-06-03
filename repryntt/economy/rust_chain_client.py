"""
Rust Chain Client — JSON-RPC 2.0 bridge to the Rust blockchain node.

The Rust node (repryntt-core) listens on TCP port 9332 and speaks
wire-framed JSON-RPC 2.0:
    4-byte big-endian length prefix + JSON payload

This module provides a drop-in replacement for BlockchainNodeClient
and _query_node() that speaks JSON-RPC instead of the old msgpack
protocol.  All blockchain reads go through here.
"""

import json
import hashlib
import logging
import os
import socket
import struct
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("repryntt.economy.rust_chain")
PLANCKS_PER_CREDIT = 100_000_000

# Default Rust node address
RUST_RPC_HOST = os.environ.get("REPRYNTT_RUST_RPC_HOST", "127.0.0.1")
RUST_RPC_PORT = int(os.environ.get("REPRYNTT_RUST_RPC_PORT", "9332"))

_request_id_counter = 0
_id_lock = threading.Lock()


def _next_id() -> int:
    global _request_id_counter
    with _id_lock:
        _request_id_counter += 1
        return _request_id_counter


def rpc_call(method: str, params: Optional[dict] = None,
             host: str = RUST_RPC_HOST, port: int = RUST_RPC_PORT,
             timeout: float = 10.0) -> dict:
    """
    Send a JSON-RPC 2.0 request to the Rust blockchain node.

    Returns the `result` dict on success, or
    {"error": "..."} on failure.
    """
    req_id = _next_id()
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
        "id": req_id,
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((host, port))
            # Wire-framed: 4-byte big-endian length + JSON
            s.sendall(struct.pack("!I", len(data)))
            s.sendall(data)
            # Read response header
            header = _recv_exact(s, 4)
            if header is None:
                return {"error": "No response from Rust node"}
            resp_len = struct.unpack("!I", header)[0]
            if resp_len > 64 * 1024 * 1024:
                return {"error": "Response too large"}
            resp_data = _recv_exact(s, resp_len)
            if resp_data is None:
                return {"error": "Incomplete response from Rust node"}
            resp = json.loads(resp_data)
    except ConnectionRefusedError:
        return {"error": "Rust node not reachable (connection refused on port %d)" % port}
    except socket.timeout:
        return {"error": "Rust node timeout"}
    except Exception as e:
        return {"error": f"Rust RPC failed: {e}"}

    # JSON-RPC 2.0 response handling
    if "error" in resp and resp["error"] is not None:
        err = resp["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        return {"error": msg}

    return resp.get("result", {})


def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    """Read exactly n bytes from socket."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), 65536))
        if not chunk:
            return None
        buf += chunk
    return buf


# ═══════════════════════════════════════════════════════════════════════
# High-level convenience wrappers
# ═══════════════════════════════════════════════════════════════════════

def get_chain_height(**kw) -> dict:
    return rpc_call("get_chain_height", **kw)

def get_block(index: int, **kw) -> dict:
    return rpc_call("get_block", {"index": index}, **kw)

def get_blocks(start: int, end: int, **kw) -> dict:
    return rpc_call("get_blocks", {"start": start, "end": end}, **kw)

def get_latest_block(**kw) -> dict:
    return rpc_call("get_latest_block", **kw)

def get_balance(address: str, **kw) -> dict:
    return rpc_call("get_balance", {"address": address}, **kw)

def get_chain_info(**kw) -> dict:
    return rpc_call("get_chain_info", **kw)

def get_network_stats(**kw) -> dict:
    return rpc_call("get_network_stats", **kw)

def get_mining_stats(**kw) -> dict:
    return rpc_call("get_mining_stats", **kw)

def get_leaderboard(top_n: int = 20, **kw) -> dict:
    return rpc_call("get_leaderboard", {"top_n": top_n}, **kw)

def submit_transaction(tx: dict, **kw) -> dict:
    return rpc_call("submit_transaction", tx, **kw)

def submit_productive_work(tx: dict, **kw) -> dict:
    return rpc_call("submit_productive_work", tx, **kw)

def submit_local_credit(tx: dict, **kw) -> dict:
    return rpc_call("submit_local_credit", tx, **kw)

def get_mempool_txs(**kw) -> dict:
    return rpc_call("get_mempool_txs", **kw)

def ping(**kw) -> dict:
    return rpc_call("ping", **kw)


def canonical_tx_timestamp(now: Optional[float] = None) -> float:
    """Return the Python/Rust-compatible timestamp used in signed tx hashes."""
    return round(time.time() if now is None else float(now), 3)


def rust_tx_hash(
    *,
    from_address: str,
    to_address: str,
    amount: int,
    tx_type: str,
    nonce: int,
    timestamp: float,
    metadata: dict,
    tx_version: int = 2,
) -> str:
    """Calculate the Rust/Python-compatible SHA3-512 transaction hash."""
    tx_data = {
        "amount": amount,
        "from": from_address,
        "metadata": metadata,
        "nonce": nonce,
        "timestamp": timestamp,
        "to": to_address,
        "type": tx_type,
    }
    if tx_version >= 2:
        tx_data["chain_id"] = "RPNT-mainnet-1"
    encoded = json.dumps(tx_data, sort_keys=True).encode()
    return hashlib.sha3_512(encoded).hexdigest()


def get_next_nonce(address: str, **kw) -> int:
    """Return the next chain nonce expected by Rust RPC validation."""
    resp = rpc_call("get_nonce", {"address": address}, **kw)
    if "error" in resp:
        raise RuntimeError(resp["error"])
    return int(resp.get("nonce", 0))


def pending_nonce_tx(address: str, nonce: int, **kw) -> Optional[dict]:
    """Return a mempool tx occupying address/nonce, if one exists."""
    mempool = get_mempool_txs(**kw)
    if "error" in mempool:
        return None
    for tx in mempool.get("pending_transactions", []):
        if not isinstance(tx, dict):
            continue
        try:
            tx_nonce = int(tx.get("nonce"))
        except (TypeError, ValueError):
            continue
        if tx.get("from_address") == address and tx_nonce == nonce:
            return tx
    return None


def submit_node_signed_workload_credit(
    *,
    to_address: str,
    amount_plancks: int,
    purpose: str,
    metadata: Optional[dict] = None,
    rpc_method: str = "submit_local_credit",
    **kw,
) -> dict:
    """Submit a node-wallet-signed workload credit transaction."""
    from repryntt.economy.node_wallet import get_node_wallet

    node_wallet = get_node_wallet()
    if node_wallet is None or not node_wallet.can_sign():
        return {"error": "Node wallet cannot sign local credit transaction"}

    tx_metadata = dict(metadata or {})
    tx_metadata["purpose"] = purpose
    tx_metadata.setdefault("source", "robot_economy")
    timestamp = canonical_tx_timestamp()
    nonce = get_next_nonce(node_wallet.address, **kw)
    pending = pending_nonce_tx(node_wallet.address, nonce, **kw)
    if pending:
        return {
            "error": (
                f"Nonce {nonce} already pending in mempool; "
                "retry after the pending transaction confirms"
            ),
            "retryable": True,
            "pending_tx_hash": pending.get("tx_hash", ""),
        }
    tx_hash = rust_tx_hash(
        from_address=node_wallet.address,
        to_address=to_address,
        amount=amount_plancks,
        tx_type="workload_completion",
        nonce=nonce,
        timestamp=timestamp,
        metadata=tx_metadata,
        tx_version=2,
    )
    tx = {
        "from_address": node_wallet.address,
        "to_address": to_address,
        "amount": amount_plancks,
        "tx_type": "workload_completion",
        "nonce": nonce,
        "timestamp": timestamp,
        "metadata": tx_metadata,
        "tx_version": 2,
        "public_key": node_wallet.public_key.hex(),
        "signature": node_wallet.sign(bytes.fromhex(tx_hash)).hex(),
    }
    return rpc_call(rpc_method, tx, **kw)


def submit_signed_transfer(
    *,
    from_address: str,
    private_key: bytes,
    to_address: str,
    amount_plancks: int,
    metadata: Optional[dict] = None,
    **kw,
) -> dict:
    """Submit a normal signed transfer using a locally held Ed25519 key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    private_key_obj = ed25519.Ed25519PrivateKey.from_private_bytes(private_key)
    public_key = private_key_obj.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    timestamp = canonical_tx_timestamp()
    tx_metadata = dict(metadata or {})
    tx_metadata.setdefault("source", "robot_economy")
    nonce = get_next_nonce(from_address, **kw)
    pending = pending_nonce_tx(from_address, nonce, **kw)
    if pending:
        return {
            "error": (
                f"Nonce {nonce} already pending in mempool; "
                "retry after the pending transaction confirms"
            ),
            "retryable": True,
            "pending_tx_hash": pending.get("tx_hash", ""),
        }
    tx_hash = rust_tx_hash(
        from_address=from_address,
        to_address=to_address,
        amount=amount_plancks,
        tx_type="transfer",
        nonce=nonce,
        timestamp=timestamp,
        metadata=tx_metadata,
        tx_version=2,
    )
    tx = {
        "from_address": from_address,
        "to_address": to_address,
        "amount": amount_plancks,
        "tx_type": "transfer",
        "nonce": nonce,
        "timestamp": timestamp,
        "metadata": tx_metadata,
        "tx_version": 2,
        "public_key": public_key.hex(),
        "signature": private_key_obj.sign(bytes.fromhex(tx_hash)).hex(),
    }
    return submit_transaction(tx, **kw)


def is_rust_node_running(host: str = RUST_RPC_HOST, port: int = RUST_RPC_PORT) -> bool:
    """Quick check: can we reach the Rust node?"""
    try:
        result = ping(host=host, port=port, timeout=3.0)
        return "error" not in result
    except Exception:
        return False
