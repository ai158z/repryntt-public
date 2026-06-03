#!/usr/bin/env python3
"""
DEGEN TERMINAL v1.0 — Solana Memecoin Scalper Platform
Professional trading dashboard for the SAIGE trading bot.
Flask backend serving REST API + HTML dashboard.
Port 8888 (avoids llama.cpp on 8080)
"""

import asyncio
import json
import os
import sys
import time
import glob
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from flask import Blueprint, Flask, jsonify, render_template, request, send_from_directory, Response

# ─── Paths ───────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "tokens.db"
TOKEN_PROFILES_DIR = DATA_DIR / "token_profiles"
SIGNAL_TOKENS_DIR = DATA_DIR / "signal_tokens"
PREDICTIONS_DIR = DATA_DIR / "predictions"
PERFORMANCE_FILE = DATA_DIR / "token_performance.json"
ACTIVE_TOKENS_FILE = DATA_DIR / "active_tokens.json"
TRADE_SIGNALS_FILE = DATA_DIR / "trade_signals.json"

# Ensure dirs exist
for d in [DATA_DIR, TOKEN_PROFILES_DIR, SIGNAL_TOKENS_DIR, PREDICTIONS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ─── Flask App (standalone) + Blueprint (consolidated) ───────────────────────
trading_bp = Blueprint('trading_dashboard', __name__,
                       template_folder=str(BASE_DIR / "templates"),
                       static_folder=str(BASE_DIR / "static"))

app = Flask(__name__,
            template_folder=str(BASE_DIR / "templates"),
            static_folder=str(BASE_DIR / "static"))
app.config['JSON_SORT_KEYS'] = False

# ─── Database Helper ─────────────────────────────────────────────────────────
def get_db():
    """Get a thread-local SQLite connection."""
    db = sqlite3.connect(str(DB_PATH), timeout=5)
    db.row_factory = sqlite3.Row
    return db


def query_db(query, args=(), one=False):
    """Execute a query and return results as list of dicts."""
    try:
        db = get_db()
        cur = db.execute(query, args)
        rv = [dict(row) for row in cur.fetchall()]
        db.close()
        return (rv[0] if rv else None) if one else rv
    except Exception as e:
        print(f"[DB ERROR] {e}")
        return None if one else []


# ─── File Helpers ────────────────────────────────────────────────────────────
def read_json_file(filepath):
    """Safely read a JSON file."""
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except Exception:
        return None


def list_json_files(directory, limit=100, sort_newest=True):
    """List JSON files from a directory, optionally sorted by mtime."""
    try:
        files = list(Path(directory).glob("*.json"))
        if sort_newest:
            files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        results = []
        for fp in files[:limit]:
            data = read_json_file(fp)
            if data:
                data['_filename'] = fp.name
                data['_mtime'] = fp.stat().st_mtime
                results.append(data)
        return results
    except Exception as e:
        print(f"[FILE ERROR] {e}")
        return []


# ─── API Routes ──────────────────────────────────────────────────────────────

@trading_bp.route('/')
def dashboard():
    """Serve the main dashboard."""
    return render_template('index.html')


@trading_bp.route('/api/tokens')
def api_tokens():
    """Get all tokens from the database with full data."""
    sort_by = request.args.get('sort', 'current_market_cap')
    order = request.args.get('order', 'DESC')
    limit = min(int(request.args.get('limit', 200)), 500)
    
    # Sanitize sort column
    allowed_sorts = [
        'current_price', 'current_market_cap', 'ath_price', 'price_up_counter',
        'buys_5m', 'sells_5m', 'volume_5m', 'volume_1h', 'volume_24h',
        'price_change_5s', 'price_change_15s', 'price_change_30s',
        'price_change_1m', 'price_change_5m', 'top_20_holders_percentage',
        'last_updated', 'token_name', 'is_uptrend', 'is_new'
    ]
    if sort_by not in allowed_sorts:
        sort_by = 'current_market_cap'
    if order not in ('ASC', 'DESC'):
        order = 'DESC'
    
    tokens = query_db(
        f"SELECT * FROM tokens ORDER BY {sort_by} {order} LIMIT ?",
        (limit,)
    )
    return jsonify({
        'tokens': tokens,
        'count': len(tokens),
        'timestamp': time.time()
    })


@trading_bp.route('/api/tokens/hot')
def api_tokens_hot():
    """Get tokens with recent activity — uptrends, new tokens, high volume."""
    tokens = query_db("""
        SELECT * FROM tokens 
        WHERE (is_uptrend = 1 OR is_new = 1 OR price_change_5m > 5 OR volume_5m > 1000)
        AND is_bundled = 0
        ORDER BY price_change_5m DESC
        LIMIT 50
    """)
    return jsonify({'tokens': tokens, 'count': len(tokens)})


@trading_bp.route('/api/tokens/scam')
def api_tokens_scam():
    """Get tokens flagged as bundled/scam."""
    tokens = query_db("""
        SELECT address, token_name, current_price, current_market_cap,
               top_20_holders_percentage, is_bundled, bundling_reason
        FROM tokens WHERE is_bundled = 1
        ORDER BY top_20_holders_percentage DESC
        LIMIT 50
    """)
    return jsonify({'tokens': tokens, 'count': len(tokens)})


@trading_bp.route('/api/token/<address>')
def api_token_detail(address):
    """Get detailed data for a single token including its profile."""
    token = query_db("SELECT * FROM tokens WHERE address = ?", (address,), one=True)
    if not token:
        return jsonify({'error': 'Token not found'}), 404
    
    # Load profile
    profile_path = TOKEN_PROFILES_DIR / f"{address}.json"
    profile = read_json_file(profile_path)
    
    return jsonify({
        'token': token,
        'profile': profile,
        'timestamp': time.time()
    })


@trading_bp.route('/api/signals')
def api_signals():
    """Get recent trading signals."""
    limit = min(int(request.args.get('limit', 50)), 200)
    signals = list_json_files(SIGNAL_TOKENS_DIR, limit=limit)
    return jsonify({
        'signals': signals,
        'count': len(signals),
        'timestamp': time.time()
    })


@trading_bp.route('/api/predictions')
def api_predictions():
    """Get recent AI predictions."""
    limit = min(int(request.args.get('limit', 50)), 200)
    predictions = list_json_files(PREDICTIONS_DIR, limit=limit)
    return jsonify({
        'predictions': predictions,
        'count': len(predictions),
        'timestamp': time.time()
    })


@trading_bp.route('/api/trade-signals')
def api_trade_signals():
    """Get trade signal export history."""
    data = read_json_file(TRADE_SIGNALS_FILE)
    if data is None:
        data = []
    # Return most recent first
    if isinstance(data, list):
        data = sorted(data, key=lambda x: x.get('timestamp', 0), reverse=True)
    return jsonify({
        'signals': data[:200],
        'count': len(data),
        'timestamp': time.time()
    })


@trading_bp.route('/api/performance')
def api_performance():
    """Get token performance data (wins/losses per token)."""
    data = read_json_file(PERFORMANCE_FILE)
    if data is None:
        data = {}
    
    # Compute aggregated stats
    total_trades = 0
    total_wins = 0
    total_losses = 0
    total_profit = 0.0
    
    for addr, stats in data.items():
        total_trades += stats.get('trades', 0)
        total_wins += stats.get('wins', 0)
        total_losses += stats.get('losses', 0)
        total_profit += stats.get('total_profit_sol', 0.0)
    
    return jsonify({
        'tokens': data,
        'aggregate': {
            'total_trades': total_trades,
            'total_wins': total_wins,
            'total_losses': total_losses,
            'total_profit_sol': round(total_profit, 6),
            'win_rate': round((total_wins / total_trades * 100), 1) if total_trades > 0 else 0
        },
        'timestamp': time.time()
    })


@trading_bp.route('/api/stats')
def api_stats():
    """Get aggregated platform statistics."""
    # Token counts
    total = query_db("SELECT COUNT(*) as c FROM tokens", one=True)
    active = query_db("SELECT COUNT(*) as c FROM tokens WHERE current_market_cap >= 30000", one=True)
    uptrending = query_db("SELECT COUNT(*) as c FROM tokens WHERE is_uptrend = 1", one=True)
    new_tokens = query_db("SELECT COUNT(*) as c FROM tokens WHERE is_new = 1", one=True)
    bundled = query_db("SELECT COUNT(*) as c FROM tokens WHERE is_bundled = 1", one=True)
    
    # Volume stats
    vol_stats = query_db("""
        SELECT 
            SUM(volume_5m) as total_vol_5m,
            SUM(volume_1h) as total_vol_1h,
            SUM(volume_24h) as total_vol_24h,
            AVG(price_change_5m) as avg_change_5m,
            MAX(price_change_5m) as max_change_5m,
            MIN(price_change_5m) as min_change_5m
        FROM tokens WHERE is_bundled = 0
    """, one=True)
    
    # Top movers
    top_gainers = query_db("""
        SELECT address, token_name, current_price, price_change_5m, current_market_cap, volume_5m
        FROM tokens WHERE is_bundled = 0 AND price_change_5m > 0
        ORDER BY price_change_5m DESC LIMIT 5
    """)
    top_losers = query_db("""
        SELECT address, token_name, current_price, price_change_5m, current_market_cap, volume_5m
        FROM tokens WHERE is_bundled = 0 AND price_change_5m < 0
        ORDER BY price_change_5m ASC LIMIT 5
    """)
    
    # Signal counts
    signal_count = len(list(SIGNAL_TOKENS_DIR.glob("*.json")))
    prediction_count = len(list(PREDICTIONS_DIR.glob("*.json")))
    
    # Performance
    perf_data = read_json_file(PERFORMANCE_FILE) or {}
    perf_trades = sum(s.get('trades', 0) for s in perf_data.values())
    perf_wins = sum(s.get('wins', 0) for s in perf_data.values())
    perf_losses = sum(s.get('losses', 0) for s in perf_data.values())
    perf_profit = sum(s.get('total_profit_sol', 0.0) for s in perf_data.values())
    
    return jsonify({
        'tokens': {
            'total': total['c'] if total else 0,
            'active': active['c'] if active else 0,
            'uptrending': uptrending['c'] if uptrending else 0,
            'new': new_tokens['c'] if new_tokens else 0,
            'bundled': bundled['c'] if bundled else 0,
        },
        'volume': vol_stats or {},
        'top_gainers': top_gainers,
        'top_losers': top_losers,
        'signals': {
            'pending_signals': signal_count,
            'pending_predictions': prediction_count,
        },
        'performance': {
            'total_trades': perf_trades,
            'wins': perf_wins,
            'losses': perf_losses,
            'profit_sol': round(perf_profit, 6),
            'win_rate': round((perf_wins / perf_trades * 100), 1) if perf_trades > 0 else 0,
        },
        'timestamp': time.time()
    })


@trading_bp.route('/api/profiles')
def api_profiles():
    """Get token profiles with price history (for charts)."""
    address = request.args.get('address')
    if address:
        profile = read_json_file(TOKEN_PROFILES_DIR / f"{address}.json")
        if profile:
            return jsonify({'profile': profile})
        return jsonify({'error': 'Profile not found'}), 404
    
    # List all profiles (summary only, no full price history)
    profiles = []
    for fp in sorted(TOKEN_PROFILES_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:50]:
        data = read_json_file(fp)
        if data:
            profiles.append({
                'address': data.get('address', fp.stem),
                'token_name': data.get('token_name', 'Unknown'),
                'current_price': data.get('current_price', 0),
                'ath_price': data.get('ath_price', 0),
                'current_market_cap': data.get('current_market_cap', 0),
                'is_uptrend': data.get('is_uptrend', 0),
                'is_new': data.get('is_new', 0),
                'price_points': len(data.get('price_history', [])),
                'last_updated': data.get('last_updated', ''),
            })
    return jsonify({'profiles': profiles, 'count': len(profiles)})


@trading_bp.route('/api/config', methods=['GET'])
def api_get_config():
    """Return current bot configuration."""
    return jsonify({
        'scanner': {
            'profit_target': 0.06,
            'stop_loss': -0.025,
            'max_hold_time': 15,
            'min_market_cap': 30000,
            'min_entry_threshold': 20000,
            'pool_activity_threshold': 50,
            'large_trade_threshold': 100,
            'min_30s_price_change': 3.0,
        },
        'executor': {
            'trade_amount_sol': 0.13,
            'profit_target_pct': 5.5,
            'stop_loss_pct': -2.5,
            'max_trade_duration_s': 35,
            'slippage_bps': 600,
            'max_slippage_bps': 2000,
            'max_holder_pct': 79,
        },
        'trend_agent': {
            'min_data_points': 15,
            'monitor_interval': 5,
            'export_cooldown': 30,
            'uptrend_threshold_pct': 5.0,
            'uptrend_window_s': 400,
        }
    })


@trading_bp.route('/api/jarvis-feed')
def api_jarvis_feed():
    """Jarvis AI trading decisions feed — social-style timeline."""
    feed = []
    # 1) sim_portfolio.json trade history
    from repryntt.paths import get_data_dir as _gdd
    sim_path = Path(str(_gdd())) / "sim_portfolio.json"
    if sim_path.exists():
        try:
            data = json.loads(sim_path.read_text())
            for t in data.get("trade_history", []):
                feed.append({
                    "source": "sim",
                    "type": t.get("type", "TRADE"),
                    "symbol": t.get("symbol", "?"),
                    "amount_usd": t.get("amount_usd", 0),
                    "price": t.get("price", 0),
                    "reason": t.get("reason", ""),
                    "ts": t.get("timestamp", 0),
                })
            # Current portfolio summary
            portfolio = {
                "cash": data.get("cash", 0),
                "positions": data.get("positions", {}),
            }
        except Exception:
            portfolio = {"cash": 0, "positions": {}}
    else:
        portfolio = {"cash": 0, "positions": {}}

    # 2) trade_journal.json from Jarvis workspace
    journal_path = Path(str(_gdd())) / "agent_workspaces" / "jarvis" / "trade_journal.json"
    if journal_path.exists():
        try:
            entries = json.loads(journal_path.read_text())
            if isinstance(entries, list):
                for e in entries:
                    feed.append({
                        "source": "journal",
                        "type": e.get("action", "SCAN"),
                        "symbol": e.get("symbol", ""),
                        "amount_usd": e.get("amount_usd", 0),
                        "price": e.get("price", 0),
                        "reason": e.get("reasoning", e.get("reason", "")),
                        "ts": e.get("timestamp", 0),
                        "strategy": e.get("strategy", ""),
                        "score": e.get("score", 0),
                    })
        except Exception:
            pass

    # Sort newest first
    feed.sort(key=lambda x: x.get("ts", 0), reverse=True)

    return jsonify({"feed": feed[:50], "portfolio": portfolio})


@trading_bp.route('/api/whale-monitor')
def api_whale_monitor():
    """Whale/KOL wallet monitor — tracked wallets, recent swaps, stats."""
    try:
        from repryntt.trading.whale_monitor import list_wallets, get_stats, get_recent_signals
        wallets = list_wallets()
        stats = get_stats()
        recent = get_recent_signals()
        return jsonify({
            'wallets': wallets,
            'stats': stats,
            'recent_signals': recent,
            'timestamp': time.time()
        })
    except Exception as e:
        return jsonify({
            'wallets': [],
            'stats': {},
            'recent_signals': [],
            'error': str(e),
            'timestamp': time.time()
        })


@trading_bp.route('/api/health')
def api_health():
    """Health check endpoint."""
    db_ok = False
    token_count = 0
    try:
        result = query_db("SELECT COUNT(*) as c FROM tokens", one=True)
        if result:
            db_ok = True
            token_count = result['c']
    except Exception:
        pass
    
    profiles_count = len(list(TOKEN_PROFILES_DIR.glob("*.json")))
    signals_count = len(list(SIGNAL_TOKENS_DIR.glob("*.json")))
    
    return jsonify({
        'status': 'ok' if db_ok else 'degraded',
        'database': db_ok,
        'token_count': token_count,
        'profiles_count': profiles_count,
        'pending_signals': signals_count,
        'uptime_s': time.time() - SERVER_START_TIME,
        'timestamp': time.time()
    })


# ─── Token Removal ───────────────────────────────────────────────────────────

@trading_bp.route('/api/token/<address>/remove', methods=['POST'])
def api_remove_token(address):
    """Remove a token from the database and active tracking."""
    if not address or len(address) < 10:
        return jsonify({'error': 'Invalid address'}), 400
    try:
        db = get_db()
        # Verify token exists
        row = db.execute("SELECT base_token_symbol, token_name FROM tokens WHERE address = ?", (address,)).fetchone()
        if not row:
            db.close()
            return jsonify({'error': 'Token not found'}), 404
        name = dict(row).get('token_name', '') or dict(row).get('base_token_symbol', address[:8])
        db.execute("DELETE FROM tokens WHERE address = ?", (address,))
        db.commit()
        db.close()
        # Also remove from active_tokens.json if present
        try:
            if ACTIVE_TOKENS_FILE.exists():
                at = json.loads(ACTIVE_TOKENS_FILE.read_text())
                if isinstance(at, list) and address in at:
                    at.remove(address)
                    ACTIVE_TOKENS_FILE.write_text(json.dumps(at))
                elif isinstance(at, dict) and address in at:
                    del at[address]
                    ACTIVE_TOKENS_FILE.write_text(json.dumps(at))
        except Exception:
            pass
        return jsonify({'status': 'removed', 'address': address, 'name': name})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@trading_bp.route('/api/tokens/purge-bad', methods=['POST'])
def api_purge_bad_tokens():
    """Remove all bundled and dead tokens from the database."""
    try:
        db = get_db()
        # Remove bundled tokens
        bundled = db.execute(
            "SELECT address, token_name FROM tokens WHERE is_bundled = 1"
        ).fetchall()
        # Remove dead tokens (mcap < $1K and price near zero)
        dead = db.execute(
            "SELECT address, token_name FROM tokens "
            "WHERE current_market_cap < 1000 AND current_price < 0.000001"
        ).fetchall()
        addresses = set()
        names = []
        for row in bundled:
            d = dict(row)
            addresses.add(d['address'])
            names.append(f"{d.get('token_name', '?')} (bundled)")
        for row in dead:
            d = dict(row)
            if d['address'] not in addresses:
                addresses.add(d['address'])
                names.append(f"{d.get('token_name', '?')} (dead)")
        for addr in addresses:
            db.execute("DELETE FROM tokens WHERE address = ?", (addr,))
        db.commit()
        db.close()
        return jsonify({
            'status': 'purged',
            'removed_count': len(addresses),
            'removed': names[:50],
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─── Artemis AI Trading Proxy ───────────────────────────────────────────────
NEXUS_BASE = os.environ.get('NEXUS_URL', 'http://127.0.0.1:8089')
PORTFOLIO_PATH = Path.home() / '.repryntt' / 'workspace' / 'agents' / 'operator' / 'sim_portfolio.json'


@trading_bp.route('/api/portfolio')
def api_portfolio():
    """Read the current sim portfolio state."""
    try:
        if PORTFOLIO_PATH.exists():
            data = json.loads(PORTFOLIO_PATH.read_text())
            return jsonify({
                'cash_balance': data.get('cash_balance', 0),
                'positions': data.get('positions', {}),
                'trade_count': len(data.get('trade_history', [])),
                'starting_balance': data.get('starting_balance', 0),
                'timestamp': time.time(),
            })
        return jsonify({'cash_balance': 0, 'positions': {}, 'trade_count': 0, 'timestamp': time.time()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@trading_bp.route('/api/artemis/command', methods=['POST'])
def api_artemis_command():
    """Proxy a trade command to the Nexus daemon's Jarvis invoke endpoint.

    Body: {"prompt": "...", "token_address": "...", "symbol": "...", "action": "..."}
    The prompt is sent as-is to Jarvis. Action/symbol/address are metadata for the UI.
    """
    import requests as req_lib

    body = request.get_json(force=True, silent=True) or {}
    prompt = body.get('prompt', '').strip()
    if not prompt:
        return jsonify({'error': 'prompt is required'}), 400
    if len(prompt) > 2000:
        return jsonify({'error': 'prompt too long (max 2000 chars)'}), 400

    try:
        resp = req_lib.post(
            f'{NEXUS_BASE}/api/jarvis',
            json={'prompt': prompt, 'max_tokens': 4000},
            timeout=180,
        )
        return jsonify(resp.json()), resp.status_code
    except req_lib.exceptions.Timeout:
        return jsonify({'error': 'Artemis timed out (180s) — she may still be working'}), 504
    except req_lib.exceptions.ConnectionError:
        return jsonify({'error': 'Cannot reach Artemis daemon — is it running?'}), 502
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@trading_bp.route('/api/artemis/stream', methods=['POST'])
def api_artemis_stream():
    """Proxy a trade command to Jarvis with SSE streaming response."""
    import requests as req_lib

    body = request.get_json(force=True, silent=True) or {}
    prompt = body.get('prompt', '').strip()
    if not prompt:
        return jsonify({'error': 'prompt is required'}), 400
    if len(prompt) > 2000:
        return jsonify({'error': 'prompt too long (max 2000 chars)'}), 400

    def generate():
        try:
            resp = req_lib.post(
                f'{NEXUS_BASE}/api/jarvis/stream',
                json={'prompt': prompt, 'max_tokens': 4000},
                timeout=180,
                stream=True,
            )
            for line in resp.iter_lines(decode_unicode=True):
                if line:
                    yield line + '\n\n'
            yield 'data: {"type":"done"}\n\n'
        except req_lib.exceptions.ConnectionError:
            yield 'data: {"type":"error","message":"Cannot reach Artemis daemon"}\n\n'
        except Exception as e:
            yield f'data: {{"type":"error","message":"{str(e)[:200]}"}}\n\n'

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ─── Error Handlers ─────────────────────────────────────────────────────────
@trading_bp.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Not found'}), 404

@trading_bp.errorhandler(500)
def server_error(e):
    return jsonify({'error': 'Internal server error'}), 500


# ─── Main ────────────────────────────────────────────────────────────────────
SERVER_START_TIME = time.time()

if __name__ == '__main__':
    app.register_blueprint(trading_bp)
    print("=" * 60)
    print("  🔥 DEGEN TERMINAL v1.0")
    print("  Solana Memecoin Scalper Platform")
    print(f"  Dashboard: http://localhost:8888")
    print(f"  API Base:  http://localhost:8888/api")
    print(f"  Database:  {DB_PATH}")
    print(f"  Profiles:  {TOKEN_PROFILES_DIR}")
    print("=" * 60)
    
    app.run(
        host='0.0.0.0',
        port=8888,
        debug=False,
        threaded=True,
    )
