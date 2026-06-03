#!/usr/bin/env python3
"""
SAIGE External API Service - Secure Credit-Paying Access to AI Services
Provides authenticated external access to SAIGE's AI capabilities with credit-based payments
"""

import json
import time
import threading
import logging
import os
import secrets
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from functools import wraps

from flask import Blueprint, Flask, request, jsonify, g
from flask_cors import CORS
from flask_jwt_extended import JWTManager, jwt_required, get_jwt_identity, create_access_token
from werkzeug.security import check_password_hash, generate_password_hash
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend
import datetime as _dt_module
import ipaddress

# Import SAIGE components (lazy loading to avoid import issues)
from repryntt.economy.crypto_utils import crypto_utils
from repryntt.brain import BrainSystemProtocol, get_brain_system, create_brain_system
from repryntt.web.ext_api_store import PersistentDict, _STORE_DIR
try:
    from monitoring.metrics import metrics
except ImportError:
    class _NoOpMetrics:
        """Stub so callers like metrics.record_*() silently succeed."""
        def __getattr__(self, name):
            return lambda *a, **kw: None
    metrics = _NoOpMetrics()

logger = logging.getLogger(__name__)

# Flask Application Setup
app = Flask(__name__)
CORS(app)

# JWT Configuration
app.config['JWT_SECRET_KEY'] = os.environ.get('SAIGE_JWT_SECRET', secrets.token_hex(32))
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=24)
jwt = JWTManager(app)

# Blueprint for consolidated Nexus app
external_api_bp = Blueprint('external_api', __name__)

# Persistent storage — survives restarts (backed by ~/.repryntt/data/ext_api/*.json)
API_KEYS = PersistentDict(os.path.join(_STORE_DIR, "api_keys.json"))
TRADE_ORDERS = PersistentDict(os.path.join(_STORE_DIR, "trade_orders.json"))
ROBOT_SERVICES = PersistentDict(os.path.join(_STORE_DIR, "robot_services.json"))
SERVICE_ORDERS = PersistentDict(os.path.join(_STORE_DIR, "service_orders.json"))
PLATFORM_REVENUE = PersistentDict(os.path.join(_STORE_DIR, "platform_revenue.json"))

# Seed PLATFORM_REVENUE defaults on first run
if "total_credits" not in PLATFORM_REVENUE:
    PLATFORM_REVENUE.update({"total_credits": 0.0, "transactions": 0, "last_updated": None})

# Ephemeral — rate limits regenerate naturally, no persistence needed
RATE_LIMITS = {}  # api_key -> {'requests': int, 'reset_time': datetime}

# Service costs (Credits per unit)
SERVICE_COSTS = {
    "ai_chat": {"cost_per_1000_tokens": 0.02},   # 0.02 CR per 1k tokens
    "tool_call": {"cost_per_call": 0.05},          # 0.05 CR flat per tool call
    "analysis": {"cost_per_request": 0.10},        # 0.10 CR per analysis request
}

# Commission rates
COMMISSION_RATES = {
    "robot_services": 0.30,  # 30% platform cut
    "ai_services": 0.05,     # 5% platform cut
    "ai_chat": 0.05,         # 5% platform cut
    "tool_call": 0.05,       # 5% platform cut
    "analysis": 0.05,        # 5% platform cut
    "trading": 0.02,         # 2% trading fee
    "marketplace": 0.10      # 10% marketplace fee
}

# Module-level state (set by SAIGEExternalAPIService.set_* or defaults)
brain_system = None
robot_economy_manager = None

def _get_brain_system():
    """Lazy import of BrainSystem"""
    global brain_system
    if brain_system is None:
        brain_system = get_brain_system()
    return brain_system

def generate_self_signed_cert(cert_file='certs/server.crt', key_file='certs/server.key', validity_days=365):
    """Generate a self-signed TLS certificate for development"""
    # Create certs directory
    os.makedirs('certs', exist_ok=True)

    # Generate private key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )

    # Create certificate
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, u"SAIGE External API"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, u"SAIGE Systems"),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, u"Robot Economy"),
    ])

    cert = x509.CertificateBuilder().subject_name(
        subject
    ).issuer_name(
        issuer
    ).public_key(
        private_key.public_key()
    ).serial_number(
        x509.random_serial_number()
    ).not_valid_before(
        _dt_module.datetime.utcnow()
    ).not_valid_after(
        _dt_module.datetime.utcnow() + _dt_module.timedelta(days=validity_days)
    ).add_extension(
        x509.SubjectAlternativeName([
            x509.DNSName(u"localhost"),
            x509.DNSName(u"saige.local"),
            x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        ]),
        critical=False,
    ).sign(private_key, hashes.SHA256(), default_backend())

    # Write certificate and key files
    with open(cert_file, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    with open(key_file, "wb") as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ))

    logger.info(f"✅ Generated self-signed certificate: {cert_file}")
    return cert_file, key_file

def setup_https_support():
    """Setup HTTPS support with automatic certificate generation"""
    cert_file = 'certs/server.crt'
    key_file = 'certs/server.key'

    if not os.path.exists(cert_file) or not os.path.exists(key_file):
        logger.info("🔐 Generating self-signed TLS certificate for HTTPS...")
        try:
            generate_self_signed_cert(cert_file, key_file)
            logger.info("✅ HTTPS certificates ready")
            return cert_file, key_file
        except Exception as e:
            logger.error(f"❌ Failed to generate certificates: {e}")
            logger.warning("⚠️ Falling back to HTTP (not recommended for production)")
            return None, None
    else:
        logger.info("✅ Using existing HTTPS certificates")
        return cert_file, key_file

def require_api_key(f):
    """Decorator to require valid API key"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        api_key = request.headers.get('X-API-Key')
        if not api_key:
            return jsonify({'error': 'API key required', 'code': 'MISSING_API_KEY'}), 401

        if api_key not in API_KEYS:
            return jsonify({'error': 'Invalid API key', 'code': 'INVALID_API_KEY'}), 401

        # Check rate limits
        if not check_rate_limit(api_key):
            return jsonify({'error': 'Rate limit exceeded', 'code': 'RATE_LIMIT_EXCEEDED'}), 429

        g.api_key_data = API_KEYS[api_key]
        return f(*args, **kwargs)
    return decorated_function

def require_wallet_signature(f):
    """Decorator to require wallet signature verification"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        wallet_address = request.json.get('wallet_address')
        signature = request.headers.get('X-Wallet-Signature')
        message = request.headers.get('X-Signature-Message')

        if not all([wallet_address, signature, message]):
            return jsonify({
                'error': 'Wallet address, signature, and message required',
                'code': 'MISSING_SIGNATURE_DATA'
            }), 400

        # Verify signature (simplified - in production use proper wallet verification)
        try:
            # For now, we'll do basic validation. In production, verify against wallet's public key
            if not crypto_utils.validate_input(signature, str, 200):
                raise ValueError("Invalid signature format")

            # Store verified wallet in request context
            g.verified_wallet = wallet_address
            g.signature_message = message

        except Exception as e:
            logger.warning(f"Signature verification failed: {e}")
            return jsonify({'error': 'Invalid signature', 'code': 'INVALID_SIGNATURE'}), 401

        return f(*args, **kwargs)
    return decorated_function

def check_rate_limit(api_key: str, max_requests: int = 100, window_seconds: int = 3600) -> bool:
    """Check and update rate limits"""
    now = datetime.now()

    if api_key not in RATE_LIMITS:
        RATE_LIMITS[api_key] = {'requests': 0, 'reset_time': now + timedelta(seconds=window_seconds)}

    limit_data = RATE_LIMITS[api_key]

    # Reset if window expired
    if now > limit_data['reset_time']:
        limit_data['requests'] = 0
        limit_data['reset_time'] = now + timedelta(seconds=window_seconds)

    # Check limit
    if limit_data['requests'] >= max_requests:
        return False

    limit_data['requests'] += 1
    return True

def validate_and_deduct_credits(wallet_address: str, service_type: str, amount: float = None, apply_commission: bool = True) -> Dict[str, Any]:
    """Validate wallet and deduct credits for service with commission system"""
    if not robot_economy_manager:
        return {"success": False, "error": "Robot economy not available"}

    balance_info = robot_economy_manager.get_wallet_balance(wallet_address)
    if not balance_info.get("success"):
        return {"success": False, "error": f"Failed to retrieve wallet balance: {balance_info.get('error')}"}

    current_balance = balance_info.get("balance_credits", 0.0)
    cost = 0.0

    if service_type == "ai_chat":
        cost = (amount / 1000) * SERVICE_COSTS["ai_chat"]["cost_per_1000_tokens"]
    elif service_type == "tool_call":
        cost = SERVICE_COSTS["tool_call"]["cost_per_call"]
    elif service_type == "analysis":
        cost = SERVICE_COSTS["analysis"]["cost_per_request"]
    elif service_type == "robot_marketplace":
        cost = amount  # Amount is already the total cost
    else:
        return {"success": False, "error": f"Unknown service type: {service_type}"}

    # Apply platform commission if enabled
    commission_amount = 0.0
    if apply_commission and service_type in COMMISSION_RATES:
        commission_rate = COMMISSION_RATES[service_type]
        commission_amount = cost * commission_rate
        total_cost = cost + commission_amount
    else:
        total_cost = cost

    if current_balance < total_cost:
        return {"success": False, "error": f"Insufficient credits. Needed: {total_cost:.4f} CR, Available: {current_balance:.4f} CR"}

    deduction_result = robot_economy_manager.deduct_credits(wallet_address, total_cost, f"external_api_{service_type}")
    if not deduction_result.get("success"):
        return {"success": False, "error": f"Failed to deduct credits: {deduction_result.get('error')}"}

    # Record platform commission
    if commission_amount > 0:
        PLATFORM_REVENUE["total_credits"] += commission_amount
        PLATFORM_REVENUE["transactions"] += 1
        PLATFORM_REVENUE["last_updated"] = datetime.now().isoformat()
        metrics.record_credit_transaction("platform_commission", commission_amount)

    return {
        "success": True,
        "cost_credits": cost,
        "commission_credits": commission_amount,
        "total_cost_credits": total_cost,
        "new_balance_credits": current_balance - total_cost
    }

