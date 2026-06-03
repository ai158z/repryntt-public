//! SPL-equivalent token standard for repryntt.
//!
//! Enables creation, minting, burning, and transfer of custom fungible tokens
//! on the repryntt chain.  Similar to Solana's SPL Token Program but simpler.
//!
//! New functionality — no Python equivalent exists; this extends the chain
//! beyond native CR to support arbitrary user-created tokens.

use sha3::{Digest, Sha3_256};
use std::collections::{BTreeMap, HashMap};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::transaction::PLANCKS_PER_CREDIT;

// ── Constants ────────────────────────────────────────────────────────────────

/// Maximum token symbol length.
pub const MAX_SYMBOL_LEN: usize = 10;
/// Maximum token name length.
pub const MAX_NAME_LEN: usize = 64;
/// Maximum decimals (same as CR = 8).
pub const MAX_DECIMALS: u8 = 18;
/// Minimum token creation fee: 1 CR in plancks.
pub const TOKEN_CREATE_FEE_PLANCKS: i64 = PLANCKS_PER_CREDIT;
/// Maximum total supply for any custom token (u64::MAX).
pub const MAX_TOKEN_SUPPLY: u64 = u64::MAX;
/// Fee for minting (charged to mint authority in CR): 0.001 CR.
pub const MINT_FEE_PLANCKS: i64 = 100_000;

/// Token-related transaction types.
pub const TX_TOKEN_CREATE: &str = "token_create";
pub const TX_TOKEN_MINT: &str = "token_mint";
pub const TX_TOKEN_BURN: &str = "token_burn";
pub const TX_TOKEN_TRANSFER: &str = "token_transfer";
pub const TX_TOKEN_APPROVE: &str = "token_approve";
pub const TX_TOKEN_FREEZE: &str = "token_freeze";
pub const TX_TOKEN_THAW: &str = "token_thaw";

/// All valid token transaction types.
pub const TOKEN_TX_TYPES: &[&str] = &[
    TX_TOKEN_CREATE,
    TX_TOKEN_MINT,
    TX_TOKEN_BURN,
    TX_TOKEN_TRANSFER,
    TX_TOKEN_APPROVE,
    TX_TOKEN_FREEZE,
    TX_TOKEN_THAW,
];

// ── Token ID ─────────────────────────────────────────────────────────────────

/// Deterministic token ID: SHA3-256(creator ‖ symbol ‖ nonce), truncated to 32 hex.
pub fn token_id(creator: &str, symbol: &str, nonce: u64) -> String {
    let mut hasher = Sha3_256::new();
    hasher.update(creator.as_bytes());
    hasher.update(symbol.as_bytes());
    hasher.update(nonce.to_be_bytes());
    hex::encode(hasher.finalize())[..32].to_string()
}

// ── Token Metadata ───────────────────────────────────────────────────────────

/// On-chain metadata for a token type.
#[derive(Debug, Clone)]
pub struct TokenMeta {
    /// Unique identifier (derived from creator + symbol + nonce).
    pub id: String,
    /// Human-readable name (e.g., "Compute Credits").
    pub name: String,
    /// Ticker symbol (e.g., "CMPX").
    pub symbol: String,
    /// Decimal places (e.g., 8 means 1 token = 10^8 base units).
    pub decimals: u8,
    /// Current circulating supply (in base units).
    pub current_supply: u64,
    /// Maximum supply (0 = unlimited).
    pub max_supply: u64,
    /// Address that can mint new tokens (empty = no further minting).
    pub mint_authority: String,
    /// Address that can freeze/thaw accounts (empty = no freeze).
    pub freeze_authority: String,
    /// Creator address.
    pub creator: String,
    /// Block height at creation.
    pub created_at_block: u64,
    /// Timestamp of creation.
    pub created_at: f64,
    /// Whether further minting is permanently disabled.
    pub mint_disabled: bool,
}

impl TokenMeta {
    pub fn to_dict(&self) -> serde_json::Value {
        serde_json::json!({
            "id": self.id,
            "name": self.name,
            "symbol": self.symbol,
            "decimals": self.decimals,
            "current_supply": self.current_supply,
            "max_supply": self.max_supply,
            "mint_authority": self.mint_authority,
            "freeze_authority": self.freeze_authority,
            "creator": self.creator,
            "created_at_block": self.created_at_block,
            "created_at": self.created_at,
            "mint_disabled": self.mint_disabled,
        })
    }

