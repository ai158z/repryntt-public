"""
DexScreener Token Discovery — polls DexScreener public API endpoints to
discover new Solana tokens WITHOUT requiring Alchemy RPC calls.

Endpoints used:
  /token-profiles/latest/v1       — new token profiles (60 req/min)
  /orders/v1/solana/{addr}        — paid order check  (60 req/min)
  /community-takeovers/latest/v1  — community takeovers (60 req/min)

Discovered tokens are written to the watch_dir as JSON files that
ai72_andahalf.py's scan_watch_dir picks up automatically.
"""

import asyncio
import aiohttp
import json
import os
import time
import logging
from datetime import datetime, timezone

# ── Paths ──────────────────────────────────────────────────────────────────
WATCH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "watch_dir")
os.makedirs(WATCH_DIR, exist_ok=True)

# ── DexScreener endpoints ─────────────────────────────────────────────────
TOKEN_PROFILES_URL   = "https://api.dexscreener.com/token-profiles/latest/v1"
COMMUNITY_CTO_URL    = "https://api.dexscreener.com/community-takeovers/latest/v1"
TOKEN_BOOSTS_URL     = "https://api.dexscreener.com/token-boosts/top/v1"
ORDERS_URL           = "https://api.dexscreener.com/orders/v1/solana/{tokenAddress}"
TOKEN_BATCH_URL      = "https://api.dexscreener.com/tokens/v1/solana/{addresses}"

# ── Config ─────────────────────────────────────────────────────────────────
POLL_INTERVAL        = 60     # seconds between discovery polls
ORDERS_BATCH_DELAY   = 0.5   # delay between individual orders checks
MAX_ORDERS_PER_CYCLE = 10    # max new tokens to check orders for per cycle


async def poll_token_profiles(session, seen_tokens, logger):
    """
    Poll /token-profiles/latest/v1 for new Solana tokens.
    Returns list of new token addresses found this cycle.
    """
    new_addresses = []
    try:
        async with session.get(TOKEN_PROFILES_URL) as resp:
            if resp.status == 200:
                profiles = await resp.json()
                if not isinstance(profiles, list):
                    profiles = [profiles]
                for profile in profiles:
                    chain = profile.get("chainId", "")
                    addr = profile.get("tokenAddress", "")
                    if chain == "solana" and addr and addr not in seen_tokens:
                        seen_tokens.add(addr)
                        new_addresses.append(addr)
                if new_addresses:
                    logger.info(f"[discovery] Token profiles: found {len(new_addresses)} new Solana token(s)")
                else:
                    logger.debug("[discovery] Token profiles: no new Solana tokens")
            elif resp.status == 429:
                logger.warning("[discovery] Token profiles rate-limited, will retry next cycle")
            else:
                logger.debug(f"[discovery] Token profiles returned HTTP {resp.status}")
    except Exception as e:
        logger.error(f"[discovery] Token profiles error: {e}")
    return new_addresses


async def poll_community_takeovers(session, seen_tokens, logger):
    """
    Poll /community-takeovers/latest/v1 for Solana community takeover tokens.
    Returns list of new token addresses plus CTO metadata.
    """
    cto_tokens = []
    try:
        async with session.get(COMMUNITY_CTO_URL) as resp:
            if resp.status == 200:
                takeovers = await resp.json()
                if not isinstance(takeovers, list):
                    takeovers = [takeovers]
                for cto in takeovers:
                    chain = cto.get("chainId", "")
                    addr = cto.get("tokenAddress", "")
                    if chain == "solana" and addr and addr not in seen_tokens:
                        seen_tokens.add(addr)
                        cto_tokens.append({
                            "address": addr,
                            "claim_date": cto.get("claimDate", ""),
                            "description": cto.get("description", ""),
                            "source": "community_takeover"
                        })
                if cto_tokens:
                    logger.info(f"[discovery] Community takeovers: found {len(cto_tokens)} new Solana CTO token(s)")
            elif resp.status == 429:
                logger.warning("[discovery] Community takeovers rate-limited")
            else:
                logger.debug(f"[discovery] Community takeovers returned HTTP {resp.status}")
    except Exception as e:
        logger.error(f"[discovery] Community takeovers error: {e}")
    return cto_tokens


