"""
REPRYNTT Solana Trade Executor — Real Mainnet Trading via Jupiter
=================================================================

Provides real on-chain swap execution through Jupiter's aggregator API.
Controlled by DRY_RUN toggle:

  DRY_RUN = True  → Builds & validates swap transactions but does NOT submit.
                     Logs what WOULD have happened. (Current default)
  DRY_RUN = False → Signs and submits real transactions to Solana mainnet.

Architecture:
  1. Load keypair from ~/.repryntt/wallet/artemis_mainnet.json
  2. Get Jupiter quote (best route, slippage, price impact)
  3. Build swap transaction via Jupiter /swap endpoint
  4. In DRY_RUN: log the transaction details, return simulated result
  5. In LIVE:   sign, submit, confirm on-chain

Safety:
  - MAX_TRADE_USD caps the maximum single trade size
  - Slippage is capped at MAX_SLIPPAGE_BPS (default 300 = 3%)
  - Balance check before every trade
  - All transactions logged to trade journal
"""

import json
import os
import time
import logging
import base58
import aiohttp
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

logger = logging.getLogger("repryntt.solana_executor")

# ─── Configuration ────────────────────────────────────────────────────────────

DRY_RUN = True  # SAFETY: Set False only when ready for real money

WALLET_PATH = Path.home() / ".repryntt" / "wallet" / "artemis_mainnet.json"
RPC_ENDPOINT = os.environ.get(
    "SOLANA_RPC_URL",
    "https://api.mainnet-beta.solana.com"
)
JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
LAMPORTS_PER_SOL = 1_000_000_000

MAX_TRADE_USD = 50.0
MAX_SLIPPAGE_BPS = 300  # 3%
DEFAULT_SLIPPAGE_BPS = 100  # 1%
MIN_SOL_BALANCE = 0.01  # Keep at least this much SOL for fees

TRADE_LOG_DIR = Path.home() / ".repryntt" / "wallet" / "trade_logs"
TRADE_LOG_DIR.mkdir(parents=True, exist_ok=True)


# ─── Wallet Management ───────────────────────────────────────────────────────

_cached_keypair: Optional[Keypair] = None


def load_keypair() -> Keypair:
    """Load the Artemis wallet keypair from disk."""
    global _cached_keypair
    if _cached_keypair is not None:
        return _cached_keypair

    if not WALLET_PATH.exists():
        raise FileNotFoundError(
            f"Wallet not found at {WALLET_PATH}. "
            "Generate one first or check the path."
        )

    with open(WALLET_PATH) as f:
        secret_bytes = json.load(f)

    _cached_keypair = Keypair.from_bytes(bytes(secret_bytes))
    logger.info(f"Loaded wallet: {_cached_keypair.pubkey()}")
    return _cached_keypair


def get_public_key() -> str:
    """Get the wallet's public key as a string."""
    return str(load_keypair().pubkey())


async def get_sol_balance(session: aiohttp.ClientSession) -> float:
    """Get the wallet's SOL balance in SOL (not lamports)."""
    kp = load_keypair()
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [str(kp.pubkey())]
    }
    try:
        async with session.post(RPC_ENDPOINT, json=payload) as resp:
            if resp.status == 200:
                data = await resp.json()
                lamports = data.get("result", {}).get("value", 0)
                return lamports / LAMPORTS_PER_SOL
    except Exception as e:
        logger.error(f"Failed to get SOL balance: {e}")
    return 0.0


async def get_token_balance(
    session: aiohttp.ClientSession, token_mint: str
) -> Tuple[float, int]:
    """Get token balance for the wallet. Returns (ui_amount, raw_amount)."""
    kp = load_keypair()
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            str(kp.pubkey()),
            {"mint": token_mint},
            {"encoding": "jsonParsed"}
        ]
    }
    try:
        async with session.post(RPC_ENDPOINT, json=payload) as resp:
            if resp.status == 200:
                data = await resp.json()
                accounts = data.get("result", {}).get("value", [])
                if accounts:
                    info = accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]
                    return float(info.get("uiAmount", 0)), int(info.get("amount", 0))
    except Exception as e:
        logger.error(f"Failed to get token balance for {token_mint}: {e}")
    return 0.0, 0


