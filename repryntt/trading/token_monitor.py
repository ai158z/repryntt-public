import asyncio
import aiohttp
import aiosqlite
import aiofiles
import json
import logging
import os
import sys
import time
import random
from datetime import datetime, timezone, timedelta 
import shutil
import sqlite3
from collections import Counter
from repryntt.trading.dexscreener_discovery import dexscreener_discovery_loop

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Constants & Directory Paths
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_MODULE_DIR, "data")
MIN_DATA_POINTS = 5  # Minimum number of data points required for analysis
WATCH_DIR = os.path.join(_DATA_DIR, "watch_dir")
ARCHIVE_DIR = os.path.join(WATCH_DIR, "archived")
ACTIVE_TOKENS_FILE = os.path.join(_DATA_DIR, "active_tokens.json")
SIGNALS_DIR = os.path.join(_DATA_DIR, "signal_tokens")
RATE_LIMIT_TOKENS_PER_MIN = 240  # DexScreener: 240/min (4/sec)
ALCHEMY_RATE_LIMIT_PER_HOUR = 10000  # Alchemy: 10k/hr (~2.78/sec)
PRICE_REMOVAL_THRESHOLD = 0.000030
MARKET_CAP_THRESHOLD = 30000
MAX_ACTIVE_TOKENS = 20  # Hard cap — keeps API costs down and price checks fast
TOKEN_MAX_AGE_HOURS = 3  # Force-evict tokens older than this (unless protected by open position)
DAILY_RESET_HOUR_UTC = 0  # Hour (UTC) to do a full daily token refresh
GAINER_SCAN_INTERVAL = 300  # Seconds between gainer scans (5 min for swing signals)
GAINER_MIN_CHANGE_5M = 8.0  # Minimum 5m price change % to qualify as a gainer signal
GAINER_MIN_VOLUME_5M = 500  # Minimum 5m volume ($) for gainer signals
GAINER_COOLDOWN = 900  # Don't re-signal the same gainer for 15 minutes
PROFIT_TARGET = 0.06
STOP_LOSS = -0.025
MAX_HOLD_TIME = 15
TS_PRICE_HISTORY_LIMIT = 600  # ~5hrs at 30s polling — enough for 1h tracking
TS_TP1_THRESHOLD = 0.55
TS_TP2_DROP = 0.3
TS_TP2_RECOVERY = 0.4
TS_TP3_DROPS = [-0.3, -0.1, -0.2]
TRADE_EXPORT_FILE = os.path.join(_DATA_DIR, "trade_signals.json")
MIN_30S_PRICE_CHANGE = 3.0  # Legacy — kept for reference
MIN_5M_PRICE_CHANGE = 5.0  # Swing gate: 5% in 5m for TP2/TP3 export
MIN_15M_PRICE_CHANGE = 3.0  # Swing gate: 3% in 15m for momentum export
ENTRY_PRICE_INCREASE_PERCENT = 0
LOW_MARKET_CAP_CHECK_INTERVAL = 60
POOL_ACTIVITY_THRESHOLD = 50
LARGE_TRADE_THRESHOLD = 100
LARGE_VOLUME_THRESHOLD = 10000
LARGE_TRADE_VALUE_THRESHOLD = 500
RAYDIUM_AUTHORITY_ADDRESS = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
# Addresses to exclude from holder concentration (LP pools, burn, etc.)
# Pool wallets hold tokens available for purchase — not rug-risk holders
EXCLUDE_FROM_HOLDERS = frozenset({
    RAYDIUM_AUTHORITY_ADDRESS,
    "11111111111111111111111111111111",  # System / burn
})

# Known AMM / DEX program IDs — token accounts OWNED by these programs (or their
# PDAs) are liquidity pool vaults, not human holders.  When the SPL Token account
# owner field (bytes 32-64) matches one of these, we exclude the account.
KNOWN_AMM_PROGRAMS = frozenset({
    # PumpSwap / Pump.fun AMM
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",   # PumpFun bonding curve
    "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg",  # PumpFun fee/migration
    # Raydium
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium Authority v4
    "routeUGWgWzqBWFcrCfv8tritsqukccJPu3q5GPP3xS",   # Raydium Route
    # Orca / Whirlpool
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",   # Orca Whirlpool
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP",  # Orca swap v2
    # Meteora
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",   # Meteora DLMM
    # OpenBook / Serum
    "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX",    # Serum DEX v3
    "opnb2LAfJYbRMAHHvqjCwQxanZn7ReEHp1k81EQMQvR",   # OpenBook v2
})

def _amm_label(program_id: str) -> str:
    """Return a human-readable label for a known AMM program address."""
    _LABELS = {
        "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap LP",
        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "PumpFun LP",
        "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg": "PumpFun LP",
        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium LP",
        "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM LP",
        "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1": "Raydium Authority",
        "routeUGWgWzqBWFcrCfv8tritsqukccJPu3q5GPP3xS": "Raydium LP",
        "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca LP",
        "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP": "Orca LP",
        "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora LP",
        "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX": "Serum LP",
        "opnb2LAfJYbRMAHHvqjCwQxanZn7ReEHp1k81EQMQvR": "OpenBook LP",
    }
    return _LABELS.get(program_id, "LP Pool")

# ── Load .env if present (trading_bot/.env) ──
_dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
if os.path.exists(_dotenv_path):
    with open(_dotenv_path) as _ef:
        for _line in _ef:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                os.environ.setdefault(_k.strip(), _v.strip())

SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://solana-mainnet.g.alchemy.com/v2/YOUR_ALCHEMY_API_KEY")
ALCHEMY_API_KEY = os.environ.get("ALCHEMY_API_KEY", "YOUR_ALCHEMY_API_KEY")
HOLDER_UPDATE_INTERVAL_ACTIVE = 60
HOLDER_UPDATE_INTERVAL = 90
UPTREND_MIN_INCREASE = 2.0
UPTREND_CONSECUTIVE = 2
DOWNTREND_DROP_THRESHOLD = 10.0
DOWNTREND_TIME_WINDOW = 60
AI_INPUT_DIR = os.path.join(_DATA_DIR, "ai_input")
TOKEN_PROFILES_DIR = os.path.join(_DATA_DIR, "token_profiles")
MINIMUM_ENTRY_THRESHOLD = 20000  # Minimum price and market cap threshold for new tokens ($20,000)

# DEX IDs that represent bonding curve / pre-graduation pools.
# Tokens showing these with $0 liquidity are still on the bonding curve.
BONDING_CURVE_DEX_IDS = frozenset({"pumpfun", "launchlab"})


def _pick_best_pair(pairs: list) -> dict:
    """From a list of DexScreener pair dicts, return the one most likely to be
    the real graduated pool.  Preference order:
      1. Highest USD liquidity among non-bonding-curve DEXes
      2. Highest USD liquidity overall (fallback)
      3. First entry (ultimate fallback)
    """
    if not pairs:
        return {}
    if len(pairs) == 1:
        return pairs[0]

    graduated = [
        p for p in pairs
        if p.get("dexId", "") not in BONDING_CURVE_DEX_IDS
        and (p.get("liquidity") or {}).get("usd", 0) > 0
    ]
    if graduated:
        return max(graduated, key=lambda p: (p.get("liquidity") or {}).get("usd", 0))

    # No graduated pair with liquidity — pick highest liquidity overall
    with_liq = [p for p in pairs if (p.get("liquidity") or {}).get("usd", 0) > 0]
    if with_liq:
        return max(with_liq, key=lambda p: (p.get("liquidity") or {}).get("usd", 0))

    return pairs[0]


###############################################################################
# Trade Signal Tracking Classes & Helper Functions
###############################################################################

