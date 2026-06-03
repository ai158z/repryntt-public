#!/usr/bin/env python3
"""
SAIGE Trading Hook Parsers — Convert trading events into HookMessages.

These parsers handle:
- Trade signals (token pattern detected, scored above threshold)
- Whale/KOL wallet alerts (buy or sell detected)
- Trade executions (auto-exec, manual buy/sell, TP/SL)

Each fires through the HookRouter → agent dispatch OR direct notification.
"""

from __future__ import annotations
import logging
from typing import Any, Dict, Optional

from repryntt.comms.hooks.message import HookMessage

logger = logging.getLogger("hooks.trading_parsers")


def parse_trade_signal(payload: Dict[str, Any]) -> Optional[HookMessage]:
    """Parse a trade signal event (token scored above threshold).

    Expected payload:
        {"address": "...", "symbol": "...", "score": 8.5, "grade": "STRONG BUY",
         "reasoning": "...", "market_cap": 100000, "latest_price": 0.001,
         "signal_types": {"Momentum": 2, ...}, "risk_flags": [...]}
    """
    symbol = payload.get("symbol", payload.get("address", "?")[:12])
    score = payload.get("score", 0)
    grade = payload.get("grade", "")
    mcap = payload.get("market_cap", 0)
    price = payload.get("latest_price", 0)
    reasoning = payload.get("reasoning", "")

    body = (
        f"🚨 TRADE SIGNAL: {symbol}\n"
        f"Grade: {grade} (score: {score:.1f})\n"
        f"Price: ${price:.8f} | MCap: ${mcap:,.0f}\n"
        f"Signals: {payload.get('signal_types', {})}\n"
        f"Risk flags: {payload.get('risk_flags', [])}\n"
        f"Reasoning: {reasoning[:300]}"
    )

    return HookMessage(
        source="trade_signal",
        event=grade.lower().replace(" ", "_") if grade else "signal",
        sender="signal_scorer",
        subject=f"Trade Signal: {symbol} ({grade})",
        body=body,
        priority=2 if score >= 8 else 4,
        session_key=f"signal:{payload.get('address', '')}:{int(score)}",
        reply_channel="",  # notification only, no reply needed
        metadata={
            "address": payload.get("address", ""),
            "symbol": symbol,
            "score": score,
            "grade": grade,
            "market_cap": mcap,
            "latest_price": price,
            "signal_types": payload.get("signal_types", {}),
            "risk_flags": payload.get("risk_flags", []),
        },
    )


def parse_whale_alert(payload: Dict[str, Any]) -> Optional[HookMessage]:
    """Parse a whale/KOL wallet buy or sell alert.

    Expected payload:
        {"direction": "BUY"|"SELL", "symbol": "...", "whale_label": "decu",
         "whale_tier": "kol", "whale_swap_usd": 5000, "address": "...",
         "market_cap": 100000, "dex": "Jupiter v6", ...}
    """
    direction = payload.get("direction", "BUY")
    symbol = payload.get("symbol", "?")
    label = payload.get("whale_label", "unknown")
    tier = payload.get("whale_tier", "whale")
    swap_usd = payload.get("whale_swap_usd", payload.get("usd_amount", 0))
    mcap = payload.get("market_cap", 0)
    dex = payload.get("dex", "?")

    emoji = "🐋" if tier == "whale" else "👑"
    action = "BOUGHT" if direction == "BUY" else "SOLD"

    body = (
        f"{emoji} {tier.upper()} ALERT: {label} {action} {symbol}\n"
        f"Amount: ${swap_usd:,.0f} via {dex}\n"
        f"MCap: ${mcap:,.0f}\n"
        f"Wallet: {payload.get('whale_wallet', payload.get('wallet', ''))[:16]}..."
    )

    return HookMessage(
        source="whale_alert",
        event=f"whale_{direction.lower()}",
        sender=f"{tier}:{label}",
        subject=f"{tier.upper()} {label} {action} {symbol} (${swap_usd:,.0f})",
        body=body,
        priority=2 if direction == "BUY" else 4,
        session_key=f"whale:{payload.get('whale_wallet', '')}:{payload.get('address', payload.get('token_mint', ''))}:{direction}",
        reply_channel="",
        metadata={
            "direction": direction,
            "address": payload.get("address", payload.get("token_mint", "")),
            "symbol": symbol,
            "whale_label": label,
            "whale_tier": tier,
            "whale_swap_usd": swap_usd,
            "market_cap": mcap,
            "dex": dex,
        },
    )


def parse_trade_execution(payload: Dict[str, Any]) -> Optional[HookMessage]:
    """Parse a trade execution event (buy, sell, TP, SL, moon partial).

    Expected payload:
        {"action": "AUTO_BUY"|"TAKE_PROFIT"|"STOP_LOSS"|"MOON_PARTIAL"|"BUY"|"SELL",
         "symbol": "...", "amount_usd": 150, "price": 0.001, "pnl_pct": 12.5,
         "pnl_usd": 18.75, "score": 8.5, "grade": "STRONG BUY", "reason": "..."}
    """
    action = payload.get("action", "TRADE")
    symbol = payload.get("symbol", "?")
    amount = payload.get("amount_usd", payload.get("proceeds", 0))
    price = payload.get("price", payload.get("price_at_market", 0))
    pnl_pct = payload.get("pnl_pct", 0)
    pnl_usd = payload.get("pnl_usd", payload.get("pnl", 0))
    reason = payload.get("reason", "")

    if action in ("TAKE_PROFIT", "STOP_LOSS", "MOON_PARTIAL"):
        emoji = "✅" if pnl_pct > 0 else "🛑"
        body = (
            f"{emoji} {action.replace('_', ' ')}: {symbol}\n"
            f"P/L: {pnl_pct:+.1f}% (${pnl_usd:+.2f})\n"
            f"Proceeds: ${amount:.2f}\n"
            f"{reason[:200]}"
        )
        priority = 2
    elif "BUY" in action:
        body = (
            f"⚡ {action}: {symbol}\n"
            f"Amount: ${amount:.2f} at ${price:.8f}\n"
            f"Score: {payload.get('score', '?')} ({payload.get('grade', '?')})\n"
            f"{reason[:200]}"
        )
        priority = 3
    else:
        body = (
            f"📊 {action}: {symbol}\n"
            f"Amount: ${amount:.2f} at ${price:.8f}\n"
            f"P/L: {pnl_pct:+.1f}% (${pnl_usd:+.2f})\n"
            f"{reason[:200]}"
        )
        priority = 3

    return HookMessage(
        source="trade_execution",
        event=action.lower(),
        sender="trading_engine",
        subject=f"{action}: {symbol} — ${amount:.2f}",
        body=body,
        priority=priority,
        session_key=f"trade:{symbol}:{action}:{int(payload.get('timestamp', 0) or __import__('time').time())}",
        reply_channel="",
        metadata={
            "action": action,
            "symbol": symbol,
            "amount_usd": amount,
            "price": price,
            "pnl_pct": pnl_pct,
            "pnl_usd": pnl_usd,
            "score": payload.get("score", 0),
            "grade": payload.get("grade", ""),
        },
    )
