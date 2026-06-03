"""
Repryntt Solana Bridge — Cross-Chain Deposit & Withdrawal
=========================================================

Bridges SOL/USDC from the Solana network into the repryntt economy.
Deposits credit a local SOL/USDC balance that can be used on the repryntt
order book to buy CR at market price (no fixed peg — the market decides).

Architecture:
  1. User calls ``create_deposit(repryntt_address)`` → gets Artemis's Solana
     deposit address + a unique deposit ID.
  2. User sends SOL or USDC to that Solana address.
  3. ``poll_deposits()`` (daemon cron / heartbeat) detects the on-chain tx.
  4. The deposit amount is credited to the user's **SOL or USDC bridge
     balance** (NOT auto-converted to CR).
  5. User places buy orders on the CR/SOL order book at whatever price they
     want. Miners and holders place sell orders. The market sets the price.
  6. Withdrawals: user can request SOL/USDC withdrawal from their bridge
     balance (sends real SOL/USDC back to their Solana address).

Market model:
  - CR has NO fixed USD peg. Price is determined by the order book.
  - SOL/USDC are the quote currencies (like USD on a stock exchange).
  - Miners earn CR through Proof of Power → sell on the order book for SOL.
  - Users deposit SOL → buy CR on the order book → spend CR on AI services.

Security:
  - Only confirmed on-chain Solana transactions trigger balance credits
  - Each tx signature is recorded to prevent double-crediting
  - All deposits logged to JSONL audit file
  - Bridge balances persisted to disk
"""

import asyncio
import json
import logging
import os
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

import aiohttp

logger = logging.getLogger("repryntt.economy.payment_gateway")

# ── Configuration ─────────────────────────────────────────────────────────────

PLANCKS_PER_CREDIT = 100_000_000

# No fixed price — market determines CR value.  These are bridge limits only.
MAX_SOL_PER_DEPOSIT = float(os.environ.get("REPRYNTT_MAX_SOL_DEPOSIT", "100"))
MAX_USDC_PER_DEPOSIT = float(os.environ.get("REPRYNTT_MAX_USDC_DEPOSIT", "10000"))

# Solana
WALLET_PATH = Path.home() / ".repryntt" / "wallet" / "artemis_mainnet.json"
RPC_ENDPOINT = os.environ.get(
    "SOLANA_RPC_URL",
    "https://api.mainnet-beta.solana.com"
)
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
LAMPORTS_PER_SOL = 1_000_000_000
USDC_DECIMALS = 6

# Repryntt blockchain node
BLOCKCHAIN_HOST = os.environ.get("REPRYNTT_NODE_HOST", "127.0.0.1")
BLOCKCHAIN_PORT = int(os.environ.get("REPRYNTT_NODE_PORT", "5001"))

# Storage
GATEWAY_DIR = Path.home() / ".repryntt" / "commerce" / "payment_gateway"
DEPOSITS_FILE = GATEWAY_DIR / "deposits.jsonl"
PENDING_FILE = GATEWAY_DIR / "pending_deposits.json"
PROCESSED_SIGS_FILE = GATEWAY_DIR / "processed_signatures.json"
GATEWAY_DIR.mkdir(parents=True, exist_ok=True)

# Poll interval
POLL_INTERVAL_S = 30

# ── State ─────────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_processed_signatures: set = set()
_pending_deposits: Dict[str, Dict[str, Any]] = {}  # deposit_id → deposit info
_solana_pubkey: Optional[str] = None

# Bridge balances — SOL/USDC held per repryntt address (persisted to disk)
# Stored as INTEGER units: SOL in lamports (1 SOL = 10^9), USDC in micro-units (1 USDC = 10^6).
# This avoids all IEEE 754 floating-point rounding issues.
BRIDGE_BALANCES_FILE = GATEWAY_DIR / "bridge_balances.json"
_bridge_balances: Dict[str, Dict[str, int]] = {}
_bridge_balances_version = 2  # v2 = integer lamports/micro-units


