# Repryntt Multi-Node Network Setup

Run repryntt on multiple machines and watch them connect as blockchain nodes.

## Quick Start (3 machines, same LAN)

### On each machine:

```bash
git clone https://github.com/ai158z/REPRYNTT.git
cd REPRYNTT
pip install -e .
repryntt start --no-llm --no-trading --no-evolution
```

That's it. Nodes auto-discover each other via **LAN UDP broadcast** on port 5099.

> Use `--no-llm` to skip the local LLM (you can add it later).
> Use `--no-trading` and `--no-evolution` if you just want to test the blockchain.

### Verify nodes found each other:

```bash
repryntt node status    # chain length, peer count
repryntt node peers     # list connected peers
repryntt doctor         # full system health check
```

## How Discovery Works

Repryntt uses **dual discovery** — nodes find each other automatically:

| Method | Port | Scope | How |
|--------|------|-------|-----|
| LAN Broadcast | UDP 5099 | Same subnet | Announces every 30s, auto-connects |
| Kademlia DHT | UDP 5100 | Internet-wide | Distributed hash table peer routing |
| Bootstrap List | TCP 5001 | Manual | `REPRYNTT_BOOTSTRAP_NODES` or `REPRYNTT_SEEDS` env var |

**On the same LAN** (e.g., all on 10.0.0.x), nodes find each other automatically.  
**Across networks**, set bootstrap nodes explicitly.

## Ports

| Port | Protocol | Purpose |
|------|----------|---------|
| 5001 | TCP | Blockchain node (block sync, peer communication) |
| 5099 | UDP | LAN discovery broadcast |
| 5100 | UDP | Kademlia DHT (scale module) |
| 6001 | TCP | Health check HTTP endpoint |
| 5000 | TCP | Web server |
| 8089 | TCP | Nexus dashboard |
| 8080 | TCP | llama.cpp local LLM (optional) |

## Full Production Setup

Start everything including the agent, LLM, and trading:

```bash
repryntt start
```

Start without the local LLM (API-only agents):

```bash
repryntt start --no-llm
```

Blockchain-only (minimal footprint):

```bash
repryntt node start
```

## Cross-Network Setup

If your machines are on different subnets or over the internet, set bootstrap nodes:

```bash
# On each machine, point to the other machines:
export REPRYNTT_BOOTSTRAP_NODES="10.0.0.19:5001,192.168.1.50:5001,10.0.1.100:5001"
repryntt start --no-llm
```

## Docker (3-node cluster)

```bash
cd docker
docker compose -f docker-compose.cluster.yml up
```

This starts 3 nodes that auto-discover and sync. See `docker/docker-compose.cluster.yml`.

## Architecture

```
Machine A (Jetson)          Machine B (Desktop)         Machine C (Laptop)
┌──────────────────┐       ┌──────────────────┐       ┌──────────────────┐
│  repryntt start  │       │  repryntt start  │       │  repryntt start  │
│                  │       │                  │       │                  │
│  Blockchain :5001│◄─TCP─►│  Blockchain :5001│◄─TCP─►│  Blockchain :5001│
│  LAN Disc  :5099 │──UDP──│  LAN Disc  :5099 │──UDP──│  LAN Disc  :5099 │
│  Kademlia  :5100 │       │  Kademlia  :5100 │       │  Kademlia  :5100 │
│  Health    :6001 │       │  Health    :6001 │       │  Health    :6001 │
│  Nexus     :8089 │       │  Nexus     :8089 │       │  Nexus     :8089 │
│  Agent Daemon    │       │  Agent Daemon    │       │  Agent Daemon    │
└──────────────────┘       └──────────────────┘       └──────────────────┘
         │                          │                          │
         └──────────────────────────┴──────────────────────────┘
                         Shared blockchain
                     Proof of Power consensus
                     69-second block intervals
```

## What Each Node Does

- **Syncs the blockchain** — all 3 machines maintain the same chain
- **Generates blocks** every 69 seconds with Proof of Power (AI computation, not hash puzzles)
- **Broadcasts** new blocks and transactions to all peers via gossip protocol
- **Resolves forks** automatically (longest valid chain wins, checked every 5 min)
- **Runs its own agent** (Andrew) independently — each can mine and transact

## Troubleshooting

### Nodes don't see each other
1. Check firewall: `sudo ufw allow 5001/tcp && sudo ufw allow 5099/udp && sudo ufw allow 5100/udp`
2. Verify same subnet: `ip addr | grep inet`
3. Try explicit bootstrap: `export REPRYNTT_BOOTSTRAP_NODES=otherIP:5001`
4. Check logs: `tail -f ~/.repryntt/logs/blockchain-node.log`

### "Port 5001 already in use"
Another blockchain instance is running. Stop it first:
```bash
repryntt stop
# or
pkill -f "repryntt.economy.qnode2"
```

### Node starts but no blocks
Blocks generate every 69 seconds. Wait a couple minutes, then check:
```bash
repryntt node status
```

## Seed Peer Configuration

New nodes need at least one seed peer to join the global repryntt blockchain.
The Rust node resolves seeds in this priority order:

1. **`REPRYNTT_SEEDS`** env var (highest priority)
2. **`REPRYNTT_BOOTSTRAP_NODES`** env var (same format, alternative name)
3. **`<data_dir>/node.conf`** file (`addnode=host:port` lines)
4. **Hardcoded fallback** (10.0.0.19:5001 — Jetson primary bootstrap)

Set either env var to `none` to explicitly disable all seeds (solo/dev mode).

DNS hostnames are supported (e.g., `bootstrap.repryntt.net:5001`).

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REPRYNTT_BOOTSTRAP_NODES` | (fallback seeds) | Comma-separated `host:port` seed peers |
| `REPRYNTT_SEEDS` | (fallback seeds) | Same as above (Rust-native name, takes priority) |
| `REPRYNTT_BOOTSTRAP_URL` | (none) | HTTP rendezvous server URL (e.g. `http://34.x.x.x:6600`) |
| `REPRYNTT_SCALE` | `1` | Enable scale modules (gossip, DHT, etc.) |
| `REPRYNTT_DATA_DIR` | `~/.repryntt` | Data directory |
| `REPRYNTT_NODE_PORT` | `5001` | Blockchain TCP port |