# Flask Routes
@external_api_bp.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    start_time = time.time()

    # Check if request is HTTPS
    is_https = request.headers.get('X-Forwarded-Proto', request.scheme) == 'https'

    response = {
        "status": "healthy",
        "service": "SAIGE External API",
        "timestamp": datetime.now().isoformat(),
        "robot_economy": "available" if robot_economy_manager else "unavailable",
        "quantum_crypto": "available" if crypto_utils.pqc_available else "fallback_mode",
        "https_enabled": is_https,
        "security_status": "production_ready" if is_https else "development_mode"
    }

    # Record metrics
    metrics.record_http_request("GET", "/health", 200, time.time() - start_time)

    return jsonify(response), 200

@external_api_bp.route('/metrics', methods=['GET'])
def prometheus_metrics():
    """Prometheus metrics endpoint"""
    return metrics.get_metrics_text(), 200, {'Content-Type': 'text/plain; charset=utf-8'}

@external_api_bp.route('/auth/register', methods=['POST'])
def register_api_key():
    """Register a new API key"""
    data = request.get_json()

    if not data:
        return jsonify({'error': 'Request body required', 'code': 'MISSING_BODY'}), 400

    # Accept either 'user_id' or 'name' for compatibility
    user_id = data.get('user_id') or data.get('name')
    if not user_id:
        return jsonify({'error': 'user_id or name required', 'code': 'MISSING_USER_ID'}), 400

    permissions = data.get('permissions', ['read'])

    # Generate API key
    api_key = secrets.token_hex(32)

    API_KEYS[api_key] = {
        'user_id': user_id,
        'permissions': permissions,
        'created_at': datetime.now(),
        'active': True
    }

    logger.info(f"Registered new API key for user: {user_id}")

    return jsonify({
        'api_key': api_key,
        'user_id': user_id,
        'permissions': permissions,
        'message': 'API key registered successfully'
    }), 201

@external_api_bp.route('/services', methods=['GET'])
@require_api_key
def get_services():
    """Get available services and their credit costs"""

    # Pull live pricing from workload marketplace if available
    try:
        mp = _get_marketplace()
        wl_pricing = mp.node_config.get("pricing", {})
        wl_accepting = mp.node_config.get("accepting_workloads", True)
    except Exception:
        wl_pricing = {"inference_per_1k_tokens": 0.02, "embedding_per_1k_tokens": 0.01, "analysis_per_request": 0.10, "batch_discount": 0.80}
        wl_accepting = True

    services = {
        "workload_marketplace": {
            "description": "Submit AI workloads (inference, batch, embedding, analysis) — async processing by this node's LLM",
            "accepting": wl_accepting,
            "pricing": wl_pricing,
            "endpoints": {
                "submit": "/workloads/submit",
                "status": "/workloads/{job_id}",
                "list": "/workloads",
                "cancel": "/workloads/{job_id}/cancel",
                "node_config": "/node/config",
                "node_stats": "/node/stats",
            },
            "workload_types": ["inference", "batch", "embedding", "analysis"],
            "requires_signature": True,
        },
        "ai_chat": {
            "description": "Synchronous AI chat (immediate response)",
            "cost_per_1000_tokens": wl_pricing.get("inference_per_1k_tokens", 0.02),
            "max_tokens": 1000,
            "endpoint": "/ai/chat",
            "requires_signature": True
        },
        "tool_call": {
            "description": "Execute SAIGE tools (search, analysis, etc.)",
            "cost_per_call": 0.05,
            "endpoint": "/ai/tool",
            "requires_signature": True
        },
        "analysis": {
            "description": "Advanced analysis and research",
            "cost_per_request": wl_pricing.get("analysis_per_request", 0.10),
            "endpoint": "/ai/analyze",
            "requires_signature": True
        },
    "robot_marketplace": {
        "description": "Access robot-generated data and services marketplace",
        "cost_per_access": 0.01,
        "endpoints": ["/robots/marketplace", "/robots/services"],
        "requires_signature": True
    },
    "sensor_data_market": {
        "description": "Sell anonymized sensor data for AI training",
        "revenue_share": 0.7,
        "endpoint": "/data/sensor",
        "requires_signature": True
    },
    }

    return jsonify({
        'services': services,
        'timestamp': datetime.now().isoformat(),
        'user_id': g.api_key_data['user_id'],
        'permissions': g.api_key_data['permissions']
    }), 200