class TradeSignalToken:
    def __init__(self, token_name, initial_price, address):
        self.token_name = token_name
        self.address = address  # Store address to access profile file
        self.price_history = self.load_price_history()  # Load existing history
        # If no history exists or it's empty, start with the initial price
        if not self.price_history:
            self.price_history = [(time.time(), initial_price)]
        self.lows_history = []  # Store post-ATH lows
        self.pool_buys_5m = 0
        self.pool_sells_5m = 0
        self.pool_volume_5m = 0.0
        self.buy_volume_5m = 0.0
        self.sell_volume_5m = 0.0
        self.top_20_holders_percentage = 0.0
        self.last_holder_update = 0

    def load_price_history(self):
        """Load existing price history and lows history from the token profile file."""
        profile_file = os.path.join(TOKEN_PROFILES_DIR, f"{self.address}.json")
        if os.path.exists(profile_file):
            try:
                with open(profile_file, "r") as f:
                    profile_data = json.load(f)
                    self.price_history = profile_data.get("price_history", [])
                    self.lows_history = profile_data.get("lows_history", [])  # Load lows_history
                    return self.price_history
            except Exception as e:
                logging.error(f"Error loading price history for {self.address}: {e}")
        return []

    def update_price(self, new_price, buys_5m=None, sells_5m=None, volume_5m=None):
        current_time = time.time()
        if self.price_history and self.price_history[-1][1] == new_price:
            logging.debug(f"Skipping duplicate price {new_price} for {self.token_name}")
            return
        self.price_history.append((current_time, new_price))
        if buys_5m is not None:
            self.pool_buys_5m = buys_5m
        if sells_5m is not None:
            self.pool_sells_5m = sells_5m
        if volume_5m is not None:
            self.pool_volume_5m = volume_5m
            total_trades = self.pool_buys_5m + self.pool_sells_5m
            if total_trades > 0:
                buy_ratio = self.pool_buys_5m / total_trades
                self.buy_volume_5m = self.pool_volume_5m * buy_ratio
                self.sell_volume_5m = self.pool_volume_5m * (1 - buy_ratio)
            else:
                self.buy_volume_5m = 0.0
                self.sell_volume_5m = 0.0
        logging.debug(f"Updated {self.token_name}: price={new_price}, buys_5m={self.pool_buys_5m}, sells_5m={self.pool_sells_5m}, volume_5m={self.pool_volume_5m}")

    def should_update_holders(self, is_active=True):
        current_time = time.time()
        interval = HOLDER_UPDATE_INTERVAL_ACTIVE if is_active else HOLDER_UPDATE_INTERVAL
        return current_time - self.last_holder_update >= interval

    def get_price_changes(self, current_time=None, current_price=None):
        if current_time is None:
            current_time = time.time()
        if current_price is None and self.price_history:
            current_time, current_price = self.price_history[-1]
        elif not self.price_history:
            return {
                "price_change_5s": 0.0,
                "price_change_15s": 0.0,
                "price_change_30s": 0.0,
                "price_change_1m": 0.0,
                "price_change_5m": 0.0,
                "price_change_15m": 0.0,
                "price_change_30m": 0.0,
            }
        
        intervals = [
            (5, "price_change_5s"),
            (15, "price_change_15s"),
            (30, "price_change_30s"),
            (60, "price_change_1m"),
            (300, "price_change_5m"),
            (900, "price_change_15m"),
            (1800, "price_change_30m"),
        ]
        changes = {}
        
        for interval, label in intervals:
            target_time = current_time - interval
            previous_price = None
            for t, p in reversed(self.price_history[:-1]):
                if t <= target_time:
                    previous_price = p
                    break
            if previous_price is not None and previous_price != 0:
                change = ((current_price - previous_price) / previous_price) * 100
                changes[label] = round(change, 2)
            else:
                changes[label] = 0.0
        
        return changes

    def check_higher_lows(self):
        """Detect if latest post-ATH low is higher than previous."""
        if len(self.price_history) < MIN_DATA_POINTS:
            return False, None, None

        # Detect ATHs and lows
        dips = []
        ath_price = 0
        ath_indices = []
        
        for i in range(len(self.price_history)):
            price = float(self.price_history[i][1])
            if i == 0 or price > ath_price:
                ath_price = price
                ath_indices.append(i)
        
        for j in range(len(ath_indices)):
            start_idx = ath_indices[j]
            end_idx = ath_indices[j + 1] if j + 1 < len(ath_indices) else len(self.price_history)
            
            low_price = float(self.price_history[start_idx][1])
            low_time = float(self.price_history[start_idx][0])
            for k in range(start_idx + 1, end_idx):
                price = float(self.price_history[k][1])
                ts = float(self.price_history[k][0])
                if price < low_price:
                    low_price = price
                    low_time = ts
            ath_at_start = float(self.price_history[start_idx][1])
            if ath_at_start - low_price >= ath_at_start * 0.01:  # Significant dip
                dips.append([low_time, low_price])
        
        # Update lows_history
        if dips:
            self.lows_history.extend(dips)
            self.lows_history = sorted([l for l in self.lows_history if time.time() - l[0] <= 86400], key=lambda x: x[0])  # 24 hours
        
        if len(self.lows_history) < 2:
            return False, None, None

        latest_low = self.lows_history[-1][1]
        prev_low = self.lows_history[-2][1]
        is_higher_low = latest_low > prev_low
        
        return is_higher_low, latest_low, prev_low

    def check_trade_conditions(self):
        if len(self.price_history) < 5:
            return None
        current_price = self.price_history[-1][1]
        initial_price = self.price_history[0][1]
        
        # Check for higher lows
        is_higher_low, latest_low, prev_low = self.check_higher_lows()
        if is_higher_low:
            return "Higher Low Buy"
        
        if (current_price / initial_price) >= (1 + TS_TP1_THRESHOLD):
            return "TP1 Buy"
        for i in range(len(self.price_history) - 1):
            try:
                dip = (self.price_history[i][1] - self.price_history[i + 1][1]) / self.price_history[i][1]
            except ZeroDivisionError:
                continue
            if dip >= TS_TP2_DROP:
                for j in range(i + 1, len(self.price_history)):
                    try:
                        recovery = (self.price_history[j][1] - self.price_history[i + 1][1]) / self.price_history[i + 1][1]
                    except ZeroDivisionError:
                        continue
                    if recovery >= TS_TP2_RECOVERY:
                        return "TP2 Buy"
        dip_stages = []
        for i in range(len(self.price_history) - 1):
            try:
                dip = (self.price_history[i][1] - self.price_history[i + 1][1]) / self.price_history[i][1]
            except ZeroDivisionError:
                continue
            if dip < 0:
                dip_stages.append(dip)
        if (len(dip_stages) >= len(TS_TP3_DROPS) and 
            all(abs(dip_stages[i]) >= abs(TS_TP3_DROPS[i]) for i in range(len(TS_TP3_DROPS)))):
            return "TP3 Buy"
        
        if (self.pool_buys_5m >= LARGE_TRADE_THRESHOLD or self.buy_volume_5m >= LARGE_TRADE_VALUE_THRESHOLD) and self.pool_sells_5m < self.pool_buys_5m * 0.5:
            return "Large Buy Detected"
        if (self.pool_sells_5m >= LARGE_TRADE_THRESHOLD or self.sell_volume_5m >= LARGE_TRADE_VALUE_THRESHOLD) and self.pool_buys_5m < self.pool_sells_5m * 0.5:
            return "Large Sell Detected"
        
        if self.pool_buys_5m >= POOL_ACTIVITY_THRESHOLD and self.pool_sells_5m < self.pool_buys_5m * 0.5:
            return "Pool Buy Surge"
        if self.pool_sells_5m >= POOL_ACTIVITY_THRESHOLD and self.pool_buys_5m < self.pool_sells_5m * 0.5:
            return "Pool Sell Surge"
        
        return None

    def check_bundling(self, holder_data):
        top_20_percentage = holder_data["percentage"]
        top_20_holders = holder_data["top_holders"]
        
        SINGLE_HOLDER_THRESHOLD = 30.0
        TOP_5_THRESHOLD = 70.0
        TOP_20_THRESHOLD = 90.0
        UNIFORMITY_THRESHOLD = 5

        # LP/burn accounts are already excluded upstream by get_top_20_holders_percentage,
        # but double-check as a safety net
        filtered_holders = [
            h for h in top_20_holders
            if h["address"] not in EXCLUDE_FROM_HOLDERS and h["address"] not in KNOWN_AMM_PROGRAMS
        ]
        
        if not filtered_holders:
            return {
                "is_bundled": False,
                "reason": "No holders after excluding LP/burn addresses",
                "top_20_percentage": top_20_percentage
            }

        dominant_holder = filtered_holders[0]
        if dominant_holder["percentage"] >= SINGLE_HOLDER_THRESHOLD:
            return {
                "is_bundled": True,
                "reason": f"Single holder {dominant_holder['address'][:6]}... owns {dominant_holder['percentage']}% (>= {SINGLE_HOLDER_THRESHOLD}%)",
                "top_holder_percentage": dominant_holder["percentage"]
            }
        
        top_5_total = sum(h["percentage"] for h in filtered_holders[:5]) if len(filtered_holders) >= 5 else top_20_percentage
        if top_5_total >= TOP_5_THRESHOLD:
            return {
                "is_bundled": True,
                "reason": f"Top 5 holders own {top_5_total}% (>= {TOP_5_THRESHOLD}%)",
                "top_5_percentage": top_5_total
            }
        
        if top_20_percentage >= TOP_20_THRESHOLD:
            return {
                "is_bundled": True,
                "reason": f"Top 20 holders own {top_20_percentage}% (>= {TOP_20_THRESHOLD}%)",
                "top_20_percentage": top_20_percentage
            }
        
        holder_percentages = [h["percentage"] for h in filtered_holders]
        percentage_counts = Counter([round(p, 2) for p in holder_percentages])
        most_common = percentage_counts.most_common(1)
        if most_common and most_common[0][1] >= UNIFORMITY_THRESHOLD:
            uniform_percentage = most_common[0][0]
            uniform_count = most_common[0][1]
            return {
                "is_bundled": True,
                "reason": f"{uniform_count} holders own identical {uniform_percentage}% each (>= {UNIFORMITY_THRESHOLD} identical wallets)",
                "uniform_percentage": uniform_percentage,
                "uniform_count": uniform_count
            }
        
        return {
            "is_bundled": False,
            "reason": "No significant bundling detected",
            "top_20_percentage": top_20_percentage
        }

class TradeSignalTracker:
    def __init__(self):
        self.exported_tokens = {}
        self.EXPORT_COOLDOWN = 120  # 2 min cooldown — quality over quantity

    def can_export_token(self, token_address):
        current_time = time.time()
        if token_address in self.exported_tokens:
            last_export = self.exported_tokens[token_address]
            if current_time - last_export < self.EXPORT_COOLDOWN:
                return False
        return True

    def mark_token_exported(self, token_address):
        self.exported_tokens[token_address] = time.time()

trade_signal_trackers = {}

async def save_trade_signal(token_name, signal, price):
    trade_data = {
        "token_name": token_name,
        "trade_signal": signal,
        "price": round(price, 8),
        "timestamp": time.time()
    }
    signals = []
    if os.path.exists(TRADE_EXPORT_FILE):
        try:
            async with aiofiles.open(TRADE_EXPORT_FILE, "r") as f:
                content = await f.read()
                signals = json.loads(content) if content else []
        except Exception:
            signals = []
    signals.append(trade_data)
    async with aiofiles.open(TRADE_EXPORT_FILE, "w") as f:
        await f.write(json.dumps(signals, indent=4))

###############################################################################
# Logging, Database & File Processing
###############################################################################

def setup_logging():
    logger = logging.getLogger("TokenMonitor")
    logger.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    return logger