def _load_state():
    """Load processed signatures, pending deposits, and bridge balances from disk."""
    global _processed_signatures, _pending_deposits, _bridge_balances
    if PROCESSED_SIGS_FILE.exists():
        try:
            with open(PROCESSED_SIGS_FILE) as f:
                _processed_signatures = set(json.load(f))
        except Exception:
            _processed_signatures = set()
    if PENDING_FILE.exists():
        try:
            with open(PENDING_FILE) as f:
                _pending_deposits = json.load(f)
        except Exception:
            _pending_deposits = {}
    if BRIDGE_BALANCES_FILE.exists():
        try:
            with open(BRIDGE_BALANCES_FILE) as f:
                raw = json.load(f)
            # Migrate v1 (float) → v2 (integer lamports/micro-units) on first load
            if raw.get("_version") != _bridge_balances_version:
                migrated: Dict[str, Dict[str, int]] = {}
                for addr, bals in raw.items():
                    if addr.startswith("_"):
                        continue  # skip metadata keys
                    sol_f = bals.get("sol", 0) if isinstance(bals, dict) else 0
                    usdc_f = bals.get("usdc", 0) if isinstance(bals, dict) else 0
                    migrated[addr] = {
                        "sol": int(round(sol_f * LAMPORTS_PER_SOL)),
                        "usdc": int(round(usdc_f * (10 ** USDC_DECIMALS))),
                    }
                _bridge_balances = migrated
                logger.info(f"Migrated {len(migrated)} bridge balances from float to integer")
                _save_state()  # persist the migration
            else:
                raw.pop("_version", None)
                _bridge_balances = {k: v for k, v in raw.items() if not k.startswith("_")}
        except Exception:
            _bridge_balances = {}