    pub fn from_dict(v: &serde_json::Value) -> Result<Self, String> {
        Ok(Self {
            id: v["id"].as_str().ok_or("missing id")?.to_string(),
            name: v["name"].as_str().ok_or("missing name")?.to_string(),
            symbol: v["symbol"].as_str().ok_or("missing symbol")?.to_string(),
            decimals: v["decimals"].as_u64().ok_or("missing decimals")? as u8,
            current_supply: v["current_supply"].as_u64().unwrap_or(0),
            max_supply: v["max_supply"].as_u64().unwrap_or(0),
            mint_authority: v["mint_authority"].as_str().unwrap_or("").to_string(),
            freeze_authority: v["freeze_authority"].as_str().unwrap_or("").to_string(),
            creator: v["creator"].as_str().ok_or("missing creator")?.to_string(),
            created_at_block: v["created_at_block"].as_u64().unwrap_or(0),
            created_at: v["created_at"].as_f64().unwrap_or(0.0),
            mint_disabled: v["mint_disabled"].as_bool().unwrap_or(false),
        })
    }
}

// ── Allowance ────────────────────────────────────────────────────────────────

/// Delegated spending allowance (like ERC-20 approve).
#[derive(Debug, Clone)]
pub struct Allowance {
    pub owner: String,
    pub spender: String,
    pub token_id: String,
    pub amount: u64,
}

// ── Token Registry ───────────────────────────────────────────────────────────

/// On-chain registry for all custom tokens.
///
/// Tracks token metadata, per-address balances, allowances, and frozen accounts.
pub struct TokenRegistry {
    /// token_id → metadata.
    pub tokens: HashMap<String, TokenMeta>,
    /// (token_id, address) → balance.
    pub balances: BTreeMap<(String, String), u64>,
    /// (token_id, owner, spender) → allowance.
    pub allowances: HashMap<(String, String, String), u64>,
    /// (token_id, address) → frozen?
    pub frozen: HashMap<(String, String), bool>,
    /// symbol → token_id (for quick lookup, symbols are unique).
    pub symbol_index: HashMap<String, String>,
}

impl TokenRegistry {
    pub fn new() -> Self {
        Self {
            tokens: HashMap::new(),
            balances: BTreeMap::new(),
            allowances: HashMap::new(),
            frozen: HashMap::new(),
            symbol_index: HashMap::new(),
        }
    }

    // ── Token Creation ──────────────────────────────────────────

    /// Create a new token type.
    pub fn create_token(
        &mut self,
        name: &str,
        symbol: &str,
        decimals: u8,
        max_supply: u64,
        creator: &str,
        mint_authority: &str,
        freeze_authority: &str,
        block_height: u64,
        nonce: u64,
    ) -> Result<String, String> {
        // Validate name
        if name.is_empty() || name.len() > MAX_NAME_LEN {
            return Err(format!("Name must be 1-{} chars", MAX_NAME_LEN));
        }

        // Validate symbol
        if symbol.is_empty() || symbol.len() > MAX_SYMBOL_LEN {
            return Err(format!("Symbol must be 1-{} chars", MAX_SYMBOL_LEN));
        }
        if !symbol.chars().all(|c| c.is_ascii_alphanumeric()) {
            return Err("Symbol must be alphanumeric ASCII".into());
        }

        // Unique symbol
        let upper_symbol = symbol.to_uppercase();
        if upper_symbol == "CR" || upper_symbol == "RPNT" {
            return Err("Cannot use reserved symbol".into());
        }
        if self.symbol_index.contains_key(&upper_symbol) {
            return Err(format!("Symbol '{}' already exists", symbol));
        }

        // Decimals
        if decimals > MAX_DECIMALS {
            return Err(format!("Decimals must be 0-{}", MAX_DECIMALS));
        }

        let id = token_id(creator, symbol, nonce);
        if self.tokens.contains_key(&id) {
            return Err("Token ID collision".into());
        }

        let meta = TokenMeta {
            id: id.clone(),
            name: name.to_string(),
            symbol: symbol.to_string(),
            decimals,
            current_supply: 0,
            max_supply,
            mint_authority: mint_authority.to_string(),
            freeze_authority: freeze_authority.to_string(),
            creator: creator.to_string(),
            created_at_block: block_height,
            created_at: now_f64(),
            mint_disabled: false,
        };

        self.tokens.insert(id.clone(), meta);
        self.symbol_index.insert(upper_symbol, id.clone());
        Ok(id)
    }

