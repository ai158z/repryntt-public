//! SQLite storage backend for the repryntt blockchain.
//!
//! Schema matches Python's `chain_storage.py` exactly so both
//! implementations can read each other's databases.

use rusqlite::{Connection, Result as SqlResult, params};
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::Path;
use std::sync::Mutex;

use crate::block::Block;

pub const CHAIN_STATE_VERSION: u64 = 1;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChainMeta {
    pub height: u64,
    pub tip_hash: String,
    pub genesis_hash: String,
    pub state_version: u64,
}

// ── SQLite backend ───────────────────────────────────────────────────────────

pub struct Storage {
    conn: Mutex<Connection>,
    path: Option<std::path::PathBuf>,
}

impl Storage {
    /// Open (or create) the SQLite database at `path`.
    pub fn open<P: AsRef<Path>>(path: P) -> SqlResult<Self> {
        let p = path.as_ref().to_path_buf();
        let conn = Connection::open(&p)?;
        // Concurrency + RAM hygiene:
        //   journal_mode=WAL         — concurrent reads while a writer is active
        //   synchronous=NORMAL       — safe under WAL, faster than FULL
        //   wal_autocheckpoint=1000  — checkpoint every 1000 frames (~4 MB
        //                              of WAL) so the WAL file doesn't grow
        //                              into the hundreds of MB it was hitting
        //   journal_size_limit=50MB  — hard cap on WAL file size on disk;
        //                              keeps WAL from ballooning between
        //                              checkpoints
        //   cache_size=-32768        — SQLite page cache cap of 32 MiB
        //                              (negative = KiB; positive = pages)
        //                              prevents SQLite's per-connection page
        //                              cache from quietly growing into hundreds
        //                              of MB on long-running nodes
        conn.execute_batch(
            "PRAGMA journal_mode=WAL;
             PRAGMA synchronous=NORMAL;
             PRAGMA wal_autocheckpoint=1000;
             PRAGMA journal_size_limit=52428800;
             PRAGMA cache_size=-32768;",
        )?;
        let storage = Self {
            conn: Mutex::new(conn),
            path: Some(p),
        };
        storage.init_schema()?;
        Ok(storage)
    }

    /// In-memory database (for tests).
    pub fn in_memory() -> SqlResult<Self> {
        let conn = Connection::open_in_memory()?;
        let storage = Self {
            conn: Mutex::new(conn),
            path: None,
        };
        storage.init_schema()?;
        Ok(storage)
    }

    /// Return the path this database was opened with (None for in-memory).
    pub fn db_path(&self) -> Option<&std::path::Path> {
        self.path.as_deref()
    }

    // ── schema ───────────────────────────────────────────────────────────

    fn init_schema(&self) -> SqlResult<()> {
        self.conn.lock().unwrap().execute_batch(
            "
            CREATE TABLE IF NOT EXISTS blocks (
                block_index INTEGER PRIMARY KEY,
                block_hash  TEXT NOT NULL,
                block_json  TEXT NOT NULL,
                created_at  REAL DEFAULT (strftime('%s','now'))
            );

            CREATE TABLE IF NOT EXISTS headers (
                block_index  INTEGER PRIMARY KEY,
                header_json  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS balances (
                address         TEXT PRIMARY KEY,
                balance_plancks INTEGER NOT NULL DEFAULT 0,
                updated_at      REAL DEFAULT (strftime('%s','now'))
            );

            CREATE TABLE IF NOT EXISTS stakes (
                address       TEXT PRIMARY KEY,
                stake_plancks INTEGER NOT NULL DEFAULT 0,
                updated_at    REAL DEFAULT (strftime('%s','now'))
            );

            CREATE TABLE IF NOT EXISTS nonces (
                address    TEXT PRIMARY KEY,
                nonce      INTEGER NOT NULL DEFAULT 0,
                updated_at REAL DEFAULT (strftime('%s','now'))
            );

            CREATE TABLE IF NOT EXISTS chain_meta (
                id            INTEGER PRIMARY KEY CHECK (id = 1),
                height        INTEGER NOT NULL,
                tip_hash      TEXT NOT NULL,
                genesis_hash  TEXT NOT NULL,
                state_version INTEGER NOT NULL,
                updated_at    REAL DEFAULT (strftime('%s','now'))
            );

            CREATE INDEX IF NOT EXISTS idx_blocks_hash ON blocks(block_hash);
            ",
        )?;
        Ok(())
    }

    // ── block operations ─────────────────────────────────────────────────

    /// Store a block (INSERT OR REPLACE).
    pub fn put_block(&self, block: &Block) -> SqlResult<()> {
        let block_dict = block.to_dict();
        let json_str = serde_json::to_string(&block_dict).expect("block serialization");
        self.conn.lock().unwrap().execute(
            "INSERT OR REPLACE INTO blocks (block_index, block_hash, block_json) VALUES (?1, ?2, ?3)",
            params![block.index as i64, block.hash, json_str],
        )?;
        Ok(())
    }

    /// Load a single block by index.
    pub fn get_block(&self, index: u64) -> SqlResult<Option<Block>> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare("SELECT block_json FROM blocks WHERE block_index = ?1")?;
        let result = stmt.query_row(params![index as i64], |row| {
            let json_str: String = row.get(0)?;
            Ok(json_str)
        });
        match result {
            Ok(json_str) => {
                let map: BTreeMap<String, Value> =
                    serde_json::from_str(&json_str).expect("stored block is valid JSON");
                Ok(Block::from_dict(&map))
            }
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(e),
        }
    }