def _save_state():
    """Persist state to disk."""
    try:
        with open(PROCESSED_SIGS_FILE, 'w') as f:
            json.dump(list(_processed_signatures)[-5000:], f)  # Keep last 5000
        with open(PENDING_FILE, 'w') as f:
            json.dump(_pending_deposits, f, indent=2)
        with open(BRIDGE_BALANCES_FILE, 'w') as f:
            save_data = dict(_bridge_balances)
            save_data["_version"] = _bridge_balances_version
            json.dump(save_data, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save gateway state: {e}")


# ── Bridge Balance Helpers ────────────────────────────────────────────────────

def get_bridge_balance(repryntt_address: str) -> Dict[str, float]:
    """Get a user's SOL/USDC bridge balance (returned as human-readable floats)."""
    with _lock:
        raw = _bridge_balances.get(repryntt_address, {"sol": 0, "usdc": 0})
        return {
            "sol": raw.get("sol", 0) / LAMPORTS_PER_SOL,
            "usdc": raw.get("usdc", 0) / (10 ** USDC_DECIMALS),
        }


def _credit_bridge_balance(repryntt_address: str, currency: str, amount: float):
    """Credit SOL or USDC to a user's bridge balance (internal — called after deposit confirms).

    `amount` is in human-readable units (e.g. 1.5 SOL, 100.0 USDC).
    Internally stored as integer lamports / micro-units.
    """
    key = currency.lower()
    if key not in ("sol", "usdc"):
        raise ValueError(f"Unsupported bridge currency: {currency}")
    # Convert float amount to integer units
    if key == "sol":
        units = int(round(amount * LAMPORTS_PER_SOL))
    else:
        units = int(round(amount * (10 ** USDC_DECIMALS)))
    with _lock:
        if repryntt_address not in _bridge_balances:
            _bridge_balances[repryntt_address] = {"sol": 0, "usdc": 0}
        _bridge_balances[repryntt_address][key] += units
        _save_state()


def debit_bridge_balance(repryntt_address: str, currency: str, amount: float) -> bool:
    """Debit SOL or USDC from a user's bridge balance (called by order book on buy-order fill).

    `amount` is in human-readable units (e.g. 0.5 SOL).  Internally compared as integer units.
    Returns True if successful, False if insufficient balance.
    """
    key = currency.lower()
    if key not in ("sol", "usdc"):
        return False
    if key == "sol":
        units = int(round(amount * LAMPORTS_PER_SOL))
    else:
        units = int(round(amount * (10 ** USDC_DECIMALS)))
    with _lock:
        bal = _bridge_balances.get(repryntt_address, {}).get(key, 0)
        if bal < units:
            return False
        _bridge_balances[repryntt_address][key] = bal - units
        _save_state()
        return True


def credit_bridge_balance_external(repryntt_address: str, currency: str, amount: float):
    """Credit bridge balance (callable by trading engine when a sell order fills — seller receives SOL/USDC)."""
    _credit_bridge_balance(repryntt_address, currency, amount)


def _log_deposit(record: dict):
    """Append a deposit record to the audit log."""
    try:
        with open(DEPOSITS_FILE, 'a') as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        logger.error(f"Failed to log deposit: {e}")


# ── Solana Wallet ─────────────────────────────────────────────────────────────

def _get_solana_pubkey() -> str:
    """Get Artemis's Solana public key (deposit address)."""
    global _solana_pubkey
    if _solana_pubkey:
        return _solana_pubkey
    try:
        from solders.keypair import Keypair
        with open(WALLET_PATH) as f:
            secret = json.load(f)
        kp = Keypair.from_bytes(bytes(secret))
        _solana_pubkey = str(kp.pubkey())
        return _solana_pubkey
    except Exception as e:
        logger.error(f"Failed to load Solana wallet: {e}")
        return ""


# ── Price Feeds ───────────────────────────────────────────────────────────────

_price_cache: Dict[str, tuple] = {}  # symbol → (price_usd, timestamp)
PRICE_CACHE_TTL = 120  # 2 minutes


async def _get_sol_price_usd(session: aiohttp.ClientSession) -> float:
    """Get current SOL price in USD."""
    cached = _price_cache.get("SOL")
    if cached and time.time() - cached[1] < PRICE_CACHE_TTL:
        return cached[0]
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                price = data.get("solana", {}).get("usd", 0)
                if price > 0:
                    _price_cache["SOL"] = (price, time.time())
                    return price
    except Exception as e:
        logger.warning(f"CoinGecko SOL price fetch failed: {e}")
    # Fallback: use Jupiter quote (SOL → USDC)
    try:
        url = f"https://quote-api.jup.ag/v6/quote?inputMint={SOL_MINT}&outputMint={USDC_MINT}&amount={LAMPORTS_PER_SOL}&slippageBps=50"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                out_amount = int(data.get("outAmount", 0))
                price = out_amount / (10 ** USDC_DECIMALS)
                if price > 0:
                    _price_cache["SOL"] = (price, time.time())
                    return price
    except Exception as e:
        logger.warning(f"Jupiter SOL price fetch failed: {e}")
    return _price_cache.get("SOL", (0,))[0]


# ── Blockchain Node Client ────────────────────────────────────────────────────

def _credit_repryntt_wallet(address: str, amount_cr: float) -> dict:
    """Credit a Repryntt blockchain wallet with CR via the node's credit_address API."""
    import socket
    import struct
    from repryntt.economy.safe_serialize import pack, unpack

    amount_plancks = int(amount_cr * PLANCKS_PER_CREDIT)
    if amount_plancks <= 0:
        return {"success": False, "error": "Amount must be positive"}

    msg = {
        "type": "credit_address",
        "address": address,
        "amount_plancks": amount_plancks,
    }

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)
    try:
        sock.connect((BLOCKCHAIN_HOST, BLOCKCHAIN_PORT))
        data = pack(msg)
        sock.sendall(struct.pack('!I', len(data)))
        sock.sendall(data)
        resp_len_bytes = sock.recv(4)
        if len(resp_len_bytes) < 4:
            return {"success": False, "error": "No response from blockchain node"}
        resp_len = struct.unpack('!I', resp_len_bytes)[0]
        resp_data = b''
        while len(resp_data) < resp_len:
            chunk = sock.recv(min(resp_len - len(resp_data), 65536))
            if not chunk:
                break
            resp_data += chunk
        return unpack(resp_data)
    except ConnectionRefusedError:
        return {"success": False, "error": "Blockchain node not running"}
    except Exception as e:
        return {"success": False, "error": f"Node communication failed: {e}"}
    finally:
        sock.close()


