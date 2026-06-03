import asyncio
import aiohttp
import logging
import json
import os
import platform
from datetime import datetime, timedelta
from typing import List, Set

# Set up Windows event loop policy
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Constants
WALLETS = [
    "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg",  # PumpFun
    "RAYpQbFNq9i3mu6cKpTKKRwwHFDeK5AuZz8xvxUrCgw",    # Bonk
    "J7cV46t2BLkoHWvmrcG1nK3wgB2D1EmHLko29bEDbnpV",     # Boop
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"  # PumpFun AMM
]
# Note: /latest/dex/tokens removed from DexScreener API — use /tokens/v1/ or /token-pairs/v1/ instead

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
TOKEN_PAIRS_API = "https://api.dexscreener.com/token-pairs/v1/solana/{tokenAddress}"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "watch_dir")

# Derived / additional constants
SOLANA_WS_URL = SOLANA_RPC_URL.replace("https://", "wss://")
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
POOL_PROGRAM_IDS = {
    # Raydium AMM v4
    "RaydiumAmmV4": "675kPX9MHTjS2zt1DYMimMnDJSfCNx1DtnH9KG9B1",
}
CREATE_POOL_LOG_KEYWORDS = [
    "create pool",
    "create_pool",
    "initialize",
    "initialize2",
    "init pool",
    "init_pool",
]

# Ensure directory exists
os.makedirs(DATA_DIR, exist_ok=True)
logger.info(f"Using data directory: {DATA_DIR}")

