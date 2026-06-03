"""
Jupiter DEX Tools — Direct on-chain Solana token swaps via Jupiter aggregator.
===============================================================================

Primary swap integration for Artemis/Andrew. No rate limits, handles Token-2022,
signs transactions locally with a dedicated keypair.

Wallet: ~/.repryntt/wallet/jupiter_trading.json
  - Auto-generated on first use (64-byte Ed25519 keypair as JSON array)
  - Fund with SOL before trading (send SOL to the displayed address)
  - File permissions set to 0600

Architecture:
  1. Get quote from Jupiter V6 API (best route, slippage, price impact)
  2. Build swap transaction (Jupiter handles routing, we provide userPublicKey)
  3. Sign locally with keypair (private key never leaves this machine)
  4. Submit to Solana RPC, confirm on-chain

Safety:
  - Max 0.15 SOL per swap (operator-enforced)
  - Blocked infrastructure/platform tokens (when buying)
  - 1 swap per heartbeat cooldown (reset by daemon)
  - Min SOL reserve for gas fees
  - All trades logged to ~/.repryntt/wallet/trade_logs/
"""

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional, Tuple

import base58
import requests

logger = logging.getLogger("repryntt.tools.jupiter")

# ─── Configuration ────────────────────────────────────────────────────────────

WALLETS_DIR = Path.home() / ".repryntt" / "wallet"
DEFAULT_WALLET_NAME = "jupiter"  # ~/.repryntt/wallet/jupiter_trading.json
RPC_ENDPOINT = os.environ.get(
    "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
)
JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://api.jup.ag/swap/v1/swap"

# Named wallet file mapping: wallet name → filename in WALLETS_DIR
WALLET_FILES = {
    "jupiter": "jupiter_trading.json",
}

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000

# Safety limits
MAX_SOL_PER_SWAP = 0.15
DEFAULT_SLIPPAGE_BPS = 150   # 1.5%
MAX_SLIPPAGE_BPS = 300       # 3%
MIN_SOL_RESERVE = 0.005      # keep for tx fees

# Per-heartbeat cooldown (reset by daemon at heartbeat start)
_last_swap_time: float = 0.0
_SWAP_COOLDOWN_SECONDS = 600

# Blocked infrastructure tokens — only when BUYING (from_token=SOL)
BLOCKED_BUY_TOKENS = {
    "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn": "PUMP (pump.fun platform token)",
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": "RAY (Raydium DEX token)",
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": "JUP (Jupiter aggregator token)",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC (stablecoin)",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT (stablecoin)",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": "mSOL (staked SOL)",
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj": "stSOL (staked SOL)",
}

# Exclude DEXes whose pools cause simulation failures (Custom:17)
# Sanctum/Marinade liquid staking pools require validator balance updates
EXCLUDED_DEXES = "Sanctum,SanctumInfinity,Marinade,Lido"

TRADE_LOG_DIR = Path.home() / ".repryntt" / "wallet" / "trade_logs"


# ─── Cooldown Management ─────────────────────────────────────────────────────

def reset_jupiter_cooldown():
    """Called by daemon at heartbeat start to allow 1 swap per heartbeat."""
    global _last_swap_time
    _last_swap_time = 0.0


# ─── Wallet Management ───────────────────────────────────────────────────────

_cached_keypairs: dict = {}  # name → Keypair


