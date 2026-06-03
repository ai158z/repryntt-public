---
name: repryntt-gateway
description: >
  Use the repryntt payment gateway to deposit SOL or USDC and receive Credits (CR)
  on the repryntt blockchain. Use when the user wants to buy Credits, check
  deposit status, or fund their wallet for paid repryntt services.
tags: [payments, crypto, solana, gateway]
---

# Payment Gateway (SOL/USDC → Credits)

## Overview

The gateway accepts Solana deposits (SOL or USDC) and mints Credits (CR)
on the repryntt blockchain. This is how you fund a wallet to use repryntt's
paid services. The gateway itself is free to use — you're depositing funds.

## Prerequisites

- repryntt running (`repryntt start`)
- A repryntt wallet (see [repryntt-auth](../repryntt-auth/))

## All Gateway Endpoints Are Free

No Credits needed to create or check deposits — you're adding funds.

### Check Gateway Status

```bash
curl http://<host>:8089/gateway/status
```

### Create a Deposit

```bash
curl -X POST http://<host>:8089/gateway/deposit \
  -H "Content-Type: application/json" \
  -d '{"repryntt_address": "YOUR_WALLET"}'
```

Returns:
```json
{
  "deposit_id": "dep_abc123",
  "solana_address": "9SWXM8dac...",
  "accepted_tokens": ["SOL", "USDC"],
  "pricing": {"cr_price_usd": 0.01, "max_credits": 1000}
}
```

### Check Deposit Status

```bash
curl http://<host>:8089/gateway/deposit/dep_abc123
```

### List Deposits

```bash
curl "http://<host>:8089/gateway/deposits?status=completed"
```

## Pricing

CR is **market-priced** — there is no fixed USD peg.
Price is determined by buyers and sellers on the CR/SOL order book.

| Parameter | Value |
|-----------|-------|
| Trading pair | CR/SOL |
| Price discovery | Order book (no peg) |
| Accepted deposits | SOL, USDC |

### What Credits Buy

| Service | Cost |
|---------|------|
| AI workload (inference) | Node-set pricing (default 0.02 CR / 1k tokens) |
| AI workload (batch) | 80% of inference rate |
| AI workload (embedding) | 0.01 CR / 1k tokens |
| AI workload (analysis) | 0.10 CR / request |
| Tool call | 0.05 CR |
| AI chat (sync) | 0.02 CR / 1k tokens |

## Alternative: Free Startup Credits

```bash
curl -X POST http://<host>:8089/ext-api/wallet/faucet \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: test" \
  -H "X-Signature-Message: faucet" \
  -H "Content-Type: application/json" \
  -d '{"wallet_address": "YOUR_WALLET"}'
```

Grants 1,000 CR (one-time per wallet — enough for 20,000 tool calls).

## Related Skills

- [repryntt-auth](../repryntt-auth/) — Register + create wallet (do this first)
- [repryntt-blockchain](../repryntt-blockchain/) — Chain operations
- [repryntt-tools](../repryntt-tools/) — Spend Credits on tools
