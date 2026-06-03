---
name: repryntt-compute
description: >
  Submit AI workloads (inference, batch, embedding, analysis) to any repryntt
  node's local LLM. Node operators set their own pricing and earn Credits.
  Use when the user wants to run AI workloads on the decentralized network.
tags: [compute, marketplace, gpu, inference, workloads]
---

# Workload Marketplace — Run AI on Any Node

## Overview

Any repryntt node runs a local LLM. External users submit workloads, pay CR,
and the node processes them. Node operators set their own pricing and earn CR.
5% platform commission. All jobs are async with persistent queue.

## Prerequisites

- repryntt running with blockchain (`repryntt start`)
- API key + funded wallet (see [repryntt-auth](../repryntt-auth/))

## Workload Types

| Type | Description | Payload |
|------|-------------|---------|
| `inference` | Single prompt → response | `{prompt, max_tokens, temperature}` or `{messages, max_tokens}` |
| `batch` | Multiple prompts (up to 100) | `{prompts: [...], max_tokens}` |
| `embedding` | Text → vectors (up to 500) | `{texts: [...]}` |
| `analysis` | Deep chain-of-thought | `{query}` |

## Submit a Workload (Credit-Gated)

```bash
curl -X POST http://<host>:8089/ext-api/workloads/submit \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: sign" \
  -H "X-Signature-Message: workload" \
  -H "Content-Type: application/json" \
  -d '{
    "workload_type": "inference",
    "payload": {
      "prompt": "Explain quantum computing in 3 sentences",
      "max_tokens": 500,
      "temperature": 0.7
    },
    "max_price_cr": 0.05,
    "wallet_address": "YOUR_WALLET"
  }'
```

Returns `202 Accepted`:
```json
{
  "success": true,
  "job_id": "a1b2c3d4...",
  "estimated_cost_cr": 0.012,
  "status": "pending",
  "queue_position": 1
}
```

## Poll for Result

```bash
curl http://<host>:8089/ext-api/workloads/a1b2c3d4... \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: sign" \
  -H "X-Signature-Message: poll" \
  -H "Content-Type: application/json" \
  -d '{"wallet_address": "YOUR_WALLET"}'
```

When complete:
```json
{
  "success": true,
  "status": "completed",
  "result": {"response": "Quantum computing uses...", "model": "local"},
  "actual_cost_cr": 0.008,
  "tokens_in": 20,
  "tokens_out": 150,
  "processing_time_s": 3.2,
  "pop_proof_hash": "abc123..."
}
```

## List Your Jobs

```bash
curl "http://<host>:8089/ext-api/workloads?status=completed&limit=10" \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: sign" \
  -H "X-Signature-Message: list" \
  -H "Content-Type: application/json" \
  -d '{"wallet_address": "YOUR_WALLET"}'
```

## Cancel a Pending Job (Full Refund)

```bash
curl -X POST http://<host>:8089/ext-api/workloads/a1b2c3d4.../cancel \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: sign" \
  -H "X-Signature-Message: cancel" \
  -H "Content-Type: application/json" \
  -d '{"wallet_address": "YOUR_WALLET"}'
```

## Batch Inference Example

```bash
curl -X POST http://<host>:8089/ext-api/workloads/submit \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "X-Wallet-Signature: sign" \
  -H "X-Signature-Message: workload" \
  -H "Content-Type: application/json" \
  -d '{
    "workload_type": "batch",
    "payload": {
      "prompts": [
        "Summarize the French Revolution",
        "Explain photosynthesis",
        "What is a neural network?"
      ],
      "max_tokens": 300
    },
    "wallet_address": "YOUR_WALLET"
  }'
```

## Node Operator: Check Config & Stats (Free)

```bash
curl http://<host>:8089/ext-api/node/config \
  -H "X-API-Key: YOUR_API_KEY"

curl http://<host>:8089/ext-api/node/stats \
  -H "X-API-Key: YOUR_API_KEY"
```

## Node Operator: Set Your Pricing (Admin)

```bash
curl -X PUT http://<host>:8089/ext-api/node/config \
  -H "X-API-Key: YOUR_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "accepting_workloads": true,
    "pricing": {
      "inference_per_1k_tokens": 0.05,
      "embedding_per_1k_tokens": 0.02,
      "analysis_per_request": 0.25,
      "batch_discount": 0.75
    },
    "max_concurrent_jobs": 3,
    "max_queue_depth": 100,
    "max_tokens_limit": 8192
  }'
```

## Pricing (Operator-Set Defaults)

| Service | Default Rate |
|---------|-------------|
| Inference | 0.02 CR / 1k tokens |
| Embedding | 0.01 CR / 1k tokens |
| Analysis | 0.10 CR / request |
| Batch discount | 80% of single price |

## Job Lifecycle

| Status | Description |
|--------|-------------|
| `pending` | Queued, CR reserved from wallet |
| `processing` | Node's LLM working on it |
| `completed` | Done — result available, overpayment refunded |
| `failed` | Error — full CR refund |
| `cancelled` | User cancelled — full CR refund |

## Economics

- **User pays**: CR reserved upfront, actual cost settled on completion, overpayment refunded
- **Node earns**: 95% of actual cost (5% platform commission)
- **PoP proof**: Each completed workload generates a Proof of Power hash recorded on-chain

## Related Skills

- [repryntt-auth](../repryntt-auth/) — Register + get Credits (do first)
- [repryntt-gateway](../repryntt-gateway/) — Deposit SOL to buy CR
- [repryntt-blockchain](../repryntt-blockchain/) — Wallet management
- [repryntt-gateway](../repryntt-gateway/) — Fund wallet with SOL/USDC
