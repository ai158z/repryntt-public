# AGENTS.md — Install & run Repryntt

This file is for coding agents (Claude Code, Cursor, Grok CLI, etc.). A user
may say *"install this"* — follow the steps below exactly. **Run the known
commands here; do not invent install scripts or `sudo` commands.**

Repryntt is a self-prompting autonomous AI agent framework (local-first, your
own LLM keys) with an **optional** Rust Proof-of-Power blockchain. The
blockchain is off by default and not required.

## Requirements
- Python **3.10+**
- ~2 GB disk. No GPU required (any configured LLM API works; local models optional).

## Install (one command, any OS)
From the repository root — pick the line for the platform:

```bash
# macOS / Linux
./install.sh            # (or: python3 install.py)

# Windows (PowerShell or cmd)
python install.py
```

Both run the same cross-platform installer. It's deterministic and idempotent
(safe to re-run): verifies Python 3.10+, creates `.venv`, installs Repryntt
into it, seeds `~/.repryntt/brain/ai_config.json` from the example if absent,
and runs `repryntt doctor` to verify. Use `python install.py` if `bash` isn't
available.

## Configure keys
Edit `~/.repryntt/brain/ai_config.json`. Use `config/ai_config.example.json` as
the reference. Set **at least one** provider — e.g. an `api_key` under
`anthropic`, `openai`, `xai`, or `google_gemini` — **or** point `local` at a
running llama.cpp endpoint. If the user hasn't given you keys, ask them; never
guess or fabricate keys.

## Run
```bash
source .venv/bin/activate
repryntt start --no-blockchain      # default: local AI brain, no chain
```
Only drop `--no-blockchain` if the user explicitly wants to run the
Proof-of-Power node (it adds a heavyweight Rust process; opt-in).

## Verify
```bash
repryntt doctor      # health-checks data dir, deps, config, services
```
A clean `doctor` (no red issues) = success. Missing-API-key warnings are
expected until the user adds keys.

## Known commands (use these; don't improvise)
| Command | Purpose |
|---|---|
| `repryntt doctor` | Health check / verify install |
| `repryntt onboard` / `repryntt setup` | Guided (browser) setup wizard |
| `repryntt start [--no-blockchain]` | Start the full system |
| `repryntt status` | System overview |
| `repryntt stop` | Graceful shutdown |
| `repryntt roster` | List agents by department |

## Guardrails
- Prefer `./install.sh` + the known `repryntt` subcommands above.
- Never run `sudo` or modify system-wide Python; everything stays in `.venv`.
- The blockchain stays opt-in unless the user asks for it.
- Don't write the user's API keys anywhere except `~/.repryntt/brain/ai_config.json`.
