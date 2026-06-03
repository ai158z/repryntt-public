"""
Payment Gateway API — HTTP endpoints for buying Credits with SOL/USDC.
=======================================================================

Routes:
  GET  /gateway/status              — Gateway status, pricing, deposit address
  POST /gateway/deposit             — Create a new deposit request
  GET  /gateway/deposit/<id>        — Check deposit status
  GET  /gateway/deposits            — List recent deposits
  POST /gateway/poll                — Trigger deposit polling (admin)
"""

import json
import logging
from flask import Blueprint, request, jsonify

logger = logging.getLogger("repryntt.web.gateway")

gateway_bp = Blueprint('gateway', __name__)


@gateway_bp.route('/status', methods=['GET'])
def status():
    """Get gateway status, pricing, and deposit address."""
    from repryntt.economy.payment_gateway import get_gateway_status
    return jsonify(json.loads(get_gateway_status()))


@gateway_bp.route('/deposit', methods=['POST'])
def create_deposit():
    """Create a deposit request.

    JSON body: {"repryntt_address": "40-hex-char-address"}
    """
    data = request.get_json(silent=True) or {}
    repryntt_address = data.get("repryntt_address", "").strip()
    if not repryntt_address:
        return jsonify({"error": "repryntt_address is required"}), 400
    # Basic hex validation
    if len(repryntt_address) < 20 or not all(c in '0123456789abcdefABCDEF' for c in repryntt_address):
        return jsonify({"error": "Invalid repryntt_address — must be 40 hex chars"}), 400

    from repryntt.economy.payment_gateway import create_deposit as _create
    result = json.loads(_create(repryntt_address))
    status_code = 200 if result.get("success") else 400
    return jsonify(result), status_code


@gateway_bp.route('/deposit/<deposit_id>', methods=['GET'])
def deposit_status(deposit_id):
    """Check the status of a deposit."""
    from repryntt.economy.payment_gateway import get_deposit_status
    result = json.loads(get_deposit_status(deposit_id))
    status_code = 200 if not result.get("error") else 404
    return jsonify(result), status_code


@gateway_bp.route('/deposits', methods=['GET'])
def list_deposits():
    """List recent deposits."""
    limit = request.args.get('limit', 20, type=int)
    limit = min(limit, 100)
    from repryntt.economy.payment_gateway import list_deposits as _list
    return jsonify(json.loads(_list(limit=limit)))


@gateway_bp.route('/poll', methods=['POST'])
def poll_deposits():
    """Manually trigger deposit polling. Admin use only."""
    from repryntt.economy.payment_gateway import poll_deposits_sync
    result = poll_deposits_sync()
    return jsonify(result)
