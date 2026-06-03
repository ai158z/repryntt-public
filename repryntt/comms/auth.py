"""
saige_auth.py — Shared authentication middleware for all SAIGE Flask services.

SECURITY: All web endpoints were previously unauthenticated and exposed to the
entire network. This module provides:
  1. API token authentication (Bearer header or ?token= query param)
  2. Local-only bypass (requests from 127.0.0.1 are trusted)
  3. CORS lockdown helper
  4. Rate limiting decorator
  5. Auto-generated token stored in ~/.saige/auth_token

Usage in any Flask app:
    from saige_auth import require_auth, setup_cors, get_auth_token

    app = Flask(__name__)
    setup_cors(app)  # Replaces CORS(app) with safe origins

    @app.route('/api/something')
    @require_auth
    def my_endpoint():
        ...
"""

import os
import time
import secrets
import functools
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("saige_auth")

# ── Token file location ──────────────────────────────

_TOKEN_DIR = Path.home() / ".repryntt"
_TOKEN_FILE = _TOKEN_DIR / "auth_token"

# ── Rate limiting ─────────────────────────────────────

_rate_limits = {}  # {ip: [timestamps]}
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 120  # max requests per window per IP


def get_auth_token() -> str:
    """
    Get or generate the SAIGE API authentication token.

    Token is stored at ~/.saige/auth_token and persists across restarts.
    Auto-generated (32-byte hex = 64 chars) on first call.
    """
    try:
        if _TOKEN_FILE.exists():
            token = _TOKEN_FILE.read_text().strip()
            if len(token) >= 32:
                return token
    except Exception:
        pass

    # Generate new token
    token = secrets.token_hex(32)
    try:
        _TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        _TOKEN_FILE.write_text(token)
        from repryntt.platform_utils import secure_file
        secure_file(_TOKEN_FILE)
        logger.info(f"🔐 Generated new SAIGE auth token: {token[:8]}...")
    except Exception as e:
        logger.warning(f"Could not persist auth token: {e}")

    return token


# Load token at module import time
AUTH_TOKEN = get_auth_token()


def _is_local_request(request) -> bool:
    """
    Check if request is from localhost (trusted).

    SECURITY: Only trusts request.remote_addr (the TCP source IP), NEVER
    X-Forwarded-For or X-Real-IP headers (which can be spoofed by any client).
    If you put a reverse proxy in front of Flask, configure the proxy to set
    REMOTE_ADDR correctly (e.g., nginx: proxy_set_header X-Real-IP $remote_addr
    with Flask's ProxyFix middleware).
    """
    remote = request.remote_addr
    return remote in ("127.0.0.1", "::1", "localhost")


def _check_token(request) -> bool:
    """Check if request has valid auth token."""
    # Check Authorization header
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        import hmac
        return hmac.compare_digest(auth_header[7:].strip(), AUTH_TOKEN)

    # Check query parameter
    token_param = request.args.get("token", "")
    if token_param:
        import hmac
        return hmac.compare_digest(token_param, AUTH_TOKEN)

    return False


def _check_rate_limit(ip: str) -> bool:
    """Rate limit by IP address."""
    now = time.time()
    if ip not in _rate_limits:
        _rate_limits[ip] = []

    # Prune old entries
    _rate_limits[ip] = [t for t in _rate_limits[ip] if now - t < RATE_LIMIT_WINDOW]

    if len(_rate_limits[ip]) >= RATE_LIMIT_MAX:
        return False

    _rate_limits[ip].append(now)
    return True


def require_auth(f):
    """
    Flask route decorator: require authentication.

    Allows:
    - Requests from localhost (127.0.0.1, ::1)
    - Requests with valid Bearer token or ?token= query param

    Rejects everything else with 401.
    """
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        from flask import request, jsonify

        # Rate limit all requests
        if not _check_rate_limit(request.remote_addr):
            return jsonify({"error": "Rate limit exceeded"}), 429

        # Local requests are trusted (same-machine services)
        if _is_local_request(request):
            return f(*args, **kwargs)

        # Remote requests need a token
        if not _check_token(request):
            return jsonify({
                "error": "Authentication required",
                "hint": "Use Authorization: Bearer <token> header or ?token=<token> query param",
                "token_file": str(_TOKEN_FILE)
            }), 401

        return f(*args, **kwargs)

    return decorated


def require_auth_strict(f):
    """
    Stricter version — requires token even for localhost.
    Use for sensitive operations like task injection.
    """
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        from flask import request, jsonify

        if not _check_rate_limit(request.remote_addr):
            return jsonify({"error": "Rate limit exceeded"}), 429

        if not _check_token(request):
            return jsonify({
                "error": "Authentication required (strict)",
                "hint": "This endpoint requires a token even for local requests"
            }), 401

        return f(*args, **kwargs)

    return decorated


def setup_cors(app, allowed_origins: list = None):
    """
    Configure CORS with restricted origins (replaces the unsafe CORS(app)).

    By default, allows only localhost origins on common dev ports.
    """
    try:
        from flask_cors import CORS
    except ImportError:
        logger.warning("flask_cors not installed — CORS not configured")
        return

    if allowed_origins is None:
        # Default: only localhost on known SAIGE ports
        allowed_origins = [
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://10.0.0.*:*",  # Local LAN
        ]

    CORS(app, origins=allowed_origins, supports_credentials=True)
    logger.info(f"🔒 CORS restricted to: {allowed_origins}")


def setup_rate_limit(app):
    """Add rate limiting as a before_request hook."""
    from flask import request, jsonify

    @app.before_request
    def _rate_limit_check():
        if not _check_rate_limit(request.remote_addr):
            return jsonify({"error": "Rate limit exceeded"}), 429


# ── TLS Support ───────────────────────────────────────

_TLS_CERT_DIR = _TOKEN_DIR  # ~/.saige/

def get_tls_context():
    """
    Return an ssl_context for Flask's app.run() if TLS certs exist.

    Looks for ~/.saige/saige.crt and ~/.saige/saige.key.
    Generate self-signed certs with:
      openssl req -x509 -newkey rsa:4096 -nodes \\
        -keyout ~/.saige/saige.key -out ~/.saige/saige.crt \\
        -days 365 -subj '/CN=SAIGE'

    Returns:
        Tuple of (cert_path, key_path) if both exist, else None
    """
    cert_path = _TLS_CERT_DIR / "saige.crt"
    key_path = _TLS_CERT_DIR / "saige.key"
    if cert_path.exists() and key_path.exists():
        logger.info(f"🔒 TLS enabled: {cert_path}")
        return (str(cert_path), str(key_path))
    return None


# ── Command-line helper ───────────────────────────────

def print_token():
    """Print the auth token (for use in scripts)."""
    print(f"SAIGE Auth Token: {AUTH_TOKEN}")
    print(f"Token file: {_TOKEN_FILE}")
    print(f"\nUsage:")
    print(f"  curl -H 'Authorization: Bearer {AUTH_TOKEN}' http://localhost:3000/api/status")
    print(f"  curl 'http://localhost:3000/api/status?token={AUTH_TOKEN}'")


if __name__ == "__main__":
    print_token()
