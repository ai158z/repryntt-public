"""
Block Explorer + Wallet Web UI for the repryntt blockchain.

Routes:
    /chain/              — Explorer home (latest blocks, network stats)
    /chain/block/<index> — Individual block detail
    /chain/tx/<hash>     — Transaction detail
    /chain/address/<addr>— Address balance + transaction history
    /chain/wallet        — Wallet UI (create, send, receive, faucet)

All pages use the board-theme.css design system.
"""

import json
import logging
import os
import hmac
import time
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from flask import Blueprint, render_template_string, request, jsonify

log = logging.getLogger("blockchain_explorer")

explorer_bp = Blueprint("explorer", __name__)


def _get_manager():
    """Lazy-load the economy manager singleton."""
    try:
        from repryntt.economy.manager import RobotEconomyManager
        return RobotEconomyManager.get_instance()
    except Exception:
        return None


def _rpc(method: str, params: dict = None) -> dict:
    """Query the Rust blockchain node via JSON-RPC 2.0 on port 9332."""
    from repryntt.economy.rust_chain_client import rpc_call
    return rpc_call(method, params)


def _read_chain_env_value(key: str) -> str:
    env_path = Path.home() / ".repryntt" / "rust_chain" / "repryntt-chain.env"
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == key:
                return value.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def _chain_env_path() -> Path:
    return Path.home() / ".repryntt" / "rust_chain" / "repryntt-chain.env"


def _bootstrap_urls() -> list[str]:
    url = (
        os.environ.get("REPRYNTT_BOOTSTRAP_URL", "").strip()
        or _read_chain_env_value("REPRYNTT_BOOTSTRAP_URL")
    )
    if url.lower() in {"disabled", "none", "off", "false", "0"}:
        return []

    urls = []
    for part in url.split(","):
        item = part.strip().rstrip("/")
        if item and item.lower() not in {"disabled", "none", "off", "false", "0"}:
            urls.append(item)
    return urls


