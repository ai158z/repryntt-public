---
name: repryntt-tools
description: >
  Execute any of repryntt's 240+ registered tools via the credit-gated API. Use
  when the user wants to call a specific tool — web search, image generation,
  blockchain queries, MoonPay, trading, or any other tool. Each call costs
  0.05 Credits.
tags: [tools, api, automation]
---

# Execute repryntt Tools (Credit-Gated)

## Overview

repryntt has 240+ tools across 30 categories. External access goes through
`/ext-api/ai/tool` — each call costs **0.05 CR**.

## Prerequisites

- repryntt running (`repryntt start`)
- API key + funded wallet (see [repryntt-auth](../repryntt-auth/))

## Pricing

**0.05 CR per tool call** (flat rate, all tools). CR is market-priced — check the order book for current SOL/CR rate.

## Execute a Tool

```bash
curl -X POST http://<host>:8089/ext-api/ai/tool \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: sign" \
  -H "X-Signature-Message: tool" \
  -d '{
    "tool_name": "TOOL_NAME",
    "parameters": { ... },
    "wallet_address": "YOUR_WALLET"
  }'
```

Response:
```json
{
  "success": true,
  "tool_name": "web_search",
  "result": { ... },
  "cost_credits": 0.05,
  "remaining_balance_credits": 9.95
}
```

## Browse Tools (Free — No Credits)

```bash
curl http://<host>:8089/tool-api/tools           # List all tools
curl http://<host>:8089/tool-api/tools/web_search/schema  # Tool schema
```

## Tool Categories (30)

| Category | Count | Examples |
|----------|-------|---------|
| `moonpay` | 21 | `mp_wallet_list`, `mp_token_swap`, `mp_trending_tokens` |
| `swarm_tools` | 17 | `dispatch_task`, `agent_status` |
| `video` | 13 | `generate_video`, `create_thumbnail` |
| `trading` | 11 | `check_price`, `execute_swap` |
| `media` | 11 | `generate_image`, `text_to_speech` |
| `economy` | 10 | `gateway_create_deposit`, `gateway_status` |
| `web` | 8 | `web_search`, `scrape_url` |
| `knowledge` | 8 | `rag_query`, `knowledge_search` |
| `memory` | 7 | `memory_store`, `memory_recall` |
| `blockchain` | 6 | `wallet_balance`, `send_credits` |
| `code` | 6 | `generate_code`, `execute_python` |

*30 categories total — `GET /tool-api/tools` for full list.*

## Examples

### Web Search

```bash
curl -X POST http://<host>:8089/ext-api/ai/tool \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: sign" \
  -H "X-Signature-Message: tool" \
  -d '{"tool_name": "web_search", "parameters": {"query": "NVIDIA Jetson 2026"}, "wallet_address": "YOUR_WALLET"}'
```

### MoonPay Trending Tokens

```bash
curl -X POST http://<host>:8089/ext-api/ai/tool \
  -d '{"tool_name": "mp_trending_tokens", "parameters": {}, "wallet_address": "YOUR_WALLET"}' \
  -H "Content-Type: application/json" -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: sign" -H "X-Signature-Message: tool"
```

### Generate Image

```bash
curl -X POST http://<host>:8089/ext-api/ai/tool \
  -d '{"tool_name": "generate_image", "parameters": {"prompt": "Robot building blockchain"}, "wallet_address": "YOUR_WALLET"}' \
  -H "Content-Type: application/json" -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: sign" -H "X-Signature-Message: tool"
```

## Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| 401 | Bad/missing API key | See [repryntt-auth](../repryntt-auth/) |
| 402 | Insufficient credits | Fund wallet via [repryntt-gateway](../repryntt-gateway/) |
| 404 | Invalid tool name | Check `GET /tool-api/tools` |
| 429 | Rate limited | Wait, retry |

## Related Skills

- [repryntt-auth](../repryntt-auth/) — Register + get Credits (do this first)
- [repryntt-setup](../repryntt-setup/) — Free installation
- [repryntt-gateway](../repryntt-gateway/) — Fund wallet with SOL/USDC
