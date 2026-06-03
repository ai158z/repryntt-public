"""
repryntt.trading.defi_data — DexScreener and Solana RPC tools.

Extracted from SAIGE/brain/brain_system.py (Phase 2c migration).
Provides: dexscreener_trending, dexscreener_token_search, solana_rpc_query
"""

import json
import logging
import os
import time
from typing import Any, Dict

import requests

logger = logging.getLogger(__name__)

# ─── Alchemy RPC rate limiting ───────────────────────────────────────────
_alchemy_call_times: list = []
_ALCHEMY_RATE_LIMIT = 300       # max calls per hour
_ALCHEMY_RATE_WINDOW = 3600     # 1 hour in seconds
_ALCHEMY_RPC_URL = os.environ.get(
    "SOLANA_RPC_URL",
    "https://solana-mainnet.g.alchemy.com/v2/tRxgtGxhjC6y_yaW1W8phMk0yQTBUg73",
)

_HEADERS = {"Accept": "application/json", "User-Agent": "SAIGE/1.0"}


def _alchemy_rate_check() -> bool:
    """Return True if we're within rate limit, pruning old entries."""
    now = time.time()
    cutoff = now - _ALCHEMY_RATE_WINDOW
    _alchemy_call_times[:] = [t for t in _alchemy_call_times if t > cutoff]
    if len(_alchemy_call_times) >= _ALCHEMY_RATE_LIMIT:
        return False
    _alchemy_call_times.append(now)
    return True


# ─── DexScreener ─────────────────────────────────────────────────────────

