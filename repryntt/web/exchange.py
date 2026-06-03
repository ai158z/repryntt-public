"""
REPRYNTT Exchange — CR/SOL Trading Interface

A web-based order book exchange where users can:
  - View live order book (bids/asks)
  - Place limit buy/sell orders
  - View trade history
  - Deposit SOL/USDC via Solana bridge
  - Check bridge balances

Routes:
    /exchange/          — Exchange home (order book + trading UI)
    /exchange/api/*     — Internal JSON API (no API key required)
"""

import logging
import os
import time
from datetime import datetime, timedelta
from flask import Blueprint, render_template_string, request, jsonify

log = logging.getLogger("exchange")

exchange_bp = Blueprint("exchange", __name__)


def _get_manager():
    try:
        from repryntt.economy.manager import RobotEconomyManager
        return RobotEconomyManager.get_instance()
    except Exception:
        return None


def _get_trade_orders():
    """Get the shared TRADE_ORDERS dict from external_api."""
    try:
        from repryntt.web.external_api import TRADE_ORDERS
        return TRADE_ORDERS
    except Exception:
        return {}


def _get_bridge_balances():
    """Get bridge balances file."""
    from repryntt.paths import get_data_dir as _get_data_dir
    import json
    path = str(_get_data_dir() / "commerce" / "payment_gateway" / "bridge_balances.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════════════════
# HTML — Single-page exchange with live order book
# ═══════════════════════════════════════════════════════════════════════

EXCHANGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>REPRYNTT Exchange — CR/SOL</title>
<link rel="stylesheet" href="/static/board-theme.css"/>
<style>
:root { --red:#af0a0f; --green:#228854; --sell:#d32f2f; --buy:#2e7d32; --bg:#0d1117; --card:#161b22; --border:#30363d; }

body { background:#0d1117; color:#c9d1d9; font-family:'Segoe UI',system-ui,sans-serif; margin:0; }

.topbar { background:#161b22; border-bottom:1px solid #30363d; padding:8px 16px; display:flex; justify-content:space-between; align-items:center; }
.topbar a { color:#58a6ff; text-decoration:none; margin:0 8px; font-size:13px; }
.topbar a:hover { color:#79c0ff; }
.topbar a.active { color:#f0883e; font-weight:bold; }

.exchange-header { background:linear-gradient(135deg,#161b22 0%,#1c2333 100%); border-bottom:1px solid #30363d; padding:12px 16px; display:flex; align-items:center; gap:20px; }
.pair-name { font-size:24px; font-weight:bold; color:#f0f6fc; }
.pair-price { font-size:28px; font-weight:bold; font-family:monospace; }
.pair-price.up { color:#2e7d32; }
.pair-price.down { color:#d32f2f; }
.pair-stat { text-align:center; }
.pair-stat .label { font-size:10px; color:#8b949e; text-transform:uppercase; }
.pair-stat .val { font-size:14px; font-weight:bold; font-family:monospace; color:#c9d1d9; }

.exchange-grid { display:grid; grid-template-columns:1fr 300px; gap:0; min-height:calc(100vh - 120px); }

/* Order Book */
.book-panel { background:#161b22; border-right:1px solid #30363d; display:flex; flex-direction:column; }
.book-header { padding:8px 12px; font-size:12px; font-weight:bold; color:#8b949e; border-bottom:1px solid #30363d; display:flex; justify-content:space-between; }
.book-table { flex:1; overflow-y:auto; }
.book-table table { width:100%; border-collapse:collapse; font-size:12px; font-family:monospace; }
.book-table th { color:#8b949e; font-size:10px; padding:2px 8px; text-align:right; position:sticky; top:0; background:#161b22; }
.book-table th:first-child { text-align:left; }
.book-table td { padding:2px 8px; text-align:right; border:none; }
.book-table td:first-child { text-align:left; }
.book-table tr { position:relative; }
.book-table tr:hover { background:#1c2333; cursor:pointer; }

.ask-row td { color:#d32f2f; }
.bid-row td { color:#2e7d32; }

.ask-row { background:linear-gradient(to left, rgba(211,47,47,0.08) var(--depth), transparent var(--depth)); }
.bid-row { background:linear-gradient(to left, rgba(46,125,50,0.08) var(--depth), transparent var(--depth)); }

.spread-row { background:#1c2333; border-top:1px solid #30363d; border-bottom:1px solid #30363d; }
.spread-row td { color:#8b949e; font-size:11px; text-align:center; padding:4px; }

/* Trading Panel */
.trade-panel { background:#161b22; display:flex; flex-direction:column; }
.trade-tabs { display:flex; border-bottom:1px solid #30363d; }
.trade-tab { flex:1; padding:10px; text-align:center; font-weight:bold; font-size:13px; cursor:pointer; border:none; background:transparent; color:#8b949e; }
.trade-tab.active-buy { color:#2e7d32; border-bottom:2px solid #2e7d32; }
.trade-tab.active-sell { color:#d32f2f; border-bottom:2px solid #d32f2f; }

.trade-form { padding:12px; flex:1; }
.trade-form label { display:block; font-size:11px; color:#8b949e; margin:8px 0 4px; text-transform:uppercase; }
.trade-form input { width:100%; background:#0d1117; border:1px solid #30363d; color:#c9d1d9; padding:8px; font-family:monospace; font-size:14px; box-sizing:border-box; }
.trade-form input:focus { border-color:#58a6ff; outline:none; }

.btn-buy { width:100%; padding:12px; background:#2e7d32; color:#fff; border:none; font-size:14px; font-weight:bold; cursor:pointer; margin-top:12px; }
.btn-buy:hover { background:#388e3c; }
.btn-sell { width:100%; padding:12px; background:#d32f2f; color:#fff; border:none; font-size:14px; font-weight:bold; cursor:pointer; margin-top:12px; }
.btn-sell:hover { background:#f44336; }

.trade-info { padding:12px; border-top:1px solid #30363d; }
.trade-info-row { display:flex; justify-content:space-between; font-size:11px; color:#8b949e; margin:2px 0; }
.trade-info-row .val { color:#c9d1d9; font-family:monospace; }

/* Bottom Panels */
.bottom-panels { border-top:1px solid #30363d; background:#161b22; }
.bottom-tabs { display:flex; border-bottom:1px solid #30363d; padding:0 12px; }
.bottom-tab { padding:8px 16px; font-size:12px; color:#8b949e; cursor:pointer; border-bottom:2px solid transparent; }
.bottom-tab.active { color:#f0883e; border-bottom-color:#f0883e; }
.bottom-content { max-height:200px; overflow-y:auto; }
.bottom-content table { width:100%; border-collapse:collapse; font-size:11px; font-family:monospace; }
.bottom-content th { color:#8b949e; padding:4px 8px; text-align:left; position:sticky; top:0; background:#161b22; font-size:10px; }
.bottom-content td { padding:4px 8px; border-bottom:1px solid #21262d; }

.msg { padding:8px; margin:8px 0; font-size:12px; border-radius:4px; }
.msg-ok { background:rgba(46,125,50,0.15); color:#4caf50; border:1px solid rgba(46,125,50,0.3); }
.msg-err { background:rgba(211,47,47,0.15); color:#ef5350; border:1px solid rgba(211,47,47,0.3); }

.deposit-section { padding:12px; border-top:1px solid #30363d; }
.deposit-section h3 { color:#f0883e; font-size:13px; margin:0 0 8px; }
.deposit-addr { background:#0d1117; border:1px solid #30363d; padding:8px; word-break:break-all; font-family:monospace; font-size:11px; color:#58a6ff; }

.empty { color:#8b949e; text-align:center; padding:20px; font-size:12px; }

@media (max-width:768px) {
  .exchange-grid { grid-template-columns:1fr; }
  .trade-panel { border-top:1px solid #30363d; }
}
</style>
</head>
<body>

<div class="topbar">
  <div>
    <a href="/">Hub</a> <a href="/chain/">Explorer</a>
    <a href="/exchange/" class="active">Exchange</a> <a href="/commerce">Commerce</a>
    <a href="/ops">Ops</a>
  </div>
  <div style="color:#8b949e;font-size:12px">REPRYNTT Exchange</div>
</div>

<!-- Header: Pair + Price -->
<div class="exchange-header">
  <div class="pair-name">CR / SOL</div>
  <div class="pair-price up" id="lastPrice">—</div>
  <div class="pair-stat"><div class="label">24h Volume</div><div class="val" id="vol24h">—</div></div>
  <div class="pair-stat"><div class="label">Best Bid</div><div class="val" id="bestBid" style="color:#2e7d32">—</div></div>
  <div class="pair-stat"><div class="label">Best Ask</div><div class="val" id="bestAsk" style="color:#d32f2f">—</div></div>
  <div class="pair-stat"><div class="label">Spread</div><div class="val" id="spread">—</div></div>
  <div class="pair-stat"><div class="label">Orders</div><div class="val" id="orderCount">0</div></div>
</div>

<div class="exchange-grid">

  <!-- LEFT: Order Book -->
  <div class="book-panel">
    <div class="book-header">
      <span>Order Book</span>
      <span id="bookStatus" style="color:#4caf50">● Live</span>
    </div>
    <div class="book-table" id="orderBook">
      <div class="empty">Loading order book...</div>
    </div>
  </div>

  <!-- RIGHT: Trade Form -->
  <div class="trade-panel">
    <div class="trade-tabs">
      <div class="trade-tab active-buy" id="tabBuy" onclick="setTradeType('buy')">Buy CR</div>
      <div class="trade-tab" id="tabSell" onclick="setTradeType('sell')">Sell CR</div>
    </div>
    <div class="trade-form">
      <label>Your Wallet Address</label>
      <input type="text" id="tradeWallet" placeholder="40-character hex address" />

      <label>Price (SOL per CR)</label>
      <input type="number" id="tradePrice" placeholder="0.00800000" step="0.00000001" min="0.00000001" />

      <label>Amount (CR)</label>
      <input type="number" id="tradeAmount" placeholder="100.00" step="0.01" min="0.01" />

      <div class="trade-info">
        <div class="trade-info-row"><span>Total</span><span class="val" id="tradeTotal">0.00000000 SOL</span></div>
        <div class="trade-info-row"><span>Fee (2%)</span><span class="val" id="tradeFee">0.00000000 SOL</span></div>
        <div class="trade-info-row"><span>You Pay</span><span class="val" id="tradeYouPay">0.00000000 SOL</span></div>
      </div>

      <button class="btn-buy" id="tradeBtn" onclick="placeOrder()">Buy CR</button>
      <div id="tradeResult"></div>
    </div>

    <div class="deposit-section">
      <h3>Deposit SOL / USDC</h3>
      <label style="font-size:11px;color:#8b949e">Your wallet address</label>
      <input type="text" id="depositWallet" placeholder="40-char repryntt address" style="background:#0d1117;border:1px solid #30363d;color:#c9d1d9;padding:6px;font-family:monospace;font-size:12px;width:100%;box-sizing:border-box;margin:4px 0"/>
      <button onclick="requestDeposit()" style="width:100%;padding:8px;background:#f0883e;color:#fff;border:none;cursor:pointer;font-weight:bold;font-size:12px;margin-top:4px">Get Deposit Address</button>
      <div id="depositResult"></div>
      <div class="trade-info" id="bridgeBalance" style="display:none">
        <div class="trade-info-row"><span>SOL Balance</span><span class="val" id="bridgeSol">0.000</span></div>
        <div class="trade-info-row"><span>USDC Balance</span><span class="val" id="bridgeUsdc">0.00</span></div>
      </div>
    </div>
  </div>

</div>

<!-- Bottom: My Orders + Trade History -->
<div class="bottom-panels">
  <div class="bottom-tabs">
    <div class="bottom-tab active" onclick="showBottom('orders')">My Open Orders</div>
    <div class="bottom-tab" onclick="showBottom('history')">Trade History</div>
  </div>
  <div class="bottom-content" id="bottomContent">
    <div class="empty">Enter your wallet address above to see orders</div>
  </div>
</div>

<script>
const API = '/exchange/api';
let tradeType = 'buy';
let refreshTimer;

// ── Helpers ──
function fmt(n, d=8) { return Number(n).toFixed(d); }
function shortAddr(a) { return a ? (a.length>20 ? a.slice(0,8)+'…'+a.slice(-4) : a) : ''; }

// ── Trade Type Toggle ──
function setTradeType(type) {
  tradeType = type;
  document.getElementById('tabBuy').className = 'trade-tab' + (type==='buy' ? ' active-buy' : '');
  document.getElementById('tabSell').className = 'trade-tab' + (type==='sell' ? ' active-sell' : '');
  const btn = document.getElementById('tradeBtn');
  btn.textContent = type==='buy' ? 'Buy CR' : 'Sell CR';
  btn.className = type==='buy' ? 'btn-buy' : 'btn-sell';
  updateTotal();
}

// ── Live Total Calculation ──
function updateTotal() {
  const price = parseFloat(document.getElementById('tradePrice').value) || 0;
  const amount = parseFloat(document.getElementById('tradeAmount').value) || 0;
  const total = price * amount;
  const fee = total * 0.02;
  document.getElementById('tradeTotal').textContent = fmt(total) + ' SOL';
  document.getElementById('tradeFee').textContent = fmt(fee) + ' SOL';
  document.getElementById('tradeYouPay').textContent = tradeType==='buy'
    ? fmt(total) + ' SOL' : fmt(amount, 2) + ' CR';
}
document.getElementById('tradePrice').addEventListener('input', updateTotal);
document.getElementById('tradeAmount').addEventListener('input', updateTotal);

// ── Fetch JSON ──
async function fetchJSON(url) {
  try { const r = await fetch(url); return await r.json(); }
  catch(e) { return {success:false, error:e.message}; }
}
async function postJSON(url, body) {
  try { const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}); return await r.json(); }
  catch(e) { return {success:false, error:e.message}; }
}

// ── Load Order Book ──
async function loadOrderBook() {
  const d = await fetchJSON(API+'/orderbook');
  if (!d.success) {
    document.getElementById('orderBook').innerHTML = '<div class="empty">Exchange offline</div>';
    document.getElementById('bookStatus').style.color = '#d32f2f';
    document.getElementById('bookStatus').textContent = '● Offline';
    return;
  }

  const asks = d.asks || [];
  const bids = d.bids || [];

  // Aggregate by price level
  const askLevels = aggregateLevels(asks, 'asc');
  const bidLevels = aggregateLevels(bids, 'desc');

  const maxAskVol = Math.max(...askLevels.map(l=>l.total), 1);
  const maxBidVol = Math.max(...bidLevels.map(l=>l.total), 1);

  let html = '<table><tr><th>Price (SOL)</th><th>Amount (CR)</th><th>Total (SOL)</th></tr>';

  // Asks (sells) — show highest first, scroll down to spread
  for (const lvl of askLevels.reverse()) {
    const depth = ((lvl.total/maxAskVol)*100).toFixed(0);
    html += `<tr class="ask-row" style="--depth:${depth}%" onclick="fillPrice(${lvl.price})">
      <td>${fmt(lvl.price)}</td><td>${fmt(lvl.amount,2)}</td><td>${fmt(lvl.total)}</td></tr>`;
  }

  // Spread row
  const bestBid = bidLevels.length ? bidLevels[0].price : 0;
  const bestAsk = askLevels.length ? askLevels[askLevels.length-1].price : 0;
  const spreadVal = bestAsk && bestBid ? ((bestAsk-bestBid)/bestAsk*100).toFixed(2) : '—';
  const lastP = d.last_price || bestBid || bestAsk || 0;
  html += `<tr class="spread-row"><td colspan="3">Spread: ${spreadVal}% · Last: ${fmt(lastP)} SOL</td></tr>`;

  // Bids (buys) — highest first
  for (const lvl of bidLevels) {
    const depth = ((lvl.total/maxBidVol)*100).toFixed(0);
    html += `<tr class="bid-row" style="--depth:${depth}%" onclick="fillPrice(${lvl.price})">
      <td>${fmt(lvl.price)}</td><td>${fmt(lvl.amount,2)}</td><td>${fmt(lvl.total)}</td></tr>`;
  }

  html += '</table>';
  document.getElementById('orderBook').innerHTML = html;

  // Update header stats
  document.getElementById('lastPrice').textContent = fmt(lastP) + ' SOL';
  document.getElementById('bestBid').textContent = bestBid ? fmt(bestBid) : '—';
  document.getElementById('bestAsk').textContent = bestAsk ? fmt(bestAsk) : '—';
  document.getElementById('spread').textContent = spreadVal + '%';
  document.getElementById('orderCount').textContent = (asks.length + bids.length).toString();
  document.getElementById('vol24h').textContent = (d.volume_24h || 0).toFixed(2) + ' SOL';
}

function aggregateLevels(orders, sort) {
  const map = {};
  for (const o of orders) {
    const p = Number(o.price).toFixed(8);
    if (!map[p]) map[p] = {price:Number(p), amount:0, total:0};
    map[p].amount += o.amount_cr || o.amount_credits || 0;
    map[p].total += (o.amount_cr || o.amount_credits || 0) * Number(p);
  }
  const levels = Object.values(map);
  levels.sort((a,b) => sort==='asc' ? a.price-b.price : b.price-a.price);
  return levels;
}

function fillPrice(price) {
  document.getElementById('tradePrice').value = price.toFixed(8);
  updateTotal();
}

// ── Place Order ──
async function placeOrder() {
  const wallet = document.getElementById('tradeWallet').value.trim();
  const price = parseFloat(document.getElementById('tradePrice').value);
  const amount = parseFloat(document.getElementById('tradeAmount').value);
  if (!wallet || wallet.length < 10) { showMsg('tradeResult','Enter your wallet address','err'); return; }
  if (!price || price <= 0) { showMsg('tradeResult','Enter a valid price','err'); return; }
  if (!amount || amount <= 0) { showMsg('tradeResult','Enter a valid amount','err'); return; }

  const btn = document.getElementById('tradeBtn');
  btn.disabled = true;
  btn.textContent = 'Submitting...';

  const d = await postJSON(API+'/order', {
    wallet_address: wallet,
    order_type: tradeType,
    amount_credits: amount,
    price_sol_per_cr: price,
    quote_currency: 'sol'
  });

  btn.disabled = false;
  btn.textContent = tradeType==='buy' ? 'Buy CR' : 'Sell CR';

  if (d.success) {
    let msg = `Order placed: ${tradeType.toUpperCase()} ${amount} CR @ ${price} SOL`;
    if (d.matches && d.matches.length) {
      const filled = d.matches.reduce((s,m) => s + m.amount_cr, 0);
      msg += ` · ${filled.toFixed(2)} CR filled immediately`;
    }
    showMsg('tradeResult', msg, 'ok');
    loadOrderBook();
    loadMyOrders();
  } else {
    showMsg('tradeResult', d.error || 'Order failed', 'err');
  }
}

// ── My Orders ──
async function loadMyOrders() {
  const wallet = document.getElementById('tradeWallet').value.trim();
  if (!wallet) return;
  const d = await fetchJSON(API+'/orders/'+wallet);
  if (!d.success || !d.orders.length) {
    document.getElementById('bottomContent').innerHTML = '<div class="empty">No open orders</div>';
    return;
  }
  let html = '<table><tr><th>ID</th><th>Type</th><th>Price</th><th>Amount</th><th>Status</th><th>Action</th></tr>';
  for (const o of d.orders) {
    const color = o.order_type==='buy' ? '#2e7d32' : '#d32f2f';
    const cancelBtn = o.status==='active' || o.status==='partial'
      ? `<button onclick="cancelOrder('${o.trade_id}')" style="background:#d32f2f;color:#fff;border:none;padding:2px 8px;cursor:pointer;font-size:10px">Cancel</button>`
      : '';
    html += `<tr>
      <td style="color:#58a6ff">${o.trade_id.slice(-8)}</td>
      <td style="color:${color};font-weight:bold">${o.order_type.toUpperCase()}</td>
      <td>${fmt(o.price)}</td>
      <td>${o.amount_credits.toFixed(2)} CR</td>
      <td>${o.status}</td>
      <td>${cancelBtn}</td>
    </tr>`;
  }
  html += '</table>';
  document.getElementById('bottomContent').innerHTML = html;
}

async function cancelOrder(tradeId) {
  const wallet = document.getElementById('tradeWallet').value.trim();
  const d = await postJSON(API+'/cancel/'+tradeId, {wallet_address:wallet});
  if (d.success) {
    showMsg('tradeResult', 'Order cancelled — funds refunded', 'ok');
    loadOrderBook();
    loadMyOrders();
  } else {
    showMsg('tradeResult', d.error || 'Cancel failed', 'err');
  }
}

// ── Deposit ──
async function requestDeposit() {
  const wallet = document.getElementById('depositWallet').value.trim();
  if (!wallet) { showMsg('depositResult','Enter your repryntt wallet address','err'); return; }
  const d = await postJSON(API+'/deposit', {repryntt_address:wallet});
  if (d.success) {
    showMsg('depositResult',
      `<div style="margin:4px 0"><b>Send SOL or USDC to:</b></div><div class="deposit-addr">${d.solana_address}</div><div style="margin-top:4px;font-size:10px;color:#8b949e">Deposits are detected automatically (30-60s). Min: 0.001 SOL / $0.01 USDC</div>`,
      'ok');
    // Also load bridge balance
    loadBridgeBalance(wallet);
  } else {
    showMsg('depositResult', d.error || 'Deposit request failed', 'err');
  }
}

async function loadBridgeBalance(wallet) {
  const d = await fetchJSON(API+'/bridge-balance/'+wallet);
  if (d.success) {
    document.getElementById('bridgeBalance').style.display = 'block';
    document.getElementById('bridgeSol').textContent = (d.sol||0).toFixed(6) + ' SOL';
    document.getElementById('bridgeUsdc').textContent = (d.usdc||0).toFixed(2) + ' USDC';
  }
}

// ── Bottom tabs ──
function showBottom(tab) {
  document.querySelectorAll('.bottom-tab').forEach(t => t.classList.remove('active'));
  event.target.classList.add('active');
  if (tab === 'orders') loadMyOrders();
  else loadTradeHistory();
}

async function loadTradeHistory() {
  const d = await fetchJSON(API+'/trades');
  if (!d.success || !d.trades.length) {
    document.getElementById('bottomContent').innerHTML = '<div class="empty">No recent trades</div>';
    return;
  }
  let html = '<table><tr><th>Time</th><th>Type</th><th>Price</th><th>Amount</th><th>Total</th></tr>';
  for (const t of d.trades) {
    const color = t.type==='buy' ? '#2e7d32' : '#d32f2f';
    html += `<tr>
      <td style="color:#8b949e">${t.time}</td>
      <td style="color:${color};font-weight:bold">${t.type.toUpperCase()}</td>
      <td>${fmt(t.price)}</td>
      <td>${t.amount.toFixed(2)} CR</td>
      <td>${fmt(t.total)} SOL</td>
    </tr>`;
  }
  html += '</table>';
  document.getElementById('bottomContent').innerHTML = html;
}

function showMsg(id, msg, type) {
  document.getElementById(id).innerHTML = `<div class="msg msg-${type}">${msg}</div>`;
}

// ── Auto-refresh ──
document.getElementById('tradeWallet').addEventListener('change', () => { loadMyOrders(); });

loadOrderBook();
refreshTimer = setInterval(loadOrderBook, 5000);  // 5s refresh
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════
# API Routes — called by frontend JS (no API key required)
# ═══════════════════════════════════════════════════════════════════════

@exchange_bp.route("/")
def exchange_home():
    return render_template_string(EXCHANGE_HTML)


@exchange_bp.route("/api/orderbook")
def api_orderbook():
    """Live order book: sorted bids (desc) and asks (asc)."""
    orders = _get_trade_orders()
    bids = []
    asks = []
    last_price = 0
    vol_24h = 0
    cutoff_24h = time.time() - 86400

    for tid, order in list(orders.items()):
        price = order.get("price_per_cr_sol", order.get("price_per_credit", 0))

        # Collect trade history for volume
        for ex in order.get("executions", []):
            vol_24h += ex.get("sol_total", 0)
            last_price = ex.get("price", last_price)

        if order.get("status") not in ("active", "partial"):
            continue

        entry = {
            "trade_id": tid,
            "price": price,
            "amount_cr": order.get("amount_credits", 0),
            "wallet": order.get("wallet_address", ""),
        }
        if order.get("order_type") == "buy":
            bids.append(entry)
        else:
            asks.append(entry)

    bids.sort(key=lambda x: -x["price"])
    asks.sort(key=lambda x: x["price"])

    return jsonify({
        "success": True,
        "bids": bids,
        "asks": asks,
        "last_price": last_price,
        "volume_24h": vol_24h,
    })


@exchange_bp.route("/api/order", methods=["POST"])
def api_place_order():
    """Place a limit order on the CR/SOL book. Requires wallet signature."""
    data = request.get_json(silent=True) or {}
    wallet = data.get("wallet_address", "").strip()
    order_type = data.get("order_type", "")
    amount = float(data.get("amount_credits", 0))
    price = float(data.get("price_sol_per_cr", 0))
    quote = data.get("quote_currency", "sol")
    signature_hex = data.get("signature", "")
    public_key_hex = data.get("public_key", "")

    if not wallet or len(wallet) < 10:
        return jsonify({"success": False, "error": "Invalid wallet address"})
    if order_type not in ("buy", "sell"):
        return jsonify({"success": False, "error": "Order type must be 'buy' or 'sell'"})
    if amount <= 0:
        return jsonify({"success": False, "error": "Amount must be positive"})
    if price <= 0 or price > 1000:
        return jsonify({"success": False, "error": "Price must be between 0 and 1000 SOL"})

    # ── SECURITY: Verify wallet ownership via Ed25519 signature ──
    if not signature_hex or not public_key_hex:
        return jsonify({"success": False, "error": "Order requires signature and public_key for wallet verification"})
    try:
        import hashlib as _hl
        from repryntt.economy.secure_crypto import SecureCrypto
        pub_key = bytes.fromhex(public_key_hex)
        sig = bytes.fromhex(signature_hex)
        # Verify address matches public key
        derived = _hl.sha3_256(pub_key).hexdigest()[:40]
        if derived != wallet:
            return jsonify({"success": False, "error": "Public key does not match wallet address"})
        # Verify signature on order data
        order_msg = f"{wallet}:{order_type}:{amount}:{price}".encode()
        order_hash = SecureCrypto.hash_data(order_msg)
        if not SecureCrypto.verify(order_hash, sig, pub_key):
            return jsonify({"success": False, "error": "Invalid order signature"})
    except Exception as e:
        return jsonify({"success": False, "error": f"Signature verification failed: {e}"})

    mgr = _get_manager()
    if not mgr:
        return jsonify({"success": False, "error": "Economy manager unavailable"})

    orders = _get_trade_orders()
    trade_id = f"trade_{int(time.time())}_{wallet[:8]}"
    # Integer math: compute total in lamports to avoid float precision loss,
    # then convert back for display.  price and amount are user floats but
    # the product is rounded to the nearest lamport (10^-9 SOL).
    total_sol_lamports = int(round(amount * price * 1_000_000_000))
    total_sol = total_sol_lamports / 1_000_000_000

    # Reserve funds
    if order_type == "sell":
        # Seller: reserve CR from their wallet
        result = mgr.deduct_credits(wallet, amount, f"sell_order_{trade_id}")
        if not result or not result.get("success"):
            return jsonify({"success": False, "error": f"Insufficient CR balance"})
    else:
        # Buyer: reserve SOL from their bridge balance
        from repryntt.economy.payment_gateway import get_bridge_balance, debit_bridge_balance
        bridge = get_bridge_balance(wallet)
        sol_bal = bridge.get("sol", 0)
        if sol_bal < total_sol - 1e-12:
            return jsonify({
                "success": False,
                "error": f"Insufficient SOL balance ({sol_bal:.6f} SOL). Deposit more via the deposit section."
            })
        # Debit bridge balance
        if not debit_bridge_balance(wallet, quote, total_sol):
            return jsonify({"success": False, "error": "Bridge debit failed — insufficient balance"})

    # Create order
    order = {
        "trade_id": trade_id,
        "wallet_address": wallet,
        "order_type": order_type,
        "amount_credits": amount,
        "price_per_cr_sol": price,
        "quote_currency": quote,
        "total_sol": total_sol,
        "status": "active",
        "created_at": datetime.now().isoformat(),
        "expires_at": (datetime.now() + timedelta(days=7)).isoformat(),
        "executions": [],
    }

    orders[trade_id] = order
    orders.sync() if hasattr(orders, "sync") else None

    # Try to match
    matches = None
    try:
        from repryntt.web.external_api import _match_trade_order
        matches = _match_trade_order(order)
    except Exception as e:
        log.warning(f"Matching failed: {e}")

    if matches:
        filled = sum(m["amount_cr"] for m in matches)
        remaining = amount - filled
        if remaining <= 0:
            order["status"] = "completed"
        else:
            order["status"] = "partial"
            order["amount_credits"] = remaining
            # Refund excess reserved funds
            if order_type == "sell":
                mgr.add_credits(wallet, remaining, f"sell_partial_refund_{trade_id}")
        orders.sync() if hasattr(orders, "sync") else None

    return jsonify({
        "success": True,
        "trade_id": trade_id,
        "order": {
            "order_type": order_type,
            "amount_credits": amount,
            "price": price,
            "total_sol": total_sol,
            "status": order.get("status", "active"),
        },
        "matches": matches,
    })


@exchange_bp.route("/api/orders/<wallet>")
def api_my_orders(wallet):
    """Get a wallet's orders."""
    orders = _get_trade_orders()
    my_orders = []
    for tid, o in list(orders.items()):
        if o.get("wallet_address") == wallet:
            my_orders.append({
                "trade_id": tid,
                "order_type": o.get("order_type", ""),
                "price": o.get("price_per_cr_sol", 0),
                "amount_credits": o.get("amount_credits", 0),
                "status": o.get("status", ""),
                "created_at": o.get("created_at", ""),
            })
    return jsonify({"success": True, "orders": my_orders})


@exchange_bp.route("/api/cancel/<trade_id>", methods=["POST"])
def api_cancel_order(trade_id):
    """Cancel an order and refund reserved funds. Requires wallet signature."""
    data = request.get_json(silent=True) or {}
    wallet = data.get("wallet_address", "")
    signature_hex = data.get("signature", "")
    public_key_hex = data.get("public_key", "")
    orders = _get_trade_orders()

    if trade_id not in orders:
        return jsonify({"success": False, "error": "Order not found"})
    order = orders[trade_id]
    if order.get("wallet_address") != wallet:
        return jsonify({"success": False, "error": "Not your order"})
    if order.get("status") not in ("active", "partial"):
        return jsonify({"success": False, "error": "Order not cancellable"})

    # Verify wallet ownership via signature
    if not signature_hex or not public_key_hex:
        return jsonify({"success": False, "error": "Cancellation requires signature for wallet verification"})
    try:
        import hashlib as _hl
        from repryntt.economy.secure_crypto import SecureCrypto
        pub_key = bytes.fromhex(public_key_hex)
        derived = _hl.sha3_256(pub_key).hexdigest()[:40]
        if derived != wallet:
            return jsonify({"success": False, "error": "Public key does not match wallet"})
        cancel_msg = f"cancel:{trade_id}:{wallet}".encode()
        cancel_hash = SecureCrypto.hash_data(cancel_msg)
        sig = bytes.fromhex(signature_hex)
        if not SecureCrypto.verify(cancel_hash, sig, pub_key):
            return jsonify({"success": False, "error": "Invalid cancellation signature"})
    except Exception as e:
        return jsonify({"success": False, "error": f"Signature verification failed: {e}"})

    mgr = _get_manager()
    remaining = order.get("amount_credits", 0)

    if order.get("order_type") == "sell" and remaining > 0:
        mgr.add_credits(wallet, remaining, f"sell_cancel_refund_{trade_id}")
    elif order.get("order_type") == "buy" and remaining > 0:
        refund_lamports = int(round(remaining * order.get("price_per_cr_sol", 0) * 1_000_000_000))
        refund_sol = refund_lamports / 1_000_000_000
        try:
            from repryntt.economy.payment_gateway import _credit_bridge_balance
            _credit_bridge_balance(wallet, order.get("quote_currency", "sol"), refund_sol)
        except Exception:
            pass

    order["status"] = "cancelled"
    orders.sync() if hasattr(orders, "sync") else None

    return jsonify({"success": True, "refunded_amount": remaining})


@exchange_bp.route("/api/trades")
def api_recent_trades():
    """Recent completed trades across all wallets."""
    orders = _get_trade_orders()
    trades = []
    for tid, o in list(orders.items()):
        for ex in o.get("executions", []):
            trades.append({
                "type": o.get("order_type", ""),
                "price": ex.get("price", 0),
                "amount": ex.get("amount_cr", 0),
                "total": ex.get("sol_total", 0),
                "time": o.get("created_at", "")[:19],
            })
    trades.sort(key=lambda x: x["time"], reverse=True)
    return jsonify({"success": True, "trades": trades[:50]})


@exchange_bp.route("/api/deposit", methods=["POST"])
def api_deposit():
    """Request a Solana deposit address."""
    data = request.get_json(silent=True) or {}
    addr = data.get("repryntt_address", "").strip()
    if not addr or len(addr) < 10:
        return jsonify({"success": False, "error": "Invalid repryntt address"})
    try:
        from repryntt.economy.payment_gateway import PaymentGateway
        gw = PaymentGateway.get_instance()
        if not gw:
            return jsonify({"success": False, "error": "Payment gateway not initialized"})
        deposit = gw.create_deposit(addr)
        return jsonify({
            "success": True,
            "solana_address": deposit.get("solana_deposit_address", ""),
            "deposit_id": deposit.get("deposit_id", ""),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@exchange_bp.route("/api/bridge-balance/<wallet>")
def api_bridge_balance(wallet):
    """Check a wallet's SOL/USDC bridge balance."""
    try:
        from repryntt.economy.payment_gateway import get_bridge_balance
        bal = get_bridge_balance(wallet)
    except Exception:
        bal = {"sol": 0, "usdc": 0}
    return jsonify({
        "success": True,
        "sol": bal.get("sol", 0),
        "usdc": bal.get("usdc", 0),
    })