    /// Load the entire chain in order.
    pub fn load_chain(&self) -> SqlResult<Vec<Block>> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare("SELECT block_json FROM blocks ORDER BY block_index ASC")?;
        let rows = stmt.query_map([], |row| {
            let json_str: String = row.get(0)?;
            Ok(json_str)
        })?;
        let mut blocks = Vec::new();
        for row in rows {
            let json_str = row?;
            let map: BTreeMap<String, Value> =
                serde_json::from_str(&json_str).expect("stored block is valid JSON");
            if let Some(block) = Block::from_dict(&map) {
                blocks.push(block);
            }
        }
        Ok(blocks)
    }

    /// How many blocks are stored.
    pub fn block_count(&self) -> SqlResult<u64> {
        let count: i64 =
            self.conn
                .lock()
                .unwrap()
                .query_row("SELECT COUNT(*) FROM blocks", [], |row| row.get(0))?;
        Ok(count as u64)
    }

    // ── balance / stake operations ───────────────────────────────────────

    pub fn put_balance(&self, address: &str, balance_plancks: i64) -> SqlResult<()> {
        self.conn.lock().unwrap().execute(
            "INSERT OR REPLACE INTO balances (address, balance_plancks, updated_at) VALUES (?1, ?2, strftime('%s','now'))",
            params![address, balance_plancks],
        )?;
        Ok(())
    }

    pub fn get_balance(&self, address: &str) -> SqlResult<i64> {
        let result = self.conn.lock().unwrap().query_row(
            "SELECT balance_plancks FROM balances WHERE address = ?1",
            params![address],
            |row| row.get(0),
        );
        match result {
            Ok(bal) => Ok(bal),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(0),
            Err(e) => Err(e),
        }
    }

    pub fn put_stake(&self, address: &str, stake_plancks: i64) -> SqlResult<()> {
        self.conn.lock().unwrap().execute(
            "INSERT OR REPLACE INTO stakes (address, stake_plancks, updated_at) VALUES (?1, ?2, strftime('%s','now'))",
            params![address, stake_plancks],
        )?;
        Ok(())
    }

    pub fn get_stake(&self, address: &str) -> SqlResult<i64> {
        let result = self.conn.lock().unwrap().query_row(
            "SELECT stake_plancks FROM stakes WHERE address = ?1",
            params![address],
            |row| row.get(0),
        );
        match result {
            Ok(s) => Ok(s),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(0),
            Err(e) => Err(e),
        }
    }

    pub fn put_nonce(&self, address: &str, nonce: u64) -> SqlResult<()> {
        self.conn.lock().unwrap().execute(
            "INSERT OR REPLACE INTO nonces (address, nonce, updated_at) VALUES (?1, ?2, strftime('%s','now'))",
            params![address, nonce as i64],
        )?;
        Ok(())
    }

    pub fn get_nonce(&self, address: &str) -> SqlResult<u64> {
        let result = self.conn.lock().unwrap().query_row(
            "SELECT nonce FROM nonces WHERE address = ?1",
            params![address],
            |row| row.get::<_, i64>(0),
        );
        match result {
            Ok(n) => Ok(n.max(0) as u64),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(0),
            Err(e) => Err(e),
        }
    }

    // ── bulk operations ──────────────────────────────────────────────────

    /// Save the full chain + runtime state in a single transaction.
    pub fn save_chain(
        &self,
        blocks: &[Block],
        balances: &BTreeMap<String, i64>,
        nonces: &BTreeMap<String, u64>,
        stakes: &BTreeMap<String, i64>,
    ) -> SqlResult<()> {
        // NOTE (bounded-chain refactor, 2026-06-01):
        // Previously this method deleted every row at index >= blocks.len()
        // before inserting, assuming `blocks` was the FULL chain history.
        // With the bounded recent-window Chain it isn't — callers now pass
        // only the in-memory window. So we ONLY INSERT OR REPLACE the rows
        // we're given and leave older rows intact. Older rows were
        // persisted by prior save_chain / put_block calls when those
        // blocks were still in the window.
        let conn = self.conn.lock().unwrap();
        let tx = conn.unchecked_transaction()?;

        for block in blocks {
            let dict = block.to_dict();
            let json_str = serde_json::to_string(&dict).expect("block serialization");
            tx.execute(
                "INSERT OR REPLACE INTO blocks (block_index, block_hash, block_json) VALUES (?1, ?2, ?3)",
                params![block.index as i64, block.hash, json_str],
            )?;
        }

        tx.execute("DELETE FROM balances", [])?;
        tx.execute("DELETE FROM stakes", [])?;
        tx.execute("DELETE FROM nonces", [])?;

        for (addr, bal) in balances {
            tx.execute(
                "INSERT OR REPLACE INTO balances (address, balance_plancks, updated_at) VALUES (?1, ?2, strftime('%s','now'))",
                params![addr, bal],
            )?;
        }

        for (addr, nonce) in nonces {
            tx.execute(
                "INSERT OR REPLACE INTO nonces (address, nonce, updated_at) VALUES (?1, ?2, strftime('%s','now'))",
                params![addr, *nonce as i64],
            )?;
        }

        for (addr, stake) in stakes {
            tx.execute(
                "INSERT OR REPLACE INTO stakes (address, stake_plancks, updated_at) VALUES (?1, ?2, strftime('%s','now'))",
                params![addr, stake],
            )?;
        }

        if let Some(latest) = blocks.last() {
            // True height = max block_index across the entire blocks table + 1
            // (genesis is block_index 0). The `blocks` slice may be the
            // recent window only; rely on the DB for the authoritative count.
            let true_height: i64 = tx.query_row(
                "SELECT COALESCE(MAX(block_index), -1) + 1 FROM blocks",
                [],
                |row| row.get(0),
            ).unwrap_or((latest.index + 1) as i64);
            tx.execute(
                "INSERT OR REPLACE INTO chain_meta (id, height, tip_hash, genesis_hash, state_version, updated_at)
                 VALUES (1, ?1, ?2, ?3, ?4, strftime('%s','now'))",
                params![
                    true_height,
                    &latest.hash,
                    crate::genesis::EXPECTED_GENESIS_HASH,
                    CHAIN_STATE_VERSION as i64
                ],
            )?;
        }

        tx.commit()?;
        Ok(())
    }

    /// Save only runtime state and chain metadata without rewriting blocks.
    pub fn save_runtime_state(
        &self,
        height: u64,
        tip_hash: &str,
        balances: &BTreeMap<String, i64>,
        nonces: &BTreeMap<String, u64>,
        stakes: &BTreeMap<String, i64>,
    ) -> SqlResult<()> {
        let conn = self.conn.lock().unwrap();
        let tx = conn.unchecked_transaction()?;

        tx.execute("DELETE FROM balances", [])?;
        tx.execute("DELETE FROM stakes", [])?;
        tx.execute("DELETE FROM nonces", [])?;

        for (addr, bal) in balances {
            tx.execute(
                "INSERT OR REPLACE INTO balances (address, balance_plancks, updated_at) VALUES (?1, ?2, strftime('%s','now'))",
                params![addr, bal],
            )?;
        }
        for (addr, nonce) in nonces {
            tx.execute(
                "INSERT OR REPLACE INTO nonces (address, nonce, updated_at) VALUES (?1, ?2, strftime('%s','now'))",
                params![addr, *nonce as i64],
            )?;
        }
        for (addr, stake) in stakes {
            tx.execute(
                "INSERT OR REPLACE INTO stakes (address, stake_plancks, updated_at) VALUES (?1, ?2, strftime('%s','now'))",
                params![addr, stake],
            )?;
        }
        tx.execute(
            "INSERT OR REPLACE INTO chain_meta (id, height, tip_hash, genesis_hash, state_version, updated_at)
             VALUES (1, ?1, ?2, ?3, ?4, strftime('%s','now'))",
            params![
                height as i64,
                tip_hash,
                crate::genesis::EXPECTED_GENESIS_HASH,
                CHAIN_STATE_VERSION as i64
            ],
        )?;

        tx.commit()?;
        Ok(())
    }

    /// Wipe everything — used when genesis mismatch is detected.
    pub fn clear(&self) -> SqlResult<()> {
        self.conn.lock().unwrap().execute_batch(
            "
            DELETE FROM blocks;
            DELETE FROM headers;
            DELETE FROM balances;
            DELETE FROM stakes;
            DELETE FROM nonces;
            DELETE FROM chain_meta;
            ",
        )?;
        Ok(())
    }

    /// Retrieve all stored balances.
    pub fn get_all_balances(&self) -> SqlResult<BTreeMap<String, i64>> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare("SELECT address, balance_plancks FROM balances")?;
        let rows = stmt.query_map([], |row| {
            let addr: String = row.get(0)?;
            let bal: i64 = row.get(1)?;
            Ok((addr, bal))
        })?;
        let mut map = BTreeMap::new();
        for row in rows {
            let (addr, bal) = row?;
            map.insert(addr, bal);
        }
        Ok(map)
    }

    /// Retrieve all stored nonces.
    pub fn get_all_nonces(&self) -> SqlResult<BTreeMap<String, u64>> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare("SELECT address, nonce FROM nonces")?;
        let rows = stmt.query_map([], |row| {
            let addr: String = row.get(0)?;
            let nonce: i64 = row.get(1)?;
            Ok((addr, nonce.max(0) as u64))
        })?;
        let mut map = BTreeMap::new();
        for row in rows {
            let (addr, nonce) = row?;
            map.insert(addr, nonce);
        }
        Ok(map)
    }

    /// Retrieve compact chain metadata, if present.
    pub fn get_chain_meta(&self) -> SqlResult<Option<ChainMeta>> {
        let result = self.conn.lock().unwrap().query_row(
            "SELECT height, tip_hash, genesis_hash, state_version FROM chain_meta WHERE id = 1",
            [],
            |row| {
                Ok(ChainMeta {
                    height: row.get::<_, i64>(0)?.max(0) as u64,
                    tip_hash: row.get(1)?,
                    genesis_hash: row.get(2)?,
                    state_version: row.get::<_, i64>(3)?.max(0) as u64,
                })
            },
        );
        match result {
            Ok(meta) => Ok(Some(meta)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(e),
        }
    }

    /// Retrieve all stored stakes.
    pub fn get_all_stakes(&self) -> SqlResult<BTreeMap<String, i64>> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare("SELECT address, stake_plancks FROM stakes")?;
        let rows = stmt.query_map([], |row| {
            let addr: String = row.get(0)?;
            let stake: i64 = row.get(1)?;
            Ok((addr, stake))
        })?;
        let mut map = BTreeMap::new();
        for row in rows {
            let (addr, stake) = row?;
            map.insert(addr, stake);
        }
        Ok(map)
    }

    /// Get the latest (highest-index) block.
    pub fn get_latest_block(&self) -> SqlResult<Option<Block>> {
        let result = self.conn.lock().unwrap().query_row(
            "SELECT block_json FROM blocks ORDER BY block_index DESC LIMIT 1",
            [],
            |row| {
                let json_str: String = row.get(0)?;
                Ok(json_str)
            },
        );
        match result {
            Ok(json_str) => {
                let map: BTreeMap<String, Value> =
                    serde_json::from_str(&json_str).expect("stored block is valid JSON");
                Ok(Block::from_dict(&map))
            }
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(e),
        }
    }

    /// Get a range of blocks [start, end) in ascending order.
    pub fn get_block_range(&self, start: u64, end: u64) -> SqlResult<Vec<Block>> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT block_json FROM blocks WHERE block_index >= ?1 AND block_index < ?2 ORDER BY block_index ASC",
        )?;
        let rows = stmt.query_map(params![start as i64, end as i64], |row| {
            let json_str: String = row.get(0)?;
            Ok(json_str)
        })?;
        let mut blocks = Vec::new();
        for row in rows {
            let json_str = row?;
            let map: BTreeMap<String, Value> =
                serde_json::from_str(&json_str).expect("stored block is valid JSON");
            if let Some(block) = Block::from_dict(&map) {
                blocks.push(block);
            }
        }
        Ok(blocks)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::genesis;

    #[test]
    fn test_store_and_load_genesis() {
        let store = Storage::in_memory().unwrap();
        let genesis = genesis::create_canonical_genesis();
        store.put_block(&genesis).unwrap();

        let loaded = store.get_block(0).unwrap().expect("genesis should load");
        assert_eq!(loaded.hash, genesis.hash);
        assert_eq!(loaded.index, 0);
        assert_eq!(loaded.transactions.len(), 1);
    }

    #[test]
    fn test_balance_roundtrip() {
        let store = Storage::in_memory().unwrap();
        store.put_balance("abc123", 500_000_000).unwrap();
        assert_eq!(store.get_balance("abc123").unwrap(), 500_000_000);
        assert_eq!(store.get_balance("unknown").unwrap(), 0);
    }

    #[test]
    fn test_nonce_roundtrip() {
        let store = Storage::in_memory().unwrap();
        store.put_nonce("abc123", 7).unwrap();
        assert_eq!(store.get_nonce("abc123").unwrap(), 7);
        assert_eq!(store.get_nonce("unknown").unwrap(), 0);
        assert_eq!(store.get_all_nonces().unwrap()["abc123"], 7);
    }

    #[test]
    fn test_save_chain_persists_runtime_state_and_meta() {
        let store = Storage::in_memory().unwrap();
        let genesis = genesis::create_canonical_genesis();
        let mut balances = BTreeMap::new();
        balances.insert("alice".to_string(), 123);
        let mut nonces = BTreeMap::new();
        nonces.insert("alice".to_string(), 2);
        let mut stakes = BTreeMap::new();
        stakes.insert("alice".to_string(), 50);

        store
            .save_chain(&[genesis.clone()], &balances, &nonces, &stakes)
            .unwrap();

        assert_eq!(store.get_all_balances().unwrap()["alice"], 123);
        assert_eq!(store.get_all_nonces().unwrap()["alice"], 2);
        assert_eq!(store.get_all_stakes().unwrap()["alice"], 50);
        let meta = store.get_chain_meta().unwrap().expect("meta should exist");
        assert_eq!(meta.height, 1);
        assert_eq!(meta.tip_hash, genesis.hash);
        assert_eq!(meta.genesis_hash, crate::genesis::EXPECTED_GENESIS_HASH);
        assert_eq!(meta.state_version, CHAIN_STATE_VERSION);
    }

    #[test]
    fn test_clear() {
        let store = Storage::in_memory().unwrap();
        let genesis = genesis::create_canonical_genesis();
        store.put_block(&genesis).unwrap();
        store.put_balance("abc", 100).unwrap();
        store.put_nonce("abc", 3).unwrap();
        store.clear().unwrap();
        assert_eq!(store.block_count().unwrap(), 0);
        assert_eq!(store.get_balance("abc").unwrap(), 0);
        assert_eq!(store.get_nonce("abc").unwrap(), 0);
        assert!(store.get_chain_meta().unwrap().is_none());
    }
}
