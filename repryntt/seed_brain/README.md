# seed_brain — Pre-warmed knowledge graph for new installs

The files in this directory are copied into `~/.repryntt/brain/` on first
install — but **only if the destination doesn't already exist**. On a
re-install or subsequent boot, an agent's accumulated state always wins;
the seed is just a starting point so day-1 isn't a cold start.

## Contents

| File | What it is |
|---|---|
| `memory_mesh.json` | A pre-warmed associative memory graph (275 nodes, 1247 edges). Captures relationships between tools, concepts, topics, and patterns the canonical Andrew agent learned over thousands of heartbeats — sanitized of operator-personal references. Lets a fresh install's agent immediately reason about which tools relate to which contexts. |
| `semantic_memory.json` | Curated heartbeat-level memories from a working session, illustrating the format and shape an agent's semantic memory takes as it accumulates. Mostly research findings on multi-agent coordination, edge inference, decentralized task allocation — universally interesting, not operator-personal. |
| `learned_behaviors.json` | Empty placeholder for the learning engine's per-behavior memory. Grows on its own. |

## What's deliberately NOT seeded

- `spatial_map.json` — the operator's home map (1654 places, useless to others)
- `daemon_state.json` — runtime cycle counters, scheduler state (resets per install)
- `cortex_reflections.jsonl` — operator's accumulated cortex reflections (personal)
- `operator_profile.json` — the operator's profile (yours, not the seed's)
- `consciousness_state.json`, `sleep_wake.json` — reset on first boot
- Wallet keys, tracked wallets, anything under `economy/wallets/` — never seeded

## How seeding works at install time

`repryntt/setup/server.py` calls `_seed_brain_state()` after the bootstrap
templates are copied. It walks every file in this directory and copies it
into `~/.repryntt/brain/` if and only if no file with that name already
exists there. Idempotent — re-running install is safe and preserves your
agent's accumulated state.