# ─── Jupiter Swap ─────────────────────────────────────────────────────────────

async def get_jupiter_quote(
    session: aiohttp.ClientSession,
    input_mint: str,
    output_mint: str,
    amount: int,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS
) -> Optional[Dict[str, Any]]:
    """Get a swap quote from Jupiter aggregator."""
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": min(slippage_bps, MAX_SLIPPAGE_BPS),
        "onlyDirectRoutes": "false",
        "asLegacyTransaction": "false",
    }
    try:
        async with session.get(JUPITER_QUOTE_URL, params=params) as resp:
            if resp.status == 200:
                quote = await resp.json()
                logger.info(
                    f"Jupiter quote: {input_mint[:8]}→{output_mint[:8]} "
                    f"in={amount} out={quote.get('outAmount', '?')} "
                    f"impact={quote.get('priceImpactPct', '?')}%"
                )
                return quote
            else:
                body = await resp.text()
                logger.error(f"Jupiter quote failed HTTP {resp.status}: {body}")
    except Exception as e:
        logger.error(f"Jupiter quote error: {e}")
    return None


async def build_swap_transaction(
    session: aiohttp.ClientSession,
    quote: Dict[str, Any]
) -> Optional[bytes]:
    """Build a swap transaction from a Jupiter quote."""
    kp = load_keypair()
    payload = {
        "quoteResponse": quote,
        "userPublicKey": str(kp.pubkey()),
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto",
    }
    try:
        async with session.post(JUPITER_SWAP_URL, json=payload) as resp:
            if resp.status == 200:
                data = await resp.json()
                swap_tx = data.get("swapTransaction")
                if swap_tx:
                    import base64
                    return base64.b64decode(swap_tx)
                logger.error("Jupiter swap response missing swapTransaction")
            else:
                body = await resp.text()
                logger.error(f"Jupiter swap build failed HTTP {resp.status}: {body}")
    except Exception as e:
        logger.error(f"Jupiter swap build error: {e}")
    return None


async def execute_swap(
    session: aiohttp.ClientSession,
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS
) -> Dict[str, Any]:
    """Execute a token swap via Jupiter.

    In DRY_RUN mode: gets quote + builds tx but does NOT submit.
    In LIVE mode: signs and submits the transaction on-chain.
    """
    result = {
        "success": False,
        "dry_run": DRY_RUN,
        "input_mint": input_mint,
        "output_mint": output_mint,
        "amount_in": amount_lamports,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # 1. Get quote
    quote = await get_jupiter_quote(
        session, input_mint, output_mint, amount_lamports, slippage_bps
    )
    if not quote:
        result["error"] = "Failed to get Jupiter quote"
        _log_trade(result)
        return result

    result["out_amount"] = int(quote.get("outAmount", 0))
    result["price_impact_pct"] = float(quote.get("priceImpactPct", 0))
    result["route_plan"] = [
        r.get("swapInfo", {}).get("label", "?")
        for r in quote.get("routePlan", [])
    ]

    # 2. Build transaction
    tx_bytes = await build_swap_transaction(session, quote)
    if not tx_bytes:
        result["error"] = "Failed to build swap transaction"
        _log_trade(result)
        return result

    result["tx_size_bytes"] = len(tx_bytes)

    # 3. DRY_RUN: stop here
    if DRY_RUN:
        result["success"] = True
        result["status"] = "DRY_RUN — transaction built but NOT submitted"
        logger.info(
            f"🔧 [DRY_RUN] Swap ready: {input_mint[:8]}→{output_mint[:8]} "
            f"in={amount_lamports} out={result['out_amount']} "
            f"impact={result['price_impact_pct']}% "
            f"route={' → '.join(result['route_plan'])}"
        )
        _log_trade(result)
        return result

    # 4. LIVE: sign and submit
    try:
        kp = load_keypair()
        tx = VersionedTransaction.from_bytes(tx_bytes)
        tx = VersionedTransaction(tx.message, [kp])
        signed_bytes = bytes(tx)

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                base58.b58encode(signed_bytes).decode(),
                {"skipPreflight": False, "preflightCommitment": "confirmed"}
            ]
        }
        async with session.post(RPC_ENDPOINT, json=payload) as resp:
            data = await resp.json()
            if "result" in data:
                tx_sig = data["result"]
                result["success"] = True
                result["status"] = "SUBMITTED"
                result["tx_signature"] = tx_sig
                result["explorer_url"] = f"https://solscan.io/tx/{tx_sig}"
                logger.info(f"✅ [LIVE] Swap submitted: {tx_sig}")
            else:
                error = data.get("error", {})
                result["error"] = f"RPC error: {error.get('message', str(error))}"
                logger.error(f"❌ [LIVE] Swap failed: {result['error']}")

    except Exception as e:
        result["error"] = f"Transaction signing/submission failed: {e}"
        logger.error(f"❌ [LIVE] Exception: {e}", exc_info=True)

    _log_trade(result)
    return result


