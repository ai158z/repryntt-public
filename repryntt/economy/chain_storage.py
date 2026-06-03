"""
Chain Storage Abstraction — Pluggable backend for repryntt blockchain data.

Replaces flat JSON files with a proper storage interface that can scale
from a single Jetson Nano to a million-node network.

Backends:
    JSONBackend    — Current behavior (flat files). Good for dev/testing.
    SQLiteBackend  — Embedded DB. Good for single-node production (100K+ blocks).
    LevelDBBackend — (future) Log-structured merge tree. Good for 1M+ blocks.

The blockchain node uses ChainStore without knowing which backend is active.
Switch backends via environment variable: REPRYNTT_CHAIN_STORAGE=sqlite
"""

import json
import hashlib
import logging
import os
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("chain_storage")


class StorageBackend(ABC):
    """Abstract interface for blockchain storage."""

    @abstractmethod
    def put_block(self, index: int, block_dict: dict) -> bool:
        """Store a block by index."""

    @abstractmethod
    def get_block(self, index: int) -> Optional[dict]:
        """Retrieve a block by index."""

    @abstractmethod
    def get_block_range(self, start: int, end: int) -> List[dict]:
        """Retrieve blocks in range [start, end)."""

    @abstractmethod
    def get_chain_length(self) -> int:
        """Return the current chain height."""

    @abstractmethod
    def get_latest_block(self) -> Optional[dict]:
        """Return the most recent block."""

    @abstractmethod
    def put_balance(self, address: str, balance_plancks: int):
        """Store/update a wallet balance."""

    @abstractmethod
    def get_balance(self, address: str) -> int:
        """Get balance for an address (0 if unknown)."""

    @abstractmethod
    def get_all_balances(self) -> Dict[str, int]:
        """Get all balances."""

    @abstractmethod
    def put_stake(self, address: str, stake_plancks: int):
        """Store/update stake amount."""

    @abstractmethod
    def get_stake(self, address: str) -> int:
        """Get stake for an address."""

    @abstractmethod
    def get_all_stakes(self) -> Dict[str, int]:
        """Get all stakes."""

    @abstractmethod
    def put_header(self, index: int, header_dict: dict):
        """Store a block header (for light clients / header-first sync)."""

    @abstractmethod
    def get_header(self, index: int) -> Optional[dict]:
        """Get a block header by index."""

    @abstractmethod
    def get_header_range(self, start: int, end: int) -> List[dict]:
        """Get headers in range [start, end)."""

    @abstractmethod
    def close(self):
        """Clean shutdown."""


# ═══════════════════════════════════════════════════════════════
# JSON Backend (current behavior — for development / small chains)
# ═══════════════════════════════════════════════════════════════

