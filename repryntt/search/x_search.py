"""
repryntt.search.x_search — Token & crypto web search tools.

Originally X/Twitter API search, now uses web search (DuckDuckGo) to avoid
exhausting X API rate limits. Searches for token addresses, names, and symbols
across the open web — Google/DuckDuckGo results include Pump.fun, GMGN, X posts,
Telegram, and other crypto community sources which give a good picture of what
a token is about.

Also fetches token description from DexScreener token-profiles API when available.

Provides: x_search_tweets, x_search_crypto
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

import requests

logger = logging.getLogger(__name__)


def _ddg_web_search(query: str, max_results: int = 15) -> list:
    """Run a DuckDuckGo web search and return raw results."""
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        raw = DDGS().text(query, max_results=min(max_results, 30))
        return list(raw) if raw else []
    except Exception as e:
        logger.error(f"DuckDuckGo search failed for '{query}': {e}")
        return []


def _fetch_dexscreener_token_info(token_address: str) -> Dict[str, Any]:
    """Fetch token info from DexScreener search API.
    Returns dict with name, symbol, description, websites, socials, price data.
    Uses the search endpoint which is the most reliable source of token metadata."""
    if not token_address or len(token_address) < 20:
        return {}
    try:
        resp = requests.get(
            f"https://api.dexscreener.com/latest/dex/search/?q={token_address}",
            timeout=10,
            headers={"Accept": "application/json", "User-Agent": "SAIGE/1.0"}
        )
        if resp.status_code == 200:
            pairs = resp.json().get("pairs", [])
            if pairs:
                pair = pairs[0]
                base = pair.get("baseToken", {})
                info = pair.get("info", {})
                return {
                    "name": base.get("name", ""),
                    "symbol": base.get("symbol", ""),
                    "description": info.get("description", ""),
                    "websites": info.get("websites", []),
                    "socials": info.get("socials", []),
                    "priceUsd": pair.get("priceUsd", ""),
                    "marketCap": pair.get("marketCap", ""),
                    "liquidity": pair.get("liquidity", {}).get("usd", ""),
                }
    except Exception as e:
        logger.debug(f"DexScreener fetch failed for {token_address}: {e}")
    return {}


def _analyze_social_presence(results: list) -> Dict[str, Any]:
    """Analyze web search results for social media presence and narrative clues.
    Returns social presence metrics and narrative hints."""
    x_posts = []
    pump_fun_links = []
    other_social = []
    narrative_snippets = []

    for r in results:
        url = r.get("href", "") or r.get("url", "")
        title = r.get("title", "")
        body = r.get("body", "") or r.get("text", "")
        url_lower = url.lower()

        if "x.com/" in url_lower or "twitter.com/" in url_lower:
            x_posts.append({"url": url, "title": title, "snippet": body})
        elif "pump.fun" in url_lower:
            pump_fun_links.append({"url": url, "title": title})
        elif any(s in url_lower for s in ["telegram", "t.me/", "discord", "reddit"]):
            other_social.append({"url": url, "platform": url_lower.split("/")[2] if "/" in url_lower else "unknown"})

        # Collect narrative-relevant snippets (non-exchange results)
        if body and not any(ex in url_lower for ex in ["solscan.io", "birdeye.so", "bitmart", "phantom.com", "opensea.io", "coingecko", "coinmarketcap"]):
            narrative_snippets.append(body[:300])

    return {
        "has_x_posts": len(x_posts) > 0,
        "x_post_count": len(x_posts),
        "x_posts": x_posts[:5],  # Top 5 X posts found
        "has_pump_fun": len(pump_fun_links) > 0,
        "pump_fun_links": pump_fun_links[:3],
        "other_social": other_social[:5],
        "social_presence": len(x_posts) > 0 or len(other_social) > 0,
        "narrative_snippets": narrative_snippets[:3],
    }


def x_search_tweets(query: str = "", max_results: int = 20,
                    sort_order: str = "relevancy", **kwargs) -> str:
    """Search the web for crypto/token information. Uses web search instead of
    X API to avoid rate limit exhaustion. Returns web results from across the
    internet including X/Twitter posts, Pump.fun, GMGN, DexScreener, and more.

    Parameters:
        query: Search query (token address, name, symbol, or any crypto topic)
        max_results: Number of results to return (default 20)
        sort_order: Ignored (kept for backward compatibility)
    """
    if not query:
        return json.dumps({"error": "query parameter is required"})

    try:
        max_results = max(5, min(30, int(max_results)))

        # Run web search via DuckDuckGo
        raw_results = _ddg_web_search(query, max_results)

        if not raw_results:
            return json.dumps({
                "success": False,
                "error": f"No web results found for: {query}",
                "query": query,
                "NEXT_STEP": "Try a different search query — search by token NAME instead of address. Use real_web_search() or scrape_web_page() for deeper info."
            })

        results = []
        for r in raw_results:
            results.append({
                "text": r.get("body", ""),
                "url": r.get("href", ""),
                "title": r.get("title", ""),
                "source": "web_search",
            })

        # Analyze social presence in results
        social_analysis = _analyze_social_presence(raw_results)

        # Check if query looks like a token address — if so, fetch DexScreener description
        token_info = {}
        query_stripped = query.strip()
        if len(query_stripped) >= 32 and query_stripped.isalnum():
            token_info = _fetch_dexscreener_token_info(query_stripped)

        response = {
            "success": True,
            "query": query,
            "result_count": len(results),
            "results": results,
            "social_analysis": social_analysis,
            "source": "web_search_ddg",
            "note": "Results via web search (DuckDuckGo). Includes X/Twitter posts, Pump.fun, GMGN, and other crypto sources found on the open web.",
            "IMPORTANT": (
                "SOCIAL PRESENCE IS NOT JUST ABOUT QUANTITY — it's about NARRATIVE. "
                "A memecoin's value comes from its STORY and the ATTENTION it captures. "
                "You MUST scrape_web_page() on at least 1-2 X/Twitter URLs found above to "
                "understand WHAT the token is about. The story/narrative is the #1 driver."
            ),
        }

        if token_info:
            response["token_profile"] = token_info

        return json.dumps(response)

    except Exception as e:
        logger.error(f"Web search failed: {e}")
        return json.dumps({"success": False, "error": str(e)})


def x_search_crypto(token_name: str = "", token_symbol: str = "",
                    token_address: str = "", max_results: int = 20,
                    **kwargs) -> str:
    """Search the web for crypto token information, sentiment, and community buzz.
    Performs MULTIPLE searches to build a complete picture of the token's narrative,
    social presence, and community. Also fetches DexScreener metadata for name,
    description, websites, and social links.

    IMPORTANT: This tool now returns a narrative_summary section that tells you
    what the token is ABOUT. For memecoins, the STORY/NARRATIVE is what drives
    attention and price. A viral story = viral token.

    Parameters:
        token_name: Token name (e.g. 'Golden House', 'World Peace')
        token_symbol: Token ticker/symbol (e.g. 'SOL', 'PATTY') — optional
        token_address: Solana token address / contract address — optional but recommended
        max_results: Number of results (default 20)
    """
    all_results = []
    token_info = {}
    searches_performed = []

    # ── Step 1: Fetch DexScreener metadata FIRST to get token name/symbol ──
    if token_address and len(token_address.strip()) >= 20:
        token_info = _fetch_dexscreener_token_info(token_address.strip())
        if token_info:
            # Use DexScreener name/symbol if caller didn't provide them
            if not token_name and token_info.get("name"):
                token_name = token_info["name"]
            if not token_symbol and token_info.get("symbol"):
                token_symbol = token_info["symbol"]

    # ── Step 2: Primary search — by token address (most unambiguous) ──
    if token_address and len(token_address.strip()) >= 20:
        query1 = token_address.strip()
        searches_performed.append(f"address: {query1[:20]}...")
        raw1 = _ddg_web_search(query1, max_results)
        all_results.extend(raw1)

    # ── Step 3: Secondary search — by token NAME to find narrative context ──
    if token_name and len(token_name.strip()) >= 2:
        # Search for the token name with crypto context to find the STORY
        name_query = f'"{token_name}" crypto solana'
        if token_symbol and token_symbol.lower() != token_name.lower():
            name_query = f'"{token_name}" "${token_symbol}" crypto solana'
        searches_performed.append(f"name: {name_query}")
        raw2 = _ddg_web_search(name_query, 10)
        # Deduplicate by URL
        seen_urls = {(r.get("href", "") or "").lower() for r in all_results}
        for r in raw2:
            url = (r.get("href", "") or "").lower()
            if url and url not in seen_urls:
                all_results.append(r)
                seen_urls.add(url)
    elif token_symbol:
        sym_query = f'"${token_symbol}" crypto solana memecoin'
        searches_performed.append(f"symbol: {sym_query}")
        raw2 = _ddg_web_search(sym_query, 10)
        seen_urls = {(r.get("href", "") or "").lower() for r in all_results}
        for r in raw2:
            url = (r.get("href", "") or "").lower()
            if url and url not in seen_urls:
                all_results.append(r)
                seen_urls.add(url)

    if not all_results and not token_info:
        return json.dumps({
            "error": "No results found. Provide token_name, token_symbol, or token_address.",
            "searches": searches_performed,
        })

    # ── Step 4: Analyze social presence across ALL results ──
    social_analysis = _analyze_social_presence(all_results)

    # ── Step 5: Build narrative summary ──
    narrative_parts = []
    if token_name:
        narrative_parts.append(f"Token Name: {token_name}")
    if token_symbol:
        narrative_parts.append(f"Symbol: ${token_symbol}")
    if token_info.get("description"):
        narrative_parts.append(f"Description: {token_info['description']}")
    if token_info.get("websites"):
        sites = [w.get("url", "") for w in token_info["websites"] if w.get("url")]
        if sites:
            narrative_parts.append(f"Official websites: {', '.join(sites)}")
    if token_info.get("socials"):
        socials = [f"{s.get('type','')}: {s.get('url','')}" for s in token_info["socials"] if s.get("url")]
        if socials:
            narrative_parts.append(f"Social links: {', '.join(socials)}")
    if social_analysis["has_x_posts"]:
        narrative_parts.append(f"X/Twitter posts found: {social_analysis['x_post_count']}")
        for xp in social_analysis["x_posts"][:3]:
            narrative_parts.append(f"  • {xp['title']}: {xp['snippet'][:150]}")
    if social_analysis["narrative_snippets"]:
        narrative_parts.append("Narrative clues from search results:")
        for snip in social_analysis["narrative_snippets"][:3]:
            narrative_parts.append(f"  • {snip[:200]}")

    # Format results for output
    formatted_results = []
    for r in all_results:
        formatted_results.append({
            "text": r.get("body", ""),
            "url": r.get("href", ""),
            "title": r.get("title", ""),
            "source": "web_search",
        })

    response = {
        "success": True,
        "searches_performed": searches_performed,
        "result_count": len(formatted_results),
        "results": formatted_results,
        "social_analysis": social_analysis,
        "narrative_summary": "\n".join(narrative_parts) if narrative_parts else "No narrative data found — use scrape_web_page() on search result URLs to discover the story.",
        "source": "multi_search_ddg_dexscreener",
    }

    if token_info:
        response["token_profile"] = token_info

    # ── Step 6: Action instructions ──
    next_steps = []
    if social_analysis["x_posts"]:
        next_steps.append(
            f"SCRAPE these X posts to understand the narrative: "
            + ", ".join(xp["url"] for xp in social_analysis["x_posts"][:2])
        )
    if token_info.get("websites"):
        sites = [w["url"] for w in token_info["websites"] if w.get("url")]
        if sites:
            next_steps.append(f"SCRAPE the project website for the full story: {sites[0]}")
    if not social_analysis["social_presence"] and not token_info.get("websites"):
        next_steps.append("No social presence found in search results. Try searching by token NAME if you only searched by address.")

    response["CRITICAL_NEXT_STEPS"] = (
        "For memecoins, the NARRATIVE/STORY is what drives attention and price. "
        "A token based on a viral news story, popular meme, or cultural moment can 10x. "
        "You MUST scrape_web_page() on X posts and website links above to understand "
        "WHAT this token is about before deciding. "
        + " | ".join(next_steps) if next_steps else
        "Use scrape_web_page() on the most relevant URLs to understand the token narrative."
    )

    return json.dumps(response)
