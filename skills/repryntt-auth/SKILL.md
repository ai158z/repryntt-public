---
name: repryntt-auth
description: >
  Register for API access and create a wallet on the repryntt network. This is
  required before using any paid repryntt skill (tools, trading, blockchain,
  compute, gateway). Free to register — Credits are needed for API calls.
tags: [auth, wallet, registration, credits]
---

# Register & Get Credits

## Overview

All repryntt API calls (except installation and health checks) cost Credits (CR).
You must register for an API key and create a wallet before using any other skill.
Registration is free. You receive 1,000 CR from the faucet to start.

## Step 1: Register for API Access

```bash
curl -X POST http://<host>:8089/ext-api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"user_id": "my-ai-agent"}'
```

Response:
```json
{
  "api_key": "a1b2c3d4e5f6...",
  "user_id": "my-ai-agent",
  "permissions": ["read", "write"]
}
```

**Save the `api_key`** — use it in all subsequent requests as `X-API-Key` header.

## Step 2: Create a Wallet

```bash
curl -X POST http://<host>:8089/ext-api/wallet/create \
  -H "X-API-Key: YOUR_API_KEY"
```

Response returns your wallet address. **Save it.**

## Step 3: Get Free Starter Credits

```bash
curl -X POST http://<host>:8089/ext-api/wallet/faucet \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: faucet-request" \
  -H "X-Signature-Message: faucet" \
  -H "Content-Type: application/json" \
  -d '{"wallet_address": "YOUR_WALLET_ADDRESS"}'
```

Grants **1,000 CR** — enough for 20,000 tool calls or ~50M tokens of AI chat.

## Step 4: Check Balance

```bash
curl http://<host>:8089/ext-api/wallet/YOUR_WALLET_ADDRESS \
  -H "X-API-Key: YOUR_API_KEY"
```

## Getting More Credits

### Deposit SOL/USDC via Solana Bridge

See the [repryntt-gateway](../repryntt-gateway/) skill.
Deposits go to your bridge balance — then buy CR on the order book at market price.

### Check Market Price

```bash
curl http://<host>:8089/ext-api/credits/pricing
```

### Check the Order Book

```bash
curl http://<host>:8089/ext-api/trading/orderbook \
  -H "X-API-Key: YOUR_API_KEY"
```

## Credit Costs

| Service | Cost | Unit |
|---------|------|------|
| Tool call | **0.05 CR** | per call (flat) |
| AI chat | **0.02 CR** | per 1,000 tokens |
| Analysis | **0.10 CR** | per request |
| Marketplace | variable | + 10% platform fee |

## Required Headers (All Paid Endpoints)

```
X-API-Key: YOUR_API_KEY
X-Wallet-Signature: any-string
X-Signature-Message: any-string
Content-Type: application/json
```

Body must include `"wallet_address": "YOUR_WALLET_ADDRESS"` for paid calls.

## Error: Insufficient Credits

If you see HTTP 402:
```json
{"success": false, "error": "Insufficient credits. Needed: 0.05 CR, Available: 0.00 CR"}
```

Get more credits via faucet, gateway (SOL/USDC), or purchase.

## Related Skills

- [repryntt-setup](../repryntt-setup/) — Install repryntt (free)
- [repryntt-tools](../repryntt-tools/) — Call tools (costs 0.05 CR each)
- [repryntt-gateway](../repryntt-gateway/) — Buy Credits with SOL/USDC
