# Production P2P Seed Setup

Repryntt public discovery follows the Bitcoin-style model: users run their own
nodes, nodes connect to static/DNS seed peers, and the gossip layer exchanges
peer addresses from there. There is no official HTTP bootstrap registry required
by the default public release.

Seed nodes are discovery helpers and public full nodes. They are not consensus
authorities, custodians, gateways, exchanges, or payment services. Every node
must still verify genesis, checkpoints, blocks, signatures, and transactions
locally.

## DNS

Create two separate production seed hosts:

```text
A seed1.repryntt.ai158z.com -> <seed1_public_ip>
A seed2.repryntt.ai158z.com -> <seed2_public_ip>
```

For production, `seed1` and `seed2` should be separate machines, ideally in
different zones or providers. Do not publish private LAN addresses as public
seed records.

## Firewall

On each seed node:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 5001/tcp
sudo ufw enable
```

Keep JSON-RPC private:

```text
9332/tcp should stay bound to 127.0.0.1 unless intentionally admin-restricted.
```

## Seed Node Install

On each seed node:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl ufw build-essential
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

Clone, install, and build:

```bash
git clone https://github.com/ai158z/repryntt.git
cd repryntt
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[p2p]"
cd repryntt-core
cargo build --release
cd ..
```

Seed 1:

```bash
export REPRYNTT_SEEDS=seed1.repryntt.ai158z.com:5001,seed2.repryntt.ai158z.com:5001
export REPRYNTT_PUBLIC_P2P_ADDR=tcp://seed1.repryntt.ai158z.com:5001
export REPRYNTT_RPC_BIND=127.0.0.1:9332
export REPRYNTT_MINING=false

repryntt chain install
repryntt chain start
```

Seed 2:

```bash
export REPRYNTT_SEEDS=seed1.repryntt.ai158z.com:5001,seed2.repryntt.ai158z.com:5001
export REPRYNTT_PUBLIC_P2P_ADDR=tcp://seed2.repryntt.ai158z.com:5001
export REPRYNTT_RPC_BIND=127.0.0.1:9332
export REPRYNTT_MINING=false

repryntt chain install
repryntt chain start
```

## Canonical Chain Data

Before making a seed public, install a verified canonical chain snapshot and
checkpoint from the current canonical node. Do not copy wallets, private keys,
logs, `.env`, local memories, API keys, or the entire runtime directory.

Minimum copied state:

```text
~/.repryntt/rust_chain/chain.db*
~/.repryntt/rust_chain/dao_state.json
~/.repryntt/rust_chain/checkpoints.json
```

Seed nodes should stay non-mining until checkpoint verification passes and peer
connectivity is healthy.

## Verification

On each seed node:

```bash
repryntt chain checkpoint verify
repryntt chain status
ss -ltnp | grep 5001
```

From another machine:

```bash
nc -vz seed1.repryntt.ai158z.com 5001
nc -vz seed2.repryntt.ai158z.com 5001
```

Expected result:

- both seed DNS names resolve to public IPs
- both seed nodes accept TCP on `5001`
- RPC remains local/private
- both seed nodes verify the same checkpoint
- new nodes can discover seeds and then learn more peers through gossip

## Optional Self-Hosted Registry

`repryntt-bootstrap` remains available as optional self-hosted/community
software for private networks or third-party operators. It is not required by
the public release defaults. If an operator chooses to run one, users must set
`REPRYNTT_BOOTSTRAP_URL` explicitly to that operator's registry URL.

AI158Z does not need to operate an official HTTP peer registry for the default
public P2P network path.