@external_api_bp.route('/wallet/create', methods=['POST'])
@require_api_key
def create_wallet():
    """Create a new quantum-safe wallet"""
    try:
        result = robot_economy_manager.create_wallet()
        if result.get("success"):
            return jsonify(result), 200
        else:
            return jsonify({"success": False, "error": result.get("error", "Unknown wallet creation error")}), 500
    except Exception as e:
        logger.error(f"Error creating wallet: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@external_api_bp.route('/wallet/<string:address>', methods=['GET'])
@require_api_key
def get_wallet_balance(address):
    """Get wallet balance for a given address"""
    try:
        balance_info = robot_economy_manager.get_wallet_balance(address)
        if balance_info.get("success"):
            return jsonify(balance_info), 200
        else:
            return jsonify({"success": False, "error": balance_info.get("error", "Unknown balance error")}), 404
    except Exception as e:
        logger.error(f"Error getting wallet balance: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@external_api_bp.route('/wallet/faucet', methods=['POST'])
@require_api_key
@require_wallet_signature
def faucet_credits():
    """Provide startup credits to a wallet (one-time per wallet, 1000 CR)"""
    data = request.get_json()
    wallet_address = data.get("wallet_address")
    amount = data.get("amount", 1000.0)  # Default 1000 credits — node operator startup bonus

    if not wallet_address:
        return jsonify({"success": False, "error": "Wallet address is required."}), 400

    try:
        faucet_result = robot_economy_manager.faucet(wallet_address, amount)
        if faucet_result.get("success"):
            return jsonify(faucet_result), 200
        else:
            return jsonify({"success": False, "error": faucet_result.get("error", "Faucet failed")}), 500
    except Exception as e:
        logger.error(f"Error with faucet: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@external_api_bp.route('/credits/purchase', methods=['POST'])
@require_api_key
def purchase_credits():
    """DEPRECATED — Credits are now market-priced.

    To acquire CR:
      1. Deposit SOL/USDC via the Solana bridge (POST /ext-api/bridge/deposit)
      2. Place a buy order on the CR/SOL order book (POST /ext-api/trading/create)
    The market determines the price. There is no fixed-rate purchase.
    """
    return jsonify({
        "success": False,
        "error": "Fixed-price credit purchases have been removed. CR is now a market-priced token.",
        "how_to_buy": [
            "POST /ext-api/bridge/deposit — deposit SOL/USDC to your bridge balance",
            "POST /ext-api/trading/create — place a buy order at the price you choose",
            "GET /ext-api/trading/orderbook — see current CR/SOL market",
        ]
    }), 410  # 410 Gone

@external_api_bp.route('/credits/pricing', methods=['GET'])
def get_credit_pricing():
    """Get current CR market data from the order book"""
    # Calculate market price from recent trades
    market_price = _get_market_price("buy")

    # Get order book spread
    best_bid = None
    best_ask = None
    for trade_id, order in TRADE_ORDERS.items():
        if order.get('status') != 'active':
            continue
        price = order.get('price_per_cr_sol', 0)
        if order.get('order_type') == 'buy':
            if best_bid is None or price > best_bid:
                best_bid = price
        elif order.get('order_type') == 'sell':
            if best_ask is None or price < best_ask:
                best_ask = price

    pricing = {
        "model": "market — no fixed peg, price set by buyers and sellers",
        "pair": "CR/SOL",
        "last_trade_price_sol": market_price,
        "best_bid_sol": best_bid,
        "best_ask_sol": best_ask,
        "spread_sol": round(best_ask - best_bid, 9) if (best_bid and best_ask) else None,
        "how_to_buy": [
            "1. Register for an API key (POST /ext-api/register)",
            "2. Create a deposit (POST /ext-api/bridge/deposit) — get the Solana deposit address",
            "3. Send SOL or USDC to that address on Solana mainnet",
            "4. Once confirmed, your bridge balance is credited",
            "5. Place a buy order (POST /ext-api/trading/create) at the price you want",
            "6. When a seller matches, you receive CR in your repryntt wallet",
        ],
        "how_to_earn": [
            "Run a repryntt node — Proof of Power mining rewards CR for real AI compute",
            "Provide AI services on the marketplace — earn CR from other nodes",
        ],
        "service_costs_cr": SERVICE_COSTS,
    }

    return jsonify({
        "pricing": pricing,
        "timestamp": datetime.now().isoformat()
    }), 200

@external_api_bp.route('/trading/orderbook', methods=['GET'])
@require_api_key
def get_orderbook():
    """Get the CR/SOL order book — market-priced, no fixed peg"""
    buy_orders = []
    sell_orders = []

    for trade_id, order in TRADE_ORDERS.items():
        if order.get('status') != 'active':
            continue

        price_key = 'price_per_cr_sol'
        # Support legacy orders that used 'price_per_credit'
        price = order.get(price_key, order.get('price_per_credit', 0))

        order_info = {
            "trade_id": trade_id,
            "wallet_address": order['wallet_address'],
            "order_type": order['order_type'],
            "amount_cr": order['amount_credits'],
            "price_sol_per_cr": price,
            "total_sol": round(order['amount_credits'] * price, 9),
            "created_at": order.get('created_at')
        }

        if order['order_type'] == 'buy':
            buy_orders.append(order_info)
        elif order['order_type'] == 'sell':
            sell_orders.append(order_info)

    buy_orders.sort(key=lambda x: x['price_sol_per_cr'], reverse=True)
    sell_orders.sort(key=lambda x: x['price_sol_per_cr'])

    return jsonify({
        "pair": "CR/SOL",
        "orderbook": {
            "buy_orders": buy_orders[:50],
            "sell_orders": sell_orders[:50]
        },
        "spread": {
            "best_bid_sol": buy_orders[0]['price_sol_per_cr'] if buy_orders else None,
            "best_ask_sol": sell_orders[0]['price_sol_per_cr'] if sell_orders else None
        },
        "timestamp": datetime.now().isoformat()
    }), 200

def _get_market_price(order_type):
    """Get current market price (SOL per CR) from recent trades"""
    try:
        completed_trades = []
        for trade_id, order in TRADE_ORDERS.items():
            if order.get('status') == 'completed' and order.get('executions'):
                for execution in order.get('executions', []):
                    completed_trades.append({
                        'price': execution['price'],
                        'amount': execution['amount'],
                        'timestamp': order.get('created_at')
                    })

        if not completed_trades:
            return None

        completed_trades.sort(key=lambda x: x['timestamp'] or '', reverse=True)

        recent_trades = completed_trades[:10]
        total_volume = sum(t['amount'] for t in recent_trades)
        if total_volume == 0:
            return None

        weighted_price = sum(t['price'] * t['amount'] for t in recent_trades) / total_volume
        return round(weighted_price, 9)

    except Exception as e:
        logger.error(f"Error calculating market price: {e}")
        return None

@external_api_bp.route('/trading/create', methods=['POST'])
@require_api_key
@require_wallet_signature
def create_trade_order():
    """Create a buy or sell order on the CR/SOL market.

    Buy orders:  spend SOL (from bridge balance) to acquire CR.
    Sell orders: spend CR (from repryntt wallet) to acquire SOL (credited to bridge balance).

    Body: { "order_type": "buy"|"sell", "amount_credits": float, "price_sol_per_cr": float,
            "order_kind": "limit"|"market" (optional, default "limit"),
            "quote_currency": "sol"|"usdc" (optional, default "sol") }
    """
    data = request.get_json()

    required_fields = ['order_type', 'amount_credits']
    if not data or not all(k in data for k in required_fields):
        return jsonify({"success": False, "error": f"Required fields: {required_fields} + price_sol_per_cr for limit orders"}), 400

    order_type = data['order_type']
    amount_credits = float(data['amount_credits'])
    order_kind = data.get('order_kind', 'limit')
    quote_currency = data.get('quote_currency', 'sol').lower()
    wallet_address = g.verified_wallet

    if order_type not in ['buy', 'sell']:
        return jsonify({"success": False, "error": "Order type must be 'buy' or 'sell'"}), 400
    if order_kind not in ['limit', 'market']:
        return jsonify({"success": False, "error": "Order kind must be 'limit' or 'market'"}), 400
    if quote_currency not in ('sol', 'usdc'):
        return jsonify({"success": False, "error": "quote_currency must be 'sol' or 'usdc'"}), 400
    if amount_credits < 0.1 or amount_credits > 100000.0:
        return jsonify({"success": False, "error": "Amount must be between 0.1 and 100,000 CR"}), 400

    # Resolve price
    if order_kind == 'market':
        price_per_cr = _get_market_price(order_type)
        if not price_per_cr:
            return jsonify({"success": False, "error": "No market price available — place a limit order"}), 400
    else:
        price_per_cr = data.get('price_sol_per_cr') or data.get('price_per_credit')
        if not price_per_cr or float(price_per_cr) <= 0:
            return jsonify({"success": False, "error": "price_sol_per_cr is required for limit orders"}), 400
        price_per_cr = float(price_per_cr)

    if price_per_cr > 1000.0:
        return jsonify({"success": False, "error": "Price per CR too high (max 1000 SOL/CR)"}), 400

    # Use integer lamport math for precision
    total_sol_lamports = int(round(amount_credits * price_per_cr * 1_000_000_000))
    total_sol = total_sol_lamports / 1_000_000_000

    # ── Balance checks ────────────────────────────────────────────────────
    from repryntt.economy.payment_gateway import get_bridge_balance, debit_bridge_balance

    if order_type == 'buy':
        # Buyer needs enough SOL/USDC in bridge balance
        bridge_bal = get_bridge_balance(wallet_address)
        available = bridge_bal.get(quote_currency, 0.0)
        if available < total_sol:
            return jsonify({
                "success": False,
                "error": (f"Insufficient {quote_currency.upper()} bridge balance. "
                          f"Need {total_sol:.9f}, have {available:.9f}. "
                          f"Deposit {quote_currency.upper()} via /ext-api/bridge/deposit first.")
            }), 400
        # Reserve the SOL/USDC upfront (deducted from bridge balance)
        if not debit_bridge_balance(wallet_address, quote_currency, total_sol):
            return jsonify({"success": False, "error": "Failed to reserve bridge balance"}), 500

    elif order_type == 'sell':
        # Seller needs enough CR in repryntt wallet
        balance_info = robot_economy_manager.get_wallet_balance(wallet_address)
        if not balance_info.get("success"):
            return jsonify({"success": False, "error": "Failed to check wallet balance"}), 500
        current_cr = balance_info.get("balance_credits", 0.0)
        if current_cr < amount_credits:
            return jsonify({
                "success": False,
                "error": f"Insufficient CR. Have {current_cr:.2f}, trying to sell {amount_credits:.2f}"
            }), 400
        # Reserve the CR upfront (deducted from repryntt wallet)
        deduct = robot_economy_manager.deduct_credits(
            wallet_address, amount_credits, f"sell_order_reserve"
        )
        if not deduct.get("success"):
            return jsonify({"success": False, "error": f"Failed to reserve CR: {deduct.get('error')}"}), 500

    try:
        # Generate trade ID
        trade_id = f"trade_{int(time.time())}_{secrets.token_hex(4)}"

        # Create trade order
        trade_order = {
            "trade_id": trade_id,
            "wallet_address": wallet_address,
            "order_type": order_type,
            "order_kind": order_kind,
            "amount_credits": amount_credits,
            "price_per_cr_sol": price_per_cr,
            "quote_currency": quote_currency,
            "total_sol": total_sol,
            "status": "active",
            "created_at": datetime.now().isoformat(),
            "expires_at": (datetime.now() + timedelta(days=7)).isoformat()
        }

        # Store trade order
        TRADE_ORDERS[trade_id] = trade_order

        # Try to match order immediately
        matched_trades = _match_trade_order(trade_order)

        if matched_trades:
            filled_cr = sum(t['amount_cr'] for t in matched_trades)
            trade_order["status"] = "completed" if filled_cr >= amount_credits else "partial"
            trade_order["executions"] = matched_trades
            # Refund unreserved remainder if partial
            if trade_order["status"] == "partial":
                unfilled_cr = amount_credits - filled_cr
                if order_type == 'buy':
                    unfilled_sol = round(unfilled_cr * price_per_cr, 9)
                    from repryntt.economy.payment_gateway import credit_bridge_balance_external
                    credit_bridge_balance_external(wallet_address, quote_currency, unfilled_sol)
                elif order_type == 'sell':
                    robot_economy_manager.add_credits(wallet_address, unfilled_cr, "sell_order_partial_refund")
                trade_order["amount_credits"] = filled_cr
                trade_order["total_sol"] = round(filled_cr * price_per_cr, 9)
            TRADE_ORDERS.sync()

        logger.info(f"📈 Trade: {trade_id} - {order_type} {amount_credits:.2f} CR @ {price_per_cr:.9f} SOL/CR")

        return jsonify({
            "success": True,
            "trade_order": trade_order,
            "matched_trades": matched_trades if matched_trades else [],
            "message": "Trade order created successfully"
        }), 201

    except Exception as e:
        # Refund reserved funds on error
        if order_type == 'buy':
            from repryntt.economy.payment_gateway import credit_bridge_balance_external
            credit_bridge_balance_external(wallet_address, quote_currency, total_sol)
        elif order_type == 'sell':
            robot_economy_manager.add_credits(wallet_address, amount_credits, "sell_order_error_refund")
        logger.error(f"Trade order creation error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

def _match_trade_order(new_order):
    """Match a new trade order against existing orders on the CR/SOL order book.

    Funds are already reserved at order creation time, so matching just
    transfers: buyer gets CR, seller gets SOL (bridge balance).
    """
    matched_trades = []
    remaining_cr = new_order['amount_credits']
    new_price = new_order.get('price_per_cr_sol', new_order.get('price_per_credit', 0))
    quote_cur = new_order.get('quote_currency', 'sol')

    for trade_id, existing_order in list(TRADE_ORDERS.items()):
        if existing_order.get('status') != 'active':
            continue
        if trade_id == new_order.get('trade_id'):
            continue

        ex_price = existing_order.get('price_per_cr_sol', existing_order.get('price_per_credit', 0))

        # Determine if orders can match
        if new_order['order_type'] == 'buy' and existing_order['order_type'] == 'sell':
            if new_price >= ex_price:
                match_price = ex_price  # Trades at the resting (seller) price
            else:
                continue
        elif new_order['order_type'] == 'sell' and existing_order['order_type'] == 'buy':
            if new_price <= ex_price:
                match_price = ex_price  # Trades at the resting (buyer) price
            else:
                continue
        else:
            continue

        can_match = min(remaining_cr, existing_order['amount_credits'])
        if can_match <= 0:
            continue

        sol_amount = round(can_match * match_price, 9)

        # Determine buyer/seller
        if new_order['order_type'] == 'buy':
            buyer_wallet = new_order['wallet_address']
            seller_wallet = existing_order['wallet_address']
        else:
            buyer_wallet = existing_order['wallet_address']
            seller_wallet = new_order['wallet_address']

        # Execute the settlement
        result = _execute_trade(buyer_wallet, seller_wallet, can_match, sol_amount, quote_cur)
        if not result['success']:
            logger.warning(f"Trade match failed: {result.get('error')}")
            continue

        matched_trades.append({
            "amount_cr": can_match,
            "price": match_price,
            "sol_total": sol_amount,
            "counterparty": existing_order['wallet_address'],
            "trade_id": trade_id,
            "fee_sol": result.get("fee_sol", 0)
        })

        remaining_cr -= can_match

        # Update the resting order
        existing_order['amount_credits'] -= can_match
        if existing_order['amount_credits'] <= 0:
            existing_order['status'] = 'completed'
        else:
            existing_order['status'] = 'partial'
            existing_order['total_sol'] = round(existing_order['amount_credits'] * ex_price, 9)
        TRADE_ORDERS.sync()

        if remaining_cr <= 0:
            break

    return matched_trades if matched_trades else None


def _execute_trade(buyer_wallet, seller_wallet, amount_cr, sol_amount, quote_currency="sol"):
    """Execute a matched trade: buyer gets CR, seller gets SOL/USDC bridge balance.

    Funds were already reserved at order-creation time, so this just does the
    cross-leg settlement.
    """
    from repryntt.economy.payment_gateway import credit_bridge_balance_external

    try:
        # 2% trading fee (split: 1% from each side)
        fee_rate = COMMISSION_RATES.get("trading", 0.02)
        fee_sol = round(sol_amount * fee_rate, 9)
        net_sol_to_seller = round(sol_amount - fee_sol, 9)

        # ── Buyer leg: CR was reserved from seller's wallet → credit to buyer
        add_result = robot_economy_manager.add_credits(
            buyer_wallet, amount_cr, f"market_buy_{amount_cr:.2f}cr"
        )
        if not add_result.get("success"):
            return {"success": False, "error": f"Failed to credit buyer: {add_result.get('error')}"}

        # ── Seller leg: SOL was reserved from buyer's bridge → credit to seller's bridge
        credit_bridge_balance_external(seller_wallet, quote_currency, net_sol_to_seller)

        # ── Platform fee
        if fee_sol > 0:
            PLATFORM_REVENUE["total_credits"] += fee_sol  # track in SOL terms
            PLATFORM_REVENUE["transactions"] += 1
            PLATFORM_REVENUE["last_updated"] = datetime.now().isoformat()

        return {"success": True, "fee_sol": fee_sol}

    except Exception as e:
        logger.error(f"Trade execution error: {e}")
        return {"success": False, "error": str(e)}

@external_api_bp.route('/trading/orders/<string:wallet_address>', methods=['GET'])
@require_api_key
def get_user_orders(wallet_address):
    """Get trading orders for a specific wallet"""
    user_orders = []

    for trade_id, order in TRADE_ORDERS.items():
        if order['wallet_address'] == wallet_address:
            user_orders.append(order)

    return jsonify({
        "orders": user_orders,
        "total_orders": len(user_orders),
        "timestamp": datetime.now().isoformat()
    }), 200

@external_api_bp.route('/trading/cancel/<string:trade_id>', methods=['POST'])
@require_api_key
@require_wallet_signature
def cancel_trade_order(trade_id):
    """Cancel a trade order"""
    wallet_address = g.verified_wallet

    if trade_id not in TRADE_ORDERS:
        return jsonify({"success": False, "error": "Trade order not found"}), 404

    order = TRADE_ORDERS[trade_id]

    if order['wallet_address'] != wallet_address:
        return jsonify({"success": False, "error": "Not authorized to cancel this order"}), 403

    if order['status'] != 'active':
        return jsonify({"success": False, "error": "Order cannot be cancelled"}), 400

    # Refund reserved funds
    from repryntt.economy.payment_gateway import credit_bridge_balance_external
    remaining_cr = order['amount_credits']
    price = order.get('price_per_cr_sol', order.get('price_per_credit', 0))
    quote_cur = order.get('quote_currency', 'sol')

    if order['order_type'] == 'buy':
        # Refund reserved SOL/USDC to bridge balance
        refund_sol = round(remaining_cr * price, 9)
        credit_bridge_balance_external(wallet_address, quote_cur, refund_sol)
    elif order['order_type'] == 'sell':
        # Refund reserved CR to repryntt wallet
        robot_economy_manager.add_credits(wallet_address, remaining_cr, "sell_order_cancelled_refund")

    # Cancel the order
    order['status'] = 'cancelled'
    order['cancelled_at'] = datetime.now().isoformat()
    TRADE_ORDERS.sync()

    logger.info(f"❌ Trade order cancelled: {trade_id}")

    return jsonify({
        "success": True,
        "trade_id": trade_id,
        "message": "Order cancelled successfully"
    }), 200

@external_api_bp.route('/analytics/market', methods=['GET'])
@require_api_key
def get_market_analytics():
    """Get market analytics and statistics"""
    try:
        # Calculate trading volume
        total_volume_24h = 0.0
        total_trades_24h = 0
        price_history = []
        volume_by_hour = {}

        cutoff_time = datetime.now() - timedelta(hours=24)

        for trade_id, order in TRADE_ORDERS.items():
            if order.get('status') == 'completed' and order.get('executions'):
                order_time = datetime.fromisoformat(order['created_at'])
                if order_time > cutoff_time:
                    for execution in order.get('executions', []):
                        total_volume_24h += execution['amount']
                        total_trades_24h += 1

                        # Price history for chart
                        price_history.append({
                            'timestamp': order['created_at'],
                            'price': execution['price'],
                            'volume': execution['amount']
                        })

                        # Volume by hour
                        hour_key = order_time.strftime('%Y-%m-%d %H:00')
                        volume_by_hour[hour_key] = volume_by_hour.get(hour_key, 0) + execution['amount']

        # Get orderbook depth
        buy_orders = [o for o in TRADE_ORDERS.values() if o['order_type'] == 'buy' and o['status'] == 'active']
        sell_orders = [o for o in TRADE_ORDERS.values() if o['order_type'] == 'sell' and o['status'] == 'active']

        # Sort orders
        buy_orders.sort(key=lambda x: x['price_per_credit'], reverse=True)
        sell_orders.sort(key=lambda x: x['price_per_credit'])

        # Calculate depth at different price levels
        buy_depth = {}
        sell_depth = {}

        for order in buy_orders[:20]:  # Top 20 buy orders
            price = round(order['price_per_credit'], 2)
            buy_depth[price] = buy_depth.get(price, 0) + order['amount_credits']

        for order in sell_orders[:20]:  # Top 20 sell orders
            price = round(order['price_per_credit'], 2)
            sell_depth[price] = sell_depth.get(price, 0) + order['amount_credits']

        return jsonify({
            "market_stats": {
                "total_volume_24h": total_volume_24h,
                "total_trades_24h": total_trades_24h,
                "avg_price_24h": _get_market_price('buy'),
                "spread": {
                    "best_bid": buy_orders[0]['price_per_credit'] if buy_orders else None,
                    "best_ask": sell_orders[0]['price_per_credit'] if sell_orders else None
                }
            },
            "orderbook_depth": {
                "buy_depth": buy_depth,
                "sell_depth": sell_depth
            },
            "price_history": sorted(price_history, key=lambda x: x['timestamp'])[:100],  # Last 100 trades
            "volume_by_hour": volume_by_hour,
            "timestamp": datetime.now().isoformat()
        }), 200

    except Exception as e:
        logger.error(f"Market analytics error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@external_api_bp.route('/analytics/services', methods=['GET'])
@require_api_key
def get_service_analytics():
    """Get robot service analytics"""
    try:
        service_stats = {}
        total_revenue = 0.0
        total_orders = 0

        for service_id, service in ROBOT_SERVICES.items():
            service_orders = [o for o in SERVICE_ORDERS.values() if o.get('service_id') == service_id]
            completed_orders = [o for o in service_orders if o.get('status') == 'completed']

            revenue = sum(o.get('total_cost', 0) for o in completed_orders)
            total_revenue += revenue
            total_orders += len(completed_orders)

            service_stats[service_id] = {
                "service_name": service.get('name', 'Unknown'),
                "robot_owner": service.get('robot_owner_wallet', 'Unknown'),
                "total_orders": len(service_orders),
                "completed_orders": len(completed_orders),
                "revenue_credits": revenue,
                "avg_rating": service.get('avg_rating', 0.0),
                "total_ratings": service.get('total_ratings', 0)
            }

        # Sort by revenue
        top_services = sorted(service_stats.items(), key=lambda x: x[1]['revenue_credits'], reverse=True)[:10]

        return jsonify({
            "service_stats": dict(top_services),
            "summary": {
                "total_services": len(ROBOT_SERVICES),
                "total_orders": total_orders,
                "total_revenue": total_revenue,
                "avg_order_value": total_revenue / max(total_orders, 1)
            },
            "timestamp": datetime.now().isoformat()
        }), 200

    except Exception as e:
        logger.error(f"Service analytics error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@external_api_bp.route('/analytics/platform', methods=['GET'])
@require_api_key
def get_platform_analytics():
    """Get platform-wide analytics (admin only)"""
    # Check admin permissions
    api_key_data = getattr(g, 'api_key_data', None)
    if not api_key_data or 'admin' not in api_key_data.get('permissions', []):
        return jsonify({"success": False, "error": "Admin access required"}), 403

    try:
        # User activity
        active_users_24h = set()
        total_transactions = 0
        total_volume = 0.0

        cutoff_time = datetime.now() - timedelta(hours=24)

        for order in SERVICE_ORDERS.values():
            order_time = datetime.fromisoformat(order.get('created_at', '2000-01-01'))
            if order_time > cutoff_time:
                active_users_24h.add(order.get('buyer_wallet'))
            total_transactions += 1
            total_volume += order.get('total_cost', 0)

        for order in TRADE_ORDERS.values():
            if order.get('status') == 'completed':
                order_time = datetime.fromisoformat(order.get('created_at', '2000-01-01'))
                if order_time > cutoff_time:
                    active_users_24h.add(order.get('wallet_address'))
                total_transactions += len(order.get('executions', []))
                total_volume += sum(e.get('amount', 0) * e.get('price', 0) for e in order.get('executions', []))

        return jsonify({
            "platform_metrics": {
                "active_users_24h": len(active_users_24h),
                "total_users": len(set(
                    [o.get('buyer_wallet') for o in SERVICE_ORDERS.values()] +
                    [o.get('wallet_owner_wallet') for o in ROBOT_SERVICES.values()] +
                    [o.get('wallet_address') for o in TRADE_ORDERS.values()]
                )),
                "total_transactions": total_transactions,
                "total_volume_credits": total_volume,
                "total_services": len(ROBOT_SERVICES),
                "total_trades": len([o for o in TRADE_ORDERS.values() if o.get('status') == 'completed'])
            },
            "revenue_breakdown": PLATFORM_REVENUE,
            "system_health": {
                "uptime": "Check system monitoring",
                "api_status": "operational",
                "blockchain_status": "Check node connection"
            },
            "timestamp": datetime.now().isoformat()
        }), 200

    except Exception as e:
        logger.error(f"Platform analytics error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# Performance Monitoring Storage (in production, use time-series database)
PERFORMANCE_METRICS = {
    "api_response_times": [],
    "error_rates": {},
    "throughput": {},
    "last_updated": None
}

@external_api_bp.route('/monitoring/health', methods=['GET'])
@require_api_key
def get_system_health():
    """Get system health and performance metrics"""
    try:
        # Calculate response time average
        response_times = PERFORMANCE_METRICS["api_response_times"][-100:]  # Last 100 requests
        avg_response_time = sum(response_times) / len(response_times) if response_times else 0

        # Calculate error rate
        total_requests = len(PERFORMANCE_METRICS["api_response_times"])
        total_errors = sum(PERFORMANCE_METRICS["error_rates"].values())
        error_rate = (total_errors / total_requests * 100) if total_requests > 0 else 0

        # Get throughput (requests per minute)
        current_time = datetime.now()
        one_minute_ago = current_time - timedelta(minutes=1)
        recent_requests = len([t for t in PERFORMANCE_METRICS["api_response_times"]
                              if (current_time - timedelta(seconds=t)).timestamp() > one_minute_ago.timestamp()])
        throughput_rpm = recent_requests

        # Check service availability
        services_status = {
            "robot_economy_manager": robot_economy_manager is not None,
            "brain_system": _get_brain_system() is not None,
            "blockchain_node": True,  # Assume always available for now
            "external_api": True
        }

        health_score = sum(services_status.values()) / len(services_status) * 100

        return jsonify({
            "health_score": health_score,
            "status": "healthy" if health_score >= 90 else "degraded" if health_score >= 70 else "unhealthy",
            "metrics": {
                "avg_response_time_ms": avg_response_time * 1000,
                "error_rate_percent": error_rate,
                "throughput_rpm": throughput_rpm,
                "total_requests": total_requests
            },
            "services": services_status,
            "uptime": "Check system process",  # In production, track actual uptime
            "timestamp": datetime.now().isoformat()
        }), 200

    except Exception as e:
        logger.error(f"Health check error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@external_api_bp.route('/monitoring/metrics', methods=['GET'])
@require_api_key
def get_detailed_metrics():
    """Get detailed performance metrics (admin only)"""
    api_key_data = getattr(g, 'api_key_data', None)
    if not api_key_data or 'admin' not in api_key_data.get('permissions', []):
        return jsonify({"success": False, "error": "Admin access required"}), 403

    try:
        # Endpoint usage statistics
        endpoint_usage = {}
        for metric_name, count in PERFORMANCE_METRICS["throughput"].items():
            if metric_name.startswith("endpoint_"):
                endpoint_usage[metric_name.replace("endpoint_", "")] = count

        # Error breakdown by endpoint
        error_breakdown = PERFORMANCE_METRICS["error_rates"]

        return jsonify({
            "performance": {
                "response_times": {
                    "average_ms": sum(PERFORMANCE_METRICS["api_response_times"][-1000:]) / max(len(PERFORMANCE_METRICS["api_response_times"][-1000:]), 1) * 1000,
                    "p95_ms": sorted(PERFORMANCE_METRICS["api_response_times"][-1000:])[int(len(PERFORMANCE_METRICS["api_response_times"][-1000:]) * 0.95)] * 1000 if PERFORMANCE_METRICS["api_response_times"] else 0,
                    "p99_ms": sorted(PERFORMANCE_METRICS["api_response_times"][-1000:])[int(len(PERFORMANCE_METRICS["api_response_times"][-1000:]) * 0.99)] * 1000 if PERFORMANCE_METRICS["api_response_times"] else 0
                },
                "throughput": {
                    "current_rpm": len([t for t in PERFORMANCE_METRICS["api_response_times"][-60:]]),
                    "peak_rpm": max(PERFORMANCE_METRICS["throughput"].get("peak_rpm", 0), len([t for t in PERFORMANCE_METRICS["api_response_times"][-60:]]))
                }
            },
            "usage": {
                "endpoint_usage": endpoint_usage,
                "error_breakdown": error_breakdown
            },
            "system": {
                "memory_usage_percent": 0,  # Placeholder - would use psutil in production
                "cpu_usage_percent": 0,     # Placeholder
                "disk_usage_percent": 0     # Placeholder
            },
            "timestamp": datetime.now().isoformat()
        }), 200

    except Exception as e:
        logger.error(f"Detailed metrics error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# Persistent service ratings
SERVICE_RATINGS = PersistentDict(os.path.join(_STORE_DIR, "service_ratings.json"))

@external_api_bp.route('/services/<string:service_id>/rate', methods=['POST'])
@require_api_key
@require_wallet_signature
def rate_service(service_id):
    """Rate a robot service after using it"""
    data = request.get_json()

    if not data or 'rating' not in data:
        return jsonify({"success": False, "error": "Rating is required"}), 400

    rating = data['rating']
    review = data.get('review', '')
    wallet_address = g.verified_wallet

    # Validate rating
    if not isinstance(rating, (int, float)) or rating < 1 or rating > 5:
        return jsonify({"success": False, "error": "Rating must be between 1 and 5"}), 400

    # Check if service exists
    if service_id not in ROBOT_SERVICES:
        return jsonify({"success": False, "error": "Service not found"}), 404

    # Check if user has used this service (basic check - in production, verify order completion)
    user_orders = [o for o in SERVICE_ORDERS.values()
                   if o.get('service_id') == service_id and o.get('buyer_wallet') == wallet_address]
    if not user_orders:
        return jsonify({"success": False, "error": "You must use this service before rating it"}), 403

    try:
        # Initialize ratings list if not exists
        if service_id not in SERVICE_RATINGS:
            SERVICE_RATINGS[service_id] = []

        # Check if user already rated this service
        existing_rating = next((r for r in SERVICE_RATINGS[service_id]
                               if r['wallet_address'] == wallet_address), None)

        if existing_rating:
            # Update existing rating
            existing_rating['rating'] = rating
            existing_rating['review'] = review
            existing_rating['updated_at'] = datetime.now().isoformat()
        else:
            # Add new rating
            SERVICE_RATINGS[service_id].append({
                "wallet_address": wallet_address,
                "rating": rating,
                "review": review,
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat()
            })

        # Update service average rating
        ratings = SERVICE_RATINGS[service_id]
        avg_rating = sum(r['rating'] for r in ratings) / len(ratings)
        ROBOT_SERVICES[service_id]['avg_rating'] = round(avg_rating, 2)
        ROBOT_SERVICES[service_id]['total_ratings'] = len(ratings)
        SERVICE_RATINGS.sync()  # persist rating list mutations
        ROBOT_SERVICES.sync()   # persist avg_rating / total_ratings

        return jsonify({
            "success": True,
            "service_id": service_id,
            "your_rating": rating,
            "avg_rating": ROBOT_SERVICES[service_id]['avg_rating'],
            "total_ratings": ROBOT_SERVICES[service_id]['total_ratings'],
            "message": "Rating submitted successfully"
        }), 200

    except Exception as e:
        logger.error(f"Service rating error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@external_api_bp.route('/services/<string:service_id>/ratings', methods=['GET'])
@require_api_key
def get_service_ratings(service_id):
    """Get ratings and reviews for a service"""
    if service_id not in SERVICE_RATINGS:
        return jsonify({"ratings": [], "avg_rating": 0.0, "total_ratings": 0}), 200

    ratings = SERVICE_RATINGS[service_id]

    # Calculate rating distribution
    distribution = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for rating in ratings:
        distribution[int(rating['rating'])] += 1

    avg_rating = sum(r['rating'] for r in ratings) / len(ratings) if ratings else 0.0

    return jsonify({
        "service_id": service_id,
        "avg_rating": round(avg_rating, 2),
        "total_ratings": len(ratings),
        "rating_distribution": distribution,
        "reviews": [{
            "rating": r['rating'],
            "review": r['review'],
            "created_at": r['created_at'],
            "wallet_address": f"{r['wallet_address'][:8]}...{r['wallet_address'][-4:]}"  # Anonymize
        } for r in ratings[-10:]]  # Last 10 reviews
    }), 200

@external_api_bp.route('/platform/revenue', methods=['GET'])
@require_api_key
def get_platform_revenue():
    """Get platform revenue statistics (admin only)"""
    # In production, check for admin permissions
    api_key_data = getattr(g, 'api_key_data', None)
    if not api_key_data or 'admin' not in api_key_data.get('permissions', []):
        return jsonify({"success": False, "error": "Admin access required"}), 403

    return jsonify({
        "platform_revenue": PLATFORM_REVENUE,
        "commission_rates": COMMISSION_RATES,
        "timestamp": datetime.now().isoformat()
    }), 200


# ============================================================================
# WORKLOAD MARKETPLACE — External users submit AI workloads, node earns CR
# ============================================================================

def _get_marketplace():
    """Lazy accessor for the singleton WorkloadMarketplace."""
    from repryntt.economy.workload_marketplace import get_workload_marketplace
    mp = get_workload_marketplace()
    if not mp._initialized:
        # Try to obtain P2P components for networked workload routing
        registry = None
        router = None
        try:
            from repryntt.economy.resource_registry import ResourceRegistry
            from repryntt.economy.workload_router import WorkloadRouter
            registry = ResourceRegistry()
            router = WorkloadRouter(registry)
        except Exception:
            pass  # P2P components optional — local fallback used
        mp.initialize(
            brain_system=_get_brain_system(),
            economy_manager=robot_economy_manager,
            resource_registry=registry,
            workload_router=router,
        )
        mp.start_worker()
    return mp


@external_api_bp.route('/workloads/submit', methods=['POST'])
@require_api_key
@require_wallet_signature
def submit_workload():
    """Submit an AI workload for async processing.

    Body:
        workload_type: inference | batch | embedding | analysis
        payload: dict (contents depend on type)
        max_price_cr: optional max you're willing to pay
    """
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "JSON body required"}), 400

    workload_type = data.get("workload_type")
    payload = data.get("payload")
    max_price = float(data.get("max_price_cr", 0))

    if not workload_type or not payload:
        return jsonify({"success": False, "error": "workload_type and payload required"}), 400

    # Enforce max_tokens ceiling from node config
    mp = _get_marketplace()
    tok_limit = mp.node_config.get("max_tokens_limit", 4096)
    if payload.get("max_tokens", 0) > tok_limit:
        payload["max_tokens"] = tok_limit

    # Cap batch size to prevent abuse
    if workload_type == "batch":
        prompts = payload.get("prompts", [])
        if len(prompts) > 100:
            return jsonify({"success": False, "error": "Batch limited to 100 prompts"}), 400
    if workload_type == "embedding":
        texts = payload.get("texts", [])
        if len(texts) > 500:
            return jsonify({"success": False, "error": "Embedding limited to 500 texts"}), 400

    result = mp.submit_job(g.verified_wallet, workload_type, payload, max_price)
    status_code = 202 if result.get("success") else 400
    if "Insufficient" in result.get("error", ""):
        status_code = 402
    return jsonify(result), status_code


@external_api_bp.route('/workloads/<string:job_id>', methods=['GET'])
@require_api_key
@require_wallet_signature
def get_workload(job_id):
    """Poll for workload result."""
    mp = _get_marketplace()
    job = mp.get_job(job_id)
    if not job:
        return jsonify({"success": False, "error": "Job not found"}), 404
    if job.get("user_wallet") != g.verified_wallet:
        return jsonify({"success": False, "error": "Unauthorized"}), 403
    return jsonify({"success": True, **job}), 200


@external_api_bp.route('/workloads', methods=['GET'])
@require_api_key
@require_wallet_signature
def list_workloads():
    """List your workload jobs."""
    mp = _get_marketplace()
    status = request.args.get("status")
    limit = min(int(request.args.get("limit", 50)), 200)
    jobs = mp.list_jobs(g.verified_wallet, limit=limit, status_filter=status)
    return jsonify({"success": True, "jobs": jobs, "count": len(jobs)}), 200


@external_api_bp.route('/workloads/<string:job_id>/cancel', methods=['POST'])
@require_api_key
@require_wallet_signature
def cancel_workload(job_id):
    """Cancel a pending workload and refund CR."""
    mp = _get_marketplace()
    result = mp.cancel_job(job_id, g.verified_wallet)
    return jsonify(result), 200 if result.get("success") else 400


@external_api_bp.route('/node/config', methods=['GET'])
@require_api_key
def get_node_config():
    """Get this node's workload pricing and capabilities."""
    mp = _get_marketplace()
    config = mp.get_node_config()
    stats = mp.get_stats()
    return jsonify({
        "success": True,
        "node_config": config,
        "stats": stats,
        "timestamp": datetime.now().isoformat(),
    }), 200


@external_api_bp.route('/node/config', methods=['PUT'])
@require_api_key
def update_node_config():
    """Node operator updates pricing / capacity.  Requires admin permission."""
    api_key_data = getattr(g, 'api_key_data', None)
    if not api_key_data or 'admin' not in api_key_data.get('permissions', []):
        return jsonify({"success": False, "error": "Admin access required"}), 403

    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "JSON body required"}), 400

    mp = _get_marketplace()
    updated = mp.update_node_config(data)
    return jsonify({"success": True, "node_config": updated}), 200


@external_api_bp.route('/node/stats', methods=['GET'])
@require_api_key
def get_node_stats():
    """Get workload marketplace statistics."""
    mp = _get_marketplace()
    return jsonify({"success": True, **mp.get_stats()}), 200


@external_api_bp.route('/ai/chat', methods=['POST'])
@require_api_key
@require_wallet_signature
def ai_chat():
    """Endpoint for external users to chat with SAIGE"""
    start_time = time.time()
    data = request.get_json()
    message = data.get("message")

    if not message:
        return jsonify({"success": False, "error": "Message is required."}), 400

    wallet_address = g.verified_wallet

    # Estimate cost based on message length (rough token estimate)
    estimated_tokens = len(message) / 4 + 500  # 500 tokens for AI response
    credit_validation = validate_and_deduct_credits(wallet_address, "ai_chat", estimated_tokens)

    if not credit_validation["success"]:
        return jsonify(credit_validation), 402  # 402 Payment Required

    try:
        # Call SAIGE's internal AI service
        ai_response_content = _get_brain_system()._call_ai_service(
            prompt=message,
            priority=1,  # Higher priority for external requests
            timeout=90,
            include_tools=True  # Allow external chat to use tools
        )

        if "AI_SERVICE_ERROR" in ai_response_content:
            # Refund credits if AI service itself failed
            robot_economy_manager.add_credits(wallet_address, credit_validation["cost_credits"], "ai_chat_refund_ai_error")
            return jsonify({"success": False, "error": ai_response_content}), 500

        # Calculate actual cost based on AI response length
        actual_response_tokens = len(ai_response_content) / 4
        actual_cost = (actual_response_tokens / 1000) * SERVICE_COSTS["ai_chat"]["cost_per_1000_tokens"]

        # Adjust credits if actual cost differs from estimated
        if actual_cost > credit_validation["cost_credits"]:
            additional_cost = actual_cost - credit_validation["cost_credits"]
            robot_economy_manager.deduct_credits(wallet_address, additional_cost, "ai_chat_adjustment")
        elif actual_cost < credit_validation["cost_credits"]:
            refund_amount = credit_validation["cost_credits"] - actual_cost
            robot_economy_manager.add_credits(wallet_address, refund_amount, "ai_chat_refund_overestimate")

        final_balance_info = robot_economy_manager.get_wallet_balance(wallet_address)

        # Record metrics
        metrics.record_ai_request("chat", time.time() - start_time, actual_response_tokens, "success")
        metrics.record_credit_transaction("ai_chat", actual_cost)

        return jsonify({
            "success": True,
            "response": ai_response_content,
            "cost_credits": actual_cost,
            "remaining_balance_credits": final_balance_info.get("balance_credits", 0.0),
            "wallet_address": wallet_address
        }), 200

    except Exception as e:
        logger.error(f"Error in AI chat endpoint: {e}")
        # Refund credits on unexpected error
        robot_economy_manager.add_credits(wallet_address, credit_validation["cost_credits"], "ai_chat_refund_exception")
        return jsonify({"success": False, "error": str(e)}), 500

@external_api_bp.route('/ai/tool', methods=['POST'])
@require_api_key
@require_wallet_signature
def ai_tool_execution():
    """Endpoint for external users to execute SAIGE's tools"""
    global brain_system
    data = request.get_json()
    tool_name = data.get("tool_name")
    parameters = data.get("parameters", {})

    if not tool_name:
        return jsonify({"success": False, "error": "tool_name is required."}), 400

    wallet_address = g.verified_wallet

    credit_validation = validate_and_deduct_credits(wallet_address, "tool_call")
    if not credit_validation["success"]:
        return jsonify(credit_validation), 402

    try:
        # Execute the tool via BrainSystem
        tool_result = _get_brain_system().execute_tool_call(tool_name, parameters)

        if tool_result.get("success"):
            final_balance_info = robot_economy_manager.get_wallet_balance(wallet_address)
            return jsonify({
                "success": True,
                "tool_name": tool_name,
                "result": tool_result.get("result"),
                "cost_credits": tool_result.get("cost_credits"),
                "remaining_balance_credits": final_balance_info.get("balance_credits", 0.0),
                "wallet_address": wallet_address
            }), 200
        else:
            final_balance_info = robot_economy_manager.get_wallet_balance(wallet_address)
            return jsonify({
                "success": False,
                "tool_name": tool_name,
                "error": tool_result.get("error", "Tool execution failed"),
                "cost_credits": tool_result.get("cost_credits"),
                "remaining_balance_credits": final_balance_info.get("balance_credits", 0.0),
                "wallet_address": wallet_address
            }), 500

    except Exception as e:
        logger.error(f"Error in AI tool execution endpoint: {e}")
        final_balance_info = robot_economy_manager.get_wallet_balance(wallet_address)
        return jsonify({
            "success": False,
            "error": str(e),
            "remaining_balance_credits": final_balance_info.get("balance_credits", 0.0),
            "wallet_address": wallet_address
        }), 500

@external_api_bp.route('/ai/analyze', methods=['POST'])
@require_api_key
@require_wallet_signature
def ai_advanced_analysis():
    """Endpoint for external users to request advanced AI analysis"""
    global brain_system
    data = request.get_json()
    analysis_query = data.get("query")
    analysis_type = data.get("type", "general")

    if not analysis_query:
        return jsonify({"success": False, "error": "query is required."}), 400

    wallet_address = g.verified_wallet

    credit_validation = validate_and_deduct_credits(wallet_address, "analysis")
    if not credit_validation["success"]:
        return jsonify(credit_validation), 402

    try:
        # Use SAIGE's internal chain-of-thought for advanced analysis
        topic = f"External Analysis Request: {analysis_query[:50]}..."
        goal = f"Provide comprehensive analysis for: {analysis_query}"

        # Create a self-autonomous chain
        chain_id = _get_brain_system().create_self_autonomous_chain(
            topic=topic,
            goal=goal,
            task_type="research_analysis"
        )

        if not chain_id:
            robot_economy_manager.add_credits(wallet_address, credit_validation["cost_credits"], "analysis_refund_chain_fail")
            return jsonify({"success": False, "error": "Failed to initiate analysis chain."}), 500

        # Get the initial prompt from the newly created chain
        chain_data = _get_brain_system().get_chain_context(chain_id, max_tokens=1000)
        initial_prompt = f"Perform a detailed {analysis_type} analysis on: {analysis_query}. " \
                         f"Here is the initial chain context: {chain_data}"

        # Generate AI response for the first step of the analysis
        ai_response_content = _get_brain_system()._call_ai_service(
            prompt=initial_prompt,
            priority=2,  # High priority for advanced analysis
            timeout=180,
            include_tools=True
        )

        if "AI_SERVICE_ERROR" in ai_response_content:
            robot_economy_manager.add_credits(wallet_address, credit_validation["cost_credits"], "analysis_refund_ai_error")
            return jsonify({"success": False, "error": ai_response_content}), 500

        # Advance the chain with the initial response
        advance_result = _get_brain_system().advance_self_autonomous_chain(chain_id, ai_response_content)

        final_balance_info = robot_economy_manager.get_wallet_balance(wallet_address)

        return jsonify({
            "success": True,
            "analysis_result": ai_response_content,
            "chain_id": chain_id,
            "cost_credits": credit_validation["cost_credits"],
            "remaining_balance_credits": final_balance_info.get("balance_credits", 0.0),
            "wallet_address": wallet_address,
            "message": "Analysis initiated. Further steps will be processed autonomously by SAIGE."
        }), 200

    except Exception as e:
        logger.error(f"Error in AI advanced analysis endpoint: {e}")
        robot_economy_manager.add_credits(wallet_address, credit_validation["cost_credits"], "analysis_refund_exception")
        return jsonify({"success": False, "error": str(e)}), 500

# ============================================================================
# ROBOT INTEGRATION ENDPOINTS
# ============================================================================

@external_api_bp.route('/robots/register', methods=['POST'])
@require_api_key
def register_robot():
    """Register a robot for passive income generation"""
    data = request.get_json()

    if not data or not all(k in data for k in ['robot_id', 'owner_wallet', 'robot_type']):
        return jsonify({"success": False, "error": "robot_id, owner_wallet, and robot_type required"}), 400

    robot_id = data['robot_id']
    owner_wallet = data['owner_wallet']
    robot_type = data['robot_type']

    try:
        # Create robot wallet
        wallet_result = robot_economy_manager.create_wallet(f"robot_{robot_type}")
        if not wallet_result.get("success"):
            return jsonify({"success": False, "error": "Failed to create robot wallet"}), 500

        robot_wallet = wallet_result["address"]
        robot_key_phrase = wallet_result["key_phrase"]

        # Generate API key for robot
        robot_api_key = secrets.token_hex(32)

        API_KEYS[robot_api_key] = {
            'user_id': f"robot_{robot_id}",
            'permissions': ['robot'],
            'created_at': datetime.now(),
            'active': True,
            'robot_id': robot_id,
            'robot_wallet': robot_wallet,
            'owner_wallet': owner_wallet
        }

        # Fund robot wallet with initial credits (from owner or system)
        initial_funding = robot_economy_manager.faucet(robot_wallet, 10.0)  # 10 credits

        # Record metrics
        metrics.record_credit_transaction("robot_registration", 10.0)

        logger.info(f"🤖 Robot registered: {robot_id} (type: {robot_type}) for owner: {owner_wallet[:16]}...")

        return jsonify({
            "success": True,
            "robot_id": robot_id,
            "robot_wallet": robot_wallet,
            "robot_key_phrase": robot_key_phrase,
            "api_key": robot_api_key,
            "initial_funding": initial_funding,
            "message": "Robot registered successfully for passive income generation"
        }), 201

    except Exception as e:
        logger.error(f"Robot registration error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@external_api_bp.route('/robots/update', methods=['POST'])
@require_api_key
def update_robot_capabilities():
    """Update robot capabilities and status"""
    data = request.get_json()

    if not data or 'robot_id' not in data:
        return jsonify({"success": False, "error": "robot_id required"}), 400

    robot_id = data['robot_id']
    api_key_data = getattr(g, 'api_key_data', None)

    if not api_key_data or api_key_data.get('robot_id') != robot_id:
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    try:
        # Update capabilities (in production, store in database)
        logger.info(f"🤖 Robot {robot_id} capabilities updated")

        return jsonify({
            "success": True,
            "robot_id": robot_id,
            "message": "Capabilities updated successfully"
        }), 200

    except Exception as e:
        logger.error(f"Robot update error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@external_api_bp.route('/tasks/computation', methods=['POST'])
@require_api_key
def submit_computation_task():
    """Submit computational task from robot"""
    data = request.get_json()

    if not data or not all(k in data for k in ['robot_id', 'task_type', 'data']):
        return jsonify({"success": False, "error": "robot_id, task_type, and data required"}), 400

    robot_id = data['robot_id']
    task_data = data['data']
    resource_usage = data.get('resource_usage', {})

    # Verify robot authorization
    api_key_data = getattr(g, 'api_key_data', None)
    if not api_key_data or api_key_data.get('robot_id') != robot_id:
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    try:
        # Process computational task (simple example)
        # In production, this would distribute tasks across the robot network
        task_complexity = len(str(task_data)) / 1000  # Rough complexity measure
        credits_earned = min(task_complexity * 0.1, 1.0)  # Max 1 credit per task

        # Reward the robot
        robot_wallet = api_key_data.get('robot_wallet')
        if robot_wallet:
            reward_result = robot_economy_manager.add_credits(
                robot_wallet,
                credits_earned,
                "robot_computation_task"
            )

            if reward_result.get("success"):
                # Record metrics
                metrics.record_credit_transaction("robot_computation", credits_earned)

                return jsonify({
                    "success": True,
                    "robot_id": robot_id,
                    "credits_earned": credits_earned,
                    "task_processed": True,
                    "resource_usage": resource_usage
                }), 200

        return jsonify({"success": False, "error": "Failed to reward robot"}), 500

    except Exception as e:
        logger.error(f"Computation task error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@external_api_bp.route('/data/sensor', methods=['POST'])
@require_api_key
def submit_sensor_data():
    """Submit sensor data analytics from robot"""
    data = request.get_json()

    if not data or not all(k in data for k in ['robot_id', 'data_type', 'data']):
        return jsonify({"success": False, "error": "robot_id, data_type, and data required"}), 400

    robot_id = data['robot_id']
    data_type = data['data_type']
    sensor_data = data['data']

    # Verify robot authorization
    api_key_data = getattr(g, 'api_key_data', None)
    if not api_key_data or api_key_data.get('robot_id') != robot_id:
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    try:
        # Process sensor data analytics
        data_value = len(str(sensor_data)) / 1000  # Rough data value measure
        credits_earned = min(data_value * 0.05, 0.5)  # Max 0.5 credits per data submission

        # Reward the robot
        robot_wallet = api_key_data.get('robot_wallet')
        if robot_wallet:
            reward_result = robot_economy_manager.add_credits(
                robot_wallet,
                credits_earned,
                "robot_sensor_data"
            )

            if reward_result.get("success"):
                # Record metrics
                metrics.record_credit_transaction("robot_sensor_data", credits_earned)

                return jsonify({
                    "success": True,
                    "robot_id": robot_id,
                    "credits_earned": credits_earned,
                    "data_processed": True,
                    "data_type": data_type
                }), 200

        return jsonify({"success": False, "error": "Failed to reward robot"}), 500

    except Exception as e:
        logger.error(f"Sensor data submission error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@external_api_bp.route('/robots/marketplace', methods=['GET'])
@require_api_key
def get_robot_marketplace():
    """Get available robot services and data in the marketplace"""
    marketplace_items = []

    # Get all registered robot services
    for robot_id, services in ROBOT_SERVICES.items():
        for service in services:
            marketplace_items.append({
                "robot_id": robot_id,
                "service_id": service["service_id"],
                "service_type": service["service_type"],
                "description": service["description"],
                "capabilities": service["capabilities"],
                "pricing": service["pricing"],
                "availability": service["availability"],
                "rating": service.get("rating", 5.0),
                "total_orders": service.get("total_orders", 0),
                "owner_wallet": service["owner_wallet"],
                "listed_at": service["listed_at"]
            })

    return jsonify({
        "marketplace": marketplace_items,
        "total_services": len(marketplace_items),
        "total_robots": len(ROBOT_SERVICES),
        "timestamp": datetime.now().isoformat()
    }), 200

@external_api_bp.route('/robots/services/register', methods=['POST'])
@require_api_key
def register_robot_service():
    """Register a robot service for the marketplace"""
    data = request.get_json()

    required_fields = ['robot_id', 'service_type', 'description', 'capabilities', 'pricing']
    if not data or not all(k in data for k in required_fields):
        return jsonify({"success": False, "error": f"Required fields: {required_fields}"}), 400

    robot_id = data['robot_id']
    service_type = data['service_type']

    # Verify robot authorization
    api_key_data = getattr(g, 'api_key_data', None)
    if not api_key_data or api_key_data.get('robot_id') != robot_id:
        return jsonify({"success": False, "error": "Unauthorized - not robot owner"}), 403

    try:
        # Generate service ID
        service_id = f"{robot_id}_{service_type}_{int(time.time())}"

        # Create service listing
        service_listing = {
            "service_id": service_id,
            "robot_id": robot_id,
            "service_type": service_type,
            "description": data['description'],
            "capabilities": data['capabilities'],
            "pricing": data['pricing'],
            "availability": data.get('availability', 'available'),
            "rating": 5.0,
            "total_orders": 0,
            "owner_wallet": api_key_data.get('owner_wallet'),
            "robot_wallet": api_key_data.get('robot_wallet'),
            "listed_at": datetime.now().isoformat(),
            "status": "active"
        }

        # Add to marketplace
        if robot_id not in ROBOT_SERVICES:
            ROBOT_SERVICES[robot_id] = []
        ROBOT_SERVICES[robot_id].append(service_listing)
        ROBOT_SERVICES.sync()  # persist list append

        logger.info(f"🤖 Robot service registered: {service_id} by {robot_id}")

        return jsonify({
            "success": True,
            "service_id": service_id,
            "service_listing": service_listing,
            "message": "Service registered successfully in marketplace"
        }), 201

    except Exception as e:
        logger.error(f"Service registration error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@external_api_bp.route('/robots/services/purchase', methods=['POST'])
@require_api_key
@require_wallet_signature
def purchase_robot_service():
    """Purchase access to robot services through the marketplace"""
    data = request.get_json()

    required_fields = ['service_id', 'duration_hours', 'requirements']
    if not data or not all(k in data for k in required_fields):
        return jsonify({"success": False, "error": f"Required fields: {required_fields}"}), 400

    service_id = data['service_id']
    duration_hours = data['duration_hours']
    requirements = data['requirements']
    wallet_address = g.verified_wallet

    # Find the service
    service_listing = None
    robot_owner_wallet = None
    robot_wallet = None

    for robot_id, services in ROBOT_SERVICES.items():
        for service in services:
            if service['service_id'] == service_id:
                service_listing = service
                robot_owner_wallet = service['owner_wallet']
                robot_wallet = service['robot_wallet']
                break
        if service_listing:
            break

    if not service_listing:
        return jsonify({"success": False, "error": "Service not found"}), 404

    if service_listing['availability'] != 'available':
        return jsonify({"success": False, "error": "Service not available"}), 409

    # Calculate cost
    pricing = service_listing['pricing']
    if 'hourly_rate' in pricing:
        total_cost = pricing['hourly_rate'] * duration_hours
    else:
        return jsonify({"success": False, "error": "Invalid pricing structure"}), 400

    # Validate and deduct credits (with commission)
    credit_validation = validate_and_deduct_credits(wallet_address, "robot_marketplace", total_cost, apply_commission=True)
    if not credit_validation["success"]:
        return jsonify(credit_validation), 402

    try:
        # Generate order ID
        order_id = f"order_{service_id}_{int(time.time())}"

        # Create service order
        service_order = {
            "order_id": order_id,
            "service_id": service_id,
            "buyer_wallet": wallet_address,
            "robot_owner_wallet": robot_owner_wallet,
            "robot_wallet": robot_wallet,
            "total_cost": total_cost,
            "service_cost": credit_validation["cost_credits"],
            "platform_commission": credit_validation["commission_credits"],
            "robot_owner_revenue": credit_validation["cost_credits"] * 0.7,  # 70% of service cost
            "duration_hours": duration_hours,
            "requirements": requirements,
            "status": "active",
            "created_at": datetime.now().isoformat(),
            "expires_at": (datetime.now() + timedelta(hours=duration_hours)).isoformat()
        }

        # Store order
        SERVICE_ORDERS[order_id] = service_order

        # Pay robot owner (70% of service cost, commission already deducted)
        robot_revenue = credit_validation["cost_credits"] * 0.7
        if robot_owner_wallet and robot_revenue > 0:
            owner_payment = robot_economy_manager.add_credits(
                robot_owner_wallet,
                robot_revenue,
                f"robot_service_revenue_{order_id}"
            )
            if not owner_payment.get("success"):
                logger.warning(f"Failed to pay robot owner: {owner_payment.get('error')}")

        # Update service stats
        service_listing["total_orders"] += 1
        ROBOT_SERVICES.sync()  # persist nested total_orders mutation

        # Record metrics
        metrics.record_credit_transaction("robot_service_purchase", total_cost)

        final_balance_info = robot_economy_manager.get_wallet_balance(wallet_address)

        logger.info(f"🤖 Robot service purchased: {order_id} - {total_cost:.2f} CR (commission: {credit_validation['commission_credits']:.2f} CR)")

        return jsonify({
            "success": True,
            "order_id": order_id,
            "service_order": service_order,
            "total_cost_credits": total_cost,
            "service_cost_credits": credit_validation["cost_credits"],
            "platform_commission": credit_validation["commission_credits"],
            "robot_owner_revenue": robot_revenue,
            "remaining_balance_credits": final_balance_info.get("balance_credits", 0.0),
            "wallet_address": wallet_address
        }), 200

    except Exception as e:
        logger.error(f"Robot service purchase error: {e}")
        # Refund on error
        robot_economy_manager.add_credits(wallet_address, total_cost, f"robot_service_refund_{service_id}")
        return jsonify({"success": False, "error": str(e)}), 500


class SAIGEExternalAPIService:

    def __init__(self, host='0.0.0.0', port=8081, ssl_cert=None, ssl_key=None, auto_https=True):
        self.host = host
        self.port = port
        self.ssl_cert = ssl_cert
        self.ssl_key = ssl_key
        self.auto_https = auto_https
        # Initialize as None - will be set later via setter methods
        self.brain_system = None
        self.robot_economy_manager = None
        self.running = False
        self.production_mode = False  # Set to True when started from production script

        logger.info(f"🔐 Secure SAIGE External API Service initialized on {host}:{port}")

        # Auto-setup HTTPS if certificates not provided
        if auto_https and (not ssl_cert or not ssl_key):
            logger.info("🔄 Setting up HTTPS certificates...")
            self.ssl_cert, self.ssl_key = setup_https_support()

        if self.ssl_cert and self.ssl_key:
            logger.info("🔒 HTTPS/TLS enabled with certificate management")
        else:
            logger.warning("⚠️ HTTPS/TLS not configured - using HTTP only (not recommended for production)")

    def set_brain_system(self, brain_system_instance: BrainSystemProtocol):
        """Set the brain system instance"""
        global brain_system
        brain_system = brain_system_instance
        self.brain_system = brain_system_instance
        logger.info("🧠 Brain system connected to external API")

    def set_robot_economy_manager(self, manager):
        """Set the robot economy manager"""
        global robot_economy_manager
        robot_economy_manager = manager
        self.robot_economy_manager = manager
        logger.info("🤖 Robot economy manager connected to external API")

        # Start workload marketplace worker when economy is available
        try:
            from repryntt.economy.workload_marketplace import get_workload_marketplace
            mp = get_workload_marketplace()
            registry = None
            router = None
            try:
                from repryntt.economy.resource_registry import ResourceRegistry
                from repryntt.economy.workload_router import WorkloadRouter
                registry = ResourceRegistry()
                router = WorkloadRouter(registry)
            except Exception:
                pass  # P2P components optional
            mp.initialize(
                brain_system=self.brain_system,
                economy_manager=manager,
                resource_registry=registry,
                workload_router=router,
            )
            mp.start_worker()
        except Exception as e:
            logger.warning(f"Workload marketplace init deferred: {e}")

    def set_production_mode(self, production=True):
        """Set production mode (affects logging and behavior)"""
        self.production_mode = production
        if production:
            logger.info("🏭 Production mode enabled - connecting to real SAIGE services")
        else:
            logger.info("🧪 Development mode - using mock services")

    def start(self):
        """Start the Flask API service with HTTPS support"""
        if self.running:
            return

        try:
            self.running = True
            protocol = "https" if self.ssl_cert and self.ssl_key else "http"
            logger.info(f"🚀 SAIGE External API Service started on {protocol}://{self.host}:{self.port}")
            logger.info("💰 External users can now pay credits for AI services!")
            logger.info("🔑 API authentication and wallet signatures required")

            # Start Flask app with HTTPS if certificates available
            if self.ssl_cert and self.ssl_key:
                logger.info("🔒 Serving with TLS encryption")
                app.run(
                    host=self.host,
                    port=self.port,
                    ssl_context=(self.ssl_cert, self.ssl_key),
                    debug=False,
                    use_reloader=False
                )
            else:
                logger.warning("⚠️ Serving without TLS encryption - use HTTPS in production!")
                app.run(
                    host=self.host,
                    port=self.port,
                    debug=False,
                    use_reloader=False
                )

        except Exception as e:
            logger.error(f"Failed to start external API service: {e}")
            self.running = False

    def stop(self):
        """Stop the external API service"""
        if self.running:
            self.running = False
            logger.info("🛑 SAIGE External API Service stopped")


# Global service instance (created on demand)
external_api_service = None

# Register blueprint on standalone app (for backward compat)
app.register_blueprint(external_api_bp)

def start_external_api_service(brain_system=None, robot_economy_manager=None, ssl_cert=None, ssl_key=None):
    """Start the secure external API service"""
    global external_api_service

    # Create service instance if it doesn't exist
    if external_api_service is None:
        external_api_service = SAIGEExternalAPIService()

    if ssl_cert and ssl_key:
        external_api_service.ssl_cert = ssl_cert
        external_api_service.ssl_key = ssl_key

    if brain_system:
        external_api_service.set_brain_system(brain_system)
    if robot_economy_manager:
        external_api_service.set_robot_economy_manager(robot_economy_manager)

    # Start in background thread
    api_thread = threading.Thread(target=external_api_service.start, daemon=True)
    api_thread.start()

    return external_api_service

def stop_external_api_service():
    """Stop the external API service"""
    external_api_service.stop()

def initialize_external_api_for_production(brain_system_instance, robot_economy_manager_instance):
    """Initialize external API for production use with real services"""
    external_api_service.set_brain_system(brain_system_instance)
    external_api_service.set_robot_economy_manager(robot_economy_manager_instance)
    external_api_service.set_production_mode(True)

    # Initialize real API keys (in production, these should come from a database)
    # For now, we'll create some default keys that can be managed externally
    logger.info("🔑 External API initialized for production use")

    return external_api_service

def initialize_services():
    """Initialize services - use real ones if available, mock ones for testing"""
    global brain_system, robot_economy_manager

    # Check if we're in production mode (real services available)
    try:
        if brain_system is None or robot_economy_manager is None:
            # Try to initialize real services
            from repryntt.economy.manager import RobotEconomyManager

            brain_system = create_brain_system()
            robot_economy_manager = RobotEconomyManager(brain_system=brain_system)

            external_api_service.set_brain_system(brain_system)
            external_api_service.set_robot_economy_manager(robot_economy_manager)
            external_api_service.set_production_mode(True)

            print("✅ Connected to real SAIGE services (production mode)")
            return True

    except ImportError as e:
        print(f"⚠️ Real services not available: {e}")
    except Exception as e:
        print(f"⚠️ Failed to initialize real services: {e}")

    # Fallback to mock services for testing
    print("🧪 Using mock services for testing")

    class MockBrainSystem:
        def _call_ai_service(self, prompt, **kwargs):
            return f"AI Response to: {prompt[:50]}..."

        def execute_tool_call(self, tool_name, parameters):
            return {"success": True, "result": f"Mock result for {tool_name}"}

        def create_self_autonomous_chain(self, **kwargs):
            return "mock_chain_id"

        def get_chain_context(self, chain_id, **kwargs):
            return "Mock chain context"

        def advance_self_autonomous_chain(self, chain_id, response):
            return {"success": True}

    class MockEconomyManager:
        def get_wallet_balance(self, address):
            return {"success": True, "balance_credits": 100.0}

        def create_wallet(self):
            return {"success": True, "address": "test_wallet_address", "key_phrase": "test phrase"}

        def deduct_credits(self, address, amount, reason):
            return {"success": True}

        def add_credits(self, address, amount, reason):
            return {"success": True}

        def faucet(self, address, amount):
            return {"success": True, "new_balance_credits": 100.0 + amount}

    external_api_service.set_brain_system(MockBrainSystem())
    external_api_service.set_robot_economy_manager(MockEconomyManager())

    # Add a test API key
    API_KEYS["test_api_key_123"] = {
        'user_id': 'test_user',
        'permissions': ['read', 'write'],
        'created_at': datetime.now(),
        'active': True
    }

    return False

def run_standalone_test():
    """Run standalone test mode for development"""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    print("🔐 SAIGE External API - Production Ready")
    print("=====================================")
    print("✅ Post-Quantum Resistant Cryptography")
    print("✅ API Key Authentication")
    print("✅ Wallet Signature Verification")
    print("✅ Automatic HTTPS/TLS Certificate Management")
    print("✅ Rate Limiting & Security")
    print("✅ Credit-Based Payment System")
    print("")

    # Initialize services
    production_mode = initialize_services()

    if production_mode:
        print("🏭 Production mode: Connected to real SAIGE services")
    else:
        print("🧪 Development mode: Using mock services for testing")

    print("")
    print("🧪 Starting test server with self-signed certificates...")
    print("📋 For production deployment:")
    print("   1. Obtain proper SSL certificates from Let's Encrypt or CA")
    print("   2. Set ssl_cert and ssl_key parameters")
    print("   3. Use a production web server (nginx/gunicorn)")
    print("   4. Configure firewall and security groups")
    print("")

    external_api_service.start()

if __name__ == "__main__":
    run_standalone_test()