async def poll_trending_tokens(session, seen_tokens, logger):
    """
    Poll /token-boosts/top/v1 for trending/boosted Solana tokens.
    These are tokens with active paid promotions — teams spending real money.
    Returns list of new token addresses found this cycle.
    """
    new_addresses = []
    try:
        async with session.get(TOKEN_BOOSTS_URL) as resp:
            if resp.status == 200:
                data = await resp.json()
                tokens = data if isinstance(data, list) else data.get("tokens", data.get("data", []))
                for token in tokens:
                    chain = token.get("chainId", "")
                    addr = token.get("tokenAddress", "")
                    if chain == "solana" and addr and addr not in seen_tokens:
                        seen_tokens.add(addr)
                        new_addresses.append(addr)
                if new_addresses:
                    logger.info(f"[discovery] Trending/boosted: found {len(new_addresses)} new Solana token(s)")
                else:
                    logger.debug("[discovery] Trending/boosted: no new Solana tokens")
            elif resp.status == 429:
                logger.warning("[discovery] Trending/boosted rate-limited, will retry next cycle")
            else:
                logger.debug(f"[discovery] Trending/boosted returned HTTP {resp.status}")
    except Exception as e:
        logger.error(f"[discovery] Trending/boosted error: {e}")
    return new_addresses


async def check_token_orders(session, address, logger):
    """
    Check /orders/v1/solana/{addr} for paid DexScreener orders.
    Returns dict with legitimacy signals or None.
    """
    url = ORDERS_URL.format(tokenAddress=address)
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                orders = await resp.json()
                if not isinstance(orders, list):
                    orders = [orders] if orders else []
                has_profile = any(
                    o.get("type") == "tokenProfile" and o.get("status") == "approved"
                    for o in orders
                )
                has_ad = any(
                    o.get("type") in ("tokenAd", "trendingBarAd") and o.get("status") == "approved"
                    for o in orders
                )
                return {
                    "has_paid_profile": has_profile,
                    "has_paid_ad": has_ad,
                    "order_count": len(orders),
                    "orders": orders
                }
            elif resp.status == 429:
                logger.warning(f"[discovery] Orders rate-limited for {address[:8]}...")
            else:
                logger.debug(f"[discovery] Orders returned HTTP {resp.status} for {address[:8]}...")
    except Exception as e:
        logger.error(f"[discovery] Orders check error for {address[:8]}: {e}")
    return None


async def fetch_batch_metadata(session, addresses, logger):
    """
    Fetch metadata for up to 30 tokens in a single call using
    /tokens/v1/solana/{addr1},{addr2},...
    Returns list of enriched token dicts ready for the watch_dir.
    """
    if not addresses:
        return []

    # Batch in groups of 30 (API limit)
    enriched = []
    for i in range(0, len(addresses), 30):
        batch = addresses[i:i+30]
        joined = ",".join(batch)
        url = TOKEN_BATCH_URL.format(addresses=joined)
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    pools = await resp.json()
                    if not isinstance(pools, list):
                        pools = [pools] if pools else []
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
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "source": "dexscreener_discovery"
                        })
                    logger.info(f"[discovery] Batch metadata: {len(by_token)} tokens from {len(batch)} addresses")
                elif resp.status == 429:
                    logger.warning("[discovery] Batch metadata rate-limited, pausing 3s")
                    await asyncio.sleep(3)
                else:
                    logger.debug(f"[discovery] Batch metadata returned HTTP {resp.status}")
        except Exception as e:
            logger.error(f"[discovery] Batch metadata error: {e}")
        if i + 30 < len(addresses):
            await asyncio.sleep(0.5)
    return enriched