    // ── Minting ─────────────────────────────────────────────────

    /// Mint new tokens to an address.
    pub fn mint(
        &mut self,
        token_id: &str,
        to_address: &str,
        amount: u64,
        caller: &str,
    ) -> Result<(), String> {
        let meta = self.tokens.get(token_id).ok_or("Token not found")?;

        if meta.mint_disabled {
            return Err("Minting permanently disabled".into());
        }
        if meta.mint_authority != caller {
            return Err("Not mint authority".into());
        }
        if amount == 0 {
            return Err("Amount must be > 0".into());
        }

        // Supply cap check
        let new_supply = meta
            .current_supply
            .checked_add(amount)
            .ok_or("Supply overflow")?;
        if meta.max_supply > 0 && new_supply > meta.max_supply {
            return Err(format!(
                "Would exceed max supply ({} + {} > {})",
                meta.current_supply, amount, meta.max_supply
            ));
        }

        // Update supply
        self.tokens.get_mut(token_id).unwrap().current_supply = new_supply;

        // Credit balance
        let key = (token_id.to_string(), to_address.to_string());
        *self.balances.entry(key).or_insert(0) += amount;

        Ok(())
    }

    /// Permanently disable minting for a token.
    pub fn disable_minting(&mut self, token_id: &str, caller: &str) -> Result<(), String> {
        let meta = self.tokens.get_mut(token_id).ok_or("Token not found")?;
        if meta.mint_authority != caller {
            return Err("Not mint authority".into());
        }
        meta.mint_disabled = true;
        meta.mint_authority.clear();
        Ok(())
    }

    // ── Burning ─────────────────────────────────────────────────

    /// Burn tokens from an address (must own them).
    pub fn burn(&mut self, token_id: &str, from_address: &str, amount: u64) -> Result<(), String> {
        if !self.tokens.contains_key(token_id) {
            return Err("Token not found".into());
        }
        if amount == 0 {
            return Err("Amount must be > 0".into());
        }

        let key = (token_id.to_string(), from_address.to_string());
        let balance = self.balances.get(&key).copied().unwrap_or(0);
        if balance < amount {
            return Err(format!("Insufficient balance ({} < {})", balance, amount));
        }

        *self.balances.get_mut(&key).unwrap() -= amount;
        self.tokens.get_mut(token_id).unwrap().current_supply -= amount;

        Ok(())
    }

    // ── Transfer ────────────────────────────────────────────────

    /// Transfer tokens between addresses.
    pub fn transfer(
        &mut self,
        token_id: &str,
        from_address: &str,
        to_address: &str,
        amount: u64,
    ) -> Result<(), String> {
        if !self.tokens.contains_key(token_id) {
            return Err("Token not found".into());
        }
        if amount == 0 {
            return Err("Amount must be > 0".into());
        }
        if from_address == to_address {
            return Err("Cannot transfer to self".into());
        }

        // Check frozen
        if self.is_frozen(token_id, from_address) {
            return Err("Sender account is frozen".into());
        }
        if self.is_frozen(token_id, to_address) {
            return Err("Recipient account is frozen".into());
        }

        let from_key = (token_id.to_string(), from_address.to_string());
        let balance = self.balances.get(&from_key).copied().unwrap_or(0);
        if balance < amount {
            return Err(format!("Insufficient balance ({} < {})", balance, amount));
        }

        *self.balances.get_mut(&from_key).unwrap() -= amount;
        let to_key = (token_id.to_string(), to_address.to_string());
        *self.balances.entry(to_key).or_insert(0) += amount;

        Ok(())
    }

