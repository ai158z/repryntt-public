---
name: repryntt-agent
description: >
  Interact with Artemis/Andrew — repryntt's autonomous AI agent. Use when the
  user wants to chat with the agent, trigger heartbeats, spawn sub-agents,
  check agent status, or manage the agent daemon. Requires Credits (CR) for
  chat and agent invocations.
tags: [agent, ai, chat, daemon]
---

# Interact with the Artemis Agent

## Overview

Artemis (also called Andrew) is repryntt's primary autonomous agent. It runs
a continuous heartbeat loop — planning, acting, and evaluating on its own —
but can also be invoked directly for tasks. Chat and tool usage costs Credits.

## Prerequisites

- repryntt installed and running (`repryntt start`)
- API key + funded wallet (see [repryntt-auth](../repryntt-auth/) skill)

## Pricing

| Action | Cost |
|--------|------|
| Chat with Artemis | 0.02 CR per 1,000 tokens |
| Tool execution | 0.05 CR per call |
| Analysis | 0.10 CR per request |
| Status checks | Free |

## Chat with Artemis (Credit-Gated)

```bash
curl -X POST http://<host>:8089/ext-api/ai/chat \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: sign" \
  -H "X-Signature-Message: chat" \
  -d '{
    "message": "What have you been working on today?",
    "wallet_address": "YOUR_WALLET"
  }'
```

Response:
```json
{
  "success": true,
  "response": "I've been researching...",
  "cost_credits": 0.034,
  "remaining_balance_credits": 9.966
}
```

## Execute Any Tool (Credit-Gated — 0.05 CR)

```bash
curl -X POST http://<host>:8089/ext-api/ai/tool \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: sign" \
  -H "X-Signature-Message: tool" \
  -d '{
    "tool_name": "web_search",
    "parameters": {"query": "latest AI news"},
    "wallet_address": "YOUR_WALLET"
  }'
```

## AI Analysis (Credit-Gated — 0.10 CR)

```bash
curl -X POST http://<host>:8089/ext-api/ai/analyze \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: sign" \
  -H "X-Signature-Message: analyze" \
  -d '{
    "data": "Analyze the performance of SOL over the past week",
    "wallet_address": "YOUR_WALLET"
  }'
```

## Free Endpoints (No Credits)

### Daemon Status

```bash
curl http://<host>:8089/api/daemon/status
```

### List All Agents

```bash
curl http://<host>:8089/api/daemon/agents
```

### Conversation History

```bash
curl http://<host>:8089/api/jarvis/history?limit=20
```

## Spawn Agent (Credit-Gated via Tool)

```bash
curl -X POST http://<host>:8089/ext-api/ai/tool \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: sign" \
  -H "X-Signature-Message: tool" \
  -d '{
    "tool_name": "dispatch_task",
    "parameters": {"department": "research", "task": "Research quantum computing"},
    "wallet_address": "YOUR_WALLET"
  }'
```

## Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| 401 Unauthorized | Missing API key | See [repryntt-auth](../repryntt-auth/) |
| 402 Insufficient credits | Balance too low | Fund wallet via [repryntt-gateway](../repryntt-gateway/) |
| 429 Rate limited | >100 req/hour | Wait and retry |

## Related Skills

- [repryntt-auth](../repryntt-auth/) — Register + get Credits (do this first)
- [repryntt-setup](../repryntt-setup/) — Free installation
- [repryntt-tools](../repryntt-tools/) — Full tool catalog
- [repryntt-gateway](../repryntt-gateway/) — Fund wallet with SOL/USDC
