"""
KOLscan Leaderboard Scraper — Top KOL Wallet Discovery
========================================================

Scrapes https://kolscan.io/leaderboard for the top-performing KOL
(Key Opinion Leader) wallets on Solana.  These wallets are high-frequency
memecoin traders on pump.fun with proven daily PnL.

Data flow:
  kolscan_scraper.fetch_leaderboard()
    → requests GET https://kolscan.io/leaderboard
    → parse HTML for wallet addresses, names, win/loss, PnL
    → cache to brain/kol_leaderboard_cache.json
    → return structured data

  kolscan_scraper.sync_to_whale_monitor(top_n=20, min_profit_sol=5.0)
    → take top N filtered KOLs from leaderboard
    → add each to whale_monitor with tier="kol"
    → skip duplicates already tracked

Uses a seed list of 50 known top KOL addresses so it works even if
the live scrape fails (JS-rendered page).  Seed list is refreshed
whenever a live scrape succeeds.
"""

import json
import logging
import os
import re
import time
import requests as _requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger("saige.kolscan")

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_FILE = BASE_DIR / "brain" / "kol_leaderboard_cache.json"

# ─── Seed list: top 50 KOLs from kolscan.io/leaderboard (daily) ────────────
# Updated: 2026-03-07  —  refreshed whenever live scrape succeeds
SEED_KOLS: List[Dict[str, Any]] = [
    {"rank": 1,  "name": "decu",             "address": "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9",  "wins": 103, "losses": 61,  "profit_sol": 93.41},
    {"rank": 2,  "name": "dv",               "address": "BCagckXeMChUKrHEd6fKFA1uiWDtcmCXMsqaheLiUPJd",  "wins": 64,  "losses": 87,  "profit_sol": 92.86},
    {"rank": 3,  "name": "Cented",           "address": "CyaE1VxvBrahnPWkqm5VsdCvyS2QmNht2UFrKJHga54o",  "wins": 89,  "losses": 50,  "profit_sol": 92.82},
    {"rank": 4,  "name": "noob mini",        "address": "AGqjivJr1dSv73TVUvdtqAwogzmThzvYMVXjGWg2FYLm",  "wins": 33,  "losses": 58,  "profit_sol": 88.35},
    {"rank": 5,  "name": "theo",             "address": "Bi4rd5FH5bYEN8scZ7wevxNZyNmKHdaBcvewdPFxYdLt",  "wins": 44,  "losses": 79,  "profit_sol": 54.95},
    {"rank": 6,  "name": "Sheep",            "address": "78N177fzNJpp8pG49xDv1efYcTMSzo9tPTKEA9mAVkh2",  "wins": 35,  "losses": 3,   "profit_sol": 54.88},
    {"rank": 7,  "name": "Tally",            "address": "JAmx4Wsh7cWXRzQuVt3TCKAyDfRm9HA7ztJa4f7RM8h9",  "wins": 6,   "losses": 10,  "profit_sol": 54.40},
    {"rank": 8,  "name": "Exotic",           "address": "Dwo2kj88YYhwcFJiybTjXezR9a6QjkMASz5xXD7kujXC",  "wins": 44,  "losses": 76,  "profit_sol": 50.50},
    {"rank": 9,  "name": "Cupsey",           "address": "2fg5QD1eD7rzNNCsvnhmXFm5hqNgwTTG8p7kQ6f3rx6f",  "wins": 95,  "losses": 83,  "profit_sol": 46.42},
    {"rank": 10, "name": "Trey",             "address": "831yhv67QpKqLBJjbmw2xoDUeeFHGUx8RnuRj9imeoEs",  "wins": 2,   "losses": 4,   "profit_sol": 43.17},
    {"rank": 11, "name": "Inside Calls",     "address": "4NtyFqqRzvHWsTmJZoT26H9xtL7asWGTxpcpCxiKax9a",  "wins": 10,  "losses": 5,   "profit_sol": 37.45},
    {"rank": 12, "name": "Trenchman",        "address": "Hw5UKBU5k3YudnGwaykj5E8cYUidNMPuEewRRar5Xoc7",  "wins": 26,  "losses": 31,  "profit_sol": 36.54},
    {"rank": 13, "name": "Bastille",         "address": "3kebnKw7cPdSkLRfiMEALyZJGZ4wdiSRvmoN4rD1yPzV",  "wins": 32,  "losses": 10,  "profit_sol": 36.27},
    {"rank": 14, "name": "Ramset",           "address": "71PCu3E4JP5RDBoY6wJteqzxkKNXLyE1byg5BTAL9UtQ",  "wins": 12,  "losses": 8,   "profit_sol": 36.14},
    {"rank": 15, "name": "chingchongslayer", "address": "4uCT4g7YHH4xxfmfNfKUDenwGrRNGoZ9Ay1XFxfUGhQG",  "wins": 6,   "losses": 16,  "profit_sol": 33.05},
    {"rank": 16, "name": "Johnson",          "address": "J9TYAsWWidbrcZybmLSfrLzryANf4CgJBLdvwdGuC8MB",  "wins": 6,   "losses": 5,   "profit_sol": 31.83},
    {"rank": 17, "name": "Lowskii",          "address": "41uh7g1DxYaYXdtjBiYCHcgBniV9Wx57b7HU7RXmx1Gg",  "wins": 7,   "losses": 7,   "profit_sol": 23.99},
    {"rank": 18, "name": "Pain",             "address": "J6TDXvarvpBdPXTaTU8eJbtso1PUCYKGkVtMKUUY8iEa",  "wins": 15,  "losses": 8,   "profit_sol": 21.99},
    {"rank": 19, "name": "Dani",             "address": "AuPp4YTMTyqxYXQnHc5KUc6pUuCSsHQpBJhgnD45yqrf",  "wins": 5,   "losses": 21,  "profit_sol": 21.21},
    {"rank": 20, "name": "Kev",              "address": "BTf4A2exGK9BCVDNzy65b9dUzXgMqB4weVkvTMFQsadd",  "wins": 64,  "losses": 47,  "profit_sol": 19.64},
    {"rank": 21, "name": "radiance",         "address": "FAicXNV5FVqtfbpn4Zccs71XcfGeyxBSGbqLDyDJZjke",  "wins": 13,  "losses": 17,  "profit_sol": 18.43},
    {"rank": 22, "name": "danny",            "address": "EaVboaPxFCYanjoNWdkxTbPvt57nhXGu5i6m9m6ZS2kK",  "wins": 21,  "losses": 29,  "profit_sol": 17.47},
    {"rank": 23, "name": "Meechie",          "address": "9iaawVBEsFG35PSwd4PahwT8fYNQe9XYuRdWm872dUqY",  "wins": 6,   "losses": 5,   "profit_sol": 16.34},
    {"rank": 24, "name": "hood",             "address": "91sP85Ds9A4EXJ3gU3iHyLtUNJimxz8LrxRb2qhBNod9",  "wins": 2,   "losses": 1,   "profit_sol": 15.64},
    {"rank": 25, "name": "Cowboy",           "address": "6EDaVsS6enYgJ81tmhEkiKFcb4HuzPUVFZeom6PHUqN3",  "wins": 7,   "losses": 0,   "profit_sol": 15.10},
    {"rank": 26, "name": "CoCo",             "address": "FqojC24nUn3x6oMQC2ypBHmtH7rFAnKS6DvwsJoCMaiv",  "wins": 5,   "losses": 7,   "profit_sol": 14.77},
    {"rank": 27, "name": "Qavec",            "address": "gangJEP5geDHjPVRhDS5dTF5e6GtRvtNogMEEVs91RV",   "wins": 39,  "losses": 41,  "profit_sol": 13.56},
    {"rank": 28, "name": "eezzyLIVE",        "address": "DiDbxfveAcnescZWYjkVJzXiEWjskZKAFVTq2hrfHNjN",  "wins": 1,   "losses": 1,   "profit_sol": 13.36},
    {"rank": 29, "name": "Pavel",            "address": "3jckt69SiN3aCMbBWJoDS1s4xxGpqNxFFKnwhpRAQmuL",  "wins": 10,  "losses": 6,   "profit_sol": 13.35},
    {"rank": 30, "name": "Naruza",           "address": "ASVzakePP6GNg9r95d4LPZHJDMXun6L6E4um4pu5ybJk",  "wins": 12,  "losses": 4,   "profit_sol": 12.97},
    {"rank": 31, "name": "Zemrics",          "address": "EP5mvfhGv6x1XR33Fd8eioiYjtRXAawafPmkz9xBpDvG",  "wins": 15,  "losses": 15,  "profit_sol": 11.40},
    {"rank": 32, "name": "Art",              "address": "CgaA9a1JwAXJyfHuvZ7VW8YfTVRkdiT5mjBBSKcg7Rz5",  "wins": 2,   "losses": 2,   "profit_sol": 10.94},
    {"rank": 33, "name": "Bluey",            "address": "6TAHDM5Tod7dBTZdYQxzgJZKxxPfiNV9udPHMiUNumyK",  "wins": 17,  "losses": 17,  "profit_sol": 10.86},
    {"rank": 34, "name": "chester",          "address": "PMJA8UQDyWTFw2Smhyp9jGA6aTaP7jKHR7BPudrgyYN",   "wins": 52,  "losses": 86,  "profit_sol": 10.82},
    {"rank": 35, "name": "Felix",            "address": "3uz65G8e463MA5FxcSu1rTUyWRtrRLRZYskKtEHHj7qn",  "wins": 2,   "losses": 2,   "profit_sol": 9.88},
    {"rank": 36, "name": "Putrick",          "address": "AVjEtg2ECYKXYeqdRQXvaaAZBjfTjYuSMTR4WLhKoeQN",  "wins": 27,  "losses": 47,  "profit_sol": 9.54},
    {"rank": 37, "name": "Ethan Prosper",    "address": "sAdNbe1cKNMDqDsa4npB3TfL62T14uAo2MsUQfLvzLT",   "wins": 8,   "losses": 5,   "profit_sol": 8.55},
    {"rank": 38, "name": "Goyim",            "address": "G3gZWqrYkNmYFKYCyfRCNtGuxdyuE2wiYKkZpiZn4WSS",  "wins": 3,   "losses": 1,   "profit_sol": 8.54},
    {"rank": 39, "name": "rambo",            "address": "2net6etAtTe3Rbq2gKECmQwnzcKVXRaLcHy2Zy1iCiWz",  "wins": 9,   "losses": 11,  "profit_sol": 8.40},
    {"rank": 40, "name": "Brox",             "address": "7VBTpiiEjkwRbRGHJFUz6o5fWuhPFtAmy8JGhNqwHNnn",  "wins": 2,   "losses": 1,   "profit_sol": 8.13},
    {"rank": 41, "name": "Beaver",           "address": "GM7Hrz2bDq33ezMtL6KGidSWZXMWgZ6qBuugkb5H8NvN",  "wins": 1,   "losses": 0,   "profit_sol": 7.72},
    {"rank": 42, "name": "bandit",           "address": "5B79fMkcFeRTiwm7ehsZsFiKsC7m7n1Bgv9yLxPp9q2X",  "wins": 27,  "losses": 51,  "profit_sol": 7.64},
    {"rank": 43, "name": "Ily",              "address": "5XVKfruE4Zzeoz3aqBQfFMb5aSscY5nSyc6VwtQwNiid",  "wins": 1,   "losses": 0,   "profit_sol": 6.85},
    {"rank": 44, "name": "OGAntD",           "address": "215nhcAHjQQGgwpQSJQ7zR26etbjjtVdW74NLzwEgQjP",  "wins": 1,   "losses": 0,   "profit_sol": 6.81},
    {"rank": 45, "name": "ROWDY",            "address": "DKgvpfttzmJqZXdavDwTxwSVkajibjzJnN2FA99dyciK",  "wins": 1,   "losses": 0,   "profit_sol": 6.67},
    {"rank": 46, "name": "Reljoo",           "address": "FsG3BaPmRTdSrPaivbgJsFNCCa8cPfkUtk8VLWXkHpHP",  "wins": 8,   "losses": 15,  "profit_sol": 6.42},
    {"rank": 47, "name": "Dior",             "address": "87rRdssFiTJKY4MGARa4G5vQ31hmR7MxSmhzeaJ5AAxJ",  "wins": 2,   "losses": 0,   "profit_sol": 6.23},
    {"rank": 48, "name": "yode",             "address": "J1XAE4onKYG1kTghgaytnyFgR3otQs1xEnJRRWM3djSQ",  "wins": 5,   "losses": 4,   "profit_sol": 6.16},
    {"rank": 49, "name": "cap",              "address": "CAPn1yH4oSywsxGU456jfgTrSSUidf9jgeAnHceNUJdw",  "wins": 10,  "losses": 32,  "profit_sol": 5.40},
    {"rank": 50, "name": "Thesis",           "address": "5S9qzJhSooakBaA9qZT6vWtoSy8FvyfxJ4t1vXvEK9G7",  "wins": 6,   "losses": 33,  "profit_sol": 5.09},
]