    /// Transfer tokens using an allowance (delegated spending).
    pub fn transfer_from(
        &mut self,
        token_id: &str,
        owner: &str,
        spender: &str,
        to_address: &str,
        amount: u64,
    ) -> Result<(), String> {
        if amount == 0 {
            return Err("Amount must be > 0".into());
        }

        let allowance_key = (token_id.to_string(), owner.to_string(), spender.to_string());
        let allowed = self.allowances.get(&allowance_key).copied().unwrap_or(0);
        if allowed < amount {
            return Err(format!("Insufficient allowance ({} < {})", allowed, amount));
        }

        // Do the transfer
        self.transfer(token_id, owner, to_address, amount)?;

        // Deduct allowance
        *self.allowances.get_mut(&allowance_key).unwrap() -= amount;

        Ok(())
    }

    // ── Approvals ───────────────────────────────────────────────

    /// Set spending allowance for a spender on owner's tokens.
    pub fn approve(
        &mut self,
        token_id: &str,
        owner: &str,
        spender: &str,
        amount: u64,
    ) -> Result<(), String> {
        if !self.tokens.contains_key(token_id) {
            return Err("Token not found".into());
        }
        if owner == spender {
            return Err("Cannot approve self".into());
        }

        let key = (token_id.to_string(), owner.to_string(), spender.to_string());
        self.allowances.insert(key, amount);
        Ok(())
    }

    /// Get current allowance.
    pub fn allowance(&self, token_id: &str, owner: &str, spender: &str) -> u64 {
        self.allowances
            .get(&(token_id.to_string(), owner.to_string(), spender.to_string()))
            .copied()
            .unwrap_or(0)
    }

    // ── Freeze / Thaw ───────────────────────────────────────────

    /// Freeze an account (only freeze authority).
    pub fn freeze(&mut self, token_id: &str, address: &str, caller: &str) -> Result<(), String> {
        let meta = self.tokens.get(token_id).ok_or("Token not found")?;
        if meta.freeze_authority.is_empty() {
            return Err("No freeze authority set".into());
        }
        if meta.freeze_authority != caller {
            return Err("Not freeze authority".into());
        }
        self.frozen
            .insert((token_id.to_string(), address.to_string()), true);
        Ok(())
    }

    /// Thaw (unfreeze) an account.
    pub fn thaw(&mut self, token_id: &str, address: &str, caller: &str) -> Result<(), String> {
        let meta = self.tokens.get(token_id).ok_or("Token not found")?;
        if meta.freeze_authority.is_empty() {
            return Err("No freeze authority set".into());
        }
        if meta.freeze_authority != caller {
            return Err("Not freeze authority".into());
        }
        self.frozen
            .remove(&(token_id.to_string(), address.to_string()));
        Ok(())
    }

    /// Check if an account is frozen for a token.
    pub fn is_frozen(&self, token_id: &str, address: &str) -> bool {
        self.frozen
            .get(&(token_id.to_string(), address.to_string()))
            .copied()
            .unwrap_or(false)
    }

    // ── Queries ─────────────────────────────────────────────────

    /// Get token balance for an address.
    pub fn balance_of(&self, token_id: &str, address: &str) -> u64 {
        self.balances
            .get(&(token_id.to_string(), address.to_string()))
            .copied()
            .unwrap_or(0)
    }

    /// Get token metadata by ID.
    pub fn get_token(&self, token_id: &str) -> Option<&TokenMeta> {
        self.tokens.get(token_id)
    }

    /// Get token ID by symbol.
    pub fn get_by_symbol(&self, symbol: &str) -> Option<&TokenMeta> {
        self.symbol_index
            .get(&symbol.to_uppercase())
            .and_then(|id| self.tokens.get(id))
    }

    /// Total number of tokens created.
    pub fn token_count(&self) -> usize {
        self.tokens.len()
    }

    /// List all token IDs.
    pub fn list_tokens(&self) -> Vec<String> {
        self.tokens.keys().cloned().collect()
    }

    /// Get all token holders for a specific token.
    pub fn holders(&self, token_id: &str) -> Vec<(String, u64)> {
        self.balances
            .iter()
            .filter(|((tid, _), bal)| tid == token_id && **bal > 0)
            .map(|((_, addr), bal)| (addr.clone(), *bal))
            .collect()
    }

    /// Registry statistics.
    pub fn stats(&self) -> TokenRegistryStats {
        let total_tokens = self.tokens.len();
        let total_holders: usize = self.balances.values().filter(|b| **b > 0).count();
        let total_supply: u64 = self.tokens.values().map(|t| t.current_supply).sum();
        TokenRegistryStats {
            total_tokens,
            total_holders,
            total_supply_all_tokens: total_supply,
            frozen_accounts: self.frozen.len(),
        }
    }