def _save_wallet_file(wallet_path: Path, kp, name: str):
    """Save a wallet keypair in human-readable JSON format."""
    import base58 as b58
    secret_bytes = bytes(kp)
    data = {
        "name": name,
        "address": str(kp.pubkey()),
        "private_key": b58.b58encode(secret_bytes).decode(),
        "raw_bytes": list(secret_bytes),
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    WALLETS_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(wallet_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)


def _load_wallet(name: str):
    """Load a named wallet keypair. Auto-creates the 'jupiter' wallet if missing."""
    if name in _cached_keypairs:
        return _cached_keypairs[name]

    from solders.keypair import Keypair

    filename = WALLET_FILES.get(name)
    if not filename:
        raise ValueError(
            f"Unknown wallet '{name}'. Available: {', '.join(WALLET_FILES.keys())}"
        )

    wallet_path = WALLETS_DIR / filename
    if wallet_path.exists():
        with open(wallet_path) as f:
            data = json.load(f)
        # Support both old format (raw byte array) and new format (dict with fields)
        if isinstance(data, list):
            # Old format: bare byte array — load and upgrade to new format
            kp = Keypair.from_bytes(bytes(data))
            _save_wallet_file(wallet_path, kp, name)
            logger.info(f"Upgraded wallet '{name}' to human-readable format")
        else:
            # New format: dict with private_key, raw_bytes, address
            kp = Keypair.from_bytes(bytes(data["raw_bytes"]))
        _cached_keypairs[name] = kp
    elif name == "jupiter":
        # Auto-generate jupiter wallet on first use
        kp = Keypair()
        _save_wallet_file(wallet_path, kp, name)
        logger.info(f"Generated new Jupiter trading wallet: {kp.pubkey()}")
        _cached_keypairs[name] = kp
    else:
        raise FileNotFoundError(
            f"Wallet '{name}' not found at {wallet_path}. "
            f"Use jupiter_list_wallets to see available wallets."
        )

    return _cached_keypairs[name]


def _resolve_keypair(wallet: str = ""):
    """Resolve wallet parameter to a Keypair. Defaults to 'jupiter'."""
    name = wallet.strip().lower() if wallet else DEFAULT_WALLET_NAME
    return _load_wallet(name)


# ─── Solana RPC Helpers ──────────────────────────────────────────────────────

def _rpc(method: str, params: list, timeout: int = 30) -> dict:
    """Call Solana JSON-RPC."""
    resp = requests.post(
        RPC_ENDPOINT,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=timeout,
    )
    return resp.json()


def _get_sol_balance(wallet: str = "") -> float:
    """Get wallet SOL balance."""
    kp = _resolve_keypair(wallet)
    result = _rpc("getBalance", [str(kp.pubkey())])
    return result.get("result", {}).get("value", 0) / LAMPORTS_PER_SOL


def _get_token_balance(mint_address: str, wallet: str = "") -> Tuple[float, int]:
    """Get token balance. Returns (ui_amount, raw_amount)."""
    kp = _resolve_keypair(wallet)
    result = _rpc("getTokenAccountsByOwner", [
        str(kp.pubkey()),
        {"mint": mint_address},
        {"encoding": "jsonParsed"},
    ])
    accounts = result.get("result", {}).get("value", [])
    if accounts:
        info = accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]
        return float(info.get("uiAmount", 0) or 0), int(info.get("amount", 0))
    return 0.0, 0


def _get_token_decimals(mint_address: str) -> int:
    """Get token decimals from on-chain mint data."""
    if mint_address == SOL_MINT:
        return 9
    result = _rpc("getAccountInfo", [mint_address, {"encoding": "jsonParsed"}])
    val = result.get("result", {}).get("value")
    if val:
        data = val.get("data", {})
        if isinstance(data, dict):
            parsed = data.get("parsed", {})
            if isinstance(parsed, dict):
                return parsed.get("info", {}).get("decimals", 6)
    return 6  # safe fallback for most SPL tokens


# ─── Trade Logging ───────────────────────────────────────────────────────────

