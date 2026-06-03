"""Local node identity helpers.

Runtime node identity must come from this machine's wallet, never from the
canonical genesis creator address.
"""

from __future__ import annotations

import os
from typing import Optional


def get_local_node_address(create: bool = True) -> Optional[str]:
    """Return the wallet address this node should use at runtime.

    Resolution order:
      1. REPRYNTT_ADDRESS, when explicitly configured.
      2. The canonical node wallet, optionally creating it on first install.

    This deliberately has no genesis fallback.  The genesis creator is fixed
    chain history, not a reusable miner identity for new installations.
    """
    explicit = os.environ.get("REPRYNTT_ADDRESS", "").strip()
    if explicit:
        return explicit

    try:
        if create:
            from repryntt.economy.node_wallet import get_node_wallet

            wallet = get_node_wallet()
            if wallet and wallet.address:
                return wallet.address
        else:
            from repryntt.economy.node_wallet import NODE_WALLET_PATH

            if NODE_WALLET_PATH.exists():
                import json

                data = json.loads(NODE_WALLET_PATH.read_text())
                address = str(data.get("address", "")).strip()
                if address:
                    return address
    except Exception:
        return None

    return None
