"""
safe_serialize.py — Drop-in replacement for pickle in SAIGE network protocols.

SECURITY: pickle.loads() on untrusted network data allows arbitrary code execution.
This module replaces pickle with msgpack (binary, fast, safe) for ALL inter-node
communication. It provides the same pack/unpack interface but NEVER evaluates
arbitrary Python objects.

Wire format: 4-byte big-endian length prefix + msgpack payload
  - Same framing the old pickle protocol used
  - Allows incremental migration (receiver auto-detects format)

Usage:
  OLD: data = pickle.dumps(msg)    →  NEW: data = safe_serialize.pack(msg)
  OLD: msg  = pickle.loads(data)   →  NEW: msg  = safe_serialize.unpack(data)
"""

import struct
import json
import logging

logger = logging.getLogger("safe_serialize")

try:
    import msgpack
    _HAS_MSGPACK = True
except ImportError:
    _HAS_MSGPACK = False
    logger.warning("msgpack not installed — falling back to JSON serialization")

# ── Maximum message size (16 MB) ──────────────────────
MAX_MESSAGE_SIZE = 16 * 1024 * 1024

# ── Pickle detection ─────────────────────────────────
# Pickle protocol opcodes that appear at the start of pickled data
_PICKLE_MARKERS = (
    b'\x80',   # PROTO (pickle protocol 2+)
    b'(',      # MARK
    b'}',      # EMPTY_DICT
    b']',      # EMPTY_LIST
    b'I',      # INT
    b'S',      # STRING
    b'\x89',   # NEWFALSE
    b'\x88',   # NEWTRUE
)


def pack(obj: dict) -> bytes:
    """
    Serialize a dict/list to bytes (safe replacement for pickle.dumps).

    Returns msgpack binary. Only serializes JSON-safe types
    (dict, list, str, int, float, bool, None, bytes).
    """
    if _HAS_MSGPACK:
        return msgpack.packb(obj, use_bin_type=True)
    else:
        # JSON fallback — slightly slower, no native bytes support
        return json.dumps(obj, default=_json_default).encode('utf-8')


def unpack(data: bytes) -> dict:
    """
    Deserialize bytes to a dict (safe replacement for pickle.loads).

    SECURITY: This function NEVER executes arbitrary code, unlike pickle.loads().
    It detects and rejects pickle-formatted data with a clear error message.

    Args:
        data: Raw bytes from the network

    Returns:
        Deserialized dict/list

    Raises:
        ValueError: If data is pickle-formatted or exceeds size limit
        Exception: If data is malformed
    """
    if not data:
        raise ValueError("Cannot unpack empty data")

    if len(data) > MAX_MESSAGE_SIZE:
        raise ValueError(f"Message too large: {len(data)} bytes (max {MAX_MESSAGE_SIZE})")

    # SECURITY: Detect and reject pickle data
    if _looks_like_pickle(data):
        raise ValueError(
            "SECURITY: Rejected pickle-formatted data. "
            "All nodes must use safe_serialize. "
            "This may indicate an outdated peer or an attack."
        )

    if _HAS_MSGPACK:
        try:
            return msgpack.unpackb(data, raw=False, strict_map_key=True)
        except (msgpack.UnpackValueError, msgpack.FormatError) as e:
            # Try JSON fallback for mixed-version clusters
            try:
                return json.loads(data.decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ValueError(f"Cannot deserialize message: {e}")
    else:
        try:
            return json.loads(data.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"Cannot deserialize message: {e}")


def pack_with_length(obj: dict) -> bytes:
    """Pack with 4-byte length prefix (matches existing socket protocol framing)."""
    payload = pack(obj)
    return struct.pack(">I", len(payload)) + payload


def unpack_from_socket(sock, max_size: int = MAX_MESSAGE_SIZE) -> dict:
    """
    Read a length-prefixed message from a socket and unpack safely.

    Replaces the common pattern:
        data = sock.recv(...)
        message = pickle.loads(data)

    With safe deserialization.
    """
    # Read 4-byte length header
    header = _recv_exact(sock, 4)
    if not header:
        raise ConnectionError("Connection closed while reading message header")

    msg_len = struct.unpack(">I", header)[0]

    if msg_len > max_size:
        raise ValueError(f"Message too large: {msg_len} bytes (max {max_size})")

    if msg_len == 0:
        raise ValueError("Empty message")

    # Read exact message body
    body = _recv_exact(sock, msg_len)
    if not body or len(body) != msg_len:
        raise ConnectionError(f"Connection closed during message read (got {len(body) if body else 0}/{msg_len})")

    return unpack(body)


def send_to_socket(sock, obj: dict):
    """
    Serialize and send a length-prefixed message over a socket.

    Replaces the common pattern:
        data = pickle.dumps(obj)
        sock.sendall(struct.pack(">I", len(data)) + data)
    """
    data = pack_with_length(obj)
    sock.sendall(data)


def _recv_exact(sock, n: int) -> bytes:
    """Read exactly n bytes from a socket."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _looks_like_pickle(data: bytes) -> bool:
    """
    Detect if data appears to be pickle-serialized.

    This catches both intentional and accidental pickle data, preventing
    RCE from old nodes that haven't been updated yet.
    """
    if not data:
        return False

    # Check first byte against known pickle opcodes
    if data[0:1] in _PICKLE_MARKERS:
        # Additional check: pickle protocol 2+ starts with \x80\x02-\x05
        if data[0:1] == b'\x80' and len(data) > 1:
            if data[1] in (2, 3, 4, 5):
                return True

        # Check for pickle STOP opcode at the end
        if data[-1:] == b'.':  # STOP opcode
            return True

    return False


def _json_default(obj):
    """JSON serialization fallback for bytes objects."""
    if isinstance(obj, bytes):
        import base64
        return {"__bytes__": base64.b64encode(obj).decode('ascii')}
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