class TokenDatabase:
    def __init__(self, db_path=None, logger=None):
        self.db_path = db_path or os.path.join(_DATA_DIR, "tokens.db")
        self.logger = logger or logging.getLogger(__name__)
        self.db = None

    async def setup(self):
        self.db = await aiosqlite.connect(self.db_path)
        create_table_query = """
        CREATE TABLE IF NOT EXISTS tokens (
            address TEXT PRIMARY KEY,
            token_name TEXT,
            dex_id TEXT,
            pair_address TEXT,
            url TEXT,
            base_token_name TEXT,
            base_token_symbol TEXT,
            quote_token_name TEXT,
            quote_token_symbol TEXT,
            price_native TEXT,
            initial_price REAL,
            initial_market_cap REAL,
            current_price REAL,
            current_market_cap REAL,
            ath_price REAL,
            price_up_counter INTEGER DEFAULT 0,
            market_cap_flags TEXT,
            file_timestamp TEXT,
            tx_id TEXT,
            raw_social_info TEXT,
            last_updated TEXT,
            buys_5m INTEGER DEFAULT 0,
            sells_5m INTEGER DEFAULT 0,
            buy_volume_5m REAL DEFAULT 0.0,
            sell_volume_5m REAL DEFAULT 0.0,
            top_20_holders_percentage REAL DEFAULT 0.0,
            top_20_holders TEXT DEFAULT '[]',
            buys_1h INTEGER DEFAULT 0,
            sells_1h INTEGER DEFAULT 0,
            buys_6h INTEGER DEFAULT 0,
            sells_6h INTEGER DEFAULT 0,
            buys_24h INTEGER DEFAULT 0,
            sells_24h INTEGER DEFAULT 0,
            volume_5m REAL DEFAULT 0,
            volume_1h REAL DEFAULT 0,
            volume_6h REAL DEFAULT 0,
            volume_24h REAL DEFAULT 0,
            price_change_5s REAL DEFAULT 0,
            price_change_15s REAL DEFAULT 0,
            price_change_30s REAL DEFAULT 0,
            price_change_1m REAL DEFAULT 0,
            price_change_5m REAL DEFAULT 0,
            is_bundled INTEGER DEFAULT 0,
            bundling_reason TEXT DEFAULT '',
            is_uptrend INTEGER DEFAULT 0,
            is_downtrend INTEGER DEFAULT 0,
            ath_timestamp TEXT DEFAULT '',
            is_new INTEGER DEFAULT 1,
            first_seen TEXT DEFAULT ''
        )
        """
        await self.db.execute(create_table_query)
        
        for column in [
            ("price_change_15s", "REAL DEFAULT 0"),
            ("price_change_15m", "REAL DEFAULT 0"),
            ("price_change_30m", "REAL DEFAULT 0"),
            ("price_change_1h", "REAL DEFAULT 0"),
            ("is_bundled", "INTEGER DEFAULT 0"),
            ("bundling_reason", "TEXT DEFAULT ''"),
            ("top_20_holders", "TEXT DEFAULT '[]'"),
            ("is_uptrend", "INTEGER DEFAULT 0"),
            ("is_downtrend", "INTEGER DEFAULT 0"),
            ("ath_timestamp", "TEXT DEFAULT ''"),
            ("is_new", "INTEGER DEFAULT 1"),
            ("first_seen", "TEXT DEFAULT ''")
        ]:
            try:
                await self.db.execute(f"ALTER TABLE tokens ADD COLUMN {column[0]} {column[1]}")
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e):
                    self.logger.error(f"Error adding column {column[0]}: {e}")
                    raise
        
        await self.db.commit()
        self.logger.info("Database initialized.")

    async def add_or_update_token(self, token, session=None):
        async with self.db.execute("SELECT * FROM tokens WHERE address = ?", (token["address"],)) as cursor:
            row = await cursor.fetchone()
        now = datetime.now(timezone.utc).isoformat()
        
        market_cap_flags = {"20": False, "30": False, "60": False, "100": False}
        top_20_percentage = 0.0
        top_20_holders_json = "[]"
        is_bundled = 0
        bundling_reason = ""
        is_new = 1 if row is None else row[47]  # Use existing is_new or set to 1 for new tokens
        
        if session:
            pair_addr = token.get("pair_address", "") or (row[3] if row else "")
            holder_data = await self.get_top_20_holders_percentage(
                token["address"], session, pair_address=pair_addr
            )
            top_20_percentage = holder_data["percentage"]
            top_20_holders_json = json.dumps(holder_data["top_holders"])
            token_instance = TradeSignalToken(token.get("token_name", row[1] if row else ""), token.get("price", row[12] if row else 0.0), token["address"])
            bundling_result = token_instance.check_bundling(holder_data)
            is_bundled = 1 if bundling_result["is_bundled"] else 0
            bundling_reason = bundling_result["reason"]
        elif row:
            top_20_percentage = row[25]
            top_20_holders_json = row[26]
            is_bundled = row[42]
            bundling_reason = row[43]

        new_price = token.get("price", 0.0)
        new_market_cap = token.get("market_cap", 0.0)
        
        if row is None:
            # Skip tokens with initial price and market cap both ≤ $20,000
            if new_price <= MINIMUM_ENTRY_THRESHOLD and new_market_cap <= MINIMUM_ENTRY_THRESHOLD:
                self.logger.info(f"Skipping token {token['address']} - initial price ({new_price}) and market cap ({new_market_cap}) are both ≤ ${MINIMUM_ENTRY_THRESHOLD}.")
                return

            total_trades = token.get("buys_5m", 0) + token.get("sells_5m", 0)
            buy_volume_5m = token.get("volume_5m", 0.0) * (token.get("buys_5m", 0) / total_trades) if total_trades > 0 else 0.0
            sell_volume_5m = token.get("volume_5m", 0.0) * (token.get("sells_5m", 0) / total_trades) if total_trades > 0 else 0.0
            await self.db.execute(
                """
                INSERT INTO tokens (
                    address, token_name, dex_id, pair_address, url,
                    base_token_name, base_token_symbol, quote_token_name, quote_token_symbol,
                    price_native, initial_price, initial_market_cap,
                    current_price, current_market_cap, ath_price, price_up_counter,
                    market_cap_flags, file_timestamp, tx_id, raw_social_info,
                    last_updated,
                    buys_5m, sells_5m, buy_volume_5m, sell_volume_5m, top_20_holders_percentage,
                    top_20_holders,
                    buys_1h, sells_1h, buys_6h, sells_6h, buys_24h, sells_24h,
                    volume_5m, volume_1h, volume_6h, volume_24h,
                    price_change_5s, price_change_15s, price_change_30s, price_change_1m, price_change_5m,
                    is_bundled, bundling_reason,
                    is_uptrend, is_downtrend, ath_timestamp, is_new, first_seen
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token["address"],
                    token.get("token_name", ""),
                    token.get("dex_id", ""),
                    token.get("pair_address", ""),
                    token.get("url", ""),
                    token.get("base_token_name", ""),
                    token.get("base_token_symbol", ""),
                    token.get("quote_token_name", ""),
                    token.get("quote_token_symbol", ""),
                    token.get("price_native", ""),
                    new_price,
                    new_market_cap,
                    new_price,
                    new_market_cap,
                    new_price,
                    0,
                    json.dumps(market_cap_flags),
                    token.get("file_timestamp", ""),
                    token.get("tx_id", ""),
                    token.get("raw_social_info", "{}"),
                    now,
                    token.get("buys_5m", 0),
                    token.get("sells_5m", 0),
                    buy_volume_5m,
                    sell_volume_5m,
                    top_20_percentage,
                    top_20_holders_json,
                    token.get("buys_1h", 0),
                    token.get("sells_1h", 0),
                    token.get("buys_6h", 0),
                    token.get("sells_6h", 0),
                    token.get("buys_24h", 0),
                    token.get("sells_24h", 0),
                    token.get("volume_5m", 0.0),
                    token.get("volume_1h", 0.0),
                    token.get("volume_6h", 0.0),
                    token.get("volume_24h", 0.0),
                    0, 0, 0, 0, 0,
                    is_bundled,
                    bundling_reason,
                    0,
                    0,
                    now,
                    is_new,
                    now
                )
            )
            await self.db.commit()
            self.logger.info(f"Inserted new token {token['address']}")
            await update_active_tokens_file(self, self.logger)
        else:
            old_current_price = row[12]
            old_ath = row[14]
            price_up_counter = row[15]
            initial_market_cap = row[11]
            is_uptrend = row[43]
            is_downtrend = row[44]
            ath_timestamp = row[45] or now
            
            try:
                market_cap_flags = json.loads(row[16]) if row[16] else {"20": False, "30": False, "60": False, "100": False}
            except Exception:
                market_cap_flags = {"20": False, "30": False, "60": False, "100": False}
            
            if old_current_price > 0 and new_price >= old_current_price * (1 + UPTREND_MIN_INCREASE / 100):
                price_up_counter += 1
            else:
                price_up_counter = 0
            
            ath_price = max(old_ath, new_price)
            ath_updated = new_price > old_ath
            ath_timestamp = now if ath_updated else ath_timestamp
            
            if price_up_counter >= UPTREND_CONSECUTIVE:
                is_uptrend = 1
                is_downtrend = 0
            else:
                is_uptrend = 0
            
            if ath_price > 0:
                drop_from_ath = ((ath_price - new_price) / ath_price) * 100
                time_since_ath = (datetime.now(timezone.utc) - datetime.fromisoformat(ath_timestamp)).total_seconds()
                if drop_from_ath >= DOWNTREND_DROP_THRESHOLD and time_since_ath >= DOWNTREND_TIME_WINDOW:
                    is_downtrend = 1
                    is_uptrend = 0
                elif not is_uptrend:
                    is_downtrend = 0
            
            thresholds = {"20": 1.20, "30": 1.30, "60": 1.60, "100": 2.00}
            for key, multiplier in thresholds.items():
                if not market_cap_flags.get(key, False) and new_market_cap >= initial_market_cap * multiplier:
                    market_cap_flags[key] = True
            
            total_trades = token.get("buys_5m", row[21]) + token.get("sells_5m", row[22])
            buy_volume_5m = token.get("volume_5m", row[33]) * (token.get("buys_5m", row[21]) / total_trades) if total_trades > 0 else 0.0
            sell_volume_5m = token.get("volume_5m", row[33]) * (token.get("sells_5m", row[22]) / total_trades) if total_trades > 0 else 0.0
            
            await self.db.execute(
                """
                UPDATE tokens
                SET token_name = ?,
                    dex_id = ?,
                    current_price = ?,
                    current_market_cap = ?,
                    ath_price = ?,
                    price_up_counter = ?,
                    market_cap_flags = ?,
                    last_updated = ?,
                    pair_address = ?,
                    raw_social_info = ?,
                    buys_5m = ?,
                    sells_5m = ?,
                    buy_volume_5m = ?,
                    sell_volume_5m = ?,
                    top_20_holders_percentage = ?,
                    top_20_holders = ?,
                    buys_1h = ?,
                    sells_1h = ?,
                    buys_6h = ?,
                    sells_6h = ?,
                    buys_24h = ?,
                    sells_24h = ?,
                    volume_5m = ?,
                    volume_1h = ?,
                    volume_6h = ?,
                    volume_24h = ?,
                    is_bundled = ?,
                    bundling_reason = ?,
                    is_uptrend = ?,
                    is_downtrend = ?,
                    ath_timestamp = ?,
                    is_new = ?
                WHERE address = ?
                """,
                (
                    token.get("token_name", row[1]),
                    token.get("dex_id", row[2]),
                    new_price,
                    new_market_cap,
                    ath_price,
                    price_up_counter,
                    json.dumps(market_cap_flags),
                    now,
                    token.get("pair_address", row[3]),
                    token.get("raw_social_info", row[19]),
                    token.get("buys_5m", row[21]),
                    token.get("sells_5m", row[22]),
                    buy_volume_5m,
                    sell_volume_5m,
                    top_20_percentage,
                    top_20_holders_json,
                    token.get("buys_1h", row[27]),
                    token.get("sells_1h", row[28]),
                    token.get("buys_6h", row[29]),
                    token.get("sells_6h", row[30]),
                    token.get("buys_24h", row[31]),
                    token.get("sells_24h", row[32]),
                    token.get("volume_5m", row[33]),
                    token.get("volume_1h", row[34]),
                    token.get("volume_6h", row[35]),
                    token.get("volume_24h", row[36]),
                    is_bundled,
                    bundling_reason,
                    is_uptrend,
                    is_downtrend,
                    ath_timestamp,
                    is_new,
                    token["address"]
                )
            )
            await self.db.commit()
            self.logger.info(f"Updated token {token['address']}")
            await update_active_tokens_file(self, self.logger)

    async def get_top_20_holders_percentage(self, token_address, session, pair_address: str = ""):
        """Get top 20 holder concentration, excluding LP pool vaults and burn addresses.

        LP detection: if pair_address is provided (from DexScreener), we call
        getTokenAccountsByOwner(pair_address) to find the exact vault token-account
        addresses the pool holds for this mint.  Those are excluded from the
        holder list so LP liquidity isn't mistaken for whale concentration.
        """
        try:
            supply_payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenSupply",
                "params": [token_address],
            }
            async with session.post(SOLANA_RPC_URL, json=supply_payload) as resp:
                supply_data = await resp.json()
                if "result" not in supply_data:
                    self.logger.error(f"Failed to get supply for {token_address}: {supply_data}")
                    return {"percentage": 0.0, "top_holders": [], "total_supply": 0}
                total_supply = supply_data["result"]["value"]["uiAmount"]

            holders_payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenLargestAccounts",
                "params": [token_address],
            }
            async with session.post(SOLANA_RPC_URL, json=holders_payload) as resp:
                holders_data = await resp.json()
                if "result" not in holders_data:
                    self.logger.error(f"Failed to get holders for {token_address}: {holders_data}")
                    return {"percentage": 0.0, "top_holders": [], "total_supply": 0}
                accounts = holders_data["result"]["value"]

            # ── Build exclude set ──
            # Static: known burn / system addresses
            exclude_addresses = set(EXCLUDE_FROM_HOLDERS)

            # Dynamic: LP vault accounts owned by the pair (pool PDA)
            # getTokenAccountsByOwner returns the token-account addresses that
            # pair_address controls for this mint — those are the LP vaults.
            if pair_address and len(pair_address) >= 32:
                try:
                    lp_payload = {
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getTokenAccountsByOwner",
                        "params": [
                            pair_address,
                            {"mint": token_address},
                            {"encoding": "jsonParsed"},
                        ],
                    }
                    async with session.post(SOLANA_RPC_URL, json=lp_payload) as lp_resp:
                        lp_data = await lp_resp.json()
                        lp_vaults = lp_data.get("result", {}).get("value", [])
                        for v in lp_vaults:
                            vault_addr = v.get("pubkey", "")
                            if vault_addr:
                                exclude_addresses.add(vault_addr)
                        if lp_vaults:
                            self.logger.debug(
                                f"[HOLDER] {token_address[:8]}...: found {len(lp_vaults)} LP vault(s) "
                                f"owned by pair {pair_address[:12]}..."
                            )
                except Exception as e:
                    self.logger.debug(f"[HOLDER] LP vault lookup failed for {token_address[:8]}...: {e}")

            # ── Filter and calculate ──
            excluded_lp_total = 0.0
            excluded_lp_count = 0
            for a in accounts:
                if a.get("address") in exclude_addresses and a.get("uiAmount"):
                    excluded_lp_total += a["uiAmount"]
                    excluded_lp_count += 1

            filtered = [
                a for a in accounts
                if a.get("address") not in exclude_addresses and a.get("uiAmount") is not None
            ]
            top_20_accounts = filtered[:20]
            total_held = sum(a["uiAmount"] for a in top_20_accounts)

            top_holders = [
                {
                    "address": acc["address"],
                    "amount": acc["uiAmount"],
                    "percentage": round((acc["uiAmount"] / total_supply) * 100, 2) if total_supply > 0 else 0.0,
                }
                for acc in top_20_accounts
            ]

            percentage = round((total_held / total_supply) * 100, 2) if total_supply > 0 else 0.0

            if excluded_lp_count:
                lp_pct = round((excluded_lp_total / total_supply) * 100, 2) if total_supply > 0 else 0.0
                self.logger.info(
                    f"[HOLDER] {token_address[:8]}...: excluded {excluded_lp_count} LP/burn account(s) "
                    f"holding {lp_pct}% of supply — real holder concentration: {percentage}%"
                )

            return {
                "percentage": percentage,
                "top_holders": top_holders,
                "total_supply": total_supply,
                "excluded_lp_accounts": excluded_lp_count,
                "excluded_lp_pct": round((excluded_lp_total / total_supply) * 100, 2) if total_supply > 0 else 0.0,
            }
        except Exception as e:
            self.logger.error(f"Error fetching top 20 holders for {token_address}: {e}")
            return {"percentage": 0.0, "top_holders": [], "total_supply": 0}

    async def get_all_tokens(self):
        tokens = []
        async with self.db.execute("SELECT * FROM tokens") as cursor:
            async for row in cursor:
                tokens.append(row)
        return tokens

    async def close(self):
        if self.db:
            await self.db.close()

async def update_active_tokens_file(db: TokenDatabase, logger):
    try:
        async with db.db.execute("SELECT * FROM tokens") as cursor:
            rows = await cursor.fetchall()
        tokens_list = []
        for row in rows:
            token_dict = {
                "address": row[0],
                "token_name": row[1],
                "dex_id": row[2],
                "pair_address": row[3],
                "url": row[4],
                "base_token_name": row[5],
                "base_token_symbol": row[6],
                "quote_token_name": row[7],
                "quote_token_symbol": row[8],
                "price_native": row[9],
                "initial_price": row[10],
                "initial_market_cap": row[11],
                "current_price": row[12],
                "current_market_cap": row[13],
                "ath_price": row[14],
                "price_up_counter": row[15],
                "market_cap_flags": row[16],
                "file_timestamp": row[17],
                "tx_id": row[18],
                "raw_social_info": row[19],
                "last_updated": row[20],
                "buys_5m": row[21],
                "sells_5m": row[22],
                "buy_volume_5m": row[23],
                "sell_volume_5m": row[24],
                "top_20_holders_percentage": row[25],
                "top_20_holders": row[26],
                "buys_1h": row[27],
                "sells_1h": row[28],
                "buys_6h": row[29],
                "sells_6h": row[30],
                "buys_24h": row[31],
                "sells_24h": row[32],
                "volume_5m": row[33],
                "volume_1h": row[34],
                "volume_6h": row[35],
                "volume_24h": row[36],
                "price_change_5s": row[37],
                "price_change_15s": row[38],
                "price_change_30s": row[39],
                "price_change_1m": row[40],
                "price_change_5m": row[41],
                "is_bundled": row[42],
                "bundling_reason": row[43],
                "is_uptrend": row[44],
                "is_downtrend": row[45],
                "ath_timestamp": row[46],
                "is_new": row[47],
                "first_seen": row[48] if len(row) > 48 else ""
            }
            tokens_list.append(token_dict)
        async with aiofiles.open(ACTIVE_TOKENS_FILE, "w") as f:
            await f.write(json.dumps(tokens_list, indent=4))
        logger.info("Active tokens file updated.")
    except Exception as e:
        logger.error(f"Error updating active tokens file: {e}")

###############################################################################
# File Ingestion / Scanning
###############################################################################

async def process_token_file(filepath, db: TokenDatabase, logger, monitor: 'TokenMonitor'):
    BATCH_TOKEN_URL = "https://api.dexscreener.com/tokens/v1/solana/{addresses}"
    try:
        async with aiofiles.open(filepath, "r") as f:
            content = await f.read()
        data = json.loads(content)

        # ── Handle flat address list (detected_tokens.json) ──────────────
        # The token fetcher saves raw addresses as ["addr1", "addr2", ...]
        # Convert them using the batch endpoint (up to 30 per call).
        if isinstance(data, list) and data and isinstance(data[0], str):
            addresses = [a for a in data if a != "So11111111111111111111111111111111111111112"]
            logger.info(f"Flat address list detected in {filepath} ({len(addresses)} addresses), fetching batch metadata...")
            enriched = []
            async with aiohttp.ClientSession() as meta_session:
                for i in range(0, len(addresses), 30):
                    batch = addresses[i:i+30]
                    joined = ",".join(batch)
                    url = BATCH_TOKEN_URL.format(addresses=joined)
                    try:
                        async with meta_session.get(url) as resp:
                            if resp.status == 200:
                                pools = await resp.json()
                                if isinstance(pools, list) and pools:
                                    # Group pools by base token address
                                    by_token = {}
                                    for pool in pools:
                                        addr = pool.get("baseToken", {}).get("address", "")
                                        if addr:
                                            by_token.setdefault(addr, []).append(pool)
                                    for addr, token_pools in by_token.items():
                                        enriched.append({
                                            "token_address": addr,
                                            "metadata": token_pools,
                                            "timestamp": datetime.now(timezone.utc).isoformat()
                                        })
                                    logger.info(f"Batch metadata: {len(by_token)} tokens from {len(batch)} addresses")
                                else:
                                    logger.debug(f"No DexScreener pools for batch of {len(batch)} addresses")
                            elif resp.status == 429:
                                logger.warning("DexScreener rate limited on batch, pausing 5s")
                                await asyncio.sleep(5)
                            else:
                                logger.debug(f"DexScreener batch returned {resp.status}")
                    except Exception as e:
                        logger.error(f"Error fetching batch metadata: {e}")
                    if i + 30 < len(addresses):
                        await asyncio.sleep(0.5)
            if not enriched:
                logger.info(f"No DexScreener data for any address in {filepath}, archiving")
                os.makedirs(ARCHIVE_DIR, exist_ok=True)
                archived_path = os.path.join(ARCHIVE_DIR, os.path.basename(filepath))
                shutil.move(filepath, archived_path)
                return
            data = enriched
        async with aiohttp.ClientSession() as session:
            for token_obj in data:
                if "token_address" not in token_obj or "metadata" not in token_obj or not token_obj["metadata"]:
                    logger.warning(f"Skipping token in {filepath}: missing token_address or metadata.")
                    continue
                md = _pick_best_pair(token_obj["metadata"])
                if not md:
                    md = token_obj["metadata"][0]
                txns = md.get("txns", {})
                volume = md.get("volume", {})

                token_dict = {
                    "address": token_obj["token_address"],
                    "token_name": md.get("baseToken", {}).get("name", ""),
                    "dex_id": md.get("dexId", ""),
                    "pair_address": md.get("pairAddress", ""),
                    "url": md.get("url", ""),
                    "base_token_name": md.get("baseToken", {}).get("name", ""),
                    "base_token_symbol": md.get("baseToken", {}).get("symbol", ""),
                    "quote_token_name": md.get("quoteToken", {}).get("name", ""),
                    "quote_token_symbol": md.get("quoteToken", {}).get("symbol", ""),
                    "price_native": md.get("priceNative", ""),
                    "price": float(md.get("priceUsd", "0")),
                    "market_cap": float(md.get("marketCap", "0")),
                    "file_timestamp": token_obj.get("timestamp", ""),
                    "tx_id": token_obj.get("transaction", ""),
                    "raw_social_info": json.dumps(md.get("info", {})),
                    "buys_5m": txns.get("m5", {}).get("buys", 0),
                    "sells_5m": txns.get("m5", {}).get("sells", 0),
                    "buys_1h": txns.get("h1", {}).get("buys", 0),
                    "sells_1h": txns.get("h1", {}).get("sells", 0),
                    "buys_6h": txns.get("h6", {}).get("buys", 0),
                    "sells_6h": txns.get("h6", {}).get("sells", 0),
                    "buys_24h": txns.get("h24", {}).get("buys", 0),
                    "sells_24h": txns.get("h24", {}).get("sells", 0),
                    "volume_5m": float(volume.get("m5", 0)),
                    "volume_1h": float(volume.get("h1", 0)),
                    "volume_6h": float(volume.get("h6", 0)),
                    "volume_24h": float(volume.get("h24", 0))
                }

                await db.add_or_update_token(token_dict, session)
                await monitor.add_active_token(token_dict["address"])

                # Export new token data for AI
                trade_signal_trackers[token_dict["address"]] = TradeSignalToken(token_dict["token_name"], token_dict["price"], token_dict["address"])
                ai_input = {
                    "address": token_dict["address"],
                    "token_name": token_dict["token_name"],
                    "price_history": trade_signal_trackers[token_dict["address"]].price_history,
                    "pool_buys_5m": token_dict["buys_5m"],
                    "pool_sells_5m": token_dict["sells_5m"],
                    "pool_volume_5m": token_dict["volume_5m"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "is_new": True,
                    "needs_x": True
                }
                os.makedirs(AI_INPUT_DIR, exist_ok=True)
                filename = os.path.join(AI_INPUT_DIR, f"ai_input_{token_dict['address']}_{int(time.time())}.json")
                async with aiofiles.open(filename, "w") as f:
                    await f.write(json.dumps(ai_input, indent=2))
                logger.info(f"Exported new token AI input for {token_dict['address']} to {filename}")

                logger.info(f"Processed token {token_dict['address']} from {filepath}")
        
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        archived_path = os.path.join(ARCHIVE_DIR, os.path.basename(filepath))
        shutil.move(filepath, archived_path)
        logger.info(f"Archived processed file: {archived_path}")
    except Exception as e:
        logger.error(f"Error processing file {filepath}: {e}", exc_info=True)

async def scan_watch_dir(db: TokenDatabase, logger, monitor: 'TokenMonitor'):
    logger.info(f"Starting to scan watch directory: {WATCH_DIR}")
    try:
        files = [f for f in os.listdir(WATCH_DIR) if f.endswith('.json')]
        if files:
            logger.info(f"Found {len(files)} existing JSON files to process")
            for filename in files:
                full_path = os.path.join(WATCH_DIR, filename)
                logger.info(f"Processing existing file: {full_path}")
                await process_token_file(full_path, db, logger, monitor)
    except Exception as e:
        logger.error(f"Error processing existing files: {e}")
    
    while True:
        try:
            for filename in os.listdir(WATCH_DIR):
                if filename.endswith('.json'):
                    full_path = os.path.join(WATCH_DIR, filename)
                    if os.path.isfile(full_path):
                        logger.info(f"Found new file to process: {full_path}")
                        await process_token_file(full_path, db, logger, monitor)
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Error scanning watch directory: {e}")
            await asyncio.sleep(2)

###############################################################################
# Token Monitor & Export Logic
###############################################################################

class TokenMonitor:
    def __init__(self, db: TokenDatabase, logger=None):
        self.db = db
        self.logger = logger or logging.getLogger(__name__)
        self.session = None
        self.signal_tracker = TradeSignalTracker()
        self.active_tokens = set()
        self.low_market_cap_tokens = set()
        self.last_dexscreener_time = 0
        self.last_alchemy_time = 0
        self.dexscreener_interval = 60 / (RATE_LIMIT_TOKENS_PER_MIN / 4)
        self.alchemy_interval = 3600 / ALCHEMY_RATE_LIMIT_PER_HOUR

    async def initialize(self):
        self.session = aiohttp.ClientSession()
        if not self.db.db:
            self.logger.error("Database connection not initialized. Running setup...")
            await self.db.setup()
        tokens = await self.db.get_all_tokens()
        for token in tokens:
            address = token[0]
            token_name = token[1]
            current_price = token[12]
            market_cap = token[13]
            if market_cap >= MARKET_CAP_THRESHOLD:
                self.active_tokens.add(address)
            else:
                self.low_market_cap_tokens.add(address)
            # Initialize TradeSignalToken and load history
            trade_signal_trackers[address] = TradeSignalToken(token_name, current_price, address)
        self.logger.info(f"Initialized with {len(self.active_tokens)} active tokens and {len(self.low_market_cap_tokens)} low market cap tokens.")

    async def close_session(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def fetch_token_data(self, addresses):
        if not addresses:
            return []
        joined = ",".join(addresses)
        url = f"https://api.dexscreener.com/tokens/v1/solana/{joined}?_={int(time.time()*1000)}"
        headers = {"Cache-Control": "no-cache", "Pragma": "no-cache"}
        try:
            now = time.time()
            if now - self.last_dexscreener_time < self.dexscreener_interval:
                await asyncio.sleep(self.dexscreener_interval - (now - self.last_dexscreener_time))
            async with self.session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.last_dexscreener_time = time.time()

                    # ── Group all returned pairs by base token address ──
                    # The batch endpoint can return multiple pairs per token
                    # (bonding curve + graduated pool). We need the best one.
                    by_token = {}
                    for entry in data:
                        addr = entry.get("baseToken", {}).get("address", "")
                        if addr:
                            by_token.setdefault(addr, []).append(entry)

                    tokens = []
                    fetch_time = time.time()
                    for addr, entries in by_token.items():
                        entry = _pick_best_pair(entries)
                        if not entry:
                            continue
                        token = {
                            "address": entry.get("baseToken", {}).get("address"),
                            "token_name": entry.get("baseToken", {}).get("name", ""),
                            "dex_id": entry.get("dexId", ""),
                            "pair_address": entry.get("pairAddress", ""),
                            "url": entry.get("url", ""),
                            "base_token_name": entry.get("baseToken", {}).get("name", ""),
                            "base_token_symbol": entry.get("baseToken", {}).get("symbol", ""),
                            "quote_token_name": entry.get("quoteToken", {}).get("name", ""),
                            "quote_token_symbol": entry.get("quoteToken", {}).get("symbol", ""),
                            "price_native": entry.get("priceNative", ""),
                            "price": float(entry.get("priceUsd", "0")),
                            "market_cap": float(entry.get("marketCap", "0")),
                            "liquidity_usd": float((entry.get("liquidity") or {}).get("usd", 0)),
                            "raw_social_info": json.dumps(entry.get("info", {})),
                            "buys_5m": entry.get("txns", {}).get("m5", {}).get("buys", 0),
                            "sells_5m": entry.get("txns", {}).get("m5", {}).get("sells", 0),
                            "buys_1h": entry.get("txns", {}).get("h1", {}).get("buys", 0),
                            "sells_1h": entry.get("txns", {}).get("h1", {}).get("sells", 0),
                            "buys_6h": entry.get("txns", {}).get("h6", {}).get("buys", 0),
                            "sells_6h": entry.get("txns", {}).get("h6", {}).get("sells", 0),
                            "buys_24h": entry.get("txns", {}).get("h24", {}).get("buys", 0),
                            "sells_24h": entry.get("txns", {}).get("h24", {}).get("sells", 0),
                            "volume_5m": float(entry.get("volume", {}).get("m5", 0)),
                            "volume_1h": float(entry.get("volume", {}).get("h1", 0)),
                            "volume_6h": float(entry.get("volume", {}).get("h6", 0)),
                            "volume_24h": float(entry.get("volume", {}).get("h24", 0))
                        }
                        if token["address"] in self.active_tokens or token["address"] in self.low_market_cap_tokens:
                            price_changes = trade_signal_trackers[token["address"]].get_price_changes(fetch_time, token["price"]) if token["address"] in trade_signal_trackers else {}
                            if price_changes.get("price_change_5m", 0.0) == 0.0 and "priceChange" in entry:
                                price_changes["price_change_5m"] = entry["priceChange"].get("m5", 0.0)
                            # Use DexScreener's native 1h price change (more reliable than in-memory)
                            api_price_change_1h = 0.0
                            if "priceChange" in entry:
                                api_price_change_1h = entry["priceChange"].get("h1", 0.0) or 0.0
                            token.update({
                                "price_change_5s": price_changes.get("price_change_5s", 0),
                                "price_change_15s": price_changes.get("price_change_15s", 0),
                                "price_change_30s": price_changes.get("price_change_30s", 0),
                                "price_change_1m": price_changes.get("price_change_1m", 0),
                                "price_change_5m": price_changes.get("price_change_5m", 0),
                                "price_change_15m": price_changes.get("price_change_15m", 0),
                                "price_change_30m": price_changes.get("price_change_30m", 0),
                                "price_change_1h": api_price_change_1h,
                            })
                            tokens.append(token)
                    return tokens
                else:
                    self.logger.error(f"DexScreener API fetch failed: HTTP {resp.status}")
                    return []
        except Exception as e:
            self.logger.error(f"Error fetching token data from DexScreener: {e}")
            return []

    async def export_signal_token(self, token, signal_type):
        try:
            if token["address"] in trade_signal_trackers:
                price_changes = trade_signal_trackers[token["address"]].get_price_changes()
                token.update({
                    "price_change_5s": price_changes["price_change_5s"],
                    "price_change_15s": price_changes["price_change_15s"],
                    "price_change_30s": price_changes["price_change_30s"],
                    "price_change_1m": price_changes["price_change_1m"],
                    "price_change_5m": price_changes["price_change_5m"],
                    "price_change_15m": price_changes.get("price_change_15m", 0),
                    "price_change_30m": price_changes.get("price_change_30m", 0),
                })
            
            # Swing gate: TP2/TP3 require 5m movement (not 30s scalping gate)
            if signal_type in ["TP2 Buy", "TP3 Buy"] and token.get("price_change_5m", 0) < MIN_5M_PRICE_CHANGE:
                self.logger.debug(f"Signal export for {token['address']} rejected - 5m price change {token.get('price_change_5m', 0)}% < {MIN_5M_PRICE_CHANGE}%")
                return
            
            required_price = token["price"] * (1 + ENTRY_PRICE_INCREASE_PERCENT / 100)
            latest_price_data = await self.fetch_token_data([token["address"]])
            if latest_price_data and latest_price_data[0]["price"] < required_price:
                self.logger.debug(f"Signal export for {token['address']} rejected - Current price {latest_price_data[0]['price']} < Required {required_price}")
                return

            os.makedirs(SIGNALS_DIR, exist_ok=True)
            file_timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            detection_timestamp = datetime.now(timezone.utc).isoformat()
            
            # Query DB for LP-filtered holder concentration (tracker field is unreliable)
            db_holder_pct = 0.0
            try:
                async with self.db.db.execute(
                    "SELECT top_20_holders_percentage FROM tokens WHERE address = ?",
                    (token["address"],)
                ) as cursor:
                    hrow = await cursor.fetchone()
                    if hrow:
                        db_holder_pct = hrow[0] or 0.0
            except Exception:
                pass

            signal_data = {
                "address": token["address"],
                "signal_type": signal_type,
                "current_price": token["price"],
                "market_cap": token["market_cap"],
                "file_timestamp": file_timestamp,
                "detection_timestamp": detection_timestamp,
                "price_history": trade_signal_trackers[token["address"]].price_history if token["address"] in trade_signal_trackers else [],
                "price_change_5s": token.get("price_change_5s", 0),
                "price_change_15s": token.get("price_change_15s", 0),
                "price_change_30s": token.get("price_change_30s", 0),
                "price_change_1m": token.get("price_change_1m", 0),
                "price_change_5m": token.get("price_change_5m", 0),
                "price_change_15m": token.get("price_change_15m", 0),
                "price_change_30m": token.get("price_change_30m", 0),
                "price_change_1h": token.get("price_change_1h", 0),
                "pool_buys_5m": trade_signal_trackers[token["address"]].pool_buys_5m if token["address"] in trade_signal_trackers else 0,
                "pool_sells_5m": trade_signal_trackers[token["address"]].pool_sells_5m if token["address"] in trade_signal_trackers else 0,
                "buy_volume_5m": trade_signal_trackers[token["address"]].buy_volume_5m if token["address"] in trade_signal_trackers else 0.0,
                "sell_volume_5m": trade_signal_trackers[token["address"]].sell_volume_5m if token["address"] in trade_signal_trackers else 0.0,
                "pool_volume_5m": trade_signal_trackers[token["address"]].pool_volume_5m if token["address"] in trade_signal_trackers else 0,
                "top_20_holders_percentage": db_holder_pct
            }
            
            filename = os.path.join(SIGNALS_DIR, f"signal_{signal_type}_{token['address']}_{file_timestamp}.json")
            async with aiofiles.open(filename, "w") as f:
                await f.write(json.dumps(signal_data, indent=2))
            self.signal_tracker.mark_token_exported(token["address"])
            self.logger.info(f"✅ Exported {signal_type} signal token {token['address']} to {filename}")

            # ── Real-time hook dispatch (rate-limited) ──
            # Toggle: PUSH_ALERTS_ENABLED in repryntt/trading/trading_engine.py
            try:
                from repryntt.trading.trading_engine import PUSH_ALERTS_ENABLED
                if PUSH_ALERTS_ENABLED:
                    from repryntt.trading.signal_scorer import get_trading_rate_limiter
                    from repryntt.trading.signal_scorer import parse_trade_signal
                    from repryntt.trading.bot_bridge import get_hook_router

                    limiter = get_trading_rate_limiter()
                    if limiter.allow(token["address"]):
                        hook = parse_trade_signal({
                            "address": token["address"],
                            "symbol": token.get("symbol", token["address"][:12]),
                            "score": 0,
                            "grade": f"REALTIME {signal_type}",
                            "reasoning": f"Real-time {signal_type} detected by ai72",
                            "market_cap": token.get("market_cap", 0),
                            "latest_price": token.get("price", 0),
                            "signal_types": {signal_type: 1},
                            "price_change_5s": signal_data.get("price_change_5s", 0),
                            "price_change_30s": signal_data.get("price_change_30s", 0),
                            "price_change_1m": signal_data.get("price_change_1m", 0),
                            "price_change_5m": signal_data.get("price_change_5m", 0),
                            "price_change_15m": signal_data.get("price_change_15m", 0),
                            "price_change_30m": signal_data.get("price_change_30m", 0),
                            "price_change_1h": signal_data.get("price_change_1h", 0),
                        })
                        if hook:
                            router = get_hook_router()
                            router.dispatch(hook)
                            self.logger.info(f"🔔 Real-time hook fired for {signal_type} {token['address'][:12]}")
            except Exception as hook_err:
                self.logger.debug(f"Hook dispatch skipped: {hook_err}")
        except Exception as e:
            self.logger.error(f"Error exporting signal token {token['address']}: {e}")

    async def export_momentum_token(self, token):
        try:
            if token["address"] in trade_signal_trackers:
                price_changes = trade_signal_trackers[token["address"]].get_price_changes()
                token.update({
                    "price_change_5s": price_changes["price_change_5s"],
                    "price_change_15s": price_changes["price_change_15s"],
                    "price_change_30s": price_changes["price_change_30s"],
                    "price_change_1m": price_changes["price_change_1m"],
                    "price_change_5m": price_changes["price_change_5m"],
                    "price_change_15m": price_changes.get("price_change_15m", 0),
                    "price_change_30m": price_changes.get("price_change_30m", 0),
                })
            
            # Swing momentum gate: 5m must be >= threshold AND 15m must be positive
            if not (price_changes.get("price_change_5m", 0) >= MIN_5M_PRICE_CHANGE and
                    price_changes.get("price_change_15m", 0) > 0):
                self.logger.debug(f"Momentum export for {token['address']} rejected - Swing conditions not met (5m={price_changes.get('price_change_5m', 0):.1f}%, 15m={price_changes.get('price_change_15m', 0):.1f}%)")
                return
            
            required_price = token["price"] * (1 + ENTRY_PRICE_INCREASE_PERCENT / 100)
            latest_price_data = await self.fetch_token_data([token["address"]])
            if latest_price_data and latest_price_data[0]["price"] < required_price:
                self.logger.debug(f"Momentum export for {token['address']} rejected - Current price below required")
                return
            
            os.makedirs(SIGNALS_DIR, exist_ok=True)
            file_timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            detection_timestamp = datetime.now(timezone.utc).isoformat()
            
            # Query DB for LP-filtered holder concentration (tracker field is unreliable)
            db_holder_pct = 0.0
            try:
                async with self.db.db.execute(
                    "SELECT top_20_holders_percentage FROM tokens WHERE address = ?",
                    (token["address"],)
                ) as cursor:
                    hrow = await cursor.fetchone()
                    if hrow:
                        db_holder_pct = hrow[0] or 0.0
            except Exception:
                pass

            momentum_data = {
                "address": token["address"],
                "signal_type": "Momentum",
                "current_price": token["price"],
                "market_cap": token["market_cap"],
                "file_timestamp": file_timestamp,
                "detection_timestamp": detection_timestamp,
                "price_history": trade_signal_trackers[token["address"]].price_history if token["address"] in trade_signal_trackers else [],
                "price_change_5s": token["price_change_5s"],
                "price_change_15s": token["price_change_15s"],
                "price_change_30s": token["price_change_30s"],
                "price_change_1m": token["price_change_1m"],
                "price_change_5m": token["price_change_5m"],
                "price_change_15m": token.get("price_change_15m", 0),
                "price_change_30m": token.get("price_change_30m", 0),
                "price_change_1h": token.get("price_change_1h", 0),
                "pool_buys_5m": trade_signal_trackers[token["address"]].pool_buys_5m if token["address"] in trade_signal_trackers else 0,
                "pool_sells_5m": trade_signal_trackers[token["address"]].pool_sells_5m if token["address"] in trade_signal_trackers else 0,
                "buy_volume_5m": trade_signal_trackers[token["address"]].buy_volume_5m if token["address"] in trade_signal_trackers else 0.0,
                "sell_volume_5m": trade_signal_trackers[token["address"]].sell_volume_5m if token["address"] in trade_signal_trackers else 0.0,
                "pool_volume_5m": trade_signal_trackers[token["address"]].pool_volume_5m if token["address"] in trade_signal_trackers else 0,
                "top_20_holders_percentage": db_holder_pct
            }
            
            filename = os.path.join(SIGNALS_DIR, f"momentum_{token['address']}_{file_timestamp}.json")
            async with aiofiles.open(filename, "w") as f:
                await f.write(json.dumps(momentum_data, indent=2))
            self.signal_tracker.mark_token_exported(token["address"])
            self.logger.info(f"✅ Exported momentum token {token['address']} to {filename}")

            # ── Real-time hook dispatch (rate-limited) ──
            # Toggle: PUSH_ALERTS_ENABLED in repryntt/trading/trading_engine.py
            try:
                from repryntt.trading.trading_engine import PUSH_ALERTS_ENABLED
                if PUSH_ALERTS_ENABLED:
                    from repryntt.trading.signal_scorer import get_trading_rate_limiter
                    from repryntt.trading.signal_scorer import parse_trade_signal
                    from repryntt.trading.bot_bridge import get_hook_router

                    limiter = get_trading_rate_limiter()
                    if limiter.allow(token["address"]):
                        hook = parse_trade_signal({
                            "address": token["address"],
                            "symbol": token.get("symbol", token["address"][:12]),
                            "score": 0,
                            "grade": "REALTIME Momentum",
                            "reasoning": "Real-time Momentum signal detected by ai72",
                            "market_cap": token.get("market_cap", 0),
                            "latest_price": token.get("price", 0),
                            "signal_types": {"Momentum": 1},
                            "price_change_5s": momentum_data.get("price_change_5s", 0),
                            "price_change_30s": momentum_data.get("price_change_30s", 0),
                            "price_change_1m": momentum_data.get("price_change_1m", 0),
                            "price_change_5m": momentum_data.get("price_change_5m", 0),
                            "price_change_15m": momentum_data.get("price_change_15m", 0),
                            "price_change_30m": momentum_data.get("price_change_30m", 0),
                            "price_change_1h": momentum_data.get("price_change_1h", 0),
                        })
                        if hook:
                            router = get_hook_router()
                            router.dispatch(hook)
                            self.logger.info(f"🔔 Real-time hook fired for Momentum {token['address'][:12]}")
            except Exception as hook_err:
                self.logger.debug(f"Hook dispatch skipped: {hook_err}")
        except Exception as e:
            self.logger.error(f"Error exporting momentum token {token['address']}: {e}")

    async def remove_low_price_tokens(self):
        try:
            query = "SELECT address, current_price, current_market_cap FROM tokens WHERE current_price < ?"
            async with self.db.db.execute(query, (PRICE_REMOVAL_THRESHOLD,)) as cursor:
                tokens_to_check = await cursor.fetchall()
            for token in tokens_to_check:
                address, price, market_cap = token
                if market_cap < MARKET_CAP_THRESHOLD:
                    # If both price and market cap are low, remove the token entirely
                    await self.db.db.execute("DELETE FROM tokens WHERE address = ?", (address,))
                    self.active_tokens.discard(address)
                    self.low_market_cap_tokens.discard(address)
                    self.logger.info(f"Removed low price/market cap token {address} (price: {price}, market cap: {market_cap}).")
                else:
                    # If only price is low but market cap is good, move to low market cap set
                    if address in self.active_tokens:
                        self.active_tokens.discard(address)
                        self.low_market_cap_tokens.add(address)
                        self.logger.info(f"Moved token {address} to low market cap set (price: {price}, market cap: {market_cap}).")
            await self.db.db.commit()
        except Exception as e:
            self.logger.error(f"Error in remove_low_price_tokens: {e}")
            
    async def remove_old_low_market_cap_tokens(self):
        try:
            eight_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat()
            query = """
            SELECT address, current_market_cap, last_updated 
            FROM tokens 
            WHERE current_market_cap < 100000 
            AND last_updated < ?
            """
            async with self.db.db.execute(query, (eight_hours_ago,)) as cursor:
                tokens_to_check = await cursor.fetchall()
            for token in tokens_to_check:
                address, market_cap, last_updated = token
                updated_time = datetime.fromisoformat(last_updated)
                if (datetime.now(timezone.utc) - updated_time).total_seconds() >= 8 * 3600:
                    await self.db.db.execute("DELETE FROM tokens WHERE address = ?", (address,))
                    self.active_tokens.discard(address)
                    self.low_market_cap_tokens.discard(address)
                    self.logger.info(f"Removed token {address} with market cap {market_cap} after 8 hours.")
            await self.db.db.commit()
        except Exception as e:
            self.logger.error(f"Error in remove_old_low_market_cap_tokens: {e}")

    async def remove_stale_tokens(self, max_age_hours: int = 3):
        """Remove tokens older than max_age_hours from active tracking and DB.

        Uses first_seen (when available) to determine true age, falling back
        to last_updated. Tokens with open positions are protected.
        """
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
            query = """
            SELECT address, token_name, current_market_cap, last_updated, first_seen
            FROM tokens
            WHERE COALESCE(NULLIF(first_seen, ''), last_updated) < ?
               OR last_updated < ?
            """
            async with self.db.db.execute(query, (cutoff, cutoff)) as cursor:
                stale_rows = await cursor.fetchall()

            if not stale_rows:
                return

            # Load open positions to protect tokens we're actively trading
            protected_addresses = set()
            from repryntt.paths import get_data_dir as _gdd
            portfolio_path = os.path.join(str(_gdd()), "agent_workspaces", "jarvis", "sim_portfolio.json")
            try:
                if os.path.exists(portfolio_path):
                    async with aiofiles.open(portfolio_path, "r") as f:
                        portfolio = json.loads(await f.read())
                    for _sym, pos in portfolio.get("positions", {}).items():
                        addr = pos.get("token_address", "")
                        if addr:
                            protected_addresses.add(addr)
            except Exception as e:
                self.logger.warning(f"Could not load sim_portfolio for stale check: {e}")

            removed = 0
            for address, name, mcap, last_updated in stale_rows:
                if address in protected_addresses:
                    self.logger.debug(f"Skipping stale token {name} ({address}) — open position")
                    continue
                await self.db.db.execute("DELETE FROM tokens WHERE address = ?", (address,))
                self.active_tokens.discard(address)
                self.low_market_cap_tokens.discard(address)
                if address in trade_signal_trackers:
                    del trade_signal_trackers[address]
                removed += 1

            if removed:
                await self.db.db.commit()
                await update_active_tokens_file(self.db, self.logger)
                self.logger.info(
                    f"🧹 Stale token cleanup: removed {removed}/{len(stale_rows)} tokens "
                    f"older than {max_age_hours}h ({len(protected_addresses)} protected by open positions)"
                )
        except Exception as e:
            self.logger.error(f"Error in remove_stale_tokens: {e}", exc_info=True)

    async def export_token_profile(self, token_data, price_history, retries=3):
        """Export token data and price history to TOKEN_PROFILES_DIR with full DB data including ATH."""
        for attempt in range(retries):
            try:
                profile_file = os.path.join(TOKEN_PROFILES_DIR, f"{token_data['address']}.json")
                async with self.db.db.execute("SELECT * FROM tokens WHERE address = ?", (token_data["address"],)) as cursor:
                    row = await cursor.fetchone()
                if not row:
                    self.logger.warning(f"No database entry for {token_data['address']} - skipping export")
                    return
                
                columns = [desc[0] for desc in cursor.description]
                profile_data = dict(zip(columns, row))
                
                # Load existing price history from the profile file
                existing_price_history = []
                existing_lows_history = []
                if os.path.exists(profile_file):
                    try:
                        async with aiofiles.open(profile_file, "r") as f:
                            content = await f.read()
                            existing_data = json.loads(content)
                            existing_price_history = existing_data.get("price_history", [])
                            existing_lows_history = existing_data.get("lows_history", [])
                    except Exception as e:
                        self.logger.error(f"Error loading existing profile for {token_data['address']}: {e}")

                # Merge existing history with new price history, avoiding duplicates
                combined_price_history = existing_price_history
                for timestamp, price in price_history:
                    if not any(abs(t - timestamp) < 0.001 for t, _ in combined_price_history):
                        combined_price_history.append([timestamp, price])
                combined_price_history.sort(key=lambda x: x[0])
                
                # Merge existing lows_history with current lows_history
                combined_lows_history = existing_lows_history
                if token_data["address"] in trade_signal_trackers:
                    for low_time, low_price in trade_signal_trackers[token_data["address"]].lows_history:
                        if not any(abs(t - low_time) < 0.001 for t, _ in combined_lows_history):
                            combined_lows_history.append([low_time, low_price])
                    combined_lows_history.sort(key=lambda x: x[0])
                
                current_price = token_data.get("price", profile_data["current_price"])
                ath_price = profile_data.get("ath_price", 0)
                ath_timestamp = profile_data.get("ath_timestamp", "")
                history_prices = [price for _, price in combined_price_history]
                
                if history_prices:
                    max_history_price = max(history_prices)
                    if max_history_price > ath_price:
                        ath_price = max_history_price
                        ath_timestamp = datetime.now(timezone.utc).isoformat()
                        await self.db.db.execute(
                            "UPDATE tokens SET ath_price = ?, ath_timestamp = ? WHERE address = ?",
                            (ath_price, ath_timestamp, token_data["address"])
                        )
                        await self.db.db.commit()
                        self.logger.debug(f"Updated ATH for {token_data['address']} to {ath_price} at {ath_timestamp}")
                
                profile_data["ath_data"] = {
                    "ath_price": ath_price,
                    "ath_timestamp": ath_timestamp,
                    "current_price": current_price,
                    "percent_from_ath": ((ath_price - current_price) / ath_price * 100) if ath_price > 0 else 0
                }
                
                profile_data["price_history"] = combined_price_history
                profile_data["lows_history"] = combined_lows_history
                
                os.makedirs(TOKEN_PROFILES_DIR, exist_ok=True)
                async with aiofiles.open(profile_file, "w") as f:
                    await f.write(json.dumps(profile_data, indent=2))
                self.logger.debug(f"Exported token profile for {token_data['address']} to {profile_file}")
                break  # Success, exit retry loop
            except Exception as e:
                self.logger.error(f"Error exporting token profile for {token_data['address']} (attempt {attempt + 1}/{retries}): {e}")
                if attempt + 1 == retries:
                    self.logger.error(f"Failed to export token profile for {token_data['address']} after {retries} attempts")
                await asyncio.sleep(1)  # Wait before retrying

    async def periodic_profile_export(self):
        while True:
            try:
                for address in self.active_tokens | self.low_market_cap_tokens:
                    if address in trade_signal_trackers:
                        token_data = {
                            "address": address,
                            "price": trade_signal_trackers[address].price_history[-1][1] if trade_signal_trackers[address].price_history else 0.0,
                            "market_cap": 0.0,  # Will be updated from DB in export_token_profile
                        }
                        await self.export_token_profile(token_data, trade_signal_trackers[address].price_history)
                self.logger.info("Completed periodic profile export for all tokens.")
                await asyncio.sleep(300)  # Run every 5 minutes
            except Exception as e:
                self.logger.error(f"Error in periodic profile export: {e}")
                await asyncio.sleep(300)

    async def enforce_token_cap(self):
        """Enforce MAX_ACTIVE_TOKENS cap and TOKEN_MAX_AGE_HOURS expiry.
        Scores tokens on volume + recency + trend, with an aggressive age
        penalty so stale tokens get replaced by fresh discoveries."""

        # Load protected addresses (open positions)
        protected = set()
        from repryntt.paths import get_data_dir as _gdd
        portfolio_path = os.path.join(str(_gdd()), "agent_workspaces", "jarvis", "sim_portfolio.json")
        try:
            if os.path.exists(portfolio_path):
                import aiofiles as _af
                async with _af.open(portfolio_path, "r") as f:
                    pf = json.loads(await f.read())
                for _sym, pos in pf.get("positions", {}).items():
                    addr = pos.get("token_address", "")
                    if addr:
                        protected.add(addr)
        except Exception:
            pass

        now_utc = datetime.now(timezone.utc)

        # Score every tracked token
        scored = []
        force_evict = []
        all_addrs = list(self.active_tokens | self.low_market_cap_tokens)
        for addr in all_addrs:
            if addr in protected:
                continue
            score = 0.0
            try:
                async with self.db.db.execute(
                    "SELECT volume_24h, volume_5m, current_market_cap, is_uptrend, is_new, last_updated, first_seen FROM tokens WHERE address = ?",
                    (addr,)
                ) as cur:
                    row = await cur.fetchone()
                if row:
                    vol_24h = float(row[0] or 0)
                    vol_5m = float(row[1] or 0)
                    mcap = float(row[2] or 0)
                    is_up = int(row[3] or 0)
                    is_new = int(row[4] or 0)
                    last_updated = row[5] or ""
                    first_seen = row[6] or last_updated

                    # Age check — force-evict tokens older than TOKEN_MAX_AGE_HOURS
                    try:
                        seen_dt = datetime.fromisoformat(first_seen) if first_seen else now_utc
                        age_hours = (now_utc - seen_dt).total_seconds() / 3600
                        if age_hours >= TOKEN_MAX_AGE_HOURS:
                            force_evict.append(addr)
                            continue
                    except Exception:
                        age_hours = 0

                    score += min(vol_24h / 10000, 10.0)
                    score += min(vol_5m / 1000, 5.0)
                    score += min(mcap / 100000, 5.0)

                    if is_up:
                        score += 3.0
                    if is_new:
                        score += 2.0

                    try:
                        updated_dt = datetime.fromisoformat(last_updated)
                        update_age_min = (now_utc - updated_dt).total_seconds() / 60
                        if update_age_min < 10:
                            score += 3.0
                        elif update_age_min < 30:
                            score += 1.5
                    except Exception:
                        pass

                    # Age penalty — tokens tracked >1h get progressively penalized
                    if age_hours > 1:
                        score -= (age_hours - 1) * 5.0
                else:
                    score = -1
            except Exception:
                score = -1

            scored.append((addr, score))

        # Force-evict expired tokens first
        evicted = 0
        for addr in force_evict:
            await self.db.db.execute("DELETE FROM tokens WHERE address = ?", (addr,))
            self.active_tokens.discard(addr)
            self.low_market_cap_tokens.discard(addr)
            if addr in trade_signal_trackers:
                del trade_signal_trackers[addr]
            evicted += 1

        if force_evict:
            self.logger.info(
                f"⏰ Expired {len(force_evict)} tokens older than {TOKEN_MAX_AGE_HOURS}h")

        # Then score-evict to stay under the cap
        current_total = len(self.active_tokens) + len(self.low_market_cap_tokens)
        if current_total > MAX_ACTIVE_TOKENS:
            scored.sort(key=lambda x: x[1])
            to_evict = current_total - MAX_ACTIVE_TOKENS
            score_evicted = 0
            for addr, score in scored:
                if score_evicted >= to_evict:
                    break
                await self.db.db.execute("DELETE FROM tokens WHERE address = ?", (addr,))
                self.active_tokens.discard(addr)
                self.low_market_cap_tokens.discard(addr)
                if addr in trade_signal_trackers:
                    del trade_signal_trackers[addr]
                score_evicted += 1
            evicted += score_evicted

        if evicted:
            await self.db.db.commit()
            await update_active_tokens_file(self.db, self.logger)
            self.logger.info(
                f"🔒 Token cap enforced: evicted {evicted} total "
                f"(now {len(self.active_tokens) + len(self.low_market_cap_tokens)}/{MAX_ACTIVE_TOKENS})")

    async def scan_gainers(self):
        """Periodically scan DB for top gainers and export them as buy signals.
        This bridges the Degen Terminal's real-time price data to the trading
        pipeline so Jarvis can act on tokens showing early momentum."""
        gainer_last_signaled = {}
        while True:
            try:
                now = time.time()
                query = """
                SELECT address, token_name, base_token_symbol, current_price,
                       current_market_cap, price_change_5m, volume_5m,
                       buys_5m, sells_5m, is_bundled
                FROM tokens
                WHERE is_bundled = 0
                  AND price_change_5m >= ?
                  AND volume_5m >= ?
                  AND current_market_cap >= ?
                ORDER BY price_change_5m DESC
                LIMIT 5
                """
                async with self.db.db.execute(
                    query, (GAINER_MIN_CHANGE_5M, GAINER_MIN_VOLUME_5M, MARKET_CAP_THRESHOLD)
                ) as cursor:
                    rows = await cursor.fetchall()

                exported = 0
                for row in rows:
                    addr = row[0]
                    token_name = row[1]
                    symbol = row[2] or token_name
                    price = row[3]
                    mcap = row[4]
                    change_5m = row[5]
                    vol_5m = row[6]
                    buys = row[7]
                    sells = row[8]

                    last = gainer_last_signaled.get(addr, 0)
                    if now - last < GAINER_COOLDOWN:
                        continue

                    if buys > 0 and sells > 0 and buys < sells:
                        continue

                    token_dict = {
                        "address": addr,
                        "token_name": token_name,
                        "symbol": symbol,
                        "price": price,
                        "market_cap": mcap,
                        "price_change_5m": change_5m,
                        "volume_5m": vol_5m,
                        "buys_5m": buys,
                        "sells_5m": sells,
                    }

                    await self.export_signal_token(token_dict, "Gainer")
                    gainer_last_signaled[addr] = now
                    exported += 1
                    self.logger.info(
                        f"🚀 Gainer signal: {symbol} ({addr[:8]}...) "
                        f"+{change_5m:.1f}% in 5m, vol ${vol_5m:.0f}, mcap ${mcap:.0f}")

                if exported:
                    self.logger.info(f"🚀 Exported {exported} gainer signal(s) to trading pipeline")

                stale = [a for a, t in gainer_last_signaled.items() if now - t > GAINER_COOLDOWN * 2]
                for a in stale:
                    del gainer_last_signaled[a]

                await asyncio.sleep(GAINER_SCAN_INTERVAL)
            except Exception as e:
                self.logger.error(f"Error in scan_gainers: {e}", exc_info=True)
                await asyncio.sleep(GAINER_SCAN_INTERVAL)

    async def daily_token_reset(self):
        """Once per day at DAILY_RESET_HOUR_UTC, wipe all non-protected tokens
        so the active list starts fresh with newly discovered tokens."""
        while True:
            try:
                now = datetime.now(timezone.utc)
                target = now.replace(hour=DAILY_RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
                if now >= target:
                    target += timedelta(days=1)
                wait_seconds = (target - now).total_seconds()
                self.logger.info(f"🔄 Daily token reset scheduled in {wait_seconds/3600:.1f}h (at {target.isoformat()})")
                await asyncio.sleep(wait_seconds)

                protected_addresses = set()
                from repryntt.paths import get_data_dir as _gdd
                portfolio_path = os.path.join(str(_gdd()), "agent_workspaces", "jarvis", "sim_portfolio.json")
                try:
                    if os.path.exists(portfolio_path):
                        async with aiofiles.open(portfolio_path, "r") as f:
                            portfolio = json.loads(await f.read())
                        for _sym, pos in portfolio.get("positions", {}).items():
                            addr = pos.get("token_address", "")
                            if addr:
                                protected_addresses.add(addr)
                except Exception as e:
                    self.logger.warning(f"Could not load sim_portfolio for daily reset: {e}")

                all_addrs = list(self.active_tokens | self.low_market_cap_tokens)
                removed = 0
                for addr in all_addrs:
                    if addr in protected_addresses:
                        continue
                    await self.db.db.execute("DELETE FROM tokens WHERE address = ?", (addr,))
                    self.active_tokens.discard(addr)
                    self.low_market_cap_tokens.discard(addr)
                    if addr in trade_signal_trackers:
                        del trade_signal_trackers[addr]
                    removed += 1

                if removed:
                    await self.db.db.commit()
                    await update_active_tokens_file(self.db, self.logger)

                self.logger.info(
                    f"🔄 Daily reset complete: cleared {removed} tokens "
                    f"({len(protected_addresses)} protected by open positions). "
                    f"Fresh discovery cycle starting now.")
            except Exception as e:
                self.logger.error(f"Error in daily_token_reset: {e}", exc_info=True)
                await asyncio.sleep(3600)

    async def monitor_tokens(self, poll_interval=0.2):  # Reduced from 1.0 to 0.2 seconds
        dex_cycle_interval = 1  # Check DexScreener every second
        last_dex_cycle = 0
        last_cap_check = 0
        while True:
            try:
                # Enforce 25-token cap every 60 seconds
                current_time = time.time()
                if current_time - last_cap_check >= 60:
                    await self.enforce_token_cap()
                    last_cap_check = current_time

                all_tokens = self.active_tokens | self.low_market_cap_tokens
                if not all_tokens:
                    await asyncio.sleep(poll_interval)
                    continue
                addresses = list(all_tokens)

                # DexScreener full data check
                current_time = time.time()
                if current_time - last_dex_cycle >= dex_cycle_interval:
                    dex_chunk_size = 15  # Process 15 tokens at a time
                    for i in range(0, len(addresses), dex_chunk_size):
                        chunk = addresses[i:i + dex_chunk_size]
                        dex_data = await self.fetch_token_data(chunk)
                        if dex_data:
                            await self.process_token_data(dex_data)
                        await asyncio.sleep(0.1)  # Small delay between chunks
                    last_dex_cycle = current_time

                await asyncio.sleep(poll_interval)
            except Exception as e:
                self.logger.error(f"Error in tokens monitoring loop: {e}", exc_info=True)
                await asyncio.sleep(poll_interval)

    async def _resolve_graduated_pair(self, address):
        """If the batch /tokens/v1/ endpoint returned a bonding curve pair,
        try /token-pairs/v1/ to find the real graduated pool."""
        TOKEN_PAIRS_URL = f"https://api.dexscreener.com/token-pairs/v1/solana/{address}"
        try:
            async with self.session.get(TOKEN_PAIRS_URL) as resp:
                if resp.status == 200:
                    pairs = await resp.json()
                    if isinstance(pairs, list) and len(pairs) > 1:
                        best = _pick_best_pair(pairs)
                        if best and best.get("dexId", "") not in BONDING_CURVE_DEX_IDS:
                            entry = best
                            txns = entry.get("txns", {})
                            vol = entry.get("volume", {})
                            return {
                                "address": entry.get("baseToken", {}).get("address", address),
                                "token_name": entry.get("baseToken", {}).get("name", ""),
                                "dex_id": entry.get("dexId", ""),
                                "pair_address": entry.get("pairAddress", ""),
                                "url": entry.get("url", ""),
                                "base_token_name": entry.get("baseToken", {}).get("name", ""),
                                "base_token_symbol": entry.get("baseToken", {}).get("symbol", ""),
                                "quote_token_name": entry.get("quoteToken", {}).get("name", ""),
                                "quote_token_symbol": entry.get("quoteToken", {}).get("symbol", ""),
                                "price_native": entry.get("priceNative", ""),
                                "price": float(entry.get("priceUsd", "0")),
                                "market_cap": float(entry.get("marketCap", "0")),
                                "liquidity_usd": float((entry.get("liquidity") or {}).get("usd", 0)),
                                "raw_social_info": json.dumps(entry.get("info", {})),
                                "buys_5m": txns.get("m5", {}).get("buys", 0),
                                "sells_5m": txns.get("m5", {}).get("sells", 0),
                                "buys_1h": txns.get("h1", {}).get("buys", 0),
                                "sells_1h": txns.get("h1", {}).get("sells", 0),
                                "buys_6h": txns.get("h6", {}).get("buys", 0),
                                "sells_6h": txns.get("h6", {}).get("sells", 0),
                                "buys_24h": txns.get("h24", {}).get("buys", 0),
                                "sells_24h": txns.get("h24", {}).get("sells", 0),
                                "volume_5m": float(vol.get("m5", 0)),
                                "volume_1h": float(vol.get("h1", 0)),
                                "volume_6h": float(vol.get("h6", 0)),
                                "volume_24h": float(vol.get("h24", 0)),
                            }
        except Exception as e:
            self.logger.debug(f"Error resolving graduated pair for {address[:12]}: {e}")
        return None

    async def process_token_data(self, new_data):
        for token_data in new_data:
            address = token_data["address"]
            if address not in self.active_tokens and address not in self.low_market_cap_tokens:
                continue

            # ── Bonding curve detection: if batch API returned a bonding curve
            # pair, try the token-pairs endpoint for the real graduated pool ──
            dex_id = token_data.get("dex_id", "")
            liq = token_data.get("liquidity_usd", 0)
            if dex_id in BONDING_CURVE_DEX_IDS or (liq == 0 and dex_id not in ("pumpswap", "raydium", "meteora", "orca")):
                resolved = await self._resolve_graduated_pair(address)
                if resolved:
                    self.logger.info(
                        f"🎓 {address[:12]}... graduated: {dex_id} → {resolved['dex_id']} "
                        f"(mcap ${resolved['market_cap']:.0f}, liq ${resolved.get('liquidity_usd', 0):.0f})"
                    )
                    token_data = resolved
                else:
                    self.logger.debug(f"Token {address[:12]}... still on bonding curve ({dex_id}), no graduated pair found")

            token_name = token_data.get("token_name", "Unknown")

            # Initialize TradeSignalToken if not present
            if address not in trade_signal_trackers:
                trade_signal_trackers[address] = TradeSignalToken(token_name, token_data["price"], address)

            # Update price and other data
            trade_signal_trackers[address].update_price(
                token_data["price"],
                token_data.get("buys_5m", 0),
                token_data.get("sells_5m", 0),
                token_data.get("volume_5m", 0.0)
            )

            # Update database with all data including price
            await self.db.add_or_update_token(token_data, self.session)
            if token_data["market_cap"] < MARKET_CAP_THRESHOLD:
                if address in self.active_tokens:
                    self.active_tokens.discard(address)
                    self.low_market_cap_tokens.add(address)
                    self.logger.info(f"Token {address} moved to low market cap set (market cap: {token_data['market_cap']}).")

            # Export profile with price history
            await self.export_token_profile(token_data, trade_signal_trackers[address].price_history)

            # Check trade signals
            price_changes = trade_signal_trackers[address].get_price_changes()
            signal = trade_signal_trackers[address].check_trade_conditions()
            if signal is not None:
                self.logger.info(f"Trade Signal: {signal} for {token_name}...")
                async with self.db.db.execute("SELECT * FROM tokens WHERE address = ?", (address,)) as cursor:
                    token_row = await cursor.fetchone()
                if token_row:
                    token_dict = {
                        "address": address,
                        "price": token_data["price"],
                        "market_cap": token_data["market_cap"],
                        "initial_price": token_row[10],
                        "buys_5m": token_data.get("buys_5m", 0),
                        "sells_5m": token_data.get("sells_5m", 0),
                        "volume_5m": token_data.get("volume_5m", 0.0)
                    }
                    async with self.db.db.execute("SELECT is_bundled FROM tokens WHERE address = ?", (address,)) as cursor:
                        is_bundled = (await cursor.fetchone())[0]
                    if not is_bundled and self.signal_tracker.can_export_token(address):
                        await self.export_signal_token(token_dict, signal)
                        await save_trade_signal(token_name, signal, token_data["price"])

            # Check momentum signals — swing gate: 5m >= threshold AND 15m positive
            if (price_changes.get("price_change_5m", 0) >= MIN_5M_PRICE_CHANGE and
                price_changes.get("price_change_15m", 0) > 0):
                token_dict = {
                    "address": address,
                    "price": token_data["price"],
                    "market_cap": token_data["market_cap"],
                    "initial_price": token_row[10] if 'token_row' in locals() else token_data["price"],
                    "buys_5m": token_data.get("buys_5m", 0),
                    "sells_5m": token_data.get("sells_5m", 0),
                    "volume_5m": token_data.get("volume_5m", 0.0)
                }
                async with self.db.db.execute("SELECT is_bundled FROM tokens WHERE address = ?", (address,)) as cursor:
                    is_bundled = (await cursor.fetchone())[0]
                if not is_bundled and self.signal_tracker.can_export_token(address):
                    await self.export_momentum_token(token_dict)
                    await save_trade_signal(token_name, "Momentum", token_data["price"])

            # Update price change metrics
            await self.db.db.execute(
                "UPDATE tokens SET price_change_5s = ?, price_change_15s = ?, price_change_30s = ?, price_change_1m = ?, price_change_5m = ?, price_change_15m = ?, price_change_30m = ?, price_change_1h = ? WHERE address = ?",
                (price_changes["price_change_5s"],
                 price_changes["price_change_15s"],
                 price_changes["price_change_30s"],
                 price_changes["price_change_1m"],
                 price_changes["price_change_5m"],
                 price_changes.get("price_change_15m", 0),
                 price_changes.get("price_change_30m", 0),
                 token_data.get("price_change_1h", 0),
                 address)
            )
            await self.db.db.commit()

    async def add_active_token(self, address):
        try:
            if not self.db.db:
                self.logger.error(f"Database connection is not available for token {address}. Reinitializing...")
                await self.db.setup()
            
            async with self.db.db.execute("SELECT current_market_cap FROM tokens WHERE address = ?", (address,)) as cursor:
                row = await cursor.fetchone()
            
            if row:
                market_cap = row[0]
                total_tracked = len(self.active_tokens) + len(self.low_market_cap_tokens)
                if total_tracked >= MAX_ACTIVE_TOKENS and address not in self.active_tokens and address not in self.low_market_cap_tokens:
                    self.logger.info(f"🔒 Token cap ({MAX_ACTIVE_TOKENS}) reached — deferring {address} until next eviction cycle.")
                    return
                if market_cap >= MARKET_CAP_THRESHOLD:
                    self.active_tokens.add(address)
                    self.low_market_cap_tokens.discard(address)
                    self.logger.debug(f"Added token {address} to active_tokens (market cap: {market_cap}).")
                else:
                    self.low_market_cap_tokens.add(address)
                    self.active_tokens.discard(address)
                    self.logger.debug(f"Added token {address} to low_market_cap_tokens (market cap: {market_cap}).")
            else:
                self.logger.warning(f"No market cap data found for token {address} in database.")
        except Exception as e:
            self.logger.error(f"Error adding active token: {e}", exc_info=True)

# Main Entry Point
async def main():
    logger = setup_logging()
    
    # Verify TOKEN_PROFILES_DIR
    try:
        os.makedirs(TOKEN_PROFILES_DIR, exist_ok=True)
        test_file = os.path.join(TOKEN_PROFILES_DIR, "test_write.json")
        with open(test_file, "w") as f:
            f.write("{}")
        os.remove(test_file)
        logger.info(f"Verified write access to {TOKEN_PROFILES_DIR}")
    except Exception as e:
        logger.error(f"Cannot write to {TOKEN_PROFILES_DIR}: {e}")
        return
    
    db = TokenDatabase(logger=logger)
    await db.setup()
    
    monitor = TokenMonitor(db, logger)
    await monitor.initialize()

    # Define periodic cleanup tasks
    async def remove_low_price_tokens_task():
        while True:
            await monitor.remove_low_price_tokens()
            await asyncio.sleep(300)  # Run every 5 minutes

    async def remove_old_low_market_cap_tokens_task():
        while True:
            await monitor.remove_old_low_market_cap_tokens()
            await asyncio.sleep(300)  # Run every 5 minutes

    async def remove_stale_tokens_task():
        while True:
            await monitor.remove_stale_tokens(max_age_hours=TOKEN_MAX_AGE_HOURS)
            await asyncio.sleep(300)  # Run every 5 minutes

    # Start concurrent tasks
    tasks = [
        asyncio.create_task(scan_watch_dir(db, logger, monitor)),
        asyncio.create_task(monitor.monitor_tokens(poll_interval=1.0)),
        asyncio.create_task(remove_low_price_tokens_task()),
        asyncio.create_task(remove_old_low_market_cap_tokens_task()),
        asyncio.create_task(remove_stale_tokens_task()),
        asyncio.create_task(monitor.periodic_profile_export()),
        asyncio.create_task(dexscreener_discovery_loop(logger)),
        asyncio.create_task(monitor.daily_token_reset()),
        asyncio.create_task(monitor.scan_gainers()),
    ]

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        for task in tasks:
            task.cancel()
        await monitor.close_session()
        await db.close()
        logger.info("Shutdown complete.")

if __name__ == "__main__":
    asyncio.run(main())

def calculate_vwap(price_history):
    volume = 1
    total_volume = 0
    total_price_volume = 0
    for timestamp, price in price_history:
        total_price_volume += price * volume
        total_volume += volume
    if total_volume > 0:
        return total_price_volume / total_volume
    else:
        return None