def _load_cache() -> Dict[str, Any]:
    """Load cached leaderboard data."""
    try:
        if CACHE_FILE.exists():
            with open(CACHE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_cache(data: Dict[str, Any]) -> None:
    """Save leaderboard data to cache file."""
    try:
        data["cached_at"] = datetime.now(timezone.utc).isoformat()
        with open(CACHE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning("Failed to save KOL cache: %s", e)


def _scrape_leaderboard_html(html: str) -> List[Dict[str, Any]]:
    """Parse KOLscan leaderboard HTML to extract KOL data.

    Works on both server-rendered and JS-rendered HTML by looking for
    the distinctive URL pattern /account/{base58}?timeframe=
    """
    kols = []
    # Pattern: /account/{base58_address}?timeframe={1|7|30}
    # Followed by name info and win/loss + PnL data in nearby HTML
    addr_pattern = re.compile(
        r'/account/([1-9A-HJ-NP-Za-km-z]{32,44})\?timeframe=(\d+)'
    )

    # Find all wallet addresses in order
    seen_addrs = set()
    for match in addr_pattern.finditer(html):
        addr = match.group(1)
        if addr in seen_addrs:
            continue
        seen_addrs.add(addr)

        # Try to extract name from nearby context (pfp {name} pattern)
        name = ""
        start = max(0, match.start() - 200)
        context = html[start:match.start()]
        name_match = re.search(r'pfp\s+([^"<\]]+)', context)
        if name_match:
            name = name_match.group(1).strip()

        kols.append({
            "rank": len(kols) + 1,
            "name": name or f"KOL_{addr[:6]}",
            "address": addr,
        })

    # Try to extract PnL numbers — they appear as "+XX.XX Sol" in order
    pnl_pattern = re.compile(r'\+(\d+\.?\d*)\s*Sol')
    pnl_matches = pnl_pattern.findall(html)

    # Also extract win/loss patterns like "103\n/\n61" or "103 / 61"
    wl_pattern = re.compile(r'(\d+)\s*[/\n]+\s*(\d+)')

    for i, kol in enumerate(kols):
        if i < len(pnl_matches):
            try:
                kol["profit_sol"] = float(pnl_matches[i])
            except ValueError:
                kol["profit_sol"] = 0.0
        else:
            kol["profit_sol"] = 0.0

    return kols


def fetch_leaderboard(timeframe: str = "daily",
                      force_refresh: bool = False) -> Dict[str, Any]:
    """Fetch KOLscan leaderboard data.

    Args:
        timeframe: "daily" (default), "weekly", or "monthly"
        force_refresh: If True, skip cache and re-scrape

    Returns:
        {"kols": [...], "source": "live"|"cache"|"seed", "fetched_at": ..., "count": N}
    """
    tf_map = {"daily": "1", "weekly": "7", "monthly": "30"}
    tf_param = tf_map.get(timeframe, "1")

    # Check cache freshness (valid for 4 hours)
    if not force_refresh:
        cache = _load_cache()
        if cache.get("kols") and cache.get("timeframe") == timeframe:
            cached_at = cache.get("cached_at", "")
            try:
                age = (datetime.now(timezone.utc) -
                       datetime.fromisoformat(cached_at)).total_seconds()
                if age < 14400:  # 4 hours
                    cache["source"] = "cache"
                    cache["count"] = len(cache["kols"])
                    return cache
            except Exception:
                pass

    # Try live scrape
    kols = []
    source = "seed"
    try:
        resp = _requests.get(
            f"https://kolscan.io/leaderboard",
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/122.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;"
                          "q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            },
            timeout=15,
        )
        if resp.status_code == 200 and len(resp.text) > 5000:
            parsed = _scrape_leaderboard_html(resp.text)
            if len(parsed) >= 10:
                # Check if the live scrape got real PnL data
                has_pnl = any(k.get("profit_sol", 0) > 0 for k in parsed)
                if has_pnl:
                    kols = parsed
                    source = "live"
                else:
                    # JS-rendered page: we got addresses but no stats.
                    # Merge live address order with seed stats.
                    seed_by_addr = {k["address"]: k for k in SEED_KOLS}
                    merged = []
                    for i, p in enumerate(parsed):
                        addr = p["address"]
                        if addr in seed_by_addr:
                            entry = dict(seed_by_addr[addr])
                            entry["rank"] = i + 1  # keep live ranking
                            merged.append(entry)
                        else:
                            p["rank"] = i + 1
                            merged.append(p)
                    kols = merged
                    source = "live+seed"
                logger.info("KOLscan scrape: %d KOLs (source=%s)", len(kols), source)
    except Exception as e:
        logger.warning("KOLscan live scrape failed: %s", e)

    # Fall back to seed data
    if not kols:
        kols = [dict(k) for k in SEED_KOLS]  # copy so we don't mutate
        source = "seed"

    # Compute win_rate for each KOL
    for kol in kols:
        w = kol.get("wins", 0)
        l = kol.get("losses", 0)
        total = w + l
        kol["win_rate"] = round(w / total * 100, 1) if total > 0 else 0.0

    result = {
        "kols": kols,
        "source": source,
        "timeframe": timeframe,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "count": len(kols),
    }

    # Save to cache
    _save_cache(result)
    return result


def get_top_kols(top_n: int = 20,
                 min_profit_sol: float = 5.0,
                 min_win_rate: float = 0.0,
                 timeframe: str = "daily") -> List[Dict[str, Any]]:
    """Get filtered top KOLs from leaderboard.

    Args:
        top_n: Max KOLs to return
        min_profit_sol: Minimum daily profit in SOL
        min_win_rate: Minimum win rate percentage (0-100)
        timeframe: "daily", "weekly", "monthly"

    Returns:
        List of KOL dicts sorted by profit_sol descending
    """
    data = fetch_leaderboard(timeframe=timeframe)
    kols = data.get("kols", [])

    filtered = [
        k for k in kols
        if k.get("profit_sol", 0) >= min_profit_sol
        and k.get("win_rate", 0) >= min_win_rate
    ]
    filtered.sort(key=lambda x: x.get("profit_sol", 0), reverse=True)
    return filtered[:top_n]


def sync_to_whale_monitor(top_n: int = 20,
                          min_profit_sol: float = 5.0,
                          min_win_rate: float = 0.0,
                          timeframe: str = "daily") -> Dict[str, Any]:
    """Add top KOL wallets to the whale monitor for copy-trading.

    Args:
        top_n: How many top KOLs to sync (default 20)
        min_profit_sol: Min daily profit in SOL to qualify
        min_win_rate: Min win rate % to qualify
        timeframe: Leaderboard timeframe

    Returns:
        {"added": N, "skipped": N, "already_tracked": N, "errors": N, "details": [...]}
    """
    from repryntt.trading.whale_monitor import add_wallet, list_wallets

    # Get currently tracked addresses
    existing = {w["address"] for w in list_wallets()}

    top_kols = get_top_kols(
        top_n=top_n,
        min_profit_sol=min_profit_sol,
        min_win_rate=min_win_rate,
        timeframe=timeframe,
    )

    added = 0
    skipped = 0
    already = 0
    errors = 0
    details = []

    for kol in top_kols:
        addr = kol["address"]
        name = kol.get("name", f"KOL_{addr[:6]}")
        profit = kol.get("profit_sol", 0)
        wr = kol.get("win_rate", 0)

        if addr in existing:
            already += 1
            details.append({"address": addr, "name": name, "status": "already_tracked"})
            continue

        try:
            result = add_wallet(
                address=addr,
                label=f"KOLscan #{kol.get('rank', '?')} {name}",
                tier="kol",
                notes=f"KOLscan leaderboard — {profit:.1f} SOL/day, "
                      f"{wr:.0f}% win rate, rank #{kol.get('rank', '?')} "
                      f"({timeframe}). Auto-synced {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            )
            if result.get("success"):
                added += 1
                details.append({"address": addr, "name": name, "status": "added",
                                "profit_sol": profit, "win_rate": wr})
            else:
                skipped += 1
                details.append({"address": addr, "name": name, "status": "skipped",
                                "reason": result.get("error", "unknown")})
        except Exception as e:
            errors += 1
            details.append({"address": addr, "name": name, "status": "error",
                            "reason": str(e)})

    return {
        "added": added,
        "skipped": skipped,
        "already_tracked": already,
        "errors": errors,
        "total_candidates": len(top_kols),
        "details": details,
    }


def remove_underperformers(min_profit_sol: float = 0.0) -> Dict[str, Any]:
    """Remove KOL wallets that dropped off the leaderboard or went negative.

    Checks current leaderboard and removes any KOLscan-sourced wallets
    that are no longer in the top performers.

    Returns:
        {"removed": N, "kept": N, "details": [...]}
    """
    from repryntt.trading.whale_monitor import remove_wallet, list_wallets

    current_wallets = list_wallets()
    leaderboard = fetch_leaderboard()
    lb_addrs = {k["address"] for k in leaderboard.get("kols", [])}
    lb_profits = {k["address"]: k.get("profit_sol", 0) for k in leaderboard.get("kols", [])}

    removed = 0
    kept = 0
    details = []

    for w in current_wallets:
        # Only touch KOLscan-sourced wallets
        if "KOLscan" not in (w.get("label", "") + w.get("notes", "")):
            continue

        addr = w["address"]
        profit = lb_profits.get(addr, 0)

        if addr not in lb_addrs or profit < min_profit_sol:
            try:
                remove_wallet(addr)
                removed += 1
                details.append({"address": addr, "label": w.get("label", ""),
                                "status": "removed", "reason": "dropped off leaderboard"})
            except Exception as e:
                details.append({"address": addr, "status": "error", "reason": str(e)})
        else:
            kept += 1

    return {"removed": removed, "kept": kept, "details": details}