def dexscreener_trending(chain: str = "solana", limit: int = 20, **kwargs) -> str:
    """Get trending / most-boosted tokens from DexScreener.

    Parameters:
        chain: Blockchain to filter (solana, ethereum, bsc, base, etc.). Use 'all' for all chains.
        limit: Max tokens to return (default 20, max 100).
    """
    try:
        limit = min(int(limit), 100)
        resp = requests.get(
            "https://api.dexscreener.com/token-boosts/top/v1",
            timeout=15,
            headers=_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()

        tokens = data if isinstance(data, list) else data.get("tokens", data.get("data", []))

        chain_lower = chain.lower()
        if chain_lower != "all":
            tokens = [t for t in tokens if t.get("chainId", "").lower() == chain_lower]

        tokens = tokens[:limit]

        results = []
        for t in tokens:
            results.append({
                "chain": t.get("chainId", "?"),
                "tokenAddress": t.get("tokenAddress", ""),
                "description": t.get("description", ""),
                "url": t.get("url", ""),
                "icon": t.get("icon", ""),
                "totalAmount": t.get("totalAmount", 0),
                "amount": t.get("amount", 0),
            })

        # Enrich with pair data (best-effort)
        if results:
            addresses = [t["tokenAddress"] for t in results[:30] if t["tokenAddress"]]
            if addresses:
                try:
                    pair_resp = requests.get(
                        f"https://api.dexscreener.com/tokens/v1/{chain_lower}/{','.join(addresses[:30])}",
                        timeout=15,
                        headers=_HEADERS,
                    )
                    if pair_resp.status_code == 200:
                        pairs = pair_resp.json()
                        pair_list = pairs if isinstance(pairs, list) else pairs.get("pairs", [])
                        addr_map = {}
                        for p in pair_list:
                            ba = p.get("baseToken", {}).get("address", "")
                            if ba and ba not in addr_map:
                                addr_map[ba] = p

                        for r in results:
                            p = addr_map.get(r["tokenAddress"])
                            if p:
                                base = p.get("baseToken", {})
                                r["name"] = base.get("name", "")
                                r["symbol"] = base.get("symbol", "")
                                r["priceUsd"] = p.get("priceUsd", "")
                                r["priceChange_24h"] = p.get("priceChange", {}).get("h24", "")
                                r["volume_24h"] = p.get("volume", {}).get("h24", "")
                                r["liquidity"] = p.get("liquidity", {}).get("usd", "")
                                r["marketCap"] = p.get("marketCap", "")
                                r["pairAddress"] = p.get("pairAddress", "")
                                r["dexId"] = p.get("dexId", "")
                except Exception:
                    pass  # Pair enrichment is best-effort

        summary = f"DexScreener trending tokens on {chain} ({len(results)} results):\n"
        for i, r in enumerate(results, 1):
            name = r.get("name") or r.get("description", "Unknown")[:30]
            symbol = r.get("symbol", "")
            price = r.get("priceUsd", "?")
            change = r.get("priceChange_24h", "?")
            vol = r.get("volume_24h", "?")
            mcap = r.get("marketCap", "?")
            addr = r.get("tokenAddress", "")
            summary += (f"\n{i}. {name} ({symbol}) — ${price}"
                        f"  24h: {change}%  Vol: ${vol}  MCap: ${mcap}"
                        f"  [{r.get('chain', '')}] Address: {addr}")

        return summary

    except Exception as e:
        return json.dumps({"error": f"DexScreener API error: {str(e)}"})


def dexscreener_token_search(query: str = "", **kwargs) -> str:
    """Search for a specific token on DexScreener by name, symbol, or address.

    Parameters:
        query: Token name, symbol (e.g. 'BONK'), or contract address.
    """
    if not query:
        return json.dumps({"error": "query parameter is required"})
    try:
        resp = requests.get(
            f"https://api.dexscreener.com/latest/dex/search/?q={query}",
            timeout=15,
            headers=_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs", [])[:10]

        if not pairs:
            return f"No results found for '{query}' on DexScreener."

        results = []
        for p in pairs:
            base = p.get("baseToken", {})
            results.append({
                "name": base.get("name", ""),
                "symbol": base.get("symbol", ""),
                "tokenAddress": base.get("address", ""),
                "chain": p.get("chainId", ""),
                "dex": p.get("dexId", ""),
                "priceUsd": p.get("priceUsd", ""),
                "priceChange_24h": p.get("priceChange", {}).get("h24", ""),
                "volume_24h": p.get("volume", {}).get("h24", ""),
                "liquidity": p.get("liquidity", {}).get("usd", ""),
                "marketCap": p.get("marketCap", ""),
                "pairAddress": p.get("pairAddress", ""),
                "url": p.get("url", ""),
            })

        summary = f"DexScreener results for '{query}' ({len(results)} pairs):\n"
        for i, r in enumerate(results, 1):
            summary += (f"\n{i}. {r['name']} ({r['symbol']}) on {r['chain']}/{r['dex']}"
                        f" — ${r['priceUsd']}  24h: {r['priceChange_24h']}%"
                        f"  Vol: ${r['volume_24h']}  Liq: ${r['liquidity']}"
                        f"  MCap: ${r['marketCap']}"
                        f"  Address: {r['tokenAddress']}")

        return summary

    except Exception as e:
        return json.dumps({"error": f"DexScreener search error: {str(e)}"})


def solana_rpc_query(method: str = "", params: str = "[]", **kwargs) -> str:
    """Query Solana blockchain via Alchemy RPC (300 calls/hr).

    Parameters:
        method: Solana JSON-RPC method. Use one of:
            getTokenLargestAccounts — top 20 holders of a token
            getTokenSupply — total/circulating supply
            getAccountInfo — raw account data
            getBalance — SOL balance of a wallet
            getSignaturesForAddress — recent transactions
            getTransaction — full details of one transaction
            getLatestBlockhash — current blockhash
            getSlot — current slot number
        params: JSON array of positional RPC arguments. Examples:
            getTokenLargestAccounts: '["<MINT_ADDRESS>"]'
            getTokenSupply: '["<MINT_ADDRESS>"]'
            getAccountInfo: '["<ADDRESS>", {"encoding": "jsonParsed"}]'
            getBalance: '["<WALLET_ADDRESS>"]'
    """
    if not method:
        return json.dumps({"error": "method parameter is required"})

    if not _alchemy_rate_check():
        return json.dumps({
            "error": "Alchemy rate limit reached (300/hr). Try again later.",
            "calls_this_hour": len(_alchemy_call_times),
            "limit": _ALCHEMY_RATE_LIMIT,
        })

    # Parse params
    try:
        if isinstance(params, str):
            parsed_params = json.loads(params)
        elif isinstance(params, list):
            parsed_params = params
        else:
            parsed_params = [params]
    except json.JSONDecodeError:
        parsed_params = [params]

    payload = {
        "id": 1,
        "jsonrpc": "2.0",
        "method": method,
        "params": parsed_params,
    }

    try:
        resp = requests.post(
            _ALCHEMY_RPC_URL,
            json=payload,
            timeout=20,
            headers={"accept": "application/json", "content-type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()

        calls_remaining = _ALCHEMY_RATE_LIMIT - len(_alchemy_call_times)

        if "error" in data:
            rpc_err = data["error"]
            err_msg = rpc_err.get("message", "") if isinstance(rpc_err, dict) else str(rpc_err)
            hint = ""
            if "Invalid param" in err_msg or "WrongSize" in err_msg:
                hint = ("HINT: params must be a JSON array with the correct arguments. "
                        "Example for getTokenSupply: params='[\"<MINT_ADDRESS>\"]'. "
                        "Make sure the mint address is a valid base58 Solana address (32-44 chars). "
                        "You sent: method=" + method + " params=" + repr(parsed_params)[:200])
            return json.dumps({
                "success": False,
                "rpc_error": rpc_err,
                "hint": hint,
                "alchemy_calls_remaining": calls_remaining,
            })

        result = data.get("result", {})

        # Human-readable summary for common methods
        summary = ""
        if method == "getBalance":
            lamports = result.get("value", 0) if isinstance(result, dict) else result
            sol = lamports / 1_000_000_000 if isinstance(lamports, (int, float)) else 0
            summary = f"Balance: {sol:.6f} SOL ({lamports} lamports)"
        elif method == "getTokenSupply":
            info = result.get("value", {}) if isinstance(result, dict) else {}
            amount = info.get("uiAmountString", info.get("amount", "?"))
            decimals = info.get("decimals", "?")
            summary = f"Token supply: {amount} (decimals: {decimals})"
        elif method == "getTokenLargestAccounts":
            accounts = result.get("value", []) if isinstance(result, dict) else []
            # Import known AMM programs to label LP accounts
            try:
                from repryntt.trading.token_monitor import KNOWN_AMM_PROGRAMS, EXCLUDE_FROM_HOLDERS, _amm_label
                _has_amm_set = True
            except ImportError:
                _has_amm_set = False
            summary = f"Top {len(accounts)} holders:\n"
            for i, acc in enumerate(accounts[:10], 1):
                addr = acc.get("address", "?")
                amount = acc.get("uiAmountString", acc.get("amount", "?"))
                label = ""
                if _has_amm_set and addr in EXCLUDE_FROM_HOLDERS:
                    label = " [EXCLUDED: burn/system]"
                summary += f"  {i}. {addr[:12]}... — {amount}{label}\n"
            if _has_amm_set:
                summary += (
                    "\n⚠️ NOTE: These are raw token accounts. The top account is often the "
                    "PumpSwap/Raydium LIQUIDITY POOL vault, NOT a human holder. "
                    "Use trading_token_detail(address) for pre-filtered holder concentration "
                    "that excludes LP pools automatically."
                )
        elif method == "getSignaturesForAddress":
            sigs = result if isinstance(result, list) else []
            summary = f"{len(sigs)} recent transactions found"
            for i, sig in enumerate(sigs[:5], 1):
                summary += f"\n  {i}. {sig.get('signature', '?')[:20]}... slot={sig.get('slot', '?')} err={sig.get('err')}"

        response: Dict[str, Any] = {
            "success": True,
            "method": method,
            "result": result,
            "alchemy_calls_remaining": calls_remaining,
        }
        if summary:
            response["summary"] = summary

        result_str = json.dumps(response)
        if len(result_str) > 8000:
            response["result"] = "[truncated — use more specific params or filter results]"
            if summary:
                response["note"] = "Full result was too large. Summary above has the key data."
            result_str = json.dumps(response)

        return result_str

    except Exception as e:
        return json.dumps({"error": f"Alchemy RPC error: {str(e)}", "method": method})