def _log_trade(trade_data: dict):
    """Append trade to daily log file."""
    try:
        TRADE_LOG_DIR.mkdir(parents=True, exist_ok=True)
        today = time.strftime("%Y-%m-%d", time.gmtime())
        log_file = TRADE_LOG_DIR / f"jupiter_trades_{today}.json"
        trades = []
        if log_file.exists():
            with open(log_file) as f:
                trades = json.load(f)
        trades.append(trade_data)
        with open(log_file, "w") as f:
            json.dump(trades, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to log jupiter trade: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AGENT-CALLABLE TOOLS (all return JSON strings)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def jupiter_list_wallets(**kw) -> str:
    """List all available Solana trading wallets with addresses and balances.
    Use this to see which wallets you can trade from."""
    results = []
    for name, filename in WALLET_FILES.items():
        path = WALLETS_DIR / filename
        entry = {"name": name, "file": filename, "exists": path.exists()}
        if path.exists():
            try:
                kp = _load_wallet(name)
                entry["address"] = str(kp.pubkey())
                entry["sol_balance"] = round(
                    _get_sol_balance(name), 9
                )
            except Exception as e:
                entry["error"] = str(e)
        results.append(entry)
    return json.dumps({"wallets": results})


def jupiter_wallet_status(wallet: str = "", **kw) -> str:
    """Show a trading wallet's address and SOL balance.
    Defaults to the Jupiter wallet.
    Use jupiter_list_wallets to see all available wallets.

    Parameters:
        wallet: Wallet name — 'jupiter' (default).
    """
    try:
        kp = _resolve_keypair(wallet)
        wname = wallet.strip().lower() if wallet else DEFAULT_WALLET_NAME
        sol = _get_sol_balance(wname)
        return json.dumps({
            "wallet": wname,
            "address": str(kp.pubkey()),
            "sol_balance": round(sol, 9),
            "min_reserve_for_gas": MIN_SOL_RESERVE,
            "max_sol_per_swap": MAX_SOL_PER_SWAP,
            "ready_to_trade": sol > MIN_SOL_RESERVE,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def jupiter_balance(token_mint: str = "", wallet: str = "", **kw) -> str:
    """Check SOL and/or token balance in a trading wallet.

    Parameters:
        token_mint: Token mint address to check (optional). If empty, only shows SOL balance.
        wallet: Wallet name — 'jupiter' (default).
    """
    try:
        kp = _resolve_keypair(wallet)
        wname = wallet.strip().lower() if wallet else DEFAULT_WALLET_NAME
        result = {
            "wallet": wname,
            "address": str(kp.pubkey()),
            "sol_balance": round(_get_sol_balance(wname), 9),
        }
        if token_mint and token_mint != SOL_MINT:
            ui_amount, raw_amount = _get_token_balance(token_mint, wname)
            result["token_mint"] = token_mint
            result["token_balance"] = ui_amount
            result["token_balance_raw"] = raw_amount
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


def jupiter_quote(input_mint: str = "", output_mint: str = "",
                  amount: str = "", slippage_bps: str = "", **kw) -> str:
    """Get a Jupiter swap quote WITHOUT executing. Preview price, output amount,
    price impact, and route. Use this to check a trade before committing.

    Parameters:
        input_mint: Mint address of token to swap FROM (SOL = So11111111111111111111111111111111111111112).
        output_mint: Mint address of token to swap TO.
        amount: Amount in HUMAN-READABLE units (e.g. '0.12' for 0.12 SOL). NOT lamports.
        slippage_bps: Slippage tolerance in basis points (default 150 = 1.5%). Max 300.
    """
    for p, v in [("input_mint", input_mint), ("output_mint", output_mint), ("amount", amount)]:
        if not v:
            return json.dumps({"error": f"{p} is required"})

    try:
        amt = float(amount)
        if amt > 1_000_000_000:
            return json.dumps({"error": f"Amount {amount} looks like raw units. Use human amounts (e.g. 0.12 SOL)."})
    except (ValueError, TypeError):
        return json.dumps({"error": f"Invalid amount: {amount}"})

    try:
        decimals = _get_token_decimals(input_mint)
        raw_amount = int(amt * (10 ** decimals))

        slip = int(slippage_bps) if slippage_bps else DEFAULT_SLIPPAGE_BPS
        slip = min(slip, MAX_SLIPPAGE_BPS)

        resp = requests.get(JUPITER_QUOTE_URL, params={
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(raw_amount),
            "slippageBps": slip,
            "excludeDexes": EXCLUDED_DEXES,
        }, timeout=15)

        if resp.status_code != 200:
            return json.dumps({"error": f"Jupiter quote failed: {resp.text[:500]}"})

        quote = resp.json()
        if "error" in quote:
            return json.dumps({"error": f"Jupiter: {quote['error']}"})

        out_decimals = _get_token_decimals(output_mint)
        out_amount = int(quote.get("outAmount", 0)) / (10 ** out_decimals)

        return json.dumps({
            "input_mint": input_mint,
            "output_mint": output_mint,
            "input_amount": amt,
            "output_amount": round(out_amount, 9),
            "price_impact_pct": quote.get("priceImpactPct", "unknown"),
            "route": [
                r.get("swapInfo", {}).get("label", "?")
                for r in quote.get("routePlan", [])
            ],
            "slippage_bps": slip,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def jupiter_swap(input_mint: str = "", output_mint: str = "",
                 amount: str = "", slippage_bps: str = "",
                 wallet: str = "", **kw) -> str:
    """Execute a LIVE on-chain token swap via Jupiter on Solana mainnet.
    Signs locally and submits the transaction. This is REAL MONEY.
    Use jupiter_quote first to preview the trade.
    Max 0.15 SOL per swap. One swap per heartbeat.

    Parameters:
        input_mint: Mint address of token to swap FROM. Use So11111111111111111111111111111111111111112 for SOL.
        output_mint: Mint address of token to swap TO.
        amount: Amount in HUMAN-READABLE units (e.g. '0.12' for 0.12 SOL, '500.0' for 500 tokens). NOT lamports or raw units.
        slippage_bps: Slippage tolerance in basis points (default 150 = 1.5%). Max 300.
        wallet: Wallet name — 'jupiter' (default). Use jupiter_list_wallets to see options.
    """
    for p, v in [("input_mint", input_mint), ("output_mint", output_mint), ("amount", amount)]:
        if not v:
            return json.dumps({"error": f"{p} is required"})

    global _last_swap_time
    now = time.time()

    # ── Cooldown ──
    if now - _last_swap_time < _SWAP_COOLDOWN_SECONDS:
        remaining = int(_SWAP_COOLDOWN_SECONDS - (now - _last_swap_time))
        return json.dumps({
            "error": f"Swap cooldown: {remaining}s remaining. 1 swap per heartbeat."
        })

    # ── Validate amount ──
    try:
        amt = float(amount)
    except (ValueError, TypeError):
        return json.dumps({"error": f"Invalid amount: {amount}"})

    if amt <= 0:
        return json.dumps({"error": "Amount must be positive"})

    if amt > 1_000_000_000:
        return json.dumps({
            "error": f"Amount {amt} looks like raw units (lamports). "
                     "Use human-readable amounts (e.g. 0.12 for SOL)."
        })

    # ── Block infrastructure tokens when buying ──
    if input_mint == SOL_MINT and output_mint in BLOCKED_BUY_TOKENS:
        name = BLOCKED_BUY_TOKENS[output_mint]
        return json.dumps({
            "error": f"BLOCKED: {name} is NOT a memecoin. "
                     "Use degen_terminal_top to find actual memecoins."
        })

    # ── Max SOL per swap ──
    if input_mint == SOL_MINT and amt > MAX_SOL_PER_SWAP:
        return json.dumps({
            "error": f"Max {MAX_SOL_PER_SWAP} SOL per swap. Got {amt}."
        })

    try:
        from solders.transaction import VersionedTransaction

        wname = wallet.strip().lower() if wallet else DEFAULT_WALLET_NAME
        kp = _resolve_keypair(wname)

        # Check SOL balance
        sol_bal = _get_sol_balance(wname)
        if input_mint == SOL_MINT:
            if sol_bal < amt + MIN_SOL_RESERVE:
                return json.dumps({
                    "error": f"Insufficient SOL. Have {sol_bal:.6f}, "
                             f"need {amt + MIN_SOL_RESERVE:.6f} "
                             f"({amt} + {MIN_SOL_RESERVE} for gas)"
                })
        else:
            if sol_bal < MIN_SOL_RESERVE:
                return json.dumps({
                    "error": f"Need at least {MIN_SOL_RESERVE} SOL for gas. "
                             f"Have {sol_bal:.6f}"
                })

        # Convert to smallest units
        decimals = _get_token_decimals(input_mint)
        raw_amount = int(amt * (10 ** decimals))

        slip = int(slippage_bps) if slippage_bps else DEFAULT_SLIPPAGE_BPS
        slip = min(slip, MAX_SLIPPAGE_BPS)

        # 1. Get quote
        quote_resp = requests.get(JUPITER_QUOTE_URL, params={
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(raw_amount),
            "slippageBps": slip,
            "excludeDexes": EXCLUDED_DEXES,
        }, timeout=15)

        if quote_resp.status_code != 200:
            return json.dumps({"error": f"Quote failed: {quote_resp.text[:500]}"})

        quote = quote_resp.json()
        if "error" in quote:
            return json.dumps({"error": f"Jupiter: {quote['error']}"})

        # 2. Build swap transaction
        swap_resp = requests.post(JUPITER_SWAP_URL, json={
            "quoteResponse": quote,
            "userPublicKey": str(kp.pubkey()),
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto",
        }, timeout=30)

        if swap_resp.status_code != 200:
            return json.dumps({"error": f"Swap build failed: {swap_resp.text[:500]}"})

        swap_data = swap_resp.json()
        swap_tx_b64 = swap_data.get("swapTransaction")
        if not swap_tx_b64:
            return json.dumps({"error": "Jupiter returned no swapTransaction"})

        # 3. Sign
        tx_bytes = base64.b64decode(swap_tx_b64)
        tx = VersionedTransaction.from_bytes(tx_bytes)
        signed_tx = VersionedTransaction(tx.message, [kp])
        signed_bytes = bytes(signed_tx)

        # 4. Submit to Solana RPC
        rpc_result = _rpc("sendTransaction", [
            base58.b58encode(signed_bytes).decode(),
            {"skipPreflight": False, "preflightCommitment": "confirmed"},
        ], timeout=60)

        if "error" in rpc_result:
            err = rpc_result["error"]
            err_msg = (
                err.get("message", str(err)) if isinstance(err, dict) else str(err)
            )
            return json.dumps({"error": f"Transaction failed: {err_msg}"})

        tx_sig = rpc_result.get("result", "")

        # Record cooldown ONLY on success
        _last_swap_time = time.time()

        out_decimals = _get_token_decimals(output_mint)
        out_amount = int(quote.get("outAmount", 0)) / (10 ** out_decimals)

        _log_trade({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "action": "swap",
            "wallet": wname,
            "wallet_address": str(kp.pubkey()),
            "input_mint": input_mint,
            "output_mint": output_mint,
            "input_amount": amt,
            "output_amount_expected": round(out_amount, 9),
            "price_impact_pct": quote.get("priceImpactPct"),
            "tx_signature": tx_sig,
            "explorer": f"https://solscan.io/tx/{tx_sig}",
        })

        return json.dumps({
            "success": True,
            "wallet": wname,
            "wallet_address": str(kp.pubkey()),
            "tx_signature": tx_sig,
            "explorer": f"https://solscan.io/tx/{tx_sig}",
            "input_amount": amt,
            "output_amount_expected": round(out_amount, 9),
            "price_impact_pct": quote.get("priceImpactPct"),
            "route": [
                r.get("swapInfo", {}).get("label", "?")
                for r in quote.get("routePlan", [])
            ],
        })

    except Exception as e:
        logger.error(f"Jupiter swap failed: {e}", exc_info=True)
        return json.dumps({"error": f"Swap failed: {str(e)}"})


def jupiter_sell_token(token_mint: str = "", sell_pct: str = "100",
                       slippage_bps: str = "", wallet: str = "", **kw) -> str:
    """Sell a token for SOL via Jupiter. Auto-detects your balance.
    Specify sell_pct to sell a portion (e.g. '50' to sell half).

    Parameters:
        token_mint: Mint address of the token to sell.
        sell_pct: Percentage of your holdings to sell (default 100 = sell all).
        slippage_bps: Slippage tolerance in basis points (default 150 = 1.5%).
        wallet: Wallet name — 'jupiter' (default).
    """
    if not token_mint:
        return json.dumps({"error": "token_mint is required"})

    try:
        wname = wallet.strip().lower() if wallet else DEFAULT_WALLET_NAME
        ui_amount, raw_amount = _get_token_balance(token_mint, wname)
        if raw_amount == 0:
            return json.dumps({
                "error": f"No balance for token {token_mint[:16]}... in {wname} wallet"
            })

        pct = float(sell_pct) if sell_pct else 100.0
        if pct <= 0 or pct > 100:
            return json.dumps({"error": "sell_pct must be between 1 and 100"})

        sell_raw = int(raw_amount * (pct / 100.0))
        decimals = _get_token_decimals(token_mint)
        sell_human = sell_raw / (10 ** decimals)

        return jupiter_swap(
            input_mint=token_mint,
            output_mint=SOL_MINT,
            amount=str(sell_human),
            slippage_bps=slippage_bps or "",
            wallet=wname,
        )
    except Exception as e:
        return json.dumps({"error": f"Sell failed: {str(e)}"})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TOOL EXPORT (for registry)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ALL_JUPITER_TOOLS = {
    "jupiter_list_wallets": jupiter_list_wallets,
    "jupiter_wallet_status": jupiter_wallet_status,
    "jupiter_balance": jupiter_balance,
    "jupiter_quote": jupiter_quote,
    "jupiter_swap": jupiter_swap,
    "jupiter_sell_token": jupiter_sell_token,
}