    // ── Serialization ───────────────────────────────────────────

    pub fn to_dict(&self) -> serde_json::Value {
        let tokens: serde_json::Map<String, serde_json::Value> = self
            .tokens
            .iter()
            .map(|(k, v)| (k.clone(), v.to_dict()))
            .collect();

        // Serialize balances as {"token_id:address": amount}
        let balances: serde_json::Map<String, serde_json::Value> = self
            .balances
            .iter()
            .filter(|(_, bal)| **bal > 0)
            .map(|((tid, addr), bal)| (format!("{}:{}", tid, addr), serde_json::json!(*bal)))
            .collect();

        let allowances: serde_json::Map<String, serde_json::Value> = self
            .allowances
            .iter()
            .filter(|(_, amt)| **amt > 0)
            .map(|((tid, owner, spender), amt)| {
                (
                    format!("{}:{}:{}", tid, owner, spender),
                    serde_json::json!(*amt),
                )
            })
            .collect();

        let frozen: Vec<String> = self
            .frozen
            .iter()
            .filter(|(_, v)| **v)
            .map(|((tid, addr), _)| format!("{}:{}", tid, addr))
            .collect();

        serde_json::json!({
            "tokens": tokens,
            "balances": balances,
            "allowances": allowances,
            "frozen": frozen,
        })
    }

    pub fn from_dict(v: &serde_json::Value) -> Self {
        let mut reg = Self::new();

        if let Some(tokens) = v["tokens"].as_object() {
            for (id, tv) in tokens {
                if let Ok(meta) = TokenMeta::from_dict(tv) {
                    reg.symbol_index
                        .insert(meta.symbol.to_uppercase(), id.clone());
                    reg.tokens.insert(id.clone(), meta);
                }
            }
        }

        if let Some(bals) = v["balances"].as_object() {
            for (key, val) in bals {
                if let Some(pos) = key.find(':') {
                    let tid = &key[..pos];
                    let addr = &key[pos + 1..];
                    if let Some(amt) = val.as_u64() {
                        reg.balances
                            .insert((tid.to_string(), addr.to_string()), amt);
                    }
                }
            }
        }

        if let Some(alls) = v["allowances"].as_object() {
            for (key, val) in alls {
                let parts: Vec<&str> = key.splitn(3, ':').collect();
                if parts.len() == 3 {
                    if let Some(amt) = val.as_u64() {
                        reg.allowances.insert(
                            (
                                parts[0].to_string(),
                                parts[1].to_string(),
                                parts[2].to_string(),
                            ),
                            amt,
                        );
                    }
                }
            }
        }

        if let Some(frozen) = v["frozen"].as_array() {
            for entry in frozen {
                if let Some(s) = entry.as_str() {
                    if let Some(pos) = s.find(':') {
                        let tid = &s[..pos];
                        let addr = &s[pos + 1..];
                        reg.frozen.insert((tid.to_string(), addr.to_string()), true);
                    }
                }
            }
        }

        reg
    }
}

impl Default for TokenRegistry {
    fn default() -> Self {
        Self::new()
    }
}

/// Token registry statistics.
#[derive(Debug, Clone)]
pub struct TokenRegistryStats {
    pub total_tokens: usize,
    pub total_holders: usize,
    pub total_supply_all_tokens: u64,
    pub frozen_accounts: usize,
}

// ── Helpers ──────────────────────────────────────────────────────────────────