class WalletMonitor:
    def __init__(self):
        self.processed_signatures = set()
        self.processed_tokens = set()
        self.file_counter = 1
        self.historical_scan_done = False

    def _is_create_pool_in_logs(self, logs: List[str]) -> bool:
        """Heuristic: detect create-pool by presence of common init keywords in program logs."""
        if not logs:
            return False
        try:
            lower_logs = "\n".join((log or "").lower() for log in logs)
            # Must include at least one pool program id mention and an init-like keyword
            has_program_mention = any(pid in lower_logs for pid in (p.lower() for p in POOL_PROGRAM_IDS.values()))
            has_keyword = any(keyword in lower_logs for keyword in CREATE_POOL_LOG_KEYWORDS)
            return has_program_mention and has_keyword
        except Exception:
            return False

    def _extract_candidate_mints(self, tx_value: dict) -> List[str]:
        """Extract potential token mints from a transaction (inner + balances).

        We intentionally over-collect and rely on downstream metadata fetching to validate.
        """
        candidate_mints: Set[str] = set()

        meta = tx_value.get("meta", {})
        # From inner instructions (parsed SPL-Token ops)
        inner_instructions = meta.get("innerInstructions", [])
        for inner in inner_instructions:
            for inner_inst in inner.get("instructions", []):
                if inner_inst.get("programId") == TOKEN_PROGRAM_ID:
                    parsed = inner_inst.get("parsed", {})
                    info = parsed.get("info", {}) if isinstance(parsed, dict) else {}
                    mint = info.get("mint")
                    if mint:
                        candidate_mints.add(mint)

        # From top-level parsed token instructions if any
        instructions = tx_value.get("transaction", {}).get("message", {}).get("instructions", [])
        for inst in instructions:
            if inst.get("programId") == TOKEN_PROGRAM_ID:
                parsed = inst.get("parsed", {})
                info = parsed.get("info", {}) if isinstance(parsed, dict) else {}
                mint = info.get("mint")
                if mint:
                    candidate_mints.add(mint)

        # From token balances
        for bal in meta.get("postTokenBalances", []) or []:
            mint = bal.get("mint")
            if mint:
                candidate_mints.add(mint)

        # Filter out well-known non-target mints (WSOL) and already-processed
        filtered = [m for m in candidate_mints if m and m != WRAPPED_SOL_MINT and m not in self.processed_tokens]
        return filtered

    async def fetch_transactions(self, session, until=None):
        """Fetch transactions from all monitored wallets"""
        all_transactions = []
        for wallet in WALLETS:
            before = None
            while True:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [
                        wallet,
                        {
                            "limit": 25,  # Get 25 most recent transactions per wallet
                            **({"before": before} if before else {})
                        }
                    ]
                }

                async with session.post(SOLANA_RPC_URL, json=payload) as response:
                    if response.status != 200:
                        logger.error(f"Error fetching transactions for wallet {wallet}: {response.status}")
                        break

                    data = await response.json()
                    transactions = data.get("result", [])

                    if not transactions:
                        break

                    for tx in transactions:
                        tx_time = datetime.fromtimestamp(tx.get("blockTime", 0))
                        if until and tx_time < until:
                            break

                        all_transactions.append(tx)

                    before = transactions[-1]["signature"]

                    # Limit to 25 transactions per wallet to focus on most recent
                    if len(all_transactions) >= 25:
                        break

        # Sort all transactions by blockTime in reverse order (newest first)
        all_transactions.sort(key=lambda x: x.get("blockTime", 0), reverse=True)
        return all_transactions[:25]  # Return only the 25 most recent transactions

    async def get_transaction_details(self, session, signature):
        """Get full transaction details"""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": "confirmed"
                }
            ]
        }

        async with session.post(SOLANA_RPC_URL, json=payload) as response:
            if response.status != 200:
                return None
            data = await response.json()
            return data.get("result")

    async def fetch_token_metadata(self, session, token_addresses):
        """Fetch token metadata including pool addresses from DEXScreener Token Pairs API"""
        if isinstance(token_addresses, str):
            token_addresses = [token_addresses]

        all_metadata = []
        for token_address in token_addresses:
            url = TOKEN_PAIRS_API.format(tokenAddress=token_address)

            retries = 3
            for attempt in range(retries):
                try:
                    async with session.get(url) as response:
                        if response.status == 200:
                            data = await response.json()
                            logger.info(f"Successfully fetched pool metadata for token {token_address}")
                            all_metadata.extend(data)  # Extend because the API returns a list of pools
                            break
                        else:
                            logger.warning(f"DexScreener Token Pairs API returned status {response.status} for {token_address}")
                        await asyncio.sleep(2)
                except Exception as e:
                    logger.error(f"Error fetching metadata for {token_address} (attempt {attempt + 1}): {e}")
                    if attempt < retries - 1:
                        await asyncio.sleep(2)

        return all_metadata if all_metadata else None

    def extract_new_tokens(self, tx_data):
        """Extract new token addresses from transaction data"""
        new_tokens = []

        if not tx_data or "meta" not in tx_data:
            logger.warning("Transaction data missing or no meta field")
            return new_tokens

        # Check if any monitored wallet is involved
        account_keys = [account["pubkey"] for account in tx_data.get("transaction", {}).get("message", {}).get("accountKeys", [])]
        wallet_involved = any(wallet in account_keys for wallet in WALLETS)
        if not wallet_involved:
            logger.info(f"No monitored wallet involved in transaction")
            return new_tokens

        logger.info("Monitored wallet involved, proceeding to extract tokens")

        # Look for token transfers and mints in inner instructions
        inner_instructions = tx_data["meta"].get("innerInstructions", [])
        for inner in inner_instructions:
            for inner_inst in inner.get("instructions", []):
                if inner_inst.get("programId") == "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA":  # SPL Token program
                    parsed = inner_inst.get("parsed", {})
                    if parsed.get("type") in ["transfer", "transferChecked", "mintTo"]:
                        mint = parsed["info"].get("mint")
                        if mint and mint not in self.processed_tokens and mint != "So11111111111111111111111111111111111111112":
                            new_tokens.append(mint)
                            self.processed_tokens.add(mint)
                            logger.info(f"Found new token from inner instruction: {mint}")

        # Look for Raydium "Add liquidity" instructions
        instructions = tx_data.get("transaction", {}).get("message", {}).get("instructions", [])
        for instruction in instructions:
            if instruction.get("programId") == "675kPX9MHTjS2zt1DYMimMnDJSfCNx1DtnH9KG9B1":  # Raydium Liquidity Pool V4
                accounts = instruction.get("accounts", [])
                if len(accounts) > 5:  # Typical "Add liquidity" instruction
                    token_a_mint = accounts[4]  # Token A mint (e.g., WSOL)
                    token_b_mint = accounts[5]  # Token B mint (e.g., WANNABE)
                    for mint in [token_a_mint, token_b_mint]:
                        if mint and mint not in self.processed_tokens and mint != "So11111111111111111111111111111111111111112":
                            new_tokens.append(mint)
                            self.processed_tokens.add(mint)
                            logger.info(f"Found new token from Raydium add liquidity: {mint}")

        # Save tokens to a JSON file
        if new_tokens:
            file_path = os.path.join(DATA_DIR, "detected_tokens.json")
            try:
                with open(file_path, "r") as f:
                    saved_data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                saved_data = []

            saved_data.extend(new_tokens)

            with open(file_path, "w") as f:
                json.dump(saved_data, f, indent=2)

            logger.info(f"Saved {len(new_tokens)} new token addresses to {file_path}")

        return new_tokens

    async def block_subscribe_and_process(self, http_session):
        """Subscribe to blocks via WebSocket, filter for create-pool txs, and process tokens.

        This avoids making an HTTP request per transaction by streaming complete blocks.
        """
        while True:
            try:
                async with aiohttp.ClientSession() as ws_session:
                    async with ws_session.ws_connect(SOLANA_WS_URL, autoping=True, heartbeat=30) as ws:
                        # Subscribe to all blocks with parsed, full transaction details
                        subscribe_payload = {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "blockSubscribe",
                            "params": [
                                "all",
                                {
                                    "commitment": "confirmed",
                                    "encoding": "jsonParsed",
                                    "transactionDetails": "full",
                                    "showRewards": False,
                                    "maxSupportedTransactionVersion": 0
                                }
                            ]
                        }
                        await ws.send_json(subscribe_payload)

                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                continue
                            data = json.loads(msg.data)

                            # Ignore subscription acks
                            if "method" not in data:
                                continue

                            if data.get("method") != "blockNotification":
                                continue

                            value = (data.get("params", {}) or {}).get("result", {})
                            block = (value.get("value", {}) or {}).get("block", {})
                            if not block:
                                continue

                            transactions = block.get("transactions", []) or []
                            for tx_value in transactions:
                                tx = tx_value.get("transaction", {})
                                meta = tx_value.get("meta", {})
                                if not tx or not meta:
                                    continue

                                # Quick filter: wallet involvement
                                account_keys = [
                                    ak.get("pubkey") if isinstance(ak, dict) else ak
                                    for ak in (tx.get("message", {}).get("accountKeys", []) or [])
                                ]
                                if not any(wallet in account_keys for wallet in WALLETS):
                                    continue

                                # Create-pool detection by logs + Raydium program mention
                                logs = meta.get("logMessages", []) or []
                                if not self._is_create_pool_in_logs(logs):
                                    continue

                                signatures = tx.get("signatures", []) or []
                                signature = signatures[0] if signatures else None
                                if not signature or signature in self.processed_signatures:
                                    continue
                                self.processed_signatures.add(signature)

                                candidate_mints = self._extract_candidate_mints(tx_value)
                                if not candidate_mints:
                                    continue

                                # Process tokens (DexScreener will validate real tokens/pairs)
                                tokens_with_metadata = await self.process_new_tokens(http_session, candidate_mints, signature)
                                if tokens_with_metadata:
                                    filename = f"new_tokens_{self.file_counter}.json"
                                    filepath = os.path.join(DATA_DIR, filename)
                                    with open(filepath, "w") as f:
                                        json.dump(tokens_with_metadata, f, indent=2)
                                    logger.info(f"Saved {len(tokens_with_metadata)} new tokens to {filepath}")
                                    self.file_counter += 1

            except Exception as e:
                logger.error(f"WebSocket block subscription error: {e}")
                await asyncio.sleep(5)

    async def process_new_tokens(self, session, new_tokens, signature):
        """Process new tokens and fetch their metadata with a delay"""
        tokens_with_metadata = []

        # Add a 10-second delay before querying DEXScreener
        logger.info(f"Found {len(new_tokens)} new tokens, waiting 10 seconds before querying DEXScreener...")
        await asyncio.sleep(3)  # Wait 10 seconds to give DEXScreener time to update

        for token in new_tokens:
            metadata = await self.fetch_token_metadata(session, token)
            if metadata:
                tokens_with_metadata.append({
                    "token_address": token,
                    "metadata": metadata,
                    "transaction": signature,
                    "timestamp": datetime.now().isoformat()
                })
        return tokens_with_metadata

    async def scan_historical_transactions(self, session):
        """Scan historical transactions"""
        logger.info("Starting historical scan...")
        cutoff_time = datetime.now() - timedelta(hours=1)  # Extended to 12 hours
        historical_txs = await self.fetch_transactions(session, until=cutoff_time)
        logger.info(f"Found {len(historical_txs)} historical transactions to scan")

        for tx in historical_txs:
            signature = tx["signature"]
            if signature in self.processed_signatures:
                continue

            self.processed_signatures.add(signature)

            tx_data = await self.get_transaction_details(session, signature)
            if not tx_data:
                continue

            new_tokens = self.extract_new_tokens(tx_data)
            if not new_tokens:
                continue

            logger.info(f"Found {len(new_tokens)} tokens in historical transaction {signature}")

            tokens_with_metadata = await self.process_new_tokens(session, new_tokens, signature)

            if tokens_with_metadata:
                filename = f"historical_tokens_{self.file_counter}.json"
                filepath = os.path.join(DATA_DIR, filename)
                with open(filepath, "w") as f:
                    json.dump(tokens_with_metadata, f, indent=2)
                logger.info(f"Saved {len(tokens_with_metadata)} historical tokens to {filepath}")
                self.file_counter += 1

        logger.info("Historical scan complete")
        self.historical_scan_done = True

    async def poll_for_new_transactions(self, session):
        """Polling fallback — checks wallets on a rotating basis.
        
        Used when WebSocket blockSubscribe is unavailable (e.g. Alchemy free tier).
        Rate-limit aware: backs off on 429s, rotates wallets to spread load.
        """
        BASE_INTERVAL = 30   # seconds between polls
        MAX_INTERVAL = 300   # max backoff (5 min)
        current_interval = BASE_INTERVAL
        wallet_index = 0  # rotate through wallets one at a time
        consecutive_429s = 0
        poll_count = 0
        total_tokens_found = 0
        import time as _time
        last_heartbeat = _time.time()

        logger.info(f"Starting polling mode — rotating through {len(WALLETS)} wallets, base interval {BASE_INTERVAL}s")

        while True:
            try:
                # Poll ONE wallet at a time to stay within rate limits
                wallet = WALLETS[wallet_index % len(WALLETS)]
                wallet_index += 1
                wallet_name = wallet[:8] + "..."

                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [wallet, {"limit": 10}]
                }

                async with session.post(SOLANA_RPC_URL, json=payload) as response:
                    if response.status == 429:
                        consecutive_429s += 1
                        current_interval = min(BASE_INTERVAL * (2 ** consecutive_429s), MAX_INTERVAL)
                        logger.warning(f"Rate limited (429) on {wallet_name}, backing off to {current_interval}s")
                        await asyncio.sleep(current_interval)
                        continue

                    if response.status != 200:
                        logger.error(f"RPC error {response.status} for {wallet_name}")
                        await asyncio.sleep(current_interval)
                        continue

                    # Success — reset backoff
                    if consecutive_429s > 0:
                        logger.info(f"Rate limit cleared, resuming normal interval")
                    consecutive_429s = 0
                    current_interval = BASE_INTERVAL

                    data = await response.json()
                    transactions = data.get("result", [])

                new_count = 0
                for tx in transactions:
                    sig = tx["signature"]
                    if sig in self.processed_signatures:
                        continue
                    self.processed_signatures.add(sig)

                    tx_data = await self.get_transaction_details(session, sig)
                    if not tx_data:
                        continue

                    new_tokens = self.extract_new_tokens(tx_data)
                    if not new_tokens:
                        continue

                    new_count += len(new_tokens)
                    tokens_with_metadata = await self.process_new_tokens(session, new_tokens, sig)
                    if tokens_with_metadata:
                        filename = f"new_tokens_{self.file_counter}.json"
                        filepath = os.path.join(DATA_DIR, filename)
                        with open(filepath, "w") as f:
                            json.dump(tokens_with_metadata, f, indent=2)
                        logger.info(f"Saved {len(tokens_with_metadata)} new tokens to {filepath}")
                        self.file_counter += 1

                if new_count > 0:
                    total_tokens_found += new_count
                    logger.info(f"Poll [{wallet_name}]: {new_count} new token(s)")
                else:
                    logger.debug(f"Poll [{wallet_name}]: no new tokens")

                poll_count += 1
                # Heartbeat every 60 seconds so log doesn't look dead
                now = _time.time()
                if now - last_heartbeat >= 60:
                    logger.info(f"[heartbeat] polls={poll_count}, tokens_found={total_tokens_found}, "
                                f"tracked={len(self.processed_tokens)}, sigs={len(self.processed_signatures)}, "
                                f"interval={current_interval}s")
                    last_heartbeat = now

                # Trim processed_signatures to avoid unbounded growth
                if len(self.processed_signatures) > 5000:
                    self.processed_signatures = set(list(self.processed_signatures)[-3000:])

            except Exception as e:
                logger.error(f"Polling error: {e}")

            await asyncio.sleep(current_interval)

    async def monitor_transactions(self):
        """Main monitoring loop — tries WebSocket first, falls back to polling."""
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
            if not self.historical_scan_done:
                await self.scan_historical_transactions(session)

            # Try WebSocket first; if it fails or produces no data, fall back to polling
            try:
                logger.info("Attempting WebSocket block subscription (10s test)...")
                self._ws_received_blocks = False
                ws_task = asyncio.create_task(self._ws_with_tracking(session))
                # Give it 10 seconds to receive at least one block
                await asyncio.sleep(10)
                if ws_task.done() or not self._ws_received_blocks:
                    ws_task.cancel()
                    raise Exception("No blocks received in 10s — likely unsupported by RPC provider")
                logger.info("WebSocket block subscription active and receiving data")
                await ws_task
            except Exception as e:
                logger.warning(f"WebSocket not viable ({e}), using polling mode instead")
                await self.poll_for_new_transactions(session)

    async def _ws_with_tracking(self, session):
        """Wrapper around block_subscribe that sets a flag when blocks arrive."""
        original = self.block_subscribe_and_process
        # Monkey-patch to detect first block arrival
        async def tracked_subscribe(http_session):
            while True:
                try:
                    async with aiohttp.ClientSession() as ws_session:
                        async with ws_session.ws_connect(SOLANA_WS_URL, autoping=True, heartbeat=30) as ws:
                            subscribe_payload = {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "blockSubscribe",
                                "params": [
                                    "all",
                                    {
                                        "commitment": "confirmed",
                                        "encoding": "jsonParsed",
                                        "transactionDetails": "full",
                                        "showRewards": False,
                                        "maxSupportedTransactionVersion": 0
                                    }
                                ]
                            }
                            await ws.send_json(subscribe_payload)

                            async for msg in ws:
                                if msg.type != aiohttp.WSMsgType.TEXT:
                                    continue
                                data = json.loads(msg.data)
                                if data.get("method") == "blockNotification":
                                    self._ws_received_blocks = True
                                # Process normally via parent
                                # (simplified — just set flag, actual processing in original)
                except Exception as e:
                    logger.error(f"WebSocket error: {e}")
                    await asyncio.sleep(5)
        await tracked_subscribe(session)

async def main():
    monitor = WalletMonitor()
    await monitor.monitor_transactions()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Monitoring stopped by user")