---
name: repryntt-setup
description: >
  Install and configure repryntt — an autonomous AI agent framework with an
  optional blockchain mode. Use when the user wants
  to set up repryntt on a new machine, configure API keys, or troubleshoot
  installation issues.
tags: [setup, installation, configuration]
---

# Install & Configure repryntt

## Overview

repryntt is a self-prompting autonomous AI agent framework with 240+ callable
tools and a hormone-driven consciousness loop. The Rust blockchain node is
optional and should only be enabled when the operator explicitly chooses it.
It runs on Linux, macOS, Windows, Docker, and Jetson-class Linux hosts.

## Prerequisites

- Python 3.10+
- pip / venv
- Git
- (Optional) NVIDIA GPU with CUDA for local LLM
- (Optional) Node.js 20+ for MoonPay CLI tools

## Installation

### Quick Install

```bash
git clone https://github.com/ai158z/REPRYNTT.git repryntt
cd repryntt
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

### Visual Setup Wizard (Recommended)

```bash
python -m repryntt.setup
# Opens http://localhost:9090 — walks through hardware detection,
# dependency installation, API configuration, and first launch.
```

### Manual Setup

```bash
# 1. Install core
pip install -e .

# 2. Configure API keys (at minimum, one LLM provider)
export GOOGLE_API_KEY="your-gemini-key"       # Recommended: Gemini 2.0 Flash
# Optional providers:
export OPENAI_API_KEY="your-openai-key"
export ANTHROPIC_API_KEY="your-anthropic-key"

# 3. Start the default local stack
repryntt start
```

### Optional Extras

```bash
# Desktop app (native window + system tray)
pip install -e ".[desktop]"
repryntt desktop

# MoonPay multi-chain tools (wallet management, swaps, bridges, fiat ramp)
npm install -g @moonpay/cli

# Voice conversation (wake word + TTS)
pip install -e ".[voice]"
```

## Start Flags

```bash
repryntt start                  # Default production startup (blockchain off unless enabled)
repryntt start --no-llm         # Skip local LLM (use cloud-only models)
repryntt start --with-blockchain # Opt in to blockchain checks for this run
repryntt start --no-blockchain  # Force blockchain off for this run
repryntt start --no-trading     # Disable trading pipeline
repryntt start --no-evolution   # Disable self-evolution loop
```

## Verify Installation

```bash
repryntt doctor    # Health-check all services and dependencies
repryntt status    # System overview (agents, channels, cycles)
```

## Web Ports (After Startup)

| Port | Service |
|------|---------|
| 8089 | **Nexus** — primary dashboard (all UIs consolidated) |
| 5001 | Optional blockchain node (TCP) |
| 6001 | Optional node health endpoint |
| 8080 | Local LLM (llama.cpp) |

Access the dashboard at `http://<host>:8089`.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GOOGLE_API_KEY` | Yes | Gemini API access |
| `OPENAI_API_KEY` | Optional | GPT-4 tier |
| `ANTHROPIC_API_KEY` | Optional | Claude tier |
| `REPRYNTT_BLOCKCHAIN_ENABLED` | Optional | Set `1` to opt into blockchain mode |
| `REPRYNTT_NODE_PORT` | Optional | Blockchain port when enabled (default: 5001) |
| `REPRYNTT_BOOTSTRAP_NODES` | Optional | Comma-separated peer list when enabled |

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `ModuleNotFoundError: repryntt` | Run `pip install -e .` from the repo root |
| Port 8089 already in use | Another instance running — `repryntt stop` first |
| No GPU detected | System runs fine CPU-only; local LLM will be slower |
| API key errors | Check `~/.repryntt/brain/ai_config.json` for correct keys |

## Related Skills

- [repryntt-agent](../repryntt-agent/) — Interact with the Artemis AI agent
- [repryntt-tools](../repryntt-tools/) — Call any of the 240+ registered tools
- [repryntt-blockchain](../repryntt-blockchain/) — Blockchain node operations