def write_to_watch_dir(enriched_tokens, orders_map, cto_map, logger):
    """
    Write discovered tokens to watch_dir as a JSON file that
    scan_watch_dir will pick up and process through the pipeline.
    """
    if not enriched_tokens:
        return

    # Attach orders/CTO metadata to each token
    for token in enriched_tokens:
        addr = token["token_address"]
        if addr in orders_map:
            token["orders_info"] = orders_map[addr]
        if addr in cto_map:
            token["community_takeover"] = cto_map[addr]

    filename = f"discovery_{int(time.time())}.json"
    filepath = os.path.join(WATCH_DIR, filename)
    try:
        with open(filepath, "w") as f:
            json.dump(enriched_tokens, f, indent=2)
        logger.info(f"[discovery] Wrote {len(enriched_tokens)} tokens to {filepath}")
    except Exception as e:
        logger.error(f"[discovery] Error writing {filepath}: {e}")


async def dexscreener_discovery_loop(logger):
    """
    Main discovery loop. Polls DexScreener endpoints every POLL_INTERVAL
    seconds to discover new Solana tokens and write them to watch_dir.
    seen_tokens resets every 3 hours so expired tokens can be re-discovered.
    """
    seen_tokens = set()
    last_seen_reset = time.time()
    SEEN_RESET_INTERVAL = 3 * 3600
    logger.info(f"[discovery] Starting DexScreener discovery loop (poll every {POLL_INTERVAL}s)")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                if time.time() - last_seen_reset >= SEEN_RESET_INTERVAL:
                    old_count = len(seen_tokens)
                    seen_tokens.clear()
                    last_seen_reset = time.time()
                    logger.info(f"[discovery] Reset seen_tokens cache ({old_count} entries cleared)")

                cycle_start = time.time()

                # 1. Poll token profiles for new tokens
                new_profile_addrs = await poll_token_profiles(session, seen_tokens, logger)

                # 2. Poll community takeovers
                cto_tokens = await poll_community_takeovers(session, seen_tokens, logger)
                cto_map = {t["address"]: t for t in cto_tokens}
                cto_addrs = [t["address"] for t in cto_tokens]

                # 3. Poll trending/boosted tokens (paid promotions)
                trending_addrs = await poll_trending_tokens(session, seen_tokens, logger)

                # Combine all new addresses
                all_new = list(set(new_profile_addrs + cto_addrs + trending_addrs))

                if not all_new:
                    elapsed = time.time() - cycle_start
                    wait = max(0, POLL_INTERVAL - elapsed)
                    logger.debug(f"[discovery] No new tokens, sleeping {wait:.0f}s")
                    await asyncio.sleep(wait)
                    continue

                # 4. Check orders (legitimacy) for up to MAX_ORDERS_PER_CYCLE tokens
                orders_map = {}
                for addr in all_new[:MAX_ORDERS_PER_CYCLE]:
                    orders_info = await check_token_orders(session, addr, logger)
                    if orders_info:
                        orders_map[addr] = orders_info
                        if orders_info["has_paid_profile"]:
                            logger.info(f"[discovery] {addr[:8]}... has PAID profile ✓")
                    await asyncio.sleep(ORDERS_BATCH_DELAY)

                # 5. Fetch batch metadata from DexScreener
                enriched = await fetch_batch_metadata(session, all_new, logger)

                # 6. Write to watch_dir for the monitor pipeline
                write_to_watch_dir(enriched, orders_map, cto_map, logger)

                elapsed = time.time() - cycle_start
                wait = max(0, POLL_INTERVAL - elapsed)
                logger.info(f"[discovery] Cycle done: {len(all_new)} new, {len(enriched)} with pools, sleeping {wait:.0f}s")
                await asyncio.sleep(wait)

            except Exception as e:
                logger.error(f"[discovery] Loop error: {e}", exc_info=True)
                await asyncio.sleep(POLL_INTERVAL)