fn now_f64() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn setup_registry() -> (TokenRegistry, String) {
        let mut reg = TokenRegistry::new();
        let id = reg
            .create_token(
                "Test Token",
                "TEST",
                8,
                1_000_000_00000000, // 1M tokens with 8 decimals
                "creator_addr",
                "creator_addr", // mint authority
                "creator_addr", // freeze authority
                100,
                0,
            )
            .unwrap();
        (reg, id)
    }

    // ── Creation ────────────────────────────────────────────────

    #[test]
    fn test_create_token() {
        let (reg, id) = setup_registry();
        assert_eq!(reg.token_count(), 1);
        let meta = reg.get_token(&id).unwrap();
        assert_eq!(meta.name, "Test Token");
        assert_eq!(meta.symbol, "TEST");
        assert_eq!(meta.decimals, 8);
        assert_eq!(meta.current_supply, 0);
        assert_eq!(meta.creator, "creator_addr");
    }

    #[test]
    fn test_create_duplicate_symbol() {
        let (mut reg, _) = setup_registry();
        let result = reg.create_token("Another", "TEST", 8, 0, "other", "other", "", 200, 1);
        assert!(result.is_err());
    }

    #[test]
    fn test_create_reserved_symbol() {
        let mut reg = TokenRegistry::new();
        assert!(
            reg.create_token("Credits", "CR", 8, 0, "a", "a", "", 0, 0)
                .is_err()
        );
        assert!(
            reg.create_token("RPNT Token", "RPNT", 8, 0, "a", "a", "", 0, 0)
                .is_err()
        );
    }

    #[test]
    fn test_create_invalid_symbol() {
        let mut reg = TokenRegistry::new();
        assert!(reg.create_token("T", "", 8, 0, "a", "a", "", 0, 0).is_err()); // Empty
        assert!(
            reg.create_token("T", "A B", 8, 0, "a", "a", "", 0, 0)
                .is_err()
        ); // Space
        assert!(
            reg.create_token("T", "AB!C", 8, 0, "a", "a", "", 0, 0)
                .is_err()
        ); // Special char
    }

    #[test]
    fn test_create_invalid_decimals() {
        let mut reg = TokenRegistry::new();
        assert!(
            reg.create_token("T", "TOK", 19, 0, "a", "a", "", 0, 0)
                .is_err()
        );
    }

    #[test]
    fn test_token_id_deterministic() {
        let id1 = token_id("creator", "SYM", 42);
        let id2 = token_id("creator", "SYM", 42);
        assert_eq!(id1, id2);
        assert_eq!(id1.len(), 32);
    }

    #[test]
    fn test_token_id_different_inputs() {
        let id1 = token_id("creator1", "SYM", 0);
        let id2 = token_id("creator2", "SYM", 0);
        assert_ne!(id1, id2);
    }

    // ── Minting ─────────────────────────────────────────────────

    #[test]
    fn test_mint() {
        let (mut reg, id) = setup_registry();
        reg.mint(&id, "alice", 1000, "creator_addr").unwrap();
        assert_eq!(reg.balance_of(&id, "alice"), 1000);
        assert_eq!(reg.get_token(&id).unwrap().current_supply, 1000);
    }

    #[test]
    fn test_mint_not_authority() {
        let (mut reg, id) = setup_registry();
        assert!(reg.mint(&id, "alice", 1000, "hacker").is_err());
    }

    #[test]
    fn test_mint_exceeds_max_supply() {
        let mut reg = TokenRegistry::new();
        let id = reg
            .create_token("T", "TOK", 0, 100, "c", "c", "", 0, 0)
            .unwrap();
        reg.mint(&id, "alice", 100, "c").unwrap();
        assert!(reg.mint(&id, "alice", 1, "c").is_err()); // Over max
    }

    #[test]
    fn test_mint_zero() {
        let (mut reg, id) = setup_registry();
        assert!(reg.mint(&id, "alice", 0, "creator_addr").is_err());
    }

    #[test]
    fn test_disable_minting() {
        let (mut reg, id) = setup_registry();
        reg.mint(&id, "alice", 1000, "creator_addr").unwrap();
        reg.disable_minting(&id, "creator_addr").unwrap();
        assert!(reg.mint(&id, "alice", 1, "creator_addr").is_err());
        assert!(reg.get_token(&id).unwrap().mint_disabled);
    }

    #[test]
    fn test_mint_unlimited_supply() {
        let mut reg = TokenRegistry::new();
        let id = reg
            .create_token("Inf", "INF", 0, 0, "c", "c", "", 0, 0)
            .unwrap();
        // max_supply=0 means unlimited
        reg.mint(&id, "alice", u64::MAX / 2, "c").unwrap();
        assert_eq!(reg.balance_of(&id, "alice"), u64::MAX / 2);
    }

    // ── Burning ─────────────────────────────────────────────────

    #[test]
    fn test_burn() {
        let (mut reg, id) = setup_registry();
        reg.mint(&id, "alice", 1000, "creator_addr").unwrap();
        reg.burn(&id, "alice", 300).unwrap();
        assert_eq!(reg.balance_of(&id, "alice"), 700);
        assert_eq!(reg.get_token(&id).unwrap().current_supply, 700);
    }

    #[test]
    fn test_burn_insufficient() {
        let (mut reg, id) = setup_registry();
        reg.mint(&id, "alice", 100, "creator_addr").unwrap();
        assert!(reg.burn(&id, "alice", 200).is_err());
    }

    #[test]
    fn test_burn_zero() {
        let (mut reg, id) = setup_registry();
        assert!(reg.burn(&id, "alice", 0).is_err());
    }

    // ── Transfer ────────────────────────────────────────────────

    #[test]
    fn test_transfer() {
        let (mut reg, id) = setup_registry();
        reg.mint(&id, "alice", 1000, "creator_addr").unwrap();
        reg.transfer(&id, "alice", "bob", 400).unwrap();
        assert_eq!(reg.balance_of(&id, "alice"), 600);
        assert_eq!(reg.balance_of(&id, "bob"), 400);
    }

    #[test]
    fn test_transfer_insufficient() {
        let (mut reg, id) = setup_registry();
        reg.mint(&id, "alice", 100, "creator_addr").unwrap();
        assert!(reg.transfer(&id, "alice", "bob", 200).is_err());
    }

    #[test]
    fn test_transfer_to_self() {
        let (mut reg, id) = setup_registry();
        reg.mint(&id, "alice", 100, "creator_addr").unwrap();
        assert!(reg.transfer(&id, "alice", "alice", 50).is_err());
    }

    #[test]
    fn test_transfer_zero() {
        let (mut reg, id) = setup_registry();
        reg.mint(&id, "alice", 100, "creator_addr").unwrap();
        assert!(reg.transfer(&id, "alice", "bob", 0).is_err());
    }

    // ── Approvals / Delegated Transfer ──────────────────────────

    #[test]
    fn test_approve_and_transfer_from() {
        let (mut reg, id) = setup_registry();
        reg.mint(&id, "alice", 1000, "creator_addr").unwrap();
        reg.approve(&id, "alice", "delegate", 500).unwrap();
        assert_eq!(reg.allowance(&id, "alice", "delegate"), 500);

        reg.transfer_from(&id, "alice", "delegate", "bob", 300)
            .unwrap();
        assert_eq!(reg.balance_of(&id, "alice"), 700);
        assert_eq!(reg.balance_of(&id, "bob"), 300);
        assert_eq!(reg.allowance(&id, "alice", "delegate"), 200);
    }

    #[test]
    fn test_transfer_from_exceeds_allowance() {
        let (mut reg, id) = setup_registry();
        reg.mint(&id, "alice", 1000, "creator_addr").unwrap();
        reg.approve(&id, "alice", "delegate", 100).unwrap();
        assert!(
            reg.transfer_from(&id, "alice", "delegate", "bob", 200)
                .is_err()
        );
    }

    #[test]
    fn test_approve_self() {
        let (mut reg, id) = setup_registry();
        assert!(reg.approve(&id, "alice", "alice", 100).is_err());
    }

    // ── Freeze / Thaw ───────────────────────────────────────────

    #[test]
    fn test_freeze_blocks_transfer() {
        let (mut reg, id) = setup_registry();
        reg.mint(&id, "alice", 1000, "creator_addr").unwrap();
        reg.freeze(&id, "alice", "creator_addr").unwrap();
        assert!(reg.is_frozen(&id, "alice"));
        assert!(reg.transfer(&id, "alice", "bob", 100).is_err());
    }

    #[test]
    fn test_thaw_enables_transfer() {
        let (mut reg, id) = setup_registry();
        reg.mint(&id, "alice", 1000, "creator_addr").unwrap();
        reg.freeze(&id, "alice", "creator_addr").unwrap();
        reg.thaw(&id, "alice", "creator_addr").unwrap();
        assert!(!reg.is_frozen(&id, "alice"));
        reg.transfer(&id, "alice", "bob", 100).unwrap();
    }

    #[test]
    fn test_freeze_not_authority() {
        let (mut reg, id) = setup_registry();
        assert!(reg.freeze(&id, "alice", "hacker").is_err());
    }

    #[test]
    fn test_freeze_receiver_blocked() {
        let (mut reg, id) = setup_registry();
        reg.mint(&id, "alice", 1000, "creator_addr").unwrap();
        reg.freeze(&id, "bob", "creator_addr").unwrap();
        assert!(reg.transfer(&id, "alice", "bob", 100).is_err());
    }

    // ── Queries ─────────────────────────────────────────────────

    #[test]
    fn test_get_by_symbol() {
        let (reg, id) = setup_registry();
        let meta = reg.get_by_symbol("test").unwrap(); // case-insensitive
        assert_eq!(meta.id, id);
    }

    #[test]
    fn test_holders() {
        let (mut reg, id) = setup_registry();
        reg.mint(&id, "alice", 500, "creator_addr").unwrap();
        reg.mint(&id, "bob", 300, "creator_addr").unwrap();
        let holders = reg.holders(&id);
        assert_eq!(holders.len(), 2);
    }

    #[test]
    fn test_stats() {
        let (mut reg, id) = setup_registry();
        reg.mint(&id, "alice", 1000, "creator_addr").unwrap();
        let stats = reg.stats();
        assert_eq!(stats.total_tokens, 1);
        assert_eq!(stats.total_holders, 1);
        assert_eq!(stats.total_supply_all_tokens, 1000);
    }

    // ── Serialization ───────────────────────────────────────────

    #[test]
    fn test_registry_roundtrip() {
        let (mut reg, id) = setup_registry();
        reg.mint(&id, "alice", 5000, "creator_addr").unwrap();
        reg.mint(&id, "bob", 3000, "creator_addr").unwrap();
        reg.approve(&id, "alice", "delegate", 1000).unwrap();
        reg.freeze(&id, "bob", "creator_addr").unwrap();

        let dict = reg.to_dict();
        let reg2 = TokenRegistry::from_dict(&dict);

        assert_eq!(reg2.token_count(), 1);
        assert_eq!(reg2.balance_of(&id, "alice"), 5000);
        assert_eq!(reg2.balance_of(&id, "bob"), 3000);
        assert_eq!(reg2.allowance(&id, "alice", "delegate"), 1000);
        assert!(reg2.is_frozen(&id, "bob"));
        assert_eq!(reg2.get_by_symbol("TEST").unwrap().id, id);
    }

    #[test]
    fn test_token_meta_roundtrip() {
        let meta = TokenMeta {
            id: "abcdef1234567890abcdef1234567890".into(),
            name: "My Token".into(),
            symbol: "MTK".into(),
            decimals: 6,
            current_supply: 999,
            max_supply: 10000,
            mint_authority: "mint_addr".into(),
            freeze_authority: "freeze_addr".into(),
            creator: "creator".into(),
            created_at_block: 42,
            created_at: 1234567890.0,
            mint_disabled: false,
        };
        let dict = meta.to_dict();
        let meta2 = TokenMeta::from_dict(&dict).unwrap();
        assert_eq!(meta.id, meta2.id);
        assert_eq!(meta.name, meta2.name);
        assert_eq!(meta.decimals, meta2.decimals);
        assert_eq!(meta.current_supply, meta2.current_supply);
    }

    // ── Multiple Tokens ─────────────────────────────────────────

    #[test]
    fn test_multiple_tokens() {
        let mut reg = TokenRegistry::new();
        let id1 = reg
            .create_token("Alpha", "ALPHA", 8, 0, "c", "c", "", 0, 0)
            .unwrap();
        let id2 = reg
            .create_token("Beta", "BETA", 6, 0, "c", "c", "", 0, 1)
            .unwrap();

        reg.mint(&id1, "alice", 1000, "c").unwrap();
        reg.mint(&id2, "alice", 2000, "c").unwrap();

        assert_eq!(reg.balance_of(&id1, "alice"), 1000);
        assert_eq!(reg.balance_of(&id2, "alice"), 2000);

        // Transfer token 1 doesn't affect token 2
        reg.transfer(&id1, "alice", "bob", 100).unwrap();
        assert_eq!(reg.balance_of(&id1, "alice"), 900);
        assert_eq!(reg.balance_of(&id2, "alice"), 2000); // Unchanged
    }
}
