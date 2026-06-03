---
name: repryntt-blockchain
description: >
  Interact with the repryntt blockchain — check balances, send Credits, view
  chain status, manage wallets, and connect to peers. Read-only queries are
  free; transactions cost Credits.
tags: [blockchain, crypto, wallet, mining]
---

# repryntt Blockchain

## Overview

repryntt runs a Proof-of-Power blockchain — nodes mine Credits (CR) by
performing real AI computation. 21M CR hard cap, 69s blocks.

## Prerequisites

- repryntt running (`repryntt start`)
- API key for wallet operations (see [repryntt-auth](../repryntt-auth/))

## Token Economics

| Parameter | Value |
|-----------|-------|
| Token | Credit (CR) |
| Max supply | 21,000,000 CR |
| Block reward | 10 CR (halves every 420,000 blocks) |
| Block interval | 69 seconds |
| Consensus | Proof of Power |

## Free Endpoints

### Node Health

```bash
curl http://<host>:6001/health
```

### Wallet Balance

```bash
curl http://<host>:8089/ext-api/wallet/YOUR_WALLET \
  -H "X-API-Key: YOUR_API_KEY"
```

### Credit Pricing

```bash
curl http://<host>:8089/ext-api/credits/pricing
```

### Market Analytics

```bash
curl http://<host>:8089/ext-api/analytics/market \
  -H "X-API-Key: YOUR_API_KEY"
```

### P2P Network Status

```bash
curl http://<host>:8089/api/p2p/status
```

### CLI Commands

```bash
repryntt node status
repryntt node peers
```

## Credit-Gated Endpoints (0.05 CR per tool call)

### Send Credits

```bash
curl -X POST http://<host>:8089/ext-api/ai/tool \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: sign" \
  -H "X-Signature-Message: tool" \
  -d '{
    "tool_name": "send_credits",
    "parameters": {"to": "RECIPIENT", "amount": 5.0},
    "wallet_address": "YOUR_WALLET"
  }'
```

### Chain Height

```bash
curl -X POST http://<host>:8089/ext-api/ai/tool \
  -d '{"tool_name": "chain_height", "parameters": {}, "wallet_address": "YOUR_WALLET"}' \
  -H "Content-Type: application/json" -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: sign" -H "X-Signature-Message: tool"
```

## Multi-Node Networking

### Same LAN (Automatic)

Nodes auto-discover via UDP port 5099.

### Remote Nodes

```bash
REPRYNTT_BOOTSTRAP_NODES=10.0.0.19:5001 repryntt start
```

## Funding Your Wallet

| Method | How | Rate |
|--------|-----|------|
| Faucet | `POST /ext-api/wallet/faucet` | 1,000 CR free (one-time) |
| SOL/USDC deposit | `POST /gateway/deposit` | Bridge balance (buy CR on order book) |
| Order book | `POST /ext-api/trading/create` | Market price (no fixed peg) |

## Related Skills

- [repryntt-auth](../repryntt-auth/) — Register + get Credits (do first)
- [repryntt-gateway](../repryntt-gateway/) — Deposit SOL/USDC
- [repryntt-compute](../repryntt-compute/) — Sell compute for Credits