def _get_repryntt_balance(address: str) -> dict:
    """Query a Repryntt address balance from the blockchain node."""
    import socket
    import struct
    from repryntt.economy.safe_serialize import pack, unpack

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)
    try:
        sock.connect((BLOCKCHAIN_HOST, BLOCKCHAIN_PORT))
        data = pack({"type": "get_balance", "address": address})
        sock.sendall(struct.pack('!I', len(data)))
        sock.sendall(data)
        resp_len_bytes = sock.recv(4)
        if len(resp_len_bytes) < 4:
            return {"success": False, "error": "No response"}
        resp_len = struct.unpack('!I', resp_len_bytes)[0]
        resp_data = b''
        while len(resp_data) < resp_len:
            chunk = sock.recv(min(resp_len - len(resp_data), 65536))
            if not chunk:
                break
            resp_data += chunk
        return unpack(resp_data)
    except ConnectionRefusedError:
        return {"success": False, "error": "Blockchain node not running"}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        sock.close()


# ── Solana Transaction Scanning ───────────────────────────────────────────────

async def _get_recent_sol_transactions(
    session: aiohttp.ClientSession,
    pubkey: str,
    limit: int = 20,
) -> List[dict]:
    """Fetch recent confirmed transactions for a Solana address."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [pubkey, {"limit": limit, "commitment": "confirmed"}],
    }
    try:
        async with session.post(RPC_ENDPOINT, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("result", [])
    except Exception as e:
        logger.warning(f"Failed to fetch Solana signatures: {e}")
    return []


async def _get_transaction_details(
    session: aiohttp.ClientSession,
    signature: str,
) -> Optional[dict]:
    """Fetch full transaction details for a Solana signature."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [signature, {"encoding": "jsonParsed", "commitment": "confirmed",
                               "maxSupportedTransactionVersion": 0}],
    }
    try:
        async with session.post(RPC_ENDPOINT, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("result")
    except Exception as e:
        logger.warning(f"Failed to fetch tx {signature[:16]}...: {e}")
    return None


def _extract_sol_deposit(tx: dict, our_pubkey: str) -> Optional[Dict[str, Any]]:
    """Extract SOL or USDC deposit amount from a confirmed transaction.

    Returns dict with {currency, amount, sender} or None if not a deposit to us.
    """
    if not tx or not tx.get("meta"):
        return None

    meta = tx["meta"]
    if meta.get("err"):
        return None  # Failed tx

    message = tx.get("transaction", {}).get("message", {})
    account_keys = message.get("accountKeys", [])

    # Get list of pubkeys (handle both parsed and unparsed formats)
    pubkeys = []
    for ak in account_keys:
        if isinstance(ak, str):
            pubkeys.append(ak)
        elif isinstance(ak, dict):
            pubkeys.append(ak.get("pubkey", ""))

    if our_pubkey not in pubkeys:
        return None

    our_idx = pubkeys.index(our_pubkey)

    # Check SOL balance change
    pre_balances = meta.get("preBalances", [])
    post_balances = meta.get("postBalances", [])
    if our_idx < len(pre_balances) and our_idx < len(post_balances):
        sol_change = post_balances[our_idx] - pre_balances[our_idx]
        if sol_change > 0:
            sol_amount = sol_change / LAMPORTS_PER_SOL
            if sol_amount >= 0.001:  # Min deposit: 0.001 SOL
                # Find sender (first signer that isn't us)
                sender = "unknown"
                for pk in pubkeys:
                    if pk != our_pubkey and pk != "11111111111111111111111111111111":
                        sender = pk
                        break
                return {
                    "currency": "SOL",
                    "amount": sol_amount,
                    "sender": sender,
                }

    # Check USDC SPL token transfers
    pre_token = meta.get("preTokenBalances", [])
    post_token = meta.get("postTokenBalances", [])

    for post_bal in post_token:
        if post_bal.get("mint") != USDC_MINT:
            continue
        if post_bal.get("owner") != our_pubkey:
            continue
        post_amount = float(post_bal.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
        # Find matching pre-balance
        pre_amount = 0
        for pre_bal in pre_token:
            if (pre_bal.get("mint") == USDC_MINT and
                    pre_bal.get("owner") == our_pubkey):
                pre_amount = float(pre_bal.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                break
        usdc_change = post_amount - pre_amount
        if usdc_change >= 0.01:  # Min deposit: $0.01
            sender = "unknown"
            for pre_bal in pre_token:
                if (pre_bal.get("mint") == USDC_MINT and
                        pre_bal.get("owner") != our_pubkey):
                    sender = pre_bal.get("owner", "unknown")
                    break
            return {
                "currency": "USDC",
                "amount": usdc_change,
                "sender": sender,
            }

    return None


# ── Core Gateway Functions ────────────────────────────────────────────────────

def create_deposit(repryntt_address: str, **kw) -> str:
    """Create a pending deposit — buyer will send SOL or USDC to our Solana address.

    Parameters:
        repryntt_address: The buyer's Repryntt blockchain address (40 hex chars).

    Returns JSON with deposit_id, solana_deposit_address, and payment instructions.
    """
    if not repryntt_address or len(repryntt_address) < 20:
        return json.dumps({"error": "Valid repryntt_address is required (40 hex chars)"})

    sol_pubkey = _get_solana_pubkey()
    if not sol_pubkey:
        return json.dumps({"error": "Solana wallet not configured"})

    deposit_id = f"dep_{int(time.time())}_{repryntt_address[:8]}"

    with _lock:
        _pending_deposits[deposit_id] = {
            "id": deposit_id,
            "repryntt_address": repryntt_address,
            "solana_deposit_address": sol_pubkey,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
        }
        _save_state()

    return json.dumps({
        "success": True,
        "deposit_id": deposit_id,
        "solana_deposit_address": sol_pubkey,
        "accepted_currencies": ["SOL", "USDC"],
        "pricing_model": "market",
        "instructions": (
            f"Send SOL or USDC to {sol_pubkey} on Solana mainnet. "
            f"Your deposit will be credited to your bridge balance. "
            f"Then place buy orders on the CR/SOL order book at the price you choose. "
            f"The market determines the CR price — there is no fixed peg."
        ),
    })


def get_deposit_status(deposit_id: str = "", **kw) -> str:
    """Check the status of a deposit.

    Parameters:
        deposit_id: The deposit ID returned by create_deposit.
    """
    if not deposit_id:
        return json.dumps({"error": "deposit_id is required"})
    dep = _pending_deposits.get(deposit_id)
    if not dep:
        return json.dumps({"error": f"Deposit {deposit_id} not found"})
    return json.dumps(dep)


def get_gateway_status(**kw) -> str:
    """Get the Solana bridge status — deposit address, stats, and bridge balances."""
    sol_pubkey = _get_solana_pubkey()
    # Count completed deposits from log
    completed = 0
    total_sol_deposited = 0.0
    total_usdc_deposited = 0.0
    if DEPOSITS_FILE.exists():
        try:
            with open(DEPOSITS_FILE) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        if rec.get("status") == "completed":
                            completed += 1
                            if rec.get("currency") == "SOL":
                                total_sol_deposited += rec.get("amount", 0)
                            elif rec.get("currency") == "USDC":
                                total_usdc_deposited += rec.get("amount", 0)
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass

    return json.dumps({
        "success": True,
        "type": "solana_bridge",
        "solana_deposit_address": sol_pubkey,
        "accepted_currencies": ["SOL", "USDC"],
        "pricing_model": "market — CR price determined by the order book, no fixed peg",
        "pending_deposits": len([d for d in _pending_deposits.values() if d.get("status") == "pending"]),
        "completed_deposits": completed,
        "total_sol_deposited": round(total_sol_deposited, 4),
        "total_usdc_deposited": round(total_usdc_deposited, 2),
        "blockchain_node": f"{BLOCKCHAIN_HOST}:{BLOCKCHAIN_PORT}",
    })


def list_deposits(limit: int = 20, **kw) -> str:
    """List recent deposits (pending and completed).

    Parameters:
        limit: Max number of recent deposits to return.
    """
    deposits = []
    # Include pending
    for dep in list(_pending_deposits.values())[-limit:]:
        deposits.append(dep)
    # Include completed from log (last N)
    if DEPOSITS_FILE.exists():
        try:
            with open(DEPOSITS_FILE) as f:
                lines = f.readlines()
            for line in lines[-limit:]:
                try:
                    deposits.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass
    return json.dumps({"deposits": deposits[-limit:]})


# ── Deposit Processing ────────────────────────────────────────────────────────

async def poll_and_process_deposits() -> Dict[str, Any]:
    """Poll Solana for new transactions and credit bridge balances.

    Called by the daemon's heartbeat cron or a dedicated polling loop.
    Deposits SOL/USDC to the user's bridge balance (NOT auto-converted to CR).
    Users then buy CR on the order book at market price.
    """
    _load_state()  # Refresh from disk

    sol_pubkey = _get_solana_pubkey()
    if not sol_pubkey:
        return {"error": "No Solana wallet configured"}

    results = {"checked": 0, "new_deposits": 0, "sol_deposited": 0.0, "usdc_deposited": 0.0, "errors": []}

    async with aiohttp.ClientSession() as session:
        # Fetch recent transactions
        sigs = await _get_recent_sol_transactions(session, sol_pubkey, limit=30)
        results["checked"] = len(sigs)

        for sig_info in sigs:
            sig = sig_info.get("signature", "")
            if not sig or sig in _processed_signatures:
                continue

            # Skip failed txs
            if sig_info.get("err"):
                with _lock:
                    _processed_signatures.add(sig)
                continue

            # Fetch full details
            tx = await _get_transaction_details(session, sig)
            if not tx:
                continue

            deposit = _extract_sol_deposit(tx, sol_pubkey)
            if not deposit:
                continue

            currency = deposit["currency"]
            amount = deposit["amount"]

            # Enforce per-deposit limits
            if currency == "SOL" and amount > MAX_SOL_PER_DEPOSIT:
                amount = MAX_SOL_PER_DEPOSIT
            elif currency == "USDC" and amount > MAX_USDC_PER_DEPOSIT:
                amount = MAX_USDC_PER_DEPOSIT

            if amount < 0.001:
                continue  # Dust deposit

            # Find matching pending deposit (by sender) or use a default address
            repryntt_address = None
            matched_deposit_id = None

            for dep_id, dep in _pending_deposits.items():
                if dep.get("status") == "pending":
                    repryntt_address = dep["repryntt_address"]
                    matched_deposit_id = dep_id
                    break  # FIFO — first pending deposit gets matched

            if not repryntt_address:
                # No pending deposit — log as unmatched
                logger.warning(f"Unmatched deposit: {amount} {currency} from {deposit['sender']}")
                _log_deposit({
                    "signature": sig,
                    "currency": currency,
                    "amount": amount,
                    "sender": deposit["sender"],
                    "status": "unmatched",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                with _lock:
                    _processed_signatures.add(sig)
                continue

            # Credit the user's bridge balance (NOT CR — they buy CR on the order book)
            _credit_bridge_balance(repryntt_address, currency, amount)

            # Mark signature as processed AFTER credit to prevent deposit loss
            # on crash.  Worst case: crash between credit and mark → double credit
            # on re-poll (detectable, preferable to lost deposits).
            with _lock:
                _processed_signatures.add(sig)

            record = {
                "deposit_id": matched_deposit_id,
                "signature": sig,
                "currency": currency,
                "amount": round(amount, 9),
                "repryntt_address": repryntt_address,
                "sender": deposit["sender"],
                "status": "completed",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            if currency == "SOL":
                results["sol_deposited"] += amount
            else:
                results["usdc_deposited"] += amount
            results["new_deposits"] += 1

            logger.info(
                f"Bridge deposit: {amount} {currency} → bridge balance for "
                f"{repryntt_address[:16]}... (use order book to buy CR)"
            )

            # Update pending deposit status
            if matched_deposit_id and matched_deposit_id in _pending_deposits:
                _pending_deposits[matched_deposit_id]["status"] = "completed"
                _pending_deposits[matched_deposit_id]["bridge_credited"] = {
                    "currency": currency, "amount": round(amount, 9)
                }
                _pending_deposits[matched_deposit_id]["solana_signature"] = sig

            _log_deposit(record)

        with _lock:
            _save_state()

    return results


def poll_deposits_sync() -> Dict[str, Any]:
    """Synchronous wrapper for poll_and_process_deposits."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, poll_and_process_deposits())
                return future.result(timeout=60)
        else:
            return asyncio.run(poll_and_process_deposits())
    except Exception as e:
        return {"error": f"Deposit polling failed: {e}"}


# ── Initialization ────────────────────────────────────────────────────────────

_load_state()