def _bootstrap_peer_snapshot() -> dict:
    """Read the discovery phonebook so the explorer can show known network nodes."""
    urls = _bootstrap_urls()
    if not urls:
        return {"bootstrap_url": "", "peers": [], "error": "bootstrap URL not configured"}

    peers_by_address = {}
    errors = []
    for url in urls:
        endpoint = f"{url}/rendezvous/peers"
        try:
            req = urllib.request.Request(endpoint, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                body = resp.read(256 * 1024).decode("utf-8")
            data = json.loads(body)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            errors.append(f"{url}: {exc}")
            continue

        peers = data.get("peers", [])
        if not isinstance(peers, list):
            continue
        for peer in peers:
            if not isinstance(peer, dict):
                continue
            key = peer.get("address") or peer.get("node_id")
            if key and key not in peers_by_address:
                peers_by_address[key] = peer

    if peers_by_address:
        return {
            "bootstrap_url": ",".join(urls),
            "peers": list(peers_by_address.values())[:100],
            "error": "",
        }

    return {"bootstrap_url": ",".join(urls), "peers": [], "error": "; ".join(errors)}


def _is_local_request() -> bool:
    remote = request.remote_addr or ""
    return remote in {"127.0.0.1", "::1", "localhost"}


def _extract_api_key() -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return (
        request.headers.get("X-API-Key", "").strip()
        or request.args.get("api_key", "").strip()
    )


def _chain_api_key_valid() -> bool:
    expected = (
        os.environ.get("REPRYNTT_API_KEY", "").strip()
        or os.environ.get("SAIGE_API_KEY", "").strip()
    )
    if not expected or expected.startswith("CHANGE_ME"):
        return False
    return hmac.compare_digest(_extract_api_key(), expected)


@explorer_bp.before_request
def _protect_chain_mutations():
    """Keep chain-changing explorer APIs local unless explicitly authenticated."""
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    if "/api/" not in request.path:
        return None
    if _is_local_request():
        return None

    wallet_secret_route = request.path.endswith("/api/wallet/create") or request.path.endswith(
        "/api/wallet/recover"
    )
    if wallet_secret_route and os.environ.get("REPRYNTT_ALLOW_REMOTE_WALLET_SECRETS") != "1":
        return jsonify({
            "success": False,
            "error": "Remote wallet secret operations are disabled. Use localhost or client-side wallet tools.",
        }), 403

    if not _chain_api_key_valid():
        return jsonify({
            "success": False,
            "error": "Remote chain mutation requires Authorization: Bearer <REPRYNTT_API_KEY>.",
        }), 401
    return None


# ── Faucet rate-limiting ──────────────────────────────────────────────
# Persisted: wallet claims saved to disk so restarts don't reset limits.
# Per-IP: max 5 claims per IP (lifetime), tracked on disk alongside wallets.

_FAUCET_STATE_PATH = Path.home() / ".repryntt" / "data" / "faucet_claims.json"
_faucet_lock = threading.Lock()
_FAUCET_AMOUNT_PLANCKS = 10_000_000_000  # 100 CR
_FAUCET_BUDGET_PLANCKS = 210_000 * 100_000_000  # 210,000 CR total


def _load_faucet_state() -> dict:
    """Load persisted faucet claims from disk."""
    if _FAUCET_STATE_PATH.exists():
        try:
            return json.loads(_FAUCET_STATE_PATH.read_text())
        except Exception:
            pass
    return {"wallets": []}


def _save_faucet_state(state: dict):
    """Persist faucet claims to disk."""
    _FAUCET_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _FAUCET_STATE_PATH.write_text(json.dumps(state, indent=2))


def _check_faucet_allowed(address: str, ip: str) -> tuple:
    """Check if this wallet is allowed to claim. Returns (allowed, error_msg).
    
    Limits: 1 claim per wallet (forever), 210K CR global budget.
    No per-IP limit — anyone can create wallets and claim.
    """
    state = _load_faucet_state()
    if address in state.get("wallets", []):
        return False, "This wallet has already claimed faucet credits."
    # Global budget: 210,000 CR
    total_distributed = len(state.get("wallets", [])) * _FAUCET_AMOUNT_PLANCKS
    if total_distributed >= _FAUCET_BUDGET_PLANCKS:
        return False, "Faucet budget exhausted (210,000 CR distributed). Faucet is closed."
    return True, ""


def _record_faucet_claim(address: str, ip: str):
    """Record a successful faucet claim."""
    with _faucet_lock:
        state = _load_faucet_state()
        if address not in state.get("wallets", []):
            state.setdefault("wallets", []).append(address)
        _save_faucet_state(state)


# ═══════════════════════════════════════════════════════════════════════
# HTML Template — single-page with JS tabs (explorer + wallet)
# ═══════════════════════════════════════════════════════════════════════

EXPLORER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>/chain/ — REPRYNTT Block Explorer</title>
<link rel="stylesheet" href="/static/board-theme.css"/>
<style>
:root { --red:#af0a0f; --green:#228854; --cyan:#34345C; --purple:#6c3483; --mono:monospace; }
.stats-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:8px; margin:8px; }
.stat-card { background:#d6daf0; border:1px solid #b7c5d9; padding:10px; text-align:center; }
.stat-card .label { font-size:10px; color:#89a; font-weight:bold; text-transform:uppercase; margin-bottom:4px; }
.stat-card .value { font-size:22px; font-weight:bold; font-family:monospace; color:#af0a0f; }
.stat-card .sub { font-size:10px; color:#89a; margin-top:2px; }
table { width:100%; border-collapse:collapse; font-size:12px; }
th { background:#98e; color:#fff; padding:4px 8px; text-align:left; font-size:11px; }
td { padding:4px 8px; border-bottom:1px solid #d6daf0; font-family:monospace; font-size:11px; }
tr:nth-child(even) { background:#eef2ff; }
tr:hover { background:#dde5ff; cursor:pointer; }
.hash { color:#34345C; font-size:10px; }
.addr { color:#6c3483; }
.amount { color:#228854; font-weight:bold; }
.tabs { display:flex; gap:0; margin:8px 8px 0; }
.tab { padding:6px 16px; background:#d6daf0; border:1px solid #b7c5d9; border-bottom:none; cursor:pointer; font-weight:bold; font-size:12px; color:#34345C; }
.tab.active { background:#eef2ff; color:#af0a0f; border-bottom:1px solid #eef2ff; margin-bottom:-1px; z-index:1; }
.tab-content { display:none; }
.tab-content.active { display:block; }
.panels { margin:8px; }
.panel { background:#d6daf0; border:1px solid #b7c5d9; margin-bottom:8px; }
.panel h2 { background:#98e; color:#fff; font-size:12px; font-weight:bold; padding:4px 8px; margin:0; }
.panel-body { padding:8px; }
input[type=text], input[type=number] { font-family:monospace; font-size:12px; padding:4px 6px; border:1px solid #b7c5d9; background:#fff; width:100%; margin:4px 0; }
button { background:#98e; color:#fff; border:none; padding:6px 16px; cursor:pointer; font-weight:bold; font-size:12px; }
button:hover { background:#87d; }
.msg { padding:6px; margin:4px 0; font-size:11px; }
.msg-ok { background:#d4edda; color:#155724; border:1px solid #c3e6cb; }
.msg-err { background:#f8d7da; color:#721c24; border:1px solid #f5c6cb; }
.msg-warn { background:#fff3cd; color:#856404; border:1px solid #ffeeba; }
.detail-box { background:#eef2ff; border:1px solid #b7c5d9; padding:8px; margin:8px 0; font-family:monospace; font-size:11px; word-break:break-all; }
.detail-row { display:flex; gap:8px; margin:2px 0; }
.detail-label { font-weight:bold; color:#34345C; min-width:120px; }
.detail-value { color:#000; flex:1; }
.loading { text-align:center; padding:20px; color:#89a; }
#search-bar { margin:8px; display:flex; gap:4px; }
#search-bar input { flex:1; }
a.addr-link { color:#6c3483; text-decoration:none; font-family:monospace; font-size:11px; }
a.addr-link:hover { text-decoration:underline; color:#8e44ad; }
a.tx-link { color:#34345C; text-decoration:none; font-family:monospace; font-size:10px; }
a.tx-link:hover { text-decoration:underline; color:#5b5b8a; }
a.block-link { color:#af0a0f; text-decoration:none; font-weight:bold; }
a.block-link:hover { text-decoration:underline; }
.badge { display:inline-block; padding:1px 6px; border-radius:3px; font-size:9px; font-weight:bold; text-transform:uppercase; }
.badge-transfer { background:#d4edda; color:#155724; }
.badge-coinbase { background:#fff3cd; color:#856404; }
.badge-stake { background:#cce5ff; color:#004085; }
.badge-unstake { background:#f8d7da; color:#721c24; }
.badge-faucet { background:#e2d5f1; color:#6c3483; }
.badge-dao { background:#ffecd2; color:#8a6d3b; }
.badge-confirmed { background:#d4edda; color:#155724; }
.badge-pending { background:#fff3cd; color:#856404; }
.confirmations { color:#228854; font-size:10px; font-weight:bold; }
.direction-sent { color:#af0a0f; }
.direction-received { color:#228854; }
.direction-self { color:#34345C; }
.pagination { display:flex; gap:4px; align-items:center; justify-content:center; margin:8px 0; }
.pagination button { padding:4px 12px; font-size:11px; }
.pagination .page-info { font-size:11px; color:#89a; }
.sub-tabs { display:flex; gap:0; margin:8px 0 0; }
.sub-tab { padding:4px 12px; background:#e2e6f0; border:1px solid #b7c5d9; border-bottom:none; cursor:pointer; font-size:11px; color:#34345C; }
.sub-tab.active { background:#eef2ff; color:#af0a0f; font-weight:bold; }
.explorer-section { margin:8px 0; }
.time-ago { color:#89a; font-size:10px; }
</style>
</head>
<body>
<div class="topbar">
  <div>
    <a href="/">Hub</a> <a href="/social">Social</a> <a href="/commerce">Commerce</a>
    <a href="/chain/" class="active">Explorer</a> <a href="/ops">Ops</a>
  </div>
  <div style="color:#89a">repryntt blockchain</div>
</div>

<div class="board-header">
  <div class="board-title">Block Explorer</div>
  <div class="board-subtitle">repryntt Proof-of-Power blockchain — real AI computation, real economy</div>
</div>

<div id="search-bar">
  <input type="text" id="searchInput" placeholder="Search by block index, tx hash, or wallet address..." onkeydown="if(event.key==='Enter')doSearch()"/>
  <button onclick="doSearch()">Search</button>
</div>

<div class="tabs">
  <div class="tab active" onclick="showTab('explorer')">Explorer</div>
  <div class="tab" onclick="showTab('wallet')">Wallet</div>
  <div class="tab" onclick="showTab('staking')">Staking</div>
  <div class="tab" onclick="showTab('mining')">Mining</div>
  <div class="tab" onclick="showTab('compute')">Compute</div>
  <div class="tab" onclick="showTab('dao')">DAO</div>
</div>

<!-- ════════════ EXPLORER TAB ════════════ -->
<div id="tab-explorer" class="tab-content active">
  <div class="stats-grid" id="networkStats"><div class="loading">Loading network stats...</div></div>

  <div class="sub-tabs">
    <div class="sub-tab active" onclick="showExplorerSub('blocks')">Blocks</div>
    <div class="sub-tab" onclick="showExplorerSub('mempool')">Mempool</div>
    <div class="sub-tab" onclick="showExplorerSub('richlist')">Rich List</div>
    <div class="sub-tab" onclick="showExplorerSub('network')">Network</div>
  </div>

  <div class="panels">
    <!-- Latest Blocks -->
    <div id="sub-blocks" class="explorer-section">
      <div class="panel">
        <h2>Latest Blocks</h2>
        <div class="panel-body" id="latestBlocks"><div class="loading">Loading...</div></div>
      </div>
    </div>

    <!-- Mempool -->
    <div id="sub-mempool" class="explorer-section" style="display:none">
      <div class="panel">
        <h2>Pending Transactions (Mempool)</h2>
        <div class="panel-body" id="mempoolBody"><div class="loading">Loading mempool...</div></div>
      </div>
    </div>

    <!-- Rich List -->
    <div id="sub-richlist" class="explorer-section" style="display:none">
      <div class="panel">
        <h2>Rich List — Top Addresses by Balance</h2>
        <div class="panel-body" id="richlistBody"><div class="loading">Loading...</div></div>
      </div>
    </div>

    <!-- Network Nodes -->
    <div id="sub-network" class="explorer-section" style="display:none">
      <div class="panel">
        <h2>Network Nodes</h2>
        <div class="panel-body" id="networkPeers"><div class="loading">Loading network nodes...</div></div>
      </div>
    </div>

    <!-- TX Detail (shown on click) -->
    <div class="panel" id="txDetail" style="display:none">
      <h2>Transaction Detail</h2>
      <div class="panel-body" id="txDetailBody"></div>
    </div>

    <!-- Block Detail (shown on click) -->
    <div class="panel" id="blockDetail" style="display:none">
      <h2>Block Detail</h2>
      <div class="panel-body" id="blockDetailBody"></div>
    </div>

    <!-- Address Detail + History (shown on click) -->
    <div class="panel" id="addressDetail" style="display:none">
      <h2>Address Detail</h2>
      <div class="panel-body" id="addressDetailBody"></div>
    </div>
  </div>
</div>

<!-- ════════════ WALLET TAB ════════════ -->
<div id="tab-wallet" class="tab-content">
  <div class="panels">
    <div class="panel">
      <h2>⚡ Getting Started — Read This First</h2>
      <div class="panel-body" style="font-size:11px;color:#456;">
        <p style="color:#af0a0f;font-weight:bold;">IMPORTANT: This is a real blockchain. If you lose your private key and mnemonic phrase, your tokens are gone forever. There is no customer support, no password reset, no recovery.</p>
        <ol>
          <li><b>Create a wallet</b> below. You'll get a <b>24-word recovery phrase</b> and a <b>private key</b>. Write them down and store them safely offline.</li>
          <li><b>Claim from the faucet</b> (100 CR per wallet, one-time). This gives you enough to stake and start earning.</li>
          <li>Go to the <b>Staking</b> tab and stake your credits to earn 0.01 CR per block (~1,252 blocks/day).</li>
          <li>Set your <b>Compute</b> contribution to earn AI workload rewards (up to 10 CR per block).</li>
        </ol>
        <p><b>Recommended setup:</b> 1 machine wallet + 3 miner wallets = 400 CR bootstrap. Stake all of them.</p>
        <p><b>Faucet budget:</b> 210,000 CR total (1% of max supply). Once exhausted, tokens are earned through mining only.</p>
      </div>
    </div>
    <div class="panel">
      <h2>Create New Wallet</h2>
      <div class="panel-body">
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px;">
          <label style="font-size:11px;font-weight:bold;">Type:</label>
          <select id="walletType" style="font-family:monospace;font-size:12px;padding:4px;border:1px solid #b7c5d9;">
            <option value="machine">Machine (node operator)</option>
            <option value="miner">Miner (compute worker)</option>
            <option value="personal">Personal (your wallet)</option>
          </select>
        </div>
        <button onclick="createWallet()">Generate Wallet</button>
        <div id="createResult"></div>
        <div id="walletBackup" style="display:none;margin-top:8px;">
          <div style="background:#fff3cd;border:2px solid #af0a0f;padding:12px;margin:8px 0;">
            <div style="color:#af0a0f;font-weight:bold;font-size:13px;margin-bottom:8px;">⚠ SAVE THIS NOW — IT WILL NOT BE SHOWN AGAIN</div>
            <div class="detail-box" style="background:#fff;margin:4px 0;">
              <div class="detail-row"><div class="detail-label">Address</div><div class="detail-value" id="newAddr"></div></div>
              <div class="detail-row"><div class="detail-label">Private Key</div><div class="detail-value" id="newPrivKey" style="color:#af0a0f;"></div></div>
            </div>
            <div style="margin-top:8px;">
              <div style="font-weight:bold;font-size:11px;margin-bottom:4px;">24-Word Recovery Phrase:</div>
              <div id="newMnemonic" style="background:#fff;border:1px solid #af0a0f;padding:8px;font-family:monospace;font-size:12px;word-spacing:8px;line-height:1.8;user-select:all;"></div>
            </div>
            <div style="margin-top:8px;font-size:10px;color:#721c24;">
              <p>✅ Write down the 24 words on paper. Store in a safe place.</p>
              <p>✅ Copy the private key (hex). You need it for staking and sending.</p>
              <p>❌ Do NOT share your private key or recovery phrase with anyone.</p>
              <p>❌ Do NOT store them in screenshots, cloud storage, or chat messages.</p>
              <p>💡 The private key and phrase can recover your wallet. Lose both = lose everything.</p>
            </div>
          </div>
          <button onclick="document.getElementById('walletBackup').style.display='none'">I've Saved It — Hide</button>
        </div>
      </div>
    </div>
    <div class="panel">
      <h2>Recover Wallet from Mnemonic</h2>
      <div class="panel-body">
        <div style="font-size:11px;color:#456;margin-bottom:8px;">Enter your 24-word recovery phrase to derive your address and private key.</div>
        <textarea id="recoverMnemonic" rows="2" style="font-family:monospace;font-size:12px;padding:6px;border:1px solid #b7c5d9;width:100%;resize:vertical;" placeholder="word1 word2 word3 ... word24"></textarea>
        <button onclick="recoverWallet()">Recover Wallet</button>
        <div id="recoverResult"></div>
      </div>
    </div>
    <div class="panel">
      <h2>Check Balance</h2>
      <div class="panel-body">
        <input type="text" id="balAddr" placeholder="Wallet address"/>
        <button onclick="checkBalance()">Check</button>
        <div id="balResult"></div>
      </div>
    </div>
    <div class="panel">
      <h2>Faucet — Bootstrap Credits (100 CR)</h2>
      <div class="panel-body">
        <div style="font-size:11px;color:#456;margin-bottom:8px;">One-time claim of 100 CR per wallet. Use it to stake and start earning. Global budget: 210,000 CR.</div>
        <input type="text" id="faucetAddr" placeholder="Your wallet address"/>
        <button onclick="claimFaucet()">Claim 100 CR</button>
        <div id="faucetResult"></div>
      </div>
    </div>
    <div class="panel">
      <h2>Send Credits (Signed Transaction)</h2>
      <div class="panel-body">
        <input type="text" id="sendFrom" placeholder="From address"/>
        <input type="text" id="sendTo" placeholder="To address"/>
        <input type="number" id="sendAmount" placeholder="Amount (Credits)" step="0.01" min="0.01"/>
        <input type="text" id="sendPrivKey" placeholder="Private key (hex) — signs the transaction locally"/>
        <button onclick="sendCredits()">Sign &amp; Send</button>
        <div style="font-size:10px;color:#89a;margin-top:4px;">All transfers require Ed25519 cryptographic signatures. Your key never leaves the browser.</div>
        <div id="sendResult"></div>
      </div>
    </div>
    <div class="panel">
      <h2>Top Wallets</h2>
      <div class="panel-body" id="leaderboard"><div class="loading">Loading...</div></div>
    </div>
  </div>
</div>

<!-- ════════════ STAKING TAB ════════════ -->
<div id="tab-staking" class="tab-content">
  <div class="panels">
    <div class="panel">
      <h2>Stake Overview</h2>
      <div class="panel-body" id="stakeOverview"><div class="loading">Enter your address to view stake info</div></div>
      <div class="panel-body">
        <input type="text" id="stakeViewAddr" placeholder="Your wallet address"/>
        <button onclick="loadStakeInfo()">View Stake</button>
      </div>
    </div>
    <div class="panel">
      <h2>Stake Credits</h2>
      <div class="panel-body">
        <div style="font-size:11px;color:#456;margin-bottom:8px;">Lock credits as stake to earn <b>0.01 CR per block</b> availability rewards. Staked credits cannot be transferred until unstaked.</div>
        <input type="text" id="stakeAddr" placeholder="Your wallet address"/>
        <input type="number" id="stakeAmount" placeholder="Amount to stake (Credits)" step="0.01" min="0.01"/>
        <input type="text" id="stakePrivKey" placeholder="Private key (hex) — signs locally, never sent"/>
        <button onclick="doStake()">Sign &amp; Stake</button>
        <div style="font-size:10px;color:#89a;margin-top:4px;">Your private key never leaves the browser. The stake message is signed client-side with Ed25519.</div>
        <div id="stakeResult"></div>
      </div>
    </div>
    <div class="panel">
      <h2>Unstake Credits</h2>
      <div class="panel-body">
        <div style="font-size:11px;color:#456;margin-bottom:8px;">Withdraw staked credits back to your available balance.</div>
        <input type="text" id="unstakeAddr" placeholder="Your wallet address"/>
        <input type="number" id="unstakeAmount" placeholder="Amount to unstake (Credits)" step="0.01" min="0.01"/>
        <input type="text" id="unstakePrivKey" placeholder="Private key (hex) — signs locally, never sent"/>
        <button onclick="doUnstake()">Sign &amp; Unstake</button>
        <div id="unstakeResult"></div>
      </div>
    </div>
    <div class="panel">
      <h2>Top Stakers</h2>
      <div class="panel-body" id="topStakers"><div class="loading">Loading...</div></div>
    </div>
    <div class="panel">
      <h2>How Staking Works</h2>
      <div class="panel-body" style="font-size:11px;color:#456;">
        <p><b>Availability Rewards:</b> Every block (~69 seconds), each staked node earns 0.01 CR. This is the base reward for keeping the network alive.</p>
        <p><b>AI Workload Rewards:</b> Up to 10 CR per block for completing AI computation tasks. Reward scales with your TFLOPS and work quality.</p>
        <p><b>Minimum Stake:</b> Any amount can be staked. Higher stakes increase your reputation score.</p>
        <p><b>Security:</b> All stake/unstake operations require Ed25519 cryptographic signatures. Your private key never leaves your browser.</p>
      </div>
    </div>
  </div>
</div>

<!-- ════════════ MINING TAB ════════════ -->
<div id="tab-mining" class="tab-content">
  <div class="stats-grid" id="miningStats"><div class="loading">Loading mining stats...</div></div>
  <div class="panels">
    <div class="panel">
      <h2>Reward Schedule</h2>
      <div class="panel-body" id="rewardSchedule"><div class="loading">Loading...</div></div>
    </div>
    <div class="panel">
      <h2>Mining Economics</h2>
      <div class="panel-body" style="font-size:11px;color:#456;">
        <p><b>Block Time:</b> ~69 seconds (target). Blocks are only produced when there are pending transactions or AI workloads.</p>
        <p><b>Base Reward:</b> 10 CR per block (AI workload reward). Halves every 420,000 blocks (~335 days at full utilization).</p>
        <p><b>Availability Reward:</b> 0.01 CR per block per staked miner — guaranteed income for keeping the network alive.</p>
        <p><b>TFLOPS Multiplier:</b> 0.5x to 2.0x based on your compute power relative to baseline (5.8 TFLOPS).</p>
        <p><b>Quality Factor:</b> 0.1x to 1.0x based on AI workload output quality (cryptographically verified).</p>
        <p><b>Max Supply:</b> 21,000,000 CR (2.1 quadrillion plancks). 1 CR = 100,000,000 plancks.</p>
        <p><b>Halvings:</b> Block reward halves every 420,000 blocks. After ~20 halvings, mining reward approaches zero.</p>
      </div>
    </div>
  </div>
</div>

<!-- ════════════ COMPUTE TAB ════════════ -->
<div id="tab-compute" class="tab-content">
  <div class="panels">
    <div class="panel">
      <h2>Your Device</h2>
      <div class="panel-body" id="computeInfo"><div class="loading">Loading device info...</div></div>
    </div>
    <div class="panel">
      <h2>Compute Contribution</h2>
      <div class="panel-body">
        <div style="margin-bottom:8px;font-size:12px;">Choose how much of your compute power to contribute to the repryntt network:</div>
        <div style="display:flex;align-items:center;gap:12px;">
          <input type="range" id="computeSlider" min="0" max="100" value="100" style="flex:1;" oninput="updateSliderLabel()"/>
          <span id="sliderLabel" style="font-family:monospace;font-weight:bold;min-width:50px;">100%</span>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:10px;color:#89a;margin-top:2px;">
          <span>None (0%)</span><span>Light (25%)</span><span>Medium (50%)</span><span>Full (100%)</span>
        </div>
        <button onclick="setComputeShare()" style="margin-top:8px;">Apply</button>
        <div id="computeResult" style="margin-top:4px;"></div>
      </div>
    </div>
    <div class="panel">
      <h2>What This Means</h2>
      <div class="panel-body" style="font-size:11px;color:#456;">
        <p><b>0%</b> — Your node relays blocks and transactions but does not process AI workloads. No mining rewards.</p>
        <p><b>25%</b> — Light contribution. Good for phones, tablets, or machines you're actively using.</p>
        <p><b>50%</b> — Balanced. Shares half your compute for AI workloads while keeping headroom.</p>
        <p><b>100%</b> — Full power. Dedicates all available compute to the network. Maximum mining rewards.</p>
        <p style="margin-top:12px;"><b>🖥️ GPUs</b> earn the most, but <b>every device counts</b>. Phones, laptops, and CPU-only machines all contribute real compute and earn rewards — just at lower rates. A phone at 100% earns roughly the same as a desktop GPU at 10%.</p>
        <p style="margin-top:4px;"><b>📱 CPU-only?</b> You're still helping — your node validates transactions, relays blocks, and handles lightweight AI tasks. The network needs relay nodes just as much as heavy compute. Set contribution to what feels comfortable for your battery/thermals.</p>
        <p style="margin-top:8px;">Rewards scale with verified compute contribution. All workloads are cryptographically verified via Proof of Productive Work.</p>
      </div>
    </div>
  </div>
</div>

<!-- ════════════ DAO TAB ════════════ -->
<div id="tab-dao" class="tab-content">
  <div class="panels">
    <div class="panel">
      <h2>Treasury</h2>
      <div class="panel-body" id="daoTreasury"><div class="loading">Loading treasury...</div></div>
    </div>
    <div class="panel">
      <h2>Submit Proposal</h2>
      <div class="panel-body">
        <div style="display:grid;gap:6px;">
          <input type="text" id="propTitle" placeholder="Proposal title" style="padding:6px;background:#1a2332;border:1px solid #2a3a4a;color:#c5d5e5;border-radius:4px;"/>
          <textarea id="propDesc" placeholder="Description" rows="2" style="padding:6px;background:#1a2332;border:1px solid #2a3a4a;color:#c5d5e5;border-radius:4px;resize:vertical;"></textarea>
          <div style="display:flex;gap:6px;">
            <input type="number" id="propAmount" placeholder="Amount (CR)" step="0.01" min="0.01" style="flex:1;padding:6px;background:#1a2332;border:1px solid #2a3a4a;color:#c5d5e5;border-radius:4px;"/>
            <input type="text" id="propRecipient" placeholder="Recipient address" style="flex:2;padding:6px;background:#1a2332;border:1px solid #2a3a4a;color:#c5d5e5;border-radius:4px;"/>
          </div>
          <input type="text" id="propProposer" placeholder="Your address (proposer)" style="padding:6px;background:#1a2332;border:1px solid #2a3a4a;color:#c5d5e5;border-radius:4px;"/>
          <button onclick="submitProposal()">Submit Proposal</button>
          <div id="propResult" style="font-size:11px;margin-top:2px;"></div>
        </div>
      </div>
    </div>
    <div class="panel" style="grid-column:1/-1;">
      <h2>Active Proposals</h2>
      <div class="panel-body" id="daoProposals"><div class="loading">Loading proposals...</div></div>
    </div>
  </div>
</div>

<script>
const API = '/chain/api';
let _chainHeight = 0;  // cached for confirmations

function showTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('tab-' + name).classList.add('active');
  if (name === 'wallet') loadLeaderboard();
  if (name === 'staking') loadTopStakers();
  if (name === 'mining') loadMiningStats();
  if (name === 'compute') loadComputeInfo();
  if (name === 'dao') loadDAO();
}

function showExplorerSub(name) {
  document.querySelectorAll('.sub-tab').forEach(t => t.classList.remove('active'));
  event.target.classList.add('active');
  ['blocks','mempool','richlist','network'].forEach(s => {
    document.getElementById('sub-'+s).style.display = s===name ? 'block' : 'none';
  });
  if (name === 'mempool') loadMempool();
  if (name === 'richlist') loadRichList();
  if (name === 'network') loadNetworkPeers();
}

async function fetchJSON(url) {
  try {
    const r = await fetch(url);
    return await r.json();
  } catch(e) {
    return {success:false, error:e.message};
  }
}

async function postJSON(url, body) {
  try {
    const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    return await r.json();
  } catch(e) {
    return {success:false, error:e.message};
  }
}

function ts(t) { return new Date(t*1000).toLocaleString(); }
function cr(plancks) { return (plancks/100000000).toFixed(2); }
function esc(s) { const d=document.createElement('div'); d.textContent=String(s); return d.innerHTML; }

function timeAgo(timestamp) {
  const secs = Math.floor(Date.now()/1000 - timestamp);
  if (secs < 60) return secs + 's ago';
  if (secs < 3600) return Math.floor(secs/60) + 'm ago';
  if (secs < 86400) return Math.floor(secs/3600) + 'h ago';
  return Math.floor(secs/86400) + 'd ago';
}

// ── Linkified helpers ──
function addrLink(a) {
  if (!a) return '<span class="addr">—</span>';
  const special = ['SYSTEM','FAUCET','STAKE_POOL','DAO','DAO_TREASURY'];
  if (special.includes(a)) return '<span class="addr" style="font-weight:bold;">'+esc(a)+'</span>';
  const short = a.length > 20 ? esc(a.slice(0,10))+'…'+esc(a.slice(-6)) : esc(a);
  return '<a href="#" onclick="loadAddressWithHistory(\''+esc(a)+'\');return false" class="addr-link" title="'+esc(a)+'">'+short+'</a>';
}
function addrLinkFull(a) {
  if (!a) return '—';
  const special = ['SYSTEM','FAUCET','STAKE_POOL','DAO','DAO_TREASURY'];
  if (special.includes(a)) return '<span class="addr" style="font-weight:bold;">'+esc(a)+'</span>';
  return '<a href="#" onclick="loadAddressWithHistory(\''+esc(a)+'\');return false" class="addr-link">'+esc(a)+'</a>';
}
function txLink(h) {
  if (!h) return '—';
  const short = esc(h.slice(0,16))+'…';
  return '<a href="#" onclick="loadTx(\''+esc(h)+'\');return false" class="tx-link" title="'+esc(h)+'">'+short+'</a>';
}
function blockLink(idx) {
  return '<a href="#" onclick="loadBlock('+idx+');return false" class="block-link">'+idx+'</a>';
}
function txTypeBadge(t) {
  const cls = {transfer:'badge-transfer',coinbase:'badge-coinbase',ai_reward:'badge-coinbase',availability_reward:'badge-coinbase',stake:'badge-stake',unstake:'badge-unstake',faucet_claim:'badge-faucet',dao_funding:'badge-dao',dao_payout:'badge-dao'}[t] || 'badge-transfer';
  return '<span class="badge '+cls+'">'+esc(t)+'</span>';
}
function confirmBadge(conf) {
  if (conf === undefined || conf === null) return '<span class="badge badge-pending">pending</span>';
  if (conf === 0) return '<span class="badge badge-pending">0 conf</span>';
  return '<span class="confirmations">✓ '+conf+' conf</span>';
}

// ── Network Stats ──
async function loadStats() {
  const d = await fetchJSON(API+'/stats');
  if (!d.success) { document.getElementById('networkStats').innerHTML='<div class="loading">Node offline</div>'; return; }
  _chainHeight = d.chain_length;
  const warnings = d.warnings || [];
  const banner = warnings.length
    ? `<div class="msg msg-warn" style="grid-column:1/-1;">${warnings.map(esc).join('<br>')}</div>`
    : '';
  const ageLabel = d.latest_block_age_seconds !== null && d.latest_block_age_seconds !== undefined
    ? timeAgo((Date.now() / 1000) - d.latest_block_age_seconds)
    : 'unknown';
  document.getElementById('networkStats').innerHTML = `
    ${banner}
    <div class="stat-card"><div class="label">Chain Height</div><div class="value">${d.chain_length}</div></div>
    <div class="stat-card"><div class="label">Total Supply</div><div class="value">${cr(d.total_supply||0)} CR</div></div>
    <div class="stat-card"><div class="label">Accounts</div><div class="value">${d.total_accounts||0}</div></div>
    <div class="stat-card"><div class="label">Peers</div><div class="value">${d.peer_count||0}</div></div>
    <div class="stat-card"><div class="label">Mempool</div><div class="value">${d.pending_txs||0}</div><div class="sub">pending txs</div></div>
    <div class="stat-card"><div class="label">Sync</div><div class="value">${esc(d.sync_state||'unknown')}</div><div class="sub">lag ${d.height_lag||0} · latest ${esc(ageLabel)}</div></div>
    <div class="stat-card"><div class="label">Network TFLOPS</div><div class="value">${(d.network_tflops||0).toFixed(1)}</div></div>
  `;
}

async function loadNetworkPeers() {
  const d = await fetchJSON(API+'/network/peers');
  if (!d.success) {
    document.getElementById('networkPeers').innerHTML = `<div class="msg msg-err">${esc(d.error || 'Bootstrap registry unavailable')}</div>`;
    return;
  }
  const peers = d.peers || [];
  let html = `<div class="mini">Bootstrap: ${esc(d.bootstrap_url || 'not configured')} · Known nodes: ${peers.length} · Active local peers: ${d.active_peer_count || 0}</div>`;
  html += `<div class="mini">Mining: ${esc(d.mining_state || 'unknown')} · Fork: ${esc(d.fork_status || 'unknown')} · Checkpoint: ${esc(d.checkpoint_status || 'unknown')}</div>`;
  if (d.mining_pause_reason) {
    html += `<div class="msg msg-warn">${esc(d.mining_pause_reason)}</div>`;
  }
  if (d.peer_diagnostics && d.peer_diagnostics.length) {
    html += `<div class="msg msg-warn">${d.peer_diagnostics.map(esc).join('<br>')}</div>`;
  }
  if (d.warnings && d.warnings.length) {
    html += `<div class="msg msg-warn">${d.warnings.map(esc).join('<br>')}</div>`;
  }
  if (!peers.length) {
    html += '<div class="loading">No announced nodes yet</div>';
    document.getElementById('networkPeers').innerHTML = html;
    return;
  }
  html += '<table><tr><th>Node</th><th>Address</th><th>Height</th><th>Version</th><th>Last Seen</th><th>Source IP</th></tr>';
  for (const p of peers) {
    const node = p.node_id || '';
    const shortNode = node.length > 18 ? node.slice(0,10)+'…'+node.slice(-6) : node;
    const last = p.last_seen ? timeAgo(p.last_seen) : '—';
    html += `<tr>
      <td><span class="addr" title="${esc(node)}">${esc(shortNode || '—')}</span></td>
      <td>${esc(p.address || '—')}</td>
      <td>${p.chain_height ?? 0}</td>
      <td>${esc(p.version || '—')}</td>
      <td class="time-ago">${esc(last)}</td>
      <td>${esc(p.source_ip || '—')}</td>
    </tr>`;
  }
  html += '</table>';
  document.getElementById('networkPeers').innerHTML = html;
}

// ── Latest Blocks (with linkified addresses) ──
async function loadBlocks() {
  const d = await fetchJSON(API+'/blocks?count=20');
  if (!d.success || !d.blocks.length) { document.getElementById('latestBlocks').innerHTML='<div class="loading">No blocks</div>'; return; }
  let html = '<table><tr><th>#</th><th>Hash</th><th>Age</th><th>Miner</th><th>TXs</th></tr>';
  for (const b of d.blocks) {
    html += `<tr>
      <td>${blockLink(b.index)}</td>
      <td class="tx-link" style="cursor:pointer" onclick="loadBlock(${b.index})">${esc(b.hash.slice(0,16))}…</td>
      <td class="time-ago">${timeAgo(b.timestamp)}</td>
      <td>${addrLink(b.miner_address)}</td>
      <td>${b.tx_count}</td>
    </tr>`;
  }
  html += '</table>';
  document.getElementById('latestBlocks').innerHTML = html;
}

// ── Block Detail (enhanced with linkified everything) ──
async function loadBlock(index) {
  const d = await fetchJSON(API+'/block/'+index);
  if (!d.success) return;
  const b = d.block;
  const confs = _chainHeight > 0 ? _chainHeight - b.index : '?';
  let html = `<div class="detail-box">
    <div class="detail-row"><div class="detail-label">Block</div><div class="detail-value">${blockLink(b.index)} ${confirmBadge(confs)}</div></div>
    <div class="detail-row"><div class="detail-label">Hash</div><div class="detail-value" style="font-size:10px;">${esc(b.hash)}</div></div>
    <div class="detail-row"><div class="detail-label">Previous Hash</div><div class="detail-value" style="font-size:10px;">${b.index > 0 ? '<a href="#" onclick="loadBlock('+(b.index-1)+');return false" class="tx-link">'+esc(b.previous_hash)+'</a>' : esc(b.previous_hash)}</div></div>
    <div class="detail-row"><div class="detail-label">Timestamp</div><div class="detail-value">${ts(b.timestamp)} <span class="time-ago">(${timeAgo(b.timestamp)})</span></div></div>
    <div class="detail-row"><div class="detail-label">Miner</div><div class="detail-value">${addrLinkFull(b.miner_address)}</div></div>
    <div class="detail-row"><div class="detail-label">Transactions</div><div class="detail-value">${b.transactions ? b.transactions.length : 0}</div></div>
    <div class="detail-row"><div class="detail-label">Confirmations</div><div class="detail-value">${confs}</div></div>
  </div>`;
  if (b.index > 0) {
    html += '<div style="margin:4px 0;font-size:11px;">← <a href="#" onclick="loadBlock('+(b.index-1)+');return false" class="block-link">Block '+(b.index-1)+'</a>';
    html += ' | <a href="#" onclick="loadBlock('+(b.index+1)+');return false" class="block-link">Block '+(b.index+1)+' →</a></div>';
  }
  if (b.transactions && b.transactions.length) {
    html += '<table><tr><th>TX Hash</th><th>Type</th><th>From</th><th>To</th><th>Amount</th><th>Data</th><th>Conf</th></tr>';
    for (const tx of b.transactions) {
      const txConfs = _chainHeight > 0 ? _chainHeight - b.index : '?';
      const amountCr = tx.amount_cr !== undefined ? tx.amount_cr : cr(tx.amount);
      const hasMeta = tx.metadata && Object.keys(tx.metadata).length > 0;
      const metaBadge = hasMeta ? '<span class="badge" style="background:#2a5a8a;font-size:9px;" title="Has on-chain metadata">'+
        (tx.metadata.n || Object.keys(tx.metadata).length)+' acts</span>' : '<span style="color:#556;">—</span>';
      html += `<tr>
        <td>${txLink(tx.tx_hash)}</td>
        <td>${txTypeBadge(tx.tx_type)}</td>
        <td>${addrLink(tx.from_address)}</td>
        <td>${addrLink(tx.to_address)}</td>
        <td class="amount">${amountCr} CR</td>
        <td>${metaBadge}</td>
        <td>${confirmBadge(txConfs)}</td>
      </tr>`;
    }
    html += '</table>';
  } else {
    html += '<div class="loading">No transactions in this block</div>';
  }
  document.getElementById('blockDetailBody').innerHTML = html;
  document.getElementById('blockDetail').style.display = 'block';
  // Hide other detail panels
  document.getElementById('txDetail').style.display = 'none';
  document.getElementById('addressDetail').style.display = 'none';
  document.getElementById('blockDetail').scrollIntoView({behavior:'smooth'});
}

// ── TX Detail (NEW — full transaction detail view) ──
async function loadTx(hash) {
  document.getElementById('txDetailBody').innerHTML = '<div class="loading">Loading transaction...</div>';
  document.getElementById('txDetail').style.display = 'block';
  document.getElementById('blockDetail').style.display = 'none';
  document.getElementById('addressDetail').style.display = 'none';

  const d = await fetchJSON(API+'/tx/'+encodeURIComponent(hash));
  if (!d.success) {
    document.getElementById('txDetailBody').innerHTML = '<div class="msg msg-err">Transaction not found: '+esc(hash.slice(0,32))+'...</div>';
    return;
  }
  const tx = d.transaction || d;
  const confs = tx.confirmations !== undefined ? tx.confirmations : '?';
  const status = tx.status || (confs > 0 ? 'confirmed' : 'pending');
  const amountCr = tx.amount_cr !== undefined ? tx.amount_cr : cr(tx.amount || 0);
  let html = `<div class="detail-box">
    <div class="detail-row"><div class="detail-label">TX Hash</div><div class="detail-value" style="font-size:10px;word-break:break-all;">${esc(tx.tx_hash || hash)}</div></div>
    <div class="detail-row"><div class="detail-label">Status</div><div class="detail-value"><span class="badge ${status==='confirmed'?'badge-confirmed':'badge-pending'}">${status}</span> ${confirmBadge(confs)}</div></div>
    <div class="detail-row"><div class="detail-label">Type</div><div class="detail-value">${txTypeBadge(tx.tx_type || 'transfer')}</div></div>
    <div class="detail-row"><div class="detail-label">Block</div><div class="detail-value">${tx.block_index !== undefined ? blockLink(tx.block_index) : 'Pending'}</div></div>
    <div class="detail-row"><div class="detail-label">Block Hash</div><div class="detail-value" style="font-size:10px;">${tx.block_hash ? esc(tx.block_hash) : '—'}</div></div>
    <div class="detail-row"><div class="detail-label">Timestamp</div><div class="detail-value">${tx.timestamp ? ts(tx.timestamp)+' <span class="time-ago">('+timeAgo(tx.timestamp)+')</span>' : '—'}</div></div>
    <div class="detail-row"><div class="detail-label">From</div><div class="detail-value">${addrLinkFull(tx.from_address)}</div></div>
    <div class="detail-row"><div class="detail-label">To</div><div class="detail-value">${addrLinkFull(tx.to_address)}</div></div>
    <div class="detail-row"><div class="detail-label">Amount</div><div class="detail-value"><span class="amount" style="font-size:16px;">${amountCr} CR</span> <span style="font-size:10px;color:#89a;">(${(tx.amount||0).toLocaleString()} plancks)</span></div></div>
    <div class="detail-row"><div class="detail-label">Miner</div><div class="detail-value">${tx.miner ? addrLinkFull(tx.miner) : '—'}</div></div>
    <div class="detail-row"><div class="detail-label">Confirmations</div><div class="detail-value">${confs}</div></div>`;
  if (tx.metadata && Object.keys(tx.metadata).length > 0) {
    if (tx.metadata.v === 2 && tx.metadata.activities) {
      html += '<div class="detail-row"><div class="detail-label">PoPW Evidence</div><div class="detail-value">';
      html += '<div style="font-size:10px;margin-bottom:4px;">';
      html += '<strong>Agent:</strong> '+esc(tx.metadata.agent||'—')+' &nbsp; ';
      html += '<strong>Activities:</strong> '+(tx.metadata.n||0)+' &nbsp; ';
      html += '<strong>Evidence:</strong> <code>'+esc(tx.metadata.ev||'')+'</code>';
      html += '</div>';
      html += '<table style="font-size:10px;"><tr><th>Tool</th><th>Category</th><th>Duration</th><th>Input Hash</th><th>Output Hash</th><th>Time</th></tr>';
      for (const a of tx.metadata.activities) {
        html += '<tr>';
        html += '<td><strong>'+esc(a.t||'')+'</strong></td>';
        html += '<td>'+esc(a.c||'')+'</td>';
        html += '<td>'+(a.ms||0)+'ms</td>';
        html += '<td><code style="font-size:9px;">'+(a.ih?esc(a.ih):'—')+'</code></td>';
        html += '<td><code style="font-size:9px;">'+(a.oh?esc(a.oh):'—')+'</code></td>';
        html += '<td>'+(a.ts?ts(a.ts):'—')+'</td>';
        html += '</tr>';
      }
      html += '</table></div></div>';
    } else {
      html += `<div class="detail-row"><div class="detail-label">Metadata</div><div class="detail-value" style="font-size:10px;">${esc(JSON.stringify(tx.metadata))}</div></div>`;
    }
  }
  html += '</div>';
  document.getElementById('txDetailBody').innerHTML = html;
  document.getElementById('txDetail').scrollIntoView({behavior:'smooth'});
}

// ── Address Detail + Transaction History (enhanced) ──
let _addrHistoryPage = 1;
let _currentAddr = '';

async function loadAddressWithHistory(addr) {
  if (!/^[a-fA-F0-9]+$/.test(addr) && !['SYSTEM','FAUCET','STAKE_POOL','DAO','DAO_TREASURY'].includes(addr)) return;
  _currentAddr = addr;
  _addrHistoryPage = 1;
  document.getElementById('addressDetailBody').innerHTML = '<div class="loading">Loading address...</div>';
  document.getElementById('addressDetail').style.display = 'block';
  document.getElementById('blockDetail').style.display = 'none';
  document.getElementById('txDetail').style.display = 'none';

  // Fetch balance and history in parallel
  const [balD, histD] = await Promise.all([
    fetchJSON(API+'/address/'+encodeURIComponent(addr)),
    fetchJSON(API+'/address/'+encodeURIComponent(addr)+'/history?page=1&limit=50')
  ]);

  let html = '<div class="detail-box">';
  html += '<div class="detail-row"><div class="detail-label">Address</div><div class="detail-value" style="font-size:10px;word-break:break-all;">'+esc(addr)+'</div></div>';
  if (balD.success) {
    html += '<div class="detail-row"><div class="detail-label">Balance</div><div class="detail-value"><span class="amount" style="font-size:16px;">'+balD.balance_credits.toFixed(2)+' CR</span></div></div>';
    html += '<div class="detail-row"><div class="detail-label">Staked</div><div class="detail-value">'+balD.stake_credits.toFixed(2)+' CR</div></div>';
    html += '<div class="detail-row"><div class="detail-label">Total (Bal+Stake)</div><div class="detail-value">'+(balD.balance_credits + balD.stake_credits).toFixed(2)+' CR</div></div>';
    html += '<div class="detail-row"><div class="detail-label">Reputation</div><div class="detail-value">'+balD.reputation.toFixed(2)+'</div></div>';
    html += '<div class="detail-row"><div class="detail-label">Nonce</div><div class="detail-value">'+balD.nonce+'</div></div>';
  }
  if (histD.success) {
    const recvCr = histD.total_received_cr !== undefined ? histD.total_received_cr.toFixed(2) : cr(histD.total_received_plancks || histD.total_received || 0);
    const sentCr = histD.total_sent_cr !== undefined ? histD.total_sent_cr.toFixed(2) : cr(histD.total_sent_plancks || histD.total_sent || 0);
    html += '<div class="detail-row"><div class="detail-label">Total Received</div><div class="detail-value"><span style="color:#228854;">'+recvCr+' CR</span></div></div>';
    html += '<div class="detail-row"><div class="detail-label">Total Sent</div><div class="detail-value"><span style="color:#af0a0f;">'+sentCr+' CR</span></div></div>';
    html += '<div class="detail-row"><div class="detail-label">TX Count</div><div class="detail-value">'+(histD.transaction_count || 0)+'</div></div>';
    if (histD.first_seen) html += '<div class="detail-row"><div class="detail-label">First Seen</div><div class="detail-value">'+ts(histD.first_seen)+' <span class="time-ago">('+timeAgo(histD.first_seen)+')</span></div></div>';
    if (histD.last_seen) html += '<div class="detail-row"><div class="detail-label">Last Active</div><div class="detail-value">'+ts(histD.last_seen)+' <span class="time-ago">('+timeAgo(histD.last_seen)+')</span></div></div>';
  }
  html += '</div>';

  // Transaction history
  html += '<h3 style="font-size:12px;margin:12px 0 4px;color:#34345C;">Transaction History</h3>';
  if (histD.success && histD.transactions && histD.transactions.length > 0) {
    html += renderAddrTxTable(histD.transactions, addr);
    html += renderPagination(histD.page || 1, histD.has_more, histD.transaction_count || 0);
  } else {
    html += '<div class="loading">No transactions found for this address</div>';
  }

  document.getElementById('addressDetailBody').innerHTML = html;
  document.getElementById('addressDetail').scrollIntoView({behavior:'smooth'});
}

function renderAddrTxTable(txs, addr) {
  let html = '<table><tr><th>TX Hash</th><th>Block</th><th>Type</th><th>Direction</th><th>Counterparty</th><th>Amount</th><th>Conf</th></tr>';
  for (const tx of txs) {
    const dir = tx.direction || (tx.from_address === addr ? 'sent' : 'received');
    const dirClass = 'direction-' + dir;
    const dirSymbol = dir === 'sent' ? '→ OUT' : dir === 'received' ? '← IN' : '↔ SELF';
    const counterparty = dir === 'sent' ? tx.to_address : tx.from_address;
    const amountCr = tx.amount_cr !== undefined ? tx.amount_cr : cr(tx.amount || 0);
    const confs = tx.confirmations !== undefined ? tx.confirmations : (_chainHeight > 0 && tx.block_index !== undefined ? _chainHeight - tx.block_index : '?');
    html += `<tr>
      <td>${txLink(tx.tx_hash)}</td>
      <td>${tx.block_index !== undefined ? blockLink(tx.block_index) : 'mempool'}</td>
      <td>${txTypeBadge(tx.tx_type)}</td>
      <td class="${dirClass}" style="font-weight:bold;font-size:10px;">${dirSymbol}</td>
      <td>${addrLink(counterparty)}</td>
      <td class="amount">${amountCr} CR</td>
      <td>${confirmBadge(confs)}</td>
    </tr>`;
  }
  html += '</table>';
  return html;
}

function renderPagination(currentPage, hasMore, totalCount) {
  let html = '<div class="pagination">';
  if (currentPage > 1) html += '<button onclick="loadAddrHistoryPage('+(currentPage-1)+')">← Prev</button>';
  html += '<span class="page-info">Page '+currentPage+' ('+totalCount+' total txs)</span>';
  if (hasMore) html += '<button onclick="loadAddrHistoryPage('+(currentPage+1)+')">Next →</button>';
  html += '</div>';
  return html;
}

async function loadAddrHistoryPage(page) {
  _addrHistoryPage = page;
  const histD = await fetchJSON(API+'/address/'+encodeURIComponent(_currentAddr)+'/history?page='+page+'&limit=50');
  if (!histD.success) return;
  // Update just the table and pagination
  const container = document.getElementById('addressDetailBody');
  const h3 = container.querySelector('h3');
  if (h3) {
    let html = renderAddrTxTable(histD.transactions || [], _currentAddr);
    html += renderPagination(histD.page || page, histD.has_more, histD.transaction_count || 0);
    // Replace everything after h3
    let sibling = h3.nextSibling;
    while (sibling) { const next = sibling.nextSibling; sibling.remove(); sibling = next; }
    h3.insertAdjacentHTML('afterend', html);
  }
}

// Keep legacy function name working
function loadAddress(addr) { loadAddressWithHistory(addr); }

// ── Mempool Viewer (NEW) ──
async function loadMempool() {
  document.getElementById('mempoolBody').innerHTML = '<div class="loading">Loading mempool...</div>';
  const d = await fetchJSON(API+'/mempool');
  if (!d.success) { document.getElementById('mempoolBody').innerHTML = '<div class="loading">Node offline</div>'; return; }
  const txs = d.pending_transactions || d.transactions || [];
  const outbox = d.popw_outbox || {count:0,batches:[]};
  const outboxHtml = renderPopwOutbox(outbox);
  if (txs.length === 0) {
    document.getElementById('mempoolBody').innerHTML =
      '<div class="loading">Rust mempool is empty — no pending on-chain transactions</div>' + outboxHtml;
    return;
  }
  let html = '<div style="margin-bottom:8px;font-size:11px;color:#89a;">'+txs.length+' pending transaction(s)</div>';
  html += '<table><tr><th>TX Hash</th><th>Type</th><th>From</th><th>To</th><th>Amount</th><th>Fee</th><th>Age</th></tr>';
  for (const tx of txs) {
    const amountCr = tx.amount_cr !== undefined ? tx.amount_cr : cr(tx.amount || 0);
    const feeCr = tx.fee ? cr(tx.fee) : '0.00';
    const age = tx.timestamp ? timeAgo(tx.timestamp) : '?';
    html += `<tr>
      <td>${txLink(tx.tx_hash)}</td>
      <td>${txTypeBadge(tx.tx_type || 'transfer')}</td>
      <td>${addrLink(tx.from_address)}</td>
      <td>${addrLink(tx.to_address)}</td>
      <td class="amount">${amountCr} CR</td>
      <td>${feeCr} CR</td>
      <td class="time-ago">${age}</td>
    </tr>`;
  }
  html += '</table>';
  document.getElementById('mempoolBody').innerHTML = html + outboxHtml;
}

function renderPopwOutbox(outbox) {
  const batches = outbox.batches || [];
  if (!outbox.count) return '';
  let html = `<h3 style="margin-top:18px;">PoPW Durable Outbox</h3>
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:8px;font-size:11px;color:#d6a85f;flex-wrap:wrap;">
      <span>${outbox.count} queued batch(es), ${outbox.total_cr || 0} CR waiting to enter Rust mempool</span>
      <button id="popwDrainBtn" onclick="drainPopwOutbox()" style="padding:6px 10px;border:1px solid #6b5426;background:#1d1a12;color:#f1c66d;border-radius:6px;cursor:pointer;">Mint queued PoPW</button>
    </div>`;
  html += '<table><tr><th>Batch</th><th>Wallet</th><th>Amount</th><th>Attempts</th><th>Last Error</th></tr>';
  for (const b of batches.slice(0, 25)) {
    html += `<tr>
      <td><code>${esc((b.popw_batch_id || '').substring(0,16))}</code></td>
      <td>${addrLink(b.wallet || '')}</td>
      <td class="amount">${b.amount_cr || 0} CR</td>
      <td>${b.attempts || 0}</td>
      <td>${esc(b.last_error || 'queued')}</td>
    </tr>`;
  }
  html += '</table>';
  return html;
}

async function drainPopwOutbox() {
  const btn = document.getElementById('popwDrainBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Submitting...'; }
  const d = await postJSON(API+'/popw/drain', {limit:500});
  if (!d.success) {
    alert('PoPW drain failed: ' + (d.error || 'unknown error'));
  }
  await loadMempool();
}

// ── Rich List (NEW) ──
async function loadRichList() {
  document.getElementById('richlistBody').innerHTML = '<div class="loading">Loading rich list...</div>';
  const d = await fetchJSON(API+'/richlist?limit=100');
  if (!d.success) { document.getElementById('richlistBody').innerHTML = '<div class="loading">Node offline</div>'; return; }
  const addrs = d.richlist || d.addresses || [];
  if (addrs.length === 0) {
    document.getElementById('richlistBody').innerHTML = '<div class="loading">No addresses with balance</div>';
    return;
  }
  let html = '<table><tr><th>#</th><th>Address</th><th>Balance</th><th>Staked</th><th>Total</th><th>% Supply</th></tr>';
  addrs.forEach((a, i) => {
    const balCr = a.balance_cr !== undefined ? a.balance_cr.toFixed(2) : cr(a.balance || 0);
    const stakeCr = a.stake_cr !== undefined ? a.stake_cr.toFixed(2) : '0.00';
    const totalCr = a.total_cr !== undefined ? a.total_cr.toFixed(2) : balCr;
    const pct = a.percent_supply !== undefined ? a.percent_supply.toFixed(4) : '?';
    html += `<tr>
      <td>${i + 1 + (d.offset || 0)}</td>
      <td>${addrLink(a.address)}</td>
      <td class="amount">${balCr} CR</td>
      <td>${stakeCr} CR</td>
      <td class="amount" style="font-size:12px;">${totalCr} CR</td>
      <td>${pct}%</td>
    </tr>`;
  });
  html += '</table>';
  document.getElementById('richlistBody').innerHTML = html;
}

// ── Enhanced Search (uses /api/search) ──
async function doSearch() {
  const q = document.getElementById('searchInput').value.trim();
  if (!q) return;

  // Use the universal search endpoint
  const d = await fetchJSON(API+'/search?q='+encodeURIComponent(q));
  if (d.success) {
    const t = d.type;
    if (t === 'block') { loadBlock(d.block.index !== undefined ? d.block.index : parseInt(q)); return; }
    if (t === 'transaction') { loadTx(d.transaction.tx_hash || q); return; }
    if (t === 'address') { loadAddressWithHistory(d.address.address || q); return; }
  }

  // Fallback: try as block index, then address
  if (/^\d+$/.test(q)) { loadBlock(parseInt(q)); return; }
  if (q.length >= 20) { loadAddressWithHistory(q); return; }

  // Nothing found
  alert('No results found for: ' + q);
}

// ── Wallet: Create ──
async function createWallet() {
  const walletType = document.getElementById('walletType').value;
  document.getElementById('createResult').innerHTML = '<div class="loading">Generating wallet (this may take a few seconds due to key stretching)...</div>';
  document.getElementById('walletBackup').style.display = 'none';
  try {
    const d = await postJSON(API+'/wallet/create', {wallet_type: walletType});
    if (d.success) {
      document.getElementById('createResult').innerHTML = `<div class="msg msg-ok">Wallet created! Type: ${esc(walletType)}. <b>Back up your keys below.</b></div>`;
      document.getElementById('newAddr').textContent = d.address;
      document.getElementById('newPrivKey').textContent = d.private_key;
      document.getElementById('newMnemonic').textContent = d.mnemonic;
      document.getElementById('walletBackup').style.display = 'block';
      // Auto-fill faucet and balance fields
      document.getElementById('faucetAddr').value = d.address;
      document.getElementById('balAddr').value = d.address;
    } else {
      document.getElementById('createResult').innerHTML = `<div class="msg msg-err">${esc(d.error)}</div>`;
    }
  } catch(e) {
    document.getElementById('createResult').innerHTML = `<div class="msg msg-err">Error: ${esc(e.message)}</div>`;
  }
}

// ── Wallet: Recover from Mnemonic ──
async function recoverWallet() {
  const mnemonic = document.getElementById('recoverMnemonic').value.trim();
  if (!mnemonic) return;
  const words = mnemonic.split(/\s+/);
  if (words.length !== 24) {
    document.getElementById('recoverResult').innerHTML = '<div class="msg msg-err">Recovery phrase must be exactly 24 words</div>';
    return;
  }
  document.getElementById('recoverResult').innerHTML = '<div class="loading">Recovering wallet (key derivation in progress)...</div>';
  try {
    const d = await postJSON(API+'/wallet/recover', {mnemonic: mnemonic});
    if (d.success) {
      document.getElementById('recoverResult').innerHTML = `
        <div class="msg msg-ok">Wallet recovered!</div>
        <div class="detail-box" style="margin-top:4px;">
          <div class="detail-row"><div class="detail-label">Address</div><div class="detail-value">${esc(d.address)}</div></div>
          <div class="detail-row"><div class="detail-label">Private Key</div><div class="detail-value" style="color:#af0a0f;">${esc(d.private_key)}</div></div>
        </div>
      `;
      document.getElementById('balAddr').value = d.address;
      document.getElementById('faucetAddr').value = d.address;
    } else {
      document.getElementById('recoverResult').innerHTML = `<div class="msg msg-err">${esc(d.error)}</div>`;
    }
  } catch(e) {
    document.getElementById('recoverResult').innerHTML = `<div class="msg msg-err">Error: ${esc(e.message)}</div>`;
  }
}

// ── Wallet: Balance ──
async function checkBalance() {
  const addr = document.getElementById('balAddr').value.trim();
  if (!addr) return;
  const d = await fetchJSON(API+'/address/'+addr);
  document.getElementById('balResult').innerHTML = d.success
    ? `<div class="msg msg-ok">Balance: <b>${d.balance_credits.toFixed(2)} CR</b> | Staked: ${d.stake_credits.toFixed(2)} CR</div>`
    : `<div class="msg msg-err">${esc(d.error)}</div>`;
}

// ── Wallet: Faucet ──
async function claimFaucet() {
  const addr = document.getElementById('faucetAddr').value.trim();
  if (!addr) return;
  const d = await postJSON(API+'/faucet', {address:addr});
  document.getElementById('faucetResult').innerHTML = d.success
    ? `<div class="msg msg-ok">Received ${d.amount_sent} CR! Balance: ${d.new_balance.toFixed(2)} CR. Faucet remaining: ${d.faucet_remaining_cr.toLocaleString()} CR</div>`
    : `<div class="msg msg-err">${esc(d.error)}</div>`;
}

// ── Wallet: Send (Client-Side Ed25519 Signing) ──
// SHA3 (Keccak) compact implementation for tx hashing + address derivation
const SHA3=(()=>{const RC=[1n,0x8082n,0x800000000000808an,0x8000000080008000n,0x808bn,0x80000001n,0x8000000080008081n,0x8000000000008009n,0x8an,0x88n,0x80008009n,0x8000000an,0x8000808bn,0x800000000000008bn,0x8000000000008089n,0x8000000000008003n,0x8000000000008002n,0x8000000000000080n,0x800an,0x800000008000000an,0x8000000080008081n,0x8000000000008080n,0x80000001n,0x8000000080008008n];
const R=[0,1,62,28,27,36,44,6,55,20,3,10,43,25,39,41,45,15,21,8,18,2,61,56,14];
const PI=[0,10,20,5,15,16,1,11,21,6,7,17,2,12,22,23,8,18,3,13,14,24,9,19,4];
function keccakf(st){for(let r=0;r<24;r++){const C=[];for(let x=0;x<5;x++)C[x]=st[x]^st[x+5]^st[x+10]^st[x+15]^st[x+20];const D=[];for(let x=0;x<5;x++)D[x]=C[(x+4)%5]^((C[(x+1)%5]<<1n)|(C[(x+1)%5]>>63n))&((1n<<64n)-1n);for(let x=0;x<5;x++)for(let y=0;y<5;y++)st[x+5*y]^=D[x];const B=new Array(25);for(let i=0;i<25;i++){const r1=R[i];B[PI[i]]=(r1?((st[i]<<BigInt(r1))|(st[i]>>BigInt(64-r1)))&((1n<<64n)-1n):st[i]);}for(let y=0;y<5;y++)for(let x=0;x<5;x++)st[x+5*y]=B[x+5*y]^((~B[(x+1)%5+5*y])&B[(x+2)%5+5*y])&((1n<<64n)-1n);st[0]^=RC[r];}}
function sha3(bits,msg){const rate=(1600-bits*2)/8;const st=new Array(25).fill(0n);const dv=new DataView(new ArrayBuffer(8));
const pad=new Uint8Array(rate);pad.set(msg.slice((msg.length/rate|0)*rate));const off=msg.length%rate;
if(off<rate)pad[off]^=0x06;pad[rate-1]^=0x80;
for(let i=0;i<msg.length-off;i+=rate){const blk=msg.slice(i,i+rate);for(let j=0;j<rate;j+=8){dv.setUint8(0,blk[j]||0);dv.setUint8(1,blk[j+1]||0);dv.setUint8(2,blk[j+2]||0);dv.setUint8(3,blk[j+3]||0);dv.setUint8(4,blk[j+4]||0);dv.setUint8(5,blk[j+5]||0);dv.setUint8(6,blk[j+6]||0);dv.setUint8(7,blk[j+7]||0);st[j/8]^=dv.getBigUint64(0,true);}keccakf(st);}
for(let j=0;j<rate;j+=8){dv.setUint8(0,pad[j]||0);dv.setUint8(1,pad[j+1]||0);dv.setUint8(2,pad[j+2]||0);dv.setUint8(3,pad[j+3]||0);dv.setUint8(4,pad[j+4]||0);dv.setUint8(5,pad[j+5]||0);dv.setUint8(6,pad[j+6]||0);dv.setUint8(7,pad[j+7]||0);st[j/8]^=dv.getBigUint64(0,true);}keccakf(st);
const out=new Uint8Array(bits/8);const odv=new DataView(out.buffer);for(let i=0;i<bits/8;i+=8)odv.setBigUint64(i,st[i/8],true);return out;}
return{sha3_256:m=>sha3(256,m),sha3_512:m=>sha3(512,m)};})();

function hexEncode(u8){return Array.from(u8).map(b=>b.toString(16).padStart(2,'0')).join('');}
function hexDecode(h){const a=new Uint8Array(h.length/2);for(let i=0;i<h.length;i+=2)a[i/2]=parseInt(h.substr(i,2),16);return a;}

function pyJsonDumps(v){
  if(v===null)return'null';
  if(typeof v==='number')return Number.isInteger(v)?String(v):String(v);
  if(typeof v==='boolean')return v?'true':'false';
  if(typeof v==='string')return JSON.stringify(v);
  if(Array.isArray(v))return'['+v.map(pyJsonDumps).join(', ')+']';
  const keys=Object.keys(v).sort();
  return'{'+keys.map(k=>JSON.stringify(k)+': '+pyJsonDumps(v[k])).join(', ')+'}';
}

async function signCanonicalTx(txType, from, to, amountPlancks, nonce, privKeyHex){
  const privBytes = hexDecode(privKeyHex);
  const pkcs8 = new Uint8Array([0x30,0x2e,0x02,0x01,0x00,0x30,0x05,0x06,0x03,0x2b,0x65,0x70,0x04,0x22,0x04,0x20,...privBytes]);
  const keyPair = await crypto.subtle.importKey('pkcs8', pkcs8, {name:'Ed25519'}, true, ['sign']);
  const jwk = await crypto.subtle.exportKey('jwk', keyPair);
  const pubBytes = new Uint8Array(atob(jwk.x.replace(/-/g,'+').replace(/_/g,'/')).split('').map(c=>c.charCodeAt(0)));
  const pubHex = hexEncode(pubBytes);
  const derivedAddr = hexEncode(SHA3.sha3_256(pubBytes)).slice(0,40);
  if (derivedAddr !== from) throw new Error('Private key does not match address');

  const timestamp = Date.now() / 1000;
  const metadata = {};
  const txVersion = 1;
  const txData = {
    amount: amountPlancks,
    from: from,
    metadata: metadata,
    nonce: nonce,
    timestamp: timestamp,
    to: to,
    type: txType
  };
  const txHashBytes = SHA3.sha3_512(new TextEncoder().encode(pyJsonDumps(txData)));
  const signature = await crypto.subtle.sign({name:'Ed25519'}, keyPair, txHashBytes);
  return {
    signature: hexEncode(new Uint8Array(signature)),
    public_key: pubHex,
    timestamp: timestamp,
    metadata: metadata,
    tx_version: txVersion
  };
}

async function sendCredits() {
  const from = document.getElementById('sendFrom').value.trim();
  const to = document.getElementById('sendTo').value.trim();
  const amount = parseFloat(document.getElementById('sendAmount').value);
  const privKeyHex = document.getElementById('sendPrivKey').value.trim();
  if (!from || !to || !amount) return;
  if (!privKeyHex || privKeyHex.length !== 64) {
    document.getElementById('sendResult').innerHTML = '<div class="msg msg-err">Enter a valid 32-byte (64 hex) private key</div>';
    return;
  }
  try {
    const nonceResp = await fetchJSON(API+'/address/'+from);
    const nonce = nonceResp.success ? (nonceResp.nonce || 0) : 0;
    const amountPlancks = Math.round(amount * 100000000);
    const signed = await signCanonicalTx('transfer', from, to, amountPlancks, nonce, privKeyHex);

    const d = await postJSON(API+'/transfer', {
      from_address: from,
      to_address: to,
      amount_credits: amount,
      signature: signed.signature,
      public_key: signed.public_key,
      nonce: nonce,
      timestamp: signed.timestamp,
      tx_version: signed.tx_version,
      metadata: signed.metadata,
    });
    document.getElementById('sendResult').innerHTML = d.success
      ? `<div class="msg msg-ok">Sent ${amount} CR! TX signed client-side with Ed25519. From balance: ${d.from_balance.toFixed(2)} CR</div>`
      : `<div class="msg msg-err">${esc(d.error)}</div>`;
    // Clear private key from input immediately
    document.getElementById('sendPrivKey').value = '';
  } catch(e) {
    document.getElementById('sendResult').innerHTML = `<div class="msg msg-err">Signing failed: ${esc(e.message)}. Your browser may not support Ed25519 Web Crypto.</div>`;
  }
}

// ── Leaderboard ──
async function loadLeaderboard() {
  const d = await fetchJSON(API+'/leaderboard');
  if (!d.success || !d.leaderboard.length) { document.getElementById('leaderboard').innerHTML='<div class="loading">No data</div>'; return; }
  let html = '<table><tr><th>#</th><th>Address</th><th>Balance</th></tr>';
  d.leaderboard.forEach((w,i) => {
    html += `<tr>
      <td>${i+1}</td>
      <td>${addrLink(w.address)}</td>
      <td class="amount">${w.balance_credits.toFixed(2)} CR</td>
    </tr>`;
  });
  html += '</table>';
  document.getElementById('leaderboard').innerHTML = html;
}

// ── Compute Info ──
async function loadComputeInfo() {
  const d = await fetchJSON(API+'/compute');
  if (!d.success) { document.getElementById('computeInfo').innerHTML='<div class="loading">Node offline</div>'; return; }
  document.getElementById('computeInfo').innerHTML = `
    <div class="stats-grid">
      <div class="stat-card"><div class="label">Device</div><div class="value" style="font-size:14px;">${d.device_name||'Unknown'}</div><div class="sub">${d.device_type||'cpu'}${d.node_role ? ' — '+d.node_role : ''}</div></div>
      <div class="stat-card"><div class="label">Est. TFLOPS</div><div class="value">${(d.tflops_measured||0).toFixed(2)}</div><div class="sub">FP16: ${(d.tflops_fp16||0).toFixed(2)} / FP32: ${(d.tflops_fp32||0).toFixed(2)}</div></div>
      <div class="stat-card"><div class="label">Contributing</div><div class="value">${((d.compute_share||0)*100).toFixed(0)}%</div><div class="sub">${(d.tflops_effective||0).toFixed(2)} effective TFLOPS</div></div>
      <div class="stat-card"><div class="label">${d.memory_label||'Memory'}</div><div class="value">${(d.memory_gb||0).toFixed(1)} GB</div></div>
    </div>
  `;
  document.getElementById('computeSlider').value = Math.round((d.compute_share||1)*100);
  updateSliderLabel();
}

function updateSliderLabel() {
  const v = document.getElementById('computeSlider').value;
  document.getElementById('sliderLabel').textContent = v + '%';
}

async function setComputeShare() {
  const v = parseInt(document.getElementById('computeSlider').value) / 100;
  const d = await postJSON(API+'/compute/share', {compute_share:v});
  if (d.success) {
    const restart = d.requires_chain_restart ? ' Chain restart required for active node.' : '';
    document.getElementById('computeResult').innerHTML = `<div class="msg msg-ok">Now contributing ${(d.compute_share*100).toFixed(0)}% — ${d.tflops_effective.toFixed(2)} TFLOPS of ${d.tflops_total.toFixed(2)} TFLOPS.${restart}</div>`;
    loadComputeInfo();
  } else {
    document.getElementById('computeResult').innerHTML = `<div class="msg msg-err">${esc(d.error)}</div>`;
  }
}

// ── Staking: View ──
async function loadStakeInfo() {
  const addr = document.getElementById('stakeViewAddr').value.trim();
  if (!addr) return;
  const d = await fetchJSON(API+'/address/'+addr);
  if (!d.success) { document.getElementById('stakeOverview').innerHTML='<div class="msg msg-err">'+esc(d.error)+'</div>'; return; }
  document.getElementById('stakeOverview').innerHTML = `
    <div class="stats-grid">
      <div class="stat-card"><div class="label">Available Balance</div><div class="value">${d.balance_credits.toFixed(2)}</div><div class="sub">CR</div></div>
      <div class="stat-card"><div class="label">Staked</div><div class="value">${d.stake_credits.toFixed(2)}</div><div class="sub">CR</div></div>
      <div class="stat-card"><div class="label">Reputation</div><div class="value">${d.reputation.toFixed(2)}</div></div>
    </div>
  `;
  // Pre-fill addresses
  document.getElementById('stakeAddr').value = addr;
  document.getElementById('unstakeAddr').value = addr;
}

async function doStake() {
  const addr = document.getElementById('stakeAddr').value.trim();
  const amount = parseFloat(document.getElementById('stakeAmount').value);
  const privKeyHex = document.getElementById('stakePrivKey').value.trim();
  if (!addr || !amount || amount <= 0) return;
  if (!privKeyHex || privKeyHex.length !== 64) {
    document.getElementById('stakeResult').innerHTML = '<div class="msg msg-err">Enter a valid 32-byte (64 hex) private key</div>';
    return;
  }
  try {
    const amountPlancks = Math.round(amount * 100000000);
    const nonceResp = await fetchJSON(API+'/address/'+addr);
    const nonce = nonceResp.success ? (nonceResp.nonce || 0) : 0;
    const signed = await signCanonicalTx('stake', addr, addr, amountPlancks, nonce, privKeyHex);
    const d = await postJSON(API+'/stake', {
      address:addr,
      amount_plancks:amountPlancks,
      nonce:nonce,
      signature:signed.signature,
      public_key:signed.public_key,
      timestamp:signed.timestamp,
      tx_version:signed.tx_version,
      metadata:signed.metadata
    });
    document.getElementById('stakeResult').innerHTML = d.success
      ? `<div class="msg msg-ok">Staked ${d.staked_credits} CR! Total stake: ${d.total_stake} CR</div>`
      : `<div class="msg msg-err">${esc(d.error)}</div>`;
    document.getElementById('stakePrivKey').value = '';
    if (d.success) loadStakeInfo();
  } catch(e) {
    document.getElementById('stakeResult').innerHTML = `<div class="msg msg-err">${esc(e.message)}</div>`;
  }
}

async function doUnstake() {
  const addr = document.getElementById('unstakeAddr').value.trim();
  const amount = parseFloat(document.getElementById('unstakeAmount').value);
  const privKeyHex = document.getElementById('unstakePrivKey').value.trim();
  if (!addr || !amount || amount <= 0) return;
  if (!privKeyHex || privKeyHex.length !== 64) {
    document.getElementById('unstakeResult').innerHTML = '<div class="msg msg-err">Enter a valid 32-byte (64 hex) private key</div>';
    return;
  }
  try {
    const amountPlancks = Math.round(amount * 100000000);
    const nonceResp = await fetchJSON(API+'/address/'+addr);
    const nonce = nonceResp.success ? (nonceResp.nonce || 0) : 0;
    const signed = await signCanonicalTx('stake_withdraw', addr, addr, amountPlancks, nonce, privKeyHex);
    const d = await postJSON(API+'/unstake', {
      address:addr,
      amount_plancks:amountPlancks,
      nonce:nonce,
      signature:signed.signature,
      public_key:signed.public_key,
      timestamp:signed.timestamp,
      tx_version:signed.tx_version,
      metadata:signed.metadata
    });
    document.getElementById('unstakeResult').innerHTML = d.success
      ? `<div class="msg msg-ok">Unstaked ${d.unstaked_credits} CR! Remaining stake: ${d.remaining_stake} CR</div>`
      : `<div class="msg msg-err">${esc(d.error)}</div>`;
    document.getElementById('unstakePrivKey').value = '';
    if (d.success) loadStakeInfo();
  } catch(e) {
    document.getElementById('unstakeResult').innerHTML = `<div class="msg msg-err">${esc(e.message)}</div>`;
  }
}

async function loadTopStakers() {
  const d = await fetchJSON(API+'/stakers');
  if (!d.success || !d.stakers.length) { document.getElementById('topStakers').innerHTML='<div class="loading">No stakers yet</div>'; return; }
  let html = '<table><tr><th>#</th><th>Address</th><th>Staked</th></tr>';
  d.stakers.forEach((s,i) => {
    html += `<tr onclick="document.getElementById('stakeViewAddr').value='${esc(s.address)}';loadStakeInfo()">
      <td>${i+1}</td>
      <td class="addr">${shortAddr(s.address)}</td>
      <td class="amount">${s.stake_credits.toFixed(2)} CR</td>
    </tr>`;
  });
  html += '</table>';
  document.getElementById('topStakers').innerHTML = html;
}

// ── Mining Dashboard ──
async function loadMiningStats() {
  const d = await fetchJSON(API+'/mining-stats');
  if (!d.success) { document.getElementById('miningStats').innerHTML='<div class="loading">Node offline</div>'; return; }
  const blocksToHalving = d.blocks_to_halving;
  const secsToHalving = blocksToHalving * d.block_time_seconds;
  const daysToHalving = (secsToHalving / 86400).toFixed(0);
  const dailyAvail = (d.availability_reward_cr * d.staker_count * (86400 / d.block_time_seconds)).toFixed(2);
  document.getElementById('miningStats').innerHTML = `
    <div class="stat-card"><div class="label">Block Height</div><div class="value">${d.block_height}</div></div>
    <div class="stat-card"><div class="label">Block Reward</div><div class="value">${d.current_reward_cr.toFixed(2)}</div><div class="sub">CR per block</div></div>
    <div class="stat-card"><div class="label">Halvings</div><div class="value">${d.halvings_completed}</div><div class="sub">${d.next_halving_block.toLocaleString()} next</div></div>
    <div class="stat-card"><div class="label">To Next Halving</div><div class="value">${blocksToHalving.toLocaleString()}</div><div class="sub">~${daysToHalving} days</div></div>
    <div class="stat-card"><div class="label">Supply Mined</div><div class="value">${d.supply_percent.toFixed(4)}%</div><div class="sub">${d.total_supply_cr.toFixed(2)} / ${d.max_supply_cr.toLocaleString()} CR</div></div>
    <div class="stat-card"><div class="label">Total Staked</div><div class="value">${d.total_staked_cr.toFixed(2)}</div><div class="sub">${d.staker_count} staker(s)</div></div>
    <div class="stat-card"><div class="label">Avail. Reward</div><div class="value">${d.availability_reward_cr}</div><div class="sub">CR/block/staker</div></div>
    <div class="stat-card"><div class="label">Block Time</div><div class="value">${d.block_time_seconds}s</div><div class="sub">~${(86400/d.block_time_seconds).toFixed(0)} blocks/day</div></div>
  `;
  // Reward schedule
  let schedHtml = '<table><tr><th>Halving</th><th>Blocks</th><th>Block Reward</th><th>Status</th></tr>';
  let reward = 10;
  for (let h=0; h<10; h++) {
    const from = h * 420000;
    const to = (h+1) * 420000 - 1;
    const status = d.block_height >= from && d.block_height <= to ? '<b style="color:#228854;">CURRENT</b>' : (d.block_height > to ? 'Completed' : 'Future');
    schedHtml += `<tr><td>${h}</td><td>${from.toLocaleString()} – ${to.toLocaleString()}</td><td class="amount">${reward.toFixed(2)} CR</td><td>${status}</td></tr>`;
    reward /= 2;
  }
  schedHtml += '</table>';
  document.getElementById('rewardSchedule').innerHTML = schedHtml;
}

// ── DAO Governance ──
async function loadDAO() {
  loadTreasury();
  loadProposals();
}

async function loadTreasury() {
  const d = await fetchJSON(API+'/dao/treasury');
  if (!d.success) { document.getElementById('daoTreasury').innerHTML='<div class="loading">Node offline</div>'; return; }
  document.getElementById('daoTreasury').innerHTML = `
    <div class="stats-grid">
      <div class="stat-card"><div class="label">Treasury Balance</div><div class="value">${d.treasury_cr.toFixed(2)}</div><div class="sub">CR</div></div>
      <div class="stat-card"><div class="label">Total Proposals</div><div class="value">${d.total_proposals}</div><div class="sub">${d.active_proposals} active</div></div>
      <div class="stat-card"><div class="label">Executed</div><div class="value">${d.executed_proposals}</div></div>
      <div class="stat-card"><div class="label">Total Allocated</div><div class="value">${d.total_allocated_cr.toFixed(2)}</div><div class="sub">CR disbursed</div></div>
    </div>
    <div style="margin-top:8px;font-size:11px;color:#89a;">
      <b>How it works:</b> 5% of every block reward flows to the DAO treasury. Anyone can propose how to spend it. Proposals need ${3} votes (quorum) and >51% approval to pass. Vote weight = your stake (min 1 for non-stakers).
    </div>
  `;
}

async function loadProposals() {
  const d = await fetchJSON(API+'/dao/proposals');
  if (!d.success || !d.proposals || d.proposals.length === 0) {
    document.getElementById('daoProposals').innerHTML='<div class="loading">No proposals yet. Be the first to submit one!</div>';
    return;
  }
  let html = '<table><tr><th>ID</th><th>Title</th><th>Amount</th><th>Status</th><th>Votes For</th><th>Votes Against</th><th>Action</th></tr>';
  d.proposals.forEach(p => {
    const amt = (p.amount_plancks / 100000000).toFixed(2);
    const statusClass = p.status === 'active' ? 'color:#3498db' : p.status === 'executed' ? 'color:#228854' : 'color:#c95';
    const shortId = p.id.substring(0, 8) + '...';
    let actions = '';
    if (p.status === 'active') {
      actions = `<button onclick="voteOnProposal('${esc(p.id)}','for')" style="padding:2px 6px;font-size:10px;margin:1px;">👍</button>
                 <button onclick="voteOnProposal('${esc(p.id)}','against')" style="padding:2px 6px;font-size:10px;margin:1px;">👎</button>
                 <button onclick="executeProposal('${esc(p.id)}')" style="padding:2px 6px;font-size:10px;margin:1px;">⚡</button>`;
    }
    html += `<tr>
      <td style="font-family:monospace;font-size:10px;">${shortId}</td>
      <td><b>${esc(p.title)}</b><br><span style="font-size:10px;color:#89a;">${esc(p.description||'').substring(0,60)}</span></td>
      <td class="amount">${amt} CR</td>
      <td style="${statusClass};font-weight:bold;">${p.status}</td>
      <td style="color:#228854;font-weight:bold;">${p.votes_for}</td>
      <td style="color:#c44;font-weight:bold;">${p.votes_against}</td>
      <td>${actions}</td>
    </tr>`;
  });
  html += '</table>';
  document.getElementById('daoProposals').innerHTML = html;
}

async function submitProposal() {
  const title = document.getElementById('propTitle').value.trim();
  const desc = document.getElementById('propDesc').value.trim();
  const amount = parseFloat(document.getElementById('propAmount').value);
  const recipient = document.getElementById('propRecipient').value.trim();
  const proposer = document.getElementById('propProposer').value.trim();
  if (!title || !amount || amount <= 0 || !recipient || !proposer) {
    document.getElementById('propResult').innerHTML = '<div class="msg msg-err">Fill in all fields</div>';
    return;
  }
  const amountPlancks = Math.round(amount * 100000000);
  const d = await postJSON(API+'/dao/propose', {proposer, title, description:desc, amount_plancks:amountPlancks, recipient});
  if (d.success) {
    document.getElementById('propResult').innerHTML = `<div class="msg msg-ok">Proposal submitted! ID: ${d.proposal_id}</div>`;
    document.getElementById('propTitle').value = '';
    document.getElementById('propDesc').value = '';
    document.getElementById('propAmount').value = '';
    loadDAO();
  } else {
    document.getElementById('propResult').innerHTML = `<div class="msg msg-err">${esc(d.error)}</div>`;
  }
}

async function voteOnProposal(proposalId, direction) {
  const voter = prompt('Enter your wallet address to vote:');
  if (!voter) return;
  const d = await postJSON(API+'/dao/vote', {proposal_id:proposalId, voter:voter.trim(), direction});
  if (d.success) {
    loadProposals();
  } else {
    alert('Vote failed: ' + (d.error || 'Unknown error'));
  }
}

async function executeProposal(proposalId) {
  if (!confirm('Execute this proposal? This will transfer funds from the DAO treasury.')) return;
  const d = await postJSON(API+'/dao/execute', {proposal_id:proposalId});
  if (d.success) {
    alert('Proposal executed! ' + d.amount_plancks/100000000 + ' CR sent to ' + d.recipient);
    loadDAO();
  } else {
    alert('Execution failed: ' + (d.error || 'Unknown error'));
  }
}

// ── Init ──
loadStats();
loadBlocks();
setInterval(loadStats, 30000);
setInterval(loadBlocks, 69000);
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════
# API Endpoints (JSON) — called by the frontend JS
# ═══════════════════════════════════════════════════════════════════════

@explorer_bp.route("/")
def explorer_home():
    return render_template_string(EXPLORER_HTML)


@explorer_bp.route("/api/stats")
def api_stats():
    """Network-level statistics."""
    info = _rpc("get_chain_info")
    if "error" in info:
        return jsonify({"success": False, "error": info["error"]})
    net = _rpc("get_network_stats")
    mempool = _rpc("get_mempool_txs")
    mempool_size = mempool.get("count", 0) if "error" not in mempool else 0
    latest_ts = float(info.get("latest_timestamp") or 0)
    latest_age = max(0.0, time.time() - latest_ts) if latest_ts > 0 else None
    peer_count = net.get("peers", info.get("peer_count", 0)) if "error" not in net else info.get("peer_count", 0)
    height_lag = net.get("height_lag", info.get("height_lag", 0)) if "error" not in net else info.get("height_lag", 0)
    stale = latest_age is None or latest_age > 15 * 60
    warnings = []
    if stale:
        if latest_age is None:
            warnings.append("Chain freshness unknown. The local node has not reported a latest block timestamp.")
        else:
            warnings.append(f"Local chain is stale: latest block is {int(latest_age // 60)} minute(s) old.")
    if peer_count == 0:
        warnings.append("No blockchain peers connected. The app will keep retrying, but transactions and balances may be stale.")
    if height_lag and height_lag > 2:
        warnings.append(f"Local node is {height_lag} block(s) behind the best known peer.")
    return jsonify({
        "success": True,
        "chain_length": info.get("height", 0),
        "total_supply": info.get("current_supply_plancks", 0),
        "total_accounts": net.get("active_wallets", 0),
        "peer_count": peer_count,
        "pending_txs": mempool_size,
        "network_tflops": 0,
        "latest_block_timestamp": latest_ts,
        "latest_block_age_seconds": latest_age,
        "chain_stale": stale,
        "height_lag": height_lag,
        "best_peer_height": net.get("best_peer_height", info.get("best_peer_height", 0)) if "error" not in net else info.get("best_peer_height", 0),
        "sync_state": net.get("sync_state", info.get("sync_state", "unknown")) if "error" not in net else info.get("sync_state", "unknown"),
        "warnings": warnings,
    })


@explorer_bp.route("/api/network/peers")
def api_network_peers():
    """Known public nodes from the bootstrap registry plus local active peer count."""
    snapshot = _bootstrap_peer_snapshot()
    net = _rpc("get_network_stats")
    active_peer_count = 0 if "error" in net else net.get("peers", 0)
    peers = snapshot.get("peers", [])
    heights = []
    for peer in peers:
        try:
            heights.append(int(peer.get("chain_height", 0)))
        except (TypeError, ValueError):
            continue
    height_drift = max(heights) - min(heights) if len(heights) >= 2 else 0
    warnings = []
    if active_peer_count == 0 and len(peers) > 1:
        warnings.append("Network nodes are announced, but this local node has 0 active peers.")
    if height_drift > 3:
        warnings.append(
            f"Network nodes are not actively synced: announced height drift is {height_drift} blocks."
        )
    if snapshot.get("error"):
        return jsonify({
            "success": False,
            "bootstrap_url": snapshot.get("bootstrap_url", ""),
            "active_peer_count": active_peer_count,
            "bootstrap_peer_count": 0 if "error" in net else net.get("bootstrap_peers", 0),
            "height_drift": height_drift,
            "fork_status": "unknown" if "error" in net else net.get("fork_status", "unknown"),
            "mining_state": "unknown" if "error" in net else net.get("mining_state", "unknown"),
            "mining_pause_reason": "" if "error" in net else net.get("mining_pause_reason", ""),
            "checkpoint_status": "unknown" if "error" in net else net.get("checkpoint_status", "unknown"),
            "peer_diagnostics": [] if "error" in net else net.get("peer_diagnostics", []),
            "warnings": warnings,
            "peers": [],
            "error": snapshot["error"],
        })
    return jsonify({
        "success": True,
        "bootstrap_url": snapshot.get("bootstrap_url", ""),
        "active_peer_count": active_peer_count,
        "bootstrap_peer_count": 0 if "error" in net else net.get("bootstrap_peers", 0),
        "known_peer_count": len(peers),
        "height_drift": height_drift,
        "fork_status": "unknown" if "error" in net else net.get("fork_status", "unknown"),
        "mining_state": "unknown" if "error" in net else net.get("mining_state", "unknown"),
        "mining_pause_reason": "" if "error" in net else net.get("mining_pause_reason", ""),
        "checkpoint_status": "unknown" if "error" in net else net.get("checkpoint_status", "unknown"),
        "checkpoint_height": None if "error" in net else net.get("checkpoint_height"),
        "checkpoint_hash": "" if "error" in net else net.get("checkpoint_hash", ""),
        "peer_diagnostics": [] if "error" in net else net.get("peer_diagnostics", []),
        "warnings": warnings,
        "peers": peers,
    })


@explorer_bp.route("/api/blocks")
def api_blocks():
    """Latest N blocks (default 20)."""
    count = min(int(request.args.get("count", 20)), 100)
    resp = _rpc("get_chain_height")
    if "error" in resp:
        return jsonify({"success": False, "error": "Node unreachable"})

    height = resp.get("height", 0)
    start = max(0, height - count)
    resp2 = _rpc("get_blocks", {"start": start, "end": height})
    if "error" in resp2:
        return jsonify({"success": False, "error": "Block fetch failed"})

    blocks = []
    for bd in reversed(resp2.get("blocks", [])):
        blocks.append({
            "index": bd["index"],
            "hash": bd.get("hash", ""),
            "previous_hash": bd.get("previous_hash", ""),
            "timestamp": bd.get("timestamp", 0),
            "miner_address": bd.get("miner", ""),
            "tx_count": bd.get("transaction_count", 0),
        })
    return jsonify({"success": True, "blocks": blocks})


@explorer_bp.route("/api/block/<int:index>")
def api_block(index):
    """Single block detail with transactions."""
    bd = _rpc("get_block", {"index": index})
    if "error" in bd:
        return jsonify({"success": False, "error": "Block not found"})
    txs = []
    for tx in bd.get("transactions", []):
        txs.append({
            "tx_hash": tx.get("tx_hash", ""),
            "tx_type": tx.get("tx_type", ""),
            "from_address": tx.get("from_address", ""),
            "to_address": tx.get("to_address", ""),
            "amount": tx.get("amount", 0),
            "amount_cr": tx.get("amount_cr", 0),
            "timestamp": tx.get("timestamp", 0),
            "nonce": tx.get("nonce", 0),
            "metadata": tx.get("metadata", {}),
        })
    return jsonify({
        "success": True,
        "block": {
            "index": bd.get("index", index),
            "hash": bd.get("hash", ""),
            "previous_hash": bd.get("previous_hash", ""),
            "timestamp": bd.get("timestamp", 0),
            "miner_address": bd.get("miner", ""),
            "proof_of_power": bd.get("proof_of_power", {}),
            "transactions": txs,
        },
    })


@explorer_bp.route("/api/address/<address>")
def api_address(address):
    """Address balance and details."""
    resp = _rpc("get_balance", {"address": address})
    if "error" in resp:
        return jsonify({"success": False, "error": resp.get("error", "Query failed")})
    return jsonify({
        "success": True,
        "address": address,
        "balance_credits": resp.get("balance_cr", 0),
        "stake_credits": resp.get("stake_cr", 0),
        "reputation": resp.get("reputation", 0.5),
        "nonce": resp.get("nonce", 0),
    })


@explorer_bp.route("/api/tx/<tx_hash>")
def api_tx(tx_hash):
    """Transaction detail by hash."""
    resp = _rpc("get_transaction", {"tx_hash": tx_hash})
    if "error" in resp:
        return jsonify({"success": False, "error": resp.get("error", "Transaction not found")})
    return jsonify({"success": True, **resp})


@explorer_bp.route("/api/address/<address>/history")
def api_address_history(address):
    """Paginated transaction history for an address."""
    page = int(request.args.get("page", 1))
    limit = min(int(request.args.get("limit", 50)), 200)
    resp = _rpc("get_address_history", {"address": address, "page": page, "limit": limit})
    if "error" in resp:
        return jsonify({"success": False, "error": resp.get("error", "Query failed")})
    return jsonify({"success": True, **resp})


@explorer_bp.route("/api/mempool")
def api_mempool():
    """Pending transactions in the mempool."""
    resp = _rpc("get_mempool_txs")
    if "error" in resp:
        return jsonify({"success": False, "error": resp.get("error", "Node unreachable")})
    popw_outbox = {"count": 0, "total_cr": 0.0, "batches": []}
    try:
        from repryntt.economy.proof_of_productive_work import get_popw_minter
        popw_outbox = get_popw_minter().get_outbox_status(limit=25)
    except Exception as exc:
        popw_outbox = {"count": 0, "total_cr": 0.0, "batches": [], "error": str(exc)}
    return jsonify({"success": True, **resp, "popw_outbox": popw_outbox})


@explorer_bp.route("/api/popw/drain", methods=["POST"])
def api_popw_drain():
    """Manually push queued local PoPW batches into the Rust mempool."""
    try:
        body = request.get_json(silent=True) or {}
        limit = int(body.get("limit") or request.args.get("limit") or 500)
        wallet = body.get("wallet") or request.args.get("wallet")
        from repryntt.economy.proof_of_productive_work import get_popw_minter
        result = get_popw_minter().drain_outbox(wallet=wallet, limit=limit)
        return jsonify({"success": True, **result})
    except Exception as exc:
        log.exception("PoPW outbox drain failed")
        return jsonify({"success": False, "error": str(exc)})


@explorer_bp.route("/api/richlist")
def api_richlist():
    """Top addresses by balance."""
    limit = min(int(request.args.get("limit", 100)), 500)
    offset = int(request.args.get("offset", 0))
    resp = _rpc("get_richlist", {"limit": limit, "offset": offset})
    if "error" in resp:
        return jsonify({"success": False, "error": resp.get("error", "Node unreachable")})
    return jsonify({"success": True, **resp})


@explorer_bp.route("/api/search")
def api_search():
    """Universal search by block index, block hash, tx hash, or address."""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"success": False, "error": "Empty query"})
    resp = _rpc("search", {"query": q})
    if "error" in resp:
        return jsonify({"success": False, "error": resp.get("error", "Not found")})
    return jsonify({"success": True, **resp})


@explorer_bp.route("/api/wallet/create", methods=["POST"])
def api_wallet_create():
    """Create a new wallet — returns address, private key, and mnemonic.
    
    Keys are generated server-side (PBKDF2 600K iterations) and returned
    to the client. Nothing is stored server-side — the user is responsible
    for backing up their keys.
    """
    data = request.get_json(silent=True) or {}
    wallet_type = data.get("wallet_type", "personal")
    if wallet_type not in ("machine", "miner", "personal"):
        return jsonify({"success": False, "error": "Invalid wallet_type. Use: machine, miner, personal"})
    try:
        from repryntt.economy.crypto_utils import crypto_utils
        address, mnemonic = crypto_utils.generate_wallet_seed()
        priv_bytes, pub_bytes = crypto_utils.derive_private_key_from_mnemonic(mnemonic)
        if priv_bytes is None:
            return jsonify({"success": False, "error": "Key derivation failed"})
        return jsonify({
            "success": True,
            "address": address,
            "private_key": priv_bytes.hex(),
            "public_key": pub_bytes.hex(),
            "mnemonic": mnemonic,
            "wallet_type": wallet_type,
        })
    except Exception as e:
        log.error(f"Wallet creation failed: {e}")
        return jsonify({"success": False, "error": "Wallet generation failed"})


@explorer_bp.route("/api/wallet/recover", methods=["POST"])
def api_wallet_recover():
    """Recover wallet from 24-word mnemonic — returns address and private key."""
    data = request.get_json(silent=True) or {}
    mnemonic = data.get("mnemonic", "").strip()
    if not mnemonic:
        return jsonify({"success": False, "error": "Missing mnemonic"})
    words = mnemonic.split()
    if len(words) != 24:
        return jsonify({"success": False, "error": "Mnemonic must be exactly 24 words"})
    try:
        from repryntt.economy.crypto_utils import crypto_utils
        # Try v3 KDF first (current), then v2, then v1 for legacy wallets
        address = crypto_utils.recover_wallet_from_mnemonic(mnemonic, kdf_version=3)
        kdf_used = 3
        if address is None:
            address = crypto_utils.recover_wallet_from_mnemonic(mnemonic, kdf_version=2)
            kdf_used = 2
        if address is None:
            address = crypto_utils.recover_wallet_from_mnemonic(mnemonic, kdf_version=1)
            kdf_used = 1
        if address is None:
            return jsonify({"success": False, "error": "Recovery failed — invalid mnemonic"})
        priv_bytes, pub_bytes = crypto_utils.derive_private_key_from_mnemonic(mnemonic, kdf_version=kdf_used)
        if priv_bytes is None:
            return jsonify({"success": False, "error": "Key derivation failed"})
        return jsonify({
            "success": True,
            "address": address,
            "private_key": priv_bytes.hex(),
            "public_key": pub_bytes.hex(),
        })
    except Exception as e:
        log.error(f"Wallet recovery failed: {e}")
        return jsonify({"success": False, "error": "Recovery failed"})


@explorer_bp.route("/api/faucet", methods=["POST"])
def api_faucet():
    """Claim bootstrap credits from faucet (100 CR, one-time per wallet)."""
    data = request.get_json(silent=True) or {}
    address = data.get("address", "")
    if not address:
        return jsonify({"success": False, "error": "Missing address"})

    # Rate limiting: 1 claim per wallet, 5 wallets per IP (persisted to disk)
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    allowed, err = _check_faucet_allowed(address, ip)
    if not allowed:
        return jsonify({"success": False, "error": err})

    from repryntt.economy.rust_chain_client import submit_node_signed_workload_credit

    resp = submit_node_signed_workload_credit(
        to_address=address,
        amount_plancks=10000000000,
        purpose="faucet_claim",
        metadata={"source": "blockchain_explorer_faucet"},
    )
    if "error" in resp:
        return jsonify({"success": False, "error": resp.get("error", "Faucet claim failed")})

    _record_faucet_claim(address, ip)
    return jsonify({"success": True, "tx_hash": resp.get("tx_hash", "")})


@explorer_bp.route("/api/transfer", methods=["POST"])
def api_transfer():
    """Transfer credits between addresses — requires Ed25519 signature."""
    data = request.get_json(silent=True) or {}
    from_addr = data.get("from_address", "")
    to_addr = data.get("to_address", "")
    amount = float(data.get("amount_credits", 0))
    signature = data.get("signature", "")
    public_key = data.get("public_key", "")
    nonce = data.get("nonce")
    timestamp = data.get("timestamp")
    tx_version = data.get("tx_version", 1)
    metadata = data.get("metadata", {})

    if not from_addr or not to_addr or amount <= 0:
        return jsonify({"success": False, "error": "Invalid parameters"})
    if not signature or not public_key:
        return jsonify({"success": False, "error": "Transfer requires signature and public_key (Ed25519)"})

    amount_plancks = int(amount * 100000000)
    resp = _rpc("submit_transaction", {
        "from_address": from_addr,
        "to_address": to_addr,
        "amount": amount_plancks,
        "tx_type": "transfer",
        "signature": signature,
        "public_key": public_key,
        "nonce": nonce,
        "timestamp": timestamp,
        "tx_version": tx_version,
        "metadata": metadata,
    })
    if "error" in resp:
        return jsonify({"success": False, "error": resp.get("error", "Transfer failed")})
    return jsonify({"success": True, "tx_hash": resp.get("tx_hash", "")})


@explorer_bp.route("/api/sign_and_transfer", methods=["POST"])
def api_sign_and_transfer():
    """DEPRECATED — Use /api/transfer with client-side signing instead.

    This endpoint previously accepted raw private keys over HTTP.
    It now returns an error directing users to sign client-side.
    """
    return jsonify({
        "success": False,
        "error": "Server-side signing removed for security. Use the explorer UI (client-side Ed25519 signing) or POST to /api/transfer with signature + public_key."
    }), 400


@explorer_bp.route("/api/leaderboard")
def api_leaderboard():
    """Top wallets by balance."""
    resp = _rpc("get_leaderboard", {"top_n": 20})
    if "error" in resp:
        return jsonify({"success": False, "error": "Node unreachable"})
    leaderboard = [
        {"address": e.get("address", ""), "balance_credits": e.get("total_earned_cr", 0)}
        for e in resp.get("leaderboard", [])
    ]
    return jsonify({"success": True, "leaderboard": leaderboard})


@explorer_bp.route("/api/stake", methods=["POST"])
def api_stake():
    """Stake credits — requires Ed25519 signature."""
    data = request.get_json(silent=True) or {}
    address = data.get("address", "")
    amount_plancks = int(data.get("amount_plancks", 0))
    signature = data.get("signature", "")
    public_key = data.get("public_key", "")
    nonce = data.get("nonce")
    timestamp = data.get("timestamp")
    tx_version = data.get("tx_version", 1)
    metadata = data.get("metadata", {})
    if not address or amount_plancks <= 0:
        return jsonify({"success": False, "error": "Invalid parameters"})
    if not signature or not public_key:
        return jsonify({"success": False, "error": "Stake requires signature and public_key"})
    resp = _rpc("submit_transaction", {
        "from_address": address,
        "to_address": address,
        "amount": amount_plancks,
        "tx_type": "stake",
        "signature": signature,
        "public_key": public_key,
        "nonce": nonce,
        "timestamp": timestamp,
        "tx_version": tx_version,
        "metadata": metadata,
    })
    if "error" in resp:
        return jsonify({"success": False, "error": resp.get("error", "Stake failed")})
    return jsonify({"success": True, "tx_hash": resp.get("tx_hash", "")})


@explorer_bp.route("/api/unstake", methods=["POST"])
def api_unstake():
    """Unstake credits — requires Ed25519 signature."""
    data = request.get_json(silent=True) or {}
    address = data.get("address", "")
    amount_plancks = int(data.get("amount_plancks", 0))
    signature = data.get("signature", "")
    public_key = data.get("public_key", "")
    nonce = data.get("nonce")
    timestamp = data.get("timestamp")
    tx_version = data.get("tx_version", 1)
    metadata = data.get("metadata", {})
    if not address or amount_plancks <= 0:
        return jsonify({"success": False, "error": "Invalid parameters"})
    if not signature or not public_key:
        return jsonify({"success": False, "error": "Unstake requires signature and public_key"})
    resp = _rpc("submit_transaction", {
        "from_address": address,
        "to_address": address,
        "amount": amount_plancks,
        "tx_type": "stake_withdraw",
        "signature": signature,
        "public_key": public_key,
        "nonce": nonce,
        "timestamp": timestamp,
        "tx_version": tx_version,
        "metadata": metadata,
    })
    if "error" in resp:
        return jsonify({"success": False, "error": resp.get("error", "Unstake failed")})
    return jsonify({"success": True, "tx_hash": resp.get("tx_hash", "")})


@explorer_bp.route("/api/stakers")
def api_stakers():
    """Top stakers by amount."""
    resp = _rpc("get_validators")
    if "error" in resp:
        return jsonify({"success": False, "error": "Node unreachable"})
    stakers = [
        {"address": v.get("address", ""), "stake_credits": v.get("stake_cr", 0)}
        for v in resp.get("validators", [])
    ]
    return jsonify({"success": True, "stakers": stakers})


@explorer_bp.route("/api/mining-stats")
def api_mining_stats():
    """Mining and reward statistics."""
    resp = _rpc("get_mining_stats")
    if "error" in resp:
        return jsonify({"success": False, "error": resp.get("error", "Node unreachable")})
    return jsonify({"success": True, **resp})


# ═══════════════════════════════════════════════════════════════════════
# DAO Governance API
# ═══════════════════════════════════════════════════════════════════════

@explorer_bp.route("/api/dao/treasury")
def api_dao_treasury():
    """DAO treasury balance and stats."""
    resp = _rpc("get_treasury")
    if "error" in resp:
        return jsonify({"success": False, "error": resp.get("error", "Node unreachable")})
    return jsonify({"success": True, **resp})


@explorer_bp.route("/api/dao/proposals")
def api_dao_proposals():
    """List all DAO proposals, optionally filtered by status."""
    status = request.args.get("status")
    params = {"status": status} if status else {}
    resp = _rpc("get_proposals", params)
    if "error" in resp:
        return jsonify({"success": False, "error": resp.get("error", "Node unreachable")})
    return jsonify({"success": True, **resp})


@explorer_bp.route("/api/dao/proposal/<proposal_id>")
def api_dao_proposal(proposal_id):
    """Get a single proposal by ID."""
    resp = _rpc("get_proposal", {"proposal_id": proposal_id})
    if "error" in resp:
        return jsonify({"success": False, "error": resp.get("error", "Proposal not found")})
    return jsonify({"success": True, **resp})


@explorer_bp.route("/api/dao/propose", methods=["POST"])
def api_dao_propose():
    """Submit a new DAO funding proposal."""
    data = request.get_json(silent=True) or {}
    proposer = data.get("proposer", "").strip()
    title = data.get("title", "").strip()
    description = data.get("description", "").strip()
    amount_plancks = int(data.get("amount_plancks", 0))
    recipient = data.get("recipient", "").strip()

    if not proposer or not title or amount_plancks <= 0 or not recipient:
        return jsonify({"success": False, "error": "Missing required fields: proposer, title, amount_plancks, recipient"})

    resp = _rpc("submit_proposal", {
        "proposer": proposer,
        "title": title,
        "description": description,
        "amount_plancks": amount_plancks,
        "recipient": recipient,
    })
    if "error" in resp:
        return jsonify({"success": False, "error": resp.get("error", "Proposal failed")})
    return jsonify({"success": True, **resp})


@explorer_bp.route("/api/dao/vote", methods=["POST"])
def api_dao_vote():
    """Vote on an active proposal."""
    data = request.get_json(silent=True) or {}
    proposal_id = data.get("proposal_id", "").strip()
    voter = data.get("voter", "").strip()
    direction = data.get("direction", "for").strip().lower()

    if not proposal_id or not voter:
        return jsonify({"success": False, "error": "Missing required fields: proposal_id, voter"})
    if direction not in ("for", "against", "yes", "no"):
        return jsonify({"success": False, "error": "direction must be 'for' or 'against'"})

    resp = _rpc("vote_proposal", {
        "proposal_id": proposal_id,
        "voter": voter,
        "direction": direction,
    })
    if "error" in resp:
        return jsonify({"success": False, "error": resp.get("error", "Vote failed")})
    return jsonify({"success": True, **resp})


@explorer_bp.route("/api/dao/execute", methods=["POST"])
def api_dao_execute():
    """Execute a passed proposal (transfers funds from DAO treasury)."""
    data = request.get_json(silent=True) or {}
    proposal_id = data.get("proposal_id", "").strip()

    if not proposal_id:
        return jsonify({"success": False, "error": "Missing proposal_id"})

    resp = _rpc("execute_proposal", {"proposal_id": proposal_id})
    if "error" in resp:
        return jsonify({"success": False, "error": resp.get("error", "Execution failed")})
    return jsonify({"success": True, **resp})


def _compute_config_path() -> Path:
    from repryntt.economy.compute_config import compute_config_path

    return compute_config_path()


def _load_compute_config() -> dict:
    """Load compute contribution config (compute_share 0.0–1.0)."""
    from repryntt.economy.compute_config import load_compute_config

    return load_compute_config()


def _save_compute_config(cfg: dict):
    from repryntt.economy.compute_config import save_compute_config

    save_compute_config(cfg)


def _estimate_tflops(profile) -> dict:
    """Estimate FP16/FP32 TFLOPS from hardware profile.

    Uses known GPU spec ranges.  Falls back to conservative baselines.
    """
    from repryntt.economy.compute_config import estimate_tflops

    return estimate_tflops(profile)


def _sync_chain_compute_env(measured_tflops: float, compute_share: float) -> bool:
    """Mirror compute contribution into the chain EnvironmentFile if present."""
    env_path = _chain_env_path()
    if not env_path.exists():
        return False
    from repryntt.economy.compute_config import read_env_file, write_env_file

    values = read_env_file(env_path)
    values["REPRYNTT_TFLOPS"] = str(round(float(measured_tflops), 4))
    values["REPRYNTT_COMPUTE_SHARE"] = str(round(float(compute_share), 4))
    write_env_file(env_path, values)
    return True


@explorer_bp.route("/api/compute")
def api_compute_info():
    """Get this node's compute capabilities and contribution settings."""
    from repryntt.economy.compute_config import local_compute_runtime

    runtime = local_compute_runtime()
    profile = runtime.get("profile")
    share = runtime["compute_share"]
    fp16 = runtime["tflops_fp16"]
    fp32 = runtime["tflops_fp32"]
    measured = runtime["tflops_measured"]
    effective = runtime["tflops_effective"]

    chain = _rpc("get_network_stats")
    chain_payload = {}
    if "error" not in chain:
        chain_payload = {
            "chain_local_measured_tflops": chain.get("local_measured_tflops"),
            "chain_local_compute_share": chain.get("local_compute_share"),
            "chain_local_effective_tflops": chain.get("local_effective_tflops"),
            "chain_availability_tflops": chain.get("availability_tflops"),
            "chain_availability_contributor_count": chain.get("availability_contributor_count"),
        }
        try:
            chain_payload["chain_compute_in_sync"] = (
                round(float(chain.get("local_compute_share")), 4) == round(float(share), 4)
                and round(float(chain.get("local_measured_tflops")), 4) == round(float(measured), 4)
            )
        except (TypeError, ValueError):
            chain_payload["chain_compute_in_sync"] = False

    if profile:
        is_cpu = profile.gpu_backend == "cpu"
        # For CPU-only, report system RAM; for GPU, report VRAM
        mem_gb = round(profile.ram_mb / 1024, 1) if is_cpu else round(profile.gpu_vram_mb / 1024, 1)
        mem_label = "System RAM" if is_cpu else "VRAM"
        # Role description
        if is_cpu:
            role = "Relay + Light Compute"
        elif fp16 >= 50:
            role = "Heavy Compute"
        else:
            role = "Compute Node"
        return jsonify({
            "success": True,
            "device_name": profile.gpu_name,
            "device_type": profile.gpu_backend,
            "tflops_measured": round(measured, 2),
            "tflops_fp16": round(fp16, 2),
            "tflops_fp32": round(fp32, 2),
            "memory_gb": mem_gb,
            "memory_label": mem_label,
            "compute_share": share,
            "tflops_effective": round(effective, 2),
            "node_role": role,
            **chain_payload,
        })
    else:
        return jsonify({
            "success": True,
            "device_name": "Unknown",
            "device_type": "cpu",
            "tflops_measured": round(measured, 2),
            "tflops_fp16": round(fp16, 2),
            "tflops_fp32": round(fp32, 2),
            "memory_gb": 0,
            "compute_share": share,
            "tflops_effective": round(effective, 2),
            **chain_payload,
        })


@explorer_bp.route("/api/compute/share", methods=["POST"])
def api_set_compute_share():
    """Set how much compute this node contributes (0.0–1.0)."""
    data = request.get_json(force=True) or {}
    from repryntt.economy.compute_config import (
        local_compute_runtime,
        normalize_compute_share,
    )

    try:
        raw_share = float(data.get("compute_share", -1))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid compute_share value"}), 400

    if not (0.0 <= raw_share <= 1.0):
        return jsonify({"success": False, "error": "compute_share must be between 0.0 and 1.0"}), 400

    # Persist
    share = normalize_compute_share(raw_share)
    cfg = _load_compute_config()
    cfg["compute_share"] = round(share, 4)
    _save_compute_config(cfg)

    runtime = local_compute_runtime()
    total = runtime["tflops_measured"]
    effective = runtime["tflops_effective"]
    env_synced = _sync_chain_compute_env(total, share)
    return jsonify({
        "success": True,
        "compute_share": round(share, 4),
        "tflops_effective": round(effective, 2),
        "tflops_total": round(total, 2),
        "env_synced": env_synced,
        "requires_chain_restart": env_synced,
    })