class JSONBackend(StorageBackend):
    """Flat JSON file storage. Simple but doesn't scale past ~50K blocks."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._chain_file = os.path.join(data_dir, "blockchain.json")
        self._balances_file = os.path.join(data_dir, "balances.json")
        self._stakes_file = os.path.join(data_dir, "stakes.json")
        self._headers_file = os.path.join(data_dir, "headers.json")
        self._lock = threading.Lock()

        # Load into memory
        self._chain = self._load_json(self._chain_file, [])
        self._balances = self._load_json(self._balances_file, {})
        self._stakes = self._load_json(self._stakes_file, {})
        self._headers = self._load_json(self._headers_file, [])

    def _load_json(self, path: str, default):
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return default

    def _save_json(self, path: str, data):
        import tempfile
        tmp_fd, tmp_path = tempfile.mkstemp(dir=self.data_dir, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise

    def put_block(self, index: int, block_dict: dict) -> bool:
        with self._lock:
            if index == len(self._chain):
                self._chain.append(block_dict)
            elif index < len(self._chain):
                self._chain[index] = block_dict
            else:
                return False
            self._save_json(self._chain_file, self._chain)
            return True

    def get_block(self, index: int) -> Optional[dict]:
        with self._lock:
            if 0 <= index < len(self._chain):
                return self._chain[index]
        return None

    def get_block_range(self, start: int, end: int) -> List[dict]:
        with self._lock:
            return self._chain[start:end]

    def get_chain_length(self) -> int:
        return len(self._chain)

    def get_latest_block(self) -> Optional[dict]:
        with self._lock:
            return self._chain[-1] if self._chain else None

    def put_balance(self, address: str, balance_plancks: int):
        with self._lock:
            self._balances[address] = balance_plancks
            self._save_json(self._balances_file, self._balances)

    def get_balance(self, address: str) -> int:
        return self._balances.get(address, 0)

    def get_all_balances(self) -> Dict[str, int]:
        return dict(self._balances)

    def put_stake(self, address: str, stake_plancks: int):
        with self._lock:
            self._stakes[address] = stake_plancks
            self._save_json(self._stakes_file, self._stakes)

    def get_stake(self, address: str) -> int:
        return self._stakes.get(address, 0)

    def get_all_stakes(self) -> Dict[str, int]:
        return dict(self._stakes)

    def put_header(self, index: int, header_dict: dict):
        with self._lock:
            while len(self._headers) <= index:
                self._headers.append(None)
            self._headers[index] = header_dict
            self._save_json(self._headers_file, self._headers)

    def get_header(self, index: int) -> Optional[dict]:
        with self._lock:
            if 0 <= index < len(self._headers):
                return self._headers[index]
        return None

    def get_header_range(self, start: int, end: int) -> List[dict]:
        with self._lock:
            return [h for h in self._headers[start:end] if h is not None]

    def close(self):
        pass  # JSON backend is always flushed


# ═══════════════════════════════════════════════════════════════
# SQLite Backend (production — handles millions of blocks)
# ═══════════════════════════════════════════════════════════════

class SQLiteBackend(StorageBackend):
    """
    SQLite-based storage. Handles millions of blocks efficiently.

    Schema:
        blocks      — Full block JSON, indexed by block_index
        headers     — Block headers only (for fast sync)
        balances    — Current balances, indexed by address
        stakes      — Current stakes, indexed by address
    """

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._db_path = os.path.join(data_dir, "blockchain.db")
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
        self._init_schema()

    def _init_schema(self):
        c = self._conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS blocks (
                block_index INTEGER PRIMARY KEY,
                block_hash TEXT NOT NULL,
                block_json TEXT NOT NULL,
                created_at REAL DEFAULT (strftime('%s','now'))
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS headers (
                block_index INTEGER PRIMARY KEY,
                header_json TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS balances (
                address TEXT PRIMARY KEY,
                balance_plancks INTEGER NOT NULL DEFAULT 0,
                updated_at REAL DEFAULT (strftime('%s','now'))
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS stakes (
                address TEXT PRIMARY KEY,
                stake_plancks INTEGER NOT NULL DEFAULT 0,
                updated_at REAL DEFAULT (strftime('%s','now'))
            )
        """)
        # Index for fast range queries
        c.execute("CREATE INDEX IF NOT EXISTS idx_blocks_hash ON blocks(block_hash)")
        self._conn.commit()

    def put_block(self, index: int, block_dict: dict) -> bool:
        with self._lock:
            try:
                block_hash = block_dict.get("hash", "")
                block_json = json.dumps(block_dict, sort_keys=True, default=str)
                self._conn.execute(
                    "INSERT OR REPLACE INTO blocks (block_index, block_hash, block_json) VALUES (?, ?, ?)",
                    (index, block_hash, block_json),
                )
                self._conn.commit()
                return True
            except Exception as e:
                logger.error(f"SQLite put_block error: {e}")
                return False

    def get_block(self, index: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT block_json FROM blocks WHERE block_index = ?", (index,)
            ).fetchone()
            if row:
                return json.loads(row[0])
        return None

    def get_block_range(self, start: int, end: int) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT block_json FROM blocks WHERE block_index >= ? AND block_index < ? ORDER BY block_index",
                (start, end),
            ).fetchall()
            return [json.loads(r[0]) for r in rows]

    def get_chain_length(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM blocks").fetchone()
            return row[0] if row else 0

    def get_latest_block(self) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT block_json FROM blocks ORDER BY block_index DESC LIMIT 1"
            ).fetchone()
            if row:
                return json.loads(row[0])
        return None

    def put_balance(self, address: str, balance_plancks: int):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO balances (address, balance_plancks, updated_at) VALUES (?, ?, ?)",
                (address, balance_plancks, time.time()),
            )
            self._conn.commit()

    def get_balance(self, address: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT balance_plancks FROM balances WHERE address = ?", (address,)
            ).fetchone()
            return row[0] if row else 0

    def get_all_balances(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT address, balance_plancks FROM balances").fetchall()
            return {r[0]: r[1] for r in rows}

    def put_stake(self, address: str, stake_plancks: int):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO stakes (address, stake_plancks, updated_at) VALUES (?, ?, ?)",
                (address, stake_plancks, time.time()),
            )
            self._conn.commit()

    def get_stake(self, address: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT stake_plancks FROM stakes WHERE address = ?", (address,)
            ).fetchone()
            return row[0] if row else 0

    def get_all_stakes(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT address, stake_plancks FROM stakes").fetchall()
            return {r[0]: r[1] for r in rows}

    def put_header(self, index: int, header_dict: dict):
        with self._lock:
            header_json = json.dumps(header_dict, sort_keys=True)
            self._conn.execute(
                "INSERT OR REPLACE INTO headers (block_index, header_json) VALUES (?, ?)",
                (index, header_json),
            )
            self._conn.commit()

    def get_header(self, index: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT header_json FROM headers WHERE block_index = ?", (index,)
            ).fetchone()
            if row:
                return json.loads(row[0])
        return None

    def get_header_range(self, start: int, end: int) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT header_json FROM headers WHERE block_index >= ? AND block_index < ? ORDER BY block_index",
                (start, end),
            ).fetchall()
            return [json.loads(r[0]) for r in rows]

    def close(self):
        with self._lock:
            self._conn.close()

    def clear(self):
        """Wipe all data — used when a foreign-genesis chain is detected."""
        with self._lock:
            self._conn.execute("DELETE FROM blocks")
            self._conn.execute("DELETE FROM headers")
            self._conn.execute("DELETE FROM balances")
            self._conn.execute("DELETE FROM stakes")
            self._conn.commit()


# ═══════════════════════════════════════════════════════════════
# Factory
# ═══════════════════════════════════════════════════════════════

def create_storage(data_dir: str = None) -> StorageBackend:
    """
    Create the appropriate storage backend.

    Set REPRYNTT_CHAIN_STORAGE env var:
        "json"   — flat JSON files (default, dev)
        "sqlite" — SQLite database (production)
    """
    if data_dir is None:
        data_dir = os.path.join(os.path.dirname(__file__), "..", "..", "robot_economy_data")
    data_dir = os.path.abspath(data_dir)

    backend = os.environ.get("REPRYNTT_CHAIN_STORAGE", "sqlite").lower()

    if backend == "json":
        logger.info(f"Chain storage: JSON backend at {data_dir}")
        return JSONBackend(data_dir)
    else:
        logger.info(f"Chain storage: SQLite backend at {data_dir}")
        return SQLiteBackend(data_dir)
