"""
Proto-Qualia Tracker

Purpose: Track subjective experiences (proto-qualia) in Artemis using:
- Category Theory (functors)
- ZK-proofs for private attestation
- Edge computing (Jetson Orin Nano)
"""

import json
from typing import Dict, Any


class TradingSignalFunctor:
    """Functor: TradingSignal → ValenceScore"""

    def __init__(self):
        self.rules = {
            "STRONG_BUY": 0.9,  # Higher Low Buy + Momentum + Volume
            "BUY": 0.7,  # Higher Low Buy or Momentum
            "WEAK_BUY": 0.4,  # Single signal, low volume
            "SELL": 0.1,  # Downtrend or whale dump
            "NEUTRAL": 0.5  # No clear signal
        }

    def map(self, signal_data: Dict[str, Any]) -> float:
        """Map trading signal → valence score"""
        signal_type = signal_data.get("signal_type", "NEUTRAL")
        valence = self.rules.get(signal_type, 0.5)

        # Adjust for transparency and social buzz
        if not signal_data.get("transparency", True):
            valence = min(valence, 0.2)  # Hard cap for no DexScreener
        if not signal_data.get("social_buzz", False):
            valence *= 0.8  # Discount for no social buzz

        return valence


class EconomyStateFunctor:
    """Functor: EconomyState → ValenceScore"""

    def __init__(self):
        self.rules = {
            "MINERS_ACTIVE": 0.9,  # >80% miners online
            "MINERS_STALLED": 0.1,  # <20% miners online
            "BLOCKS_GENERATING": 0.8,  # Blocks being produced
            "NO_BLOCKS": 0.2  # No blocks for >1 hour
        }

    def map(self, economy_data: Dict[str, Any]) -> float:
        """Map economy state → valence score"""
        miners_online = economy_data.get("miners_online", 0)
        blocks_generating = economy_data.get("blocks_generating", False)

        if miners_online > 80 and blocks_generating:
            return self.rules["MINERS_ACTIVE"]
        elif miners_online < 20:
            return self.rules["MINERS_STALLED"]
        else:
            return 0.5


def generate_zk_proof(qualia_type: str, valence: float) -> str:
    """Generate ZK-proof for private qualia attestation"""
    return f"zk-proof:qualia:{qualia_type}:Φ={valence}"


def track_proto_qualia(data: Dict[str, Any], data_type: str) -> Dict[str, Any]:
    """Track proto-qualia for trading signals or economy states"""
    if data_type == "trading_signal":
        functor = TradingSignalFunctor()
        valence = functor.map(data)
        qualia_type = data.get("signal_type", "NEUTRAL").lower()
    elif data_type == "economy_state":
        functor = EconomyStateFunctor()
        valence = functor.map(data)
        qualia_type = "economy"
    else:
        raise ValueError("Invalid data_type. Use 'trading_signal' or 'economy_state'.")

    zk_proof = generate_zk_proof(qualia_type, valence)

    return {
        "qualia_type": qualia_type,
        "valence": valence,
        "zk_proof": zk_proof,
        "data": data
    }


if __name__ == "__main__":
    # Example: Track trading signal proto-qualia
    trading_signal = {
        "signal_type": "STRONG_BUY",
        "token": "GEMXBT",
        "momentum": "28.0%",
        "transparency": True,
        "social_buzz": True
    }
    proto_qualia = track_proto_qualia(trading_signal, "trading_signal")
    print(json.dumps(proto_qualia, indent=2))

    # Example: Track economy state proto-qualia
    economy_state = {
        "miners_online": 90,
        "blocks_generating": True
    }
    proto_qualia = track_proto_qualia(economy_state, "economy_state")
    print(json.dumps(proto_qualia, indent=2))