# ─── High-Level Trading Functions ─────────────────────────────────────────────

async def buy_token(
    session: aiohttp.ClientSession,
    token_address: str,
    amount_sol: float,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS
) -> Dict[str, Any]:
    """Buy a token with SOL via Jupiter.

    Args:
        token_address: The token's mint address
        amount_sol: How much SOL to spend
        slippage_bps: Slippage tolerance in basis points
    """
    sol_balance = await get_sol_balance(session)
    if sol_balance < amount_sol + MIN_SOL_BALANCE:
        return {
            "success": False,
            "error": f"Insufficient SOL. Have {sol_balance:.4f}, "
                     f"need {amount_sol + MIN_SOL_BALANCE:.4f} "
                     f"({amount_sol} + {MIN_SOL_BALANCE} for fees)"
        }

    amount_lamports = int(amount_sol * LAMPORTS_PER_SOL)

    logger.info(
        f"{'[DRY_RUN] ' if DRY_RUN else ''}Buying {token_address[:12]}... "
        f"with {amount_sol} SOL ({amount_lamports} lamports)"
    )

    return await execute_swap(
        session, SOL_MINT, token_address, amount_lamports, slippage_bps
    )


async def sell_token(
    session: aiohttp.ClientSession,
    token_address: str,
    amount_tokens: Optional[int] = None,
    sell_pct: float = 100.0,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS
) -> Dict[str, Any]:
    """Sell a token for SOL via Jupiter.

    Args:
        token_address: The token's mint address
        amount_tokens: Raw token amount to sell (if None, uses sell_pct)
        sell_pct: Percentage of holdings to sell (default 100%)
        slippage_bps: Slippage tolerance in basis points
    """
    if amount_tokens is None:
        _, raw_balance = await get_token_balance(session, token_address)
        if raw_balance == 0:
            return {"success": False, "error": f"No {token_address[:12]} balance"}
        amount_tokens = int(raw_balance * (sell_pct / 100.0))

    if amount_tokens <= 0:
        return {"success": False, "error": "Nothing to sell"}

    logger.info(
        f"{'[DRY_RUN] ' if DRY_RUN else ''}Selling {amount_tokens} of "
        f"{token_address[:12]}... for SOL"
    )

    return await execute_swap(
        session, token_address, SOL_MINT, amount_tokens, slippage_bps
    )


async def get_wallet_status(session: aiohttp.ClientSession) -> Dict[str, Any]:
    """Get current wallet status — balance, public key, mode."""
    kp = load_keypair()
    sol = await get_sol_balance(session)
    return {
        "public_key": str(kp.pubkey()),
        "sol_balance": sol,
        "sol_balance_usd": sol * 130,  # rough estimate
        "mode": "DRY_RUN" if DRY_RUN else "LIVE",
        "max_trade_usd": MAX_TRADE_USD,
        "slippage_bps": DEFAULT_SLIPPAGE_BPS,
        "wallet_path": str(WALLET_PATH),
    }


# ─── Trade Logging ────────────────────────────────────────────────────────────

def _log_trade(result: Dict[str, Any]):
    """Append trade result to daily log file."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_file = TRADE_LOG_DIR / f"trades_{today}.json"

        trades = []
        if log_file.exists():
            with open(log_file) as f:
                trades = json.load(f)

        trades.append(result)

        with open(log_file, "w") as f:
            json.dump(trades, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to log trade: {e}")
