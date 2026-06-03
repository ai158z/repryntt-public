---
name: repryntt-trading
description: >
  Use repryntt's trading pipeline — check prices, execute swaps on Solana
  (Jupiter), view portfolio, analyze signals, and use MoonPay for multi-chain
  operations. Read-only data is free; trade execution costs Credits.
tags: [trading, crypto, solana, defi]
---

# Trading Pipeline

## Overview

repryntt runs an 8-stage trading pipeline from token discovery through
execution. Jupiter aggregator for Solana swaps, MoonPay for cross-chain.
Read-only data is free; executing trades costs 0.05 CR per tool call.

## Prerequisites

- repryntt running (`repryntt start`)
- API key + funded wallet (see [repryntt-auth](../repryntt-auth/))

## Free Endpoints (Read-Only Data)

### Portfolio Summary

```bash
curl http://<host>:8089/api/trading/portfolio
```

### Trade History

```bash
curl http://<host>:8089/api/trading/trades?limit=20
```

### Active Signals

```bash
curl http://<host>:8089/api/trading/signals
```

### All Trading Data

```bash
curl http://<host>:8089/api/trading/all
```

### Hot Tokens

```bash
curl http://<host>:8089/trading/api/tokens/hot
```

### Trading Dashboard (Free in Browser)

```
http://<host>:8089/trading
```

## Credit-Gated Endpoints (0.05 CR per tool call)

### Check Price

```bash
curl -X POST http://<host>:8089/ext-api/ai/tool \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: sign" \
  -H "X-Signature-Message: tool" \
  -d '{
    "tool_name": "check_price",
    "parameters": {"token": "SOL"},
    "wallet_address": "YOUR_WALLET"
  }'
```

### Execute Swap (Jupiter)

```bash
curl -X POST http://<host>:8089/ext-api/ai/tool \
  -d '{
    "tool_name": "execute_swap",
    "parameters": {"from_token": "SOL", "to_token": "USDC", "amount": 0.1},
    "wallet_address": "YOUR_WALLET"
  }' \
  -H "Content-Type: application/json" -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: sign" -H "X-Signature-Message: tool"
```

### MoonPay — Trending Tokens

```bash
curl -X POST http://<host>:8089/ext-api/ai/tool \
  -d '{"tool_name": "mp_trending_tokens", "parameters": {}, "wallet_address": "YOUR_WALLET"}' \
  -H "Content-Type: application/json" -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: sign" -H "X-Signature-Message: tool"
```

### MoonPay — Cross-Chain Bridge

```bash
curl -X POST http://<host>:8089/ext-api/ai/tool \
  -d '{
    "tool_name": "mp_token_bridge",
    "parameters": {
      "from_wallet": "main", "from_chain": "ethereum", "to_chain": "solana",
      "from_token": "0x0000000000000000000000000000000000000000",
      "from_amount": "0.01",
      "to_token": "So11111111111111111111111111111111111111111"
    },
    "wallet_address": "YOUR_WALLET"
  }' \
  -H "Content-Type: application/json" -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: sign" -H "X-Signature-Message: tool"
```

### MoonPay — Buy Crypto with Fiat

```bash
curl -X POST http://<host>:8089/ext-api/ai/tool \
  -d '{
    "tool_name": "mp_buy_crypto",
    "parameters": {"amount": "50", "currency": "usd", "crypto": "sol"},
    "wallet_address": "YOUR_WALLET"
  }' \
  -H "Content-Type: application/json" -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: sign" -H "X-Signature-Message: tool"
```

## AI Market Analysis (0.10 CR)

```bash
curl -X POST http://<host>:8089/ext-api/ai/analyze \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: sign" \
  -H "X-Signature-Message: analyze" \
  -d '{
    "data": "Analyze SOL price action and trading signals",
    "wallet_address": "YOUR_WALLET"
  }'
```

## Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| 402 Insufficient credits | Balance < 0.05 CR | Fund via [repryntt-gateway](../repryntt-gateway/) |
| 401 Unauthorized | Missing API key | See [repryntt-auth](../repryntt-auth/) |

## Related Skills

- [repryntt-auth](../repryntt-auth/) — Register + get Credits (do first)
- [repryntt-gateway](../repryntt-gateway/) — Fund wallet with SOL/USDC
- [repryntt-tools](../repryntt-tools/) — Full tool catalog
