"""
robot_economy.py — Robot Economy tools extracted from BrainSystem monolith.

Tools delegate to:
  1. The Rust blockchain node (via rust_chain_client) for live on-chain data
  2. The RobotEconomyManager for legacy workload submission (fallback)
"""

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("repryntt.tools.robot_economy")

_manager_cache = None


def _get_manager(brain_path=None):
    """Lazy-load the RobotEconomyManager singleton."""
    global _manager_cache
    if _manager_cache is not None:
        return _manager_cache
    try:
        from repryntt.economy.manager import RobotEconomyManager
        _manager_cache = RobotEconomyManager()
        return _manager_cache
    except Exception as e:
        logger.warning(f"RobotEconomyManager not available: {e}")
        return None


def _rust_rpc(method, params=None):
    """Call the Rust blockchain node JSON-RPC. Returns dict or None on error."""
    try:
        from repryntt.economy.rust_chain_client import rpc_call
        result = rpc_call(method, params, timeout=5.0)
        if isinstance(result, dict) and "error" in result:
            return None
        return result
    except Exception:
        return None


def _get_operator_wallet() -> str:
    """Return this device's node wallet address, with no genesis fallback."""
    try:
        from repryntt.economy.node_identity import get_local_node_address

        return get_local_node_address(create=True) or ""
    except Exception:
        return ""


_NOT_AVAILABLE = {"success": False, "error": "Robot Economy system not available"}


def start_robot_economy(brain_path=None, **kw) -> dict:
    """Start the robot economy ecosystem."""
    mgr = _get_manager(brain_path)
    if not mgr:
        return _NOT_AVAILABLE
    try:
        return mgr.start_economy()
    except Exception as e:
        logger.error(f"Error starting robot economy: {e}")
        return {"success": False, "error": str(e)}


def stop_robot_economy(brain_path=None, **kw) -> dict:
    """Stop the robot economy ecosystem."""
    mgr = _get_manager(brain_path)
    if not mgr:
        return _NOT_AVAILABLE
    try:
        return mgr.stop_economy()
    except Exception as e:
        logger.error(f"Error stopping robot economy: {e}")
        return {"success": False, "error": str(e)}


def get_economy_status(brain_path=None, **kw) -> dict:
    """Get current repryntt blockchain economy status (live from Rust node)."""
    # Try Rust node first (the real chain)
    info = _rust_rpc("get_chain_info")
    if info:
        mining = _rust_rpc("get_mining_stats") or {}
        net = _rust_rpc("get_network_stats") or {}
        operator_wallet = _get_operator_wallet()
        bal = (
            _rust_rpc("get_balance", {"address": operator_wallet})
            if operator_wallet
            else {}
        ) or {}
        return {
            "running": True,
            "phase": "mainnet",
            "chain": "repryntt-core (Rust)",
            "block_height": info.get("height", 0),
            "current_supply_cr": info.get("current_supply_cr", 0),
            "max_supply_cr": info.get("max_supply_cr", 0),
            "supply_percent": info.get("supply_percent", 0),
            "current_reward_cr": mining.get("current_reward_cr", 0),
            "halvings_completed": mining.get("halvings_completed", 0),
            "next_halving_block": mining.get("next_halving_block", 0),
            "staker_count": mining.get("staker_count", 0),
            "total_staked_cr": mining.get("total_staked_cr", 0),
            "peers": net.get("peers", 0),
            "operator_wallet": {
                "address": operator_wallet,
                "balance_cr": bal.get("balance_cr", 0),
                "stake_cr": bal.get("stake_cr", 0),
                "reputation": bal.get("reputation", 0),
            },
            "block_interval_secs": info.get("block_interval_secs", 69),
        }
    # Fallback to legacy manager
    mgr = _get_manager(brain_path)
    if not mgr:
        return _NOT_AVAILABLE
    try:
        return mgr.get_status()
    except Exception as e:
        logger.error(f"Error getting economy status: {e}")
        return {"success": False, "error": str(e)}


def submit_robot_workload(brain_path=None, workload_data: dict = None, **kw) -> dict:
    """Submit a custom workload to the robot economy.

    Parameters:
        workload_data: Dict with workload specification (purpose, type, etc.)
    """
    mgr = _get_manager(brain_path)
    if not mgr:
        return _NOT_AVAILABLE
    try:
        return mgr.submit_custom_workload(workload_data or {})
    except Exception as e:
        logger.error(f"Error submitting workload: {e}")
        return {"success": False, "error": str(e)}


def get_robot_wallet_balance(brain_path=None, address: str = "", **kw) -> dict:
    """Get wallet balance for a repryntt blockchain address.

    Parameters:
        address: Wallet address to check (defaults to this node's wallet)
    """
    if not address:
        address = _get_operator_wallet()
    if not address:
        return {
            "success": False,
            "error": "No local node wallet address configured",
        }
    bal = _rust_rpc("get_balance", {"address": address})
    if bal:
        return {
            "success": True,
            "address": bal.get("address", address),
            "balance_plancks": bal.get("balance_plancks", 0),
            "balance_credits": bal.get("balance_cr", 0),
            "stake_credits": bal.get("stake_cr", 0),
            "reputation": bal.get("reputation", 0),
            "nonce": bal.get("nonce", 0),
        }
    # Fallback
    mgr = _get_manager(brain_path)
    if not mgr:
        return _NOT_AVAILABLE
    try:
        return mgr.get_wallet_balance(address)
    except Exception as e:
        logger.error(f"Error getting wallet balance: {e}")
        return {"success": False, "error": str(e)}


def get_robot_blockchain_info(brain_path=None, **kw) -> dict:
    """Get blockchain info from the repryntt Rust node."""
    info = _rust_rpc("get_chain_info")
    if info:
        return {
            "success": True,
            "chain": "repryntt-core (Rust)",
            "chain_length": info.get("height", 0),
            "latest_hash": info.get("latest_hash", "")[:32] + "...",
            "genesis_hash": info.get("genesis_hash", "")[:32] + "...",
            "current_supply_cr": info.get("current_supply_cr", 0),
            "max_supply_cr": info.get("max_supply_cr", 0),
            "supply_percent": info.get("supply_percent", 0),
            "current_reward_cr": info.get("current_reward_cr", 0),
            "halvings_completed": info.get("halvings_completed", 0),
            "next_halving_block": info.get("next_halving_block", 0),
            "block_interval_secs": info.get("block_interval_secs", 69),
        }
    # Fallback
    mgr = _get_manager(brain_path)
    if not mgr:
        return _NOT_AVAILABLE
    try:
        return mgr.get_blockchain_info()
    except Exception as e:
        logger.error(f"Error getting blockchain info: {e}")
        return {"success": False, "error": str(e)}


def allocate_robot_dao_funds(brain_path=None, machine_address: str = "",
                             amount_mp: float = 0, purpose: str = "", **kw) -> dict:
    """Allocate DAO funds for a specific purpose.

    Parameters:
        machine_address: Target machine wallet address
        amount_mp: Amount in MP credits to allocate
        purpose: Description of the allocation purpose
    """
    mgr = _get_manager(brain_path)
    if not mgr:
        return _NOT_AVAILABLE
    try:
        return mgr.allocate_dao_funds(machine_address, float(amount_mp), purpose)
    except Exception as e:
        logger.error(f"Error allocating DAO funds: {e}")
        return {"success": False, "error": str(e)}


def create_robot_wallet(brain_path=None, **kw) -> dict:
    """Create a new quantum-safe wallet."""
    mgr = _get_manager(brain_path)
    if not mgr:
        return _NOT_AVAILABLE
    try:
        return mgr.create_wallet()
    except Exception as e:
        logger.error(f"Error creating wallet: {e}")
        return {"success": False, "error": str(e)}


def recover_robot_wallet(brain_path=None, key_phrase: str = "", **kw) -> dict:
    """Recover wallet from key phrase.

    Parameters:
        key_phrase: Recovery key phrase for the wallet
    """
    mgr = _get_manager(brain_path)
    if not mgr:
        return _NOT_AVAILABLE
    try:
        return mgr.recover_wallet(key_phrase)
    except Exception as e:
        logger.error(f"Error recovering wallet: {e}")
        return {"success": False, "error": str(e)}


def monitor_robot_economy(brain_path=None, **kw) -> dict:
    """Get comprehensive economy monitoring information."""
    status = get_economy_status(brain_path)
    blockchain_info = get_robot_blockchain_info(brain_path)
    return {
        "success": True,
        "economy_status": status,
        "blockchain_info": blockchain_info,
        "timestamp": datetime.now().isoformat(),
        "analysis": _analyze_economy_health(status, blockchain_info),
    }


def _analyze_economy_health(status: dict, blockchain_info: dict) -> dict:
    analysis = {"overall_health": "unknown", "recommendations": [], "metrics_summary": {}}
    try:
        if not status.get("running", False):
            analysis["overall_health"] = "stopped"
            analysis["recommendations"].append("Blockchain node is not running — check systemctl status repryntt-chain")
            return analysis
        height = status.get("block_height", 0)
        supply_pct = status.get("supply_percent", 0)
        staked = status.get("total_staked_cr", 0)
        supply = status.get("current_supply_cr", 0)
        analysis["metrics_summary"] = {
            "block_height": height,
            "supply_cr": supply,
            "supply_percent": supply_pct,
            "staked_cr": staked,
        }
        if height > 0:
            analysis["overall_health"] = "healthy"
        if height > 0 and staked == 0:
            analysis["recommendations"].append("No tokens staked yet — consider staking to earn availability rewards")
        if supply_pct > 50:
            analysis["recommendations"].append(f"Supply is {supply_pct}% mined — approaching maturity")
        if not analysis["recommendations"]:
            analysis["recommendations"].append("Chain operating normally")
    except Exception:
        pass
    return analysis


def get_system_health(brain_path=None, **kw) -> dict:
    """Get a combined health snapshot: blockchain node + agent performance.

    Returns node status (block height, supply, peers, operator wallet) and
    today's agent telemetry (heartbeats, tool calls, API calls, scores).
    One call per day is enough — don't build external monitoring scripts.
    """
    result = {"success": True, "timestamp": datetime.now().isoformat()}

    # ── Node health (Rust chain) ──
    info = _rust_rpc("get_chain_info")
    if info:
        operator_wallet = _get_operator_wallet()
        bal = (
            _rust_rpc("get_balance", {"address": operator_wallet})
            if operator_wallet
            else {}
        ) or {}
        result["node"] = {
            "running": True,
            "chain": "repryntt-core (Rust)",
            "block_height": info.get("height", 0),
            "current_supply_cr": info.get("current_supply_cr", 0),
            "max_supply_cr": info.get("max_supply_cr", 0),
            "supply_percent": info.get("supply_percent", 0),
            "current_reward_cr": info.get("current_reward_cr", 0),
            "halvings_completed": info.get("halvings_completed", 0),
            "operator_wallet": operator_wallet,
            "operator_balance_cr": bal.get("balance_cr", 0),
            "operator_stake_cr": bal.get("stake_cr", 0),
        }
    else:
        result["node"] = {"running": False, "error": "Rust node not reachable"}

    # ── Agent telemetry (today) ──
    try:
        telemetry_dir = Path.home() / ".repryntt" / "workspace" / "telemetry"
        today = datetime.now().strftime("%Y-%m-%d")
        jsonl_path = telemetry_dir / f"{today}.jsonl"

        agent_stats = {
            "date": today,
            "heartbeats": 0,
            "heartbeat_scores": [],
            "tool_calls": 0,
            "api_calls": 0,
            "errors": 0,
            "unique_tools_used": set(),
            "total_duration_ms": 0,
        }

        if jsonl_path.exists():
            with open(jsonl_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    etype = evt.get("event_type", "")
                    meta = evt.get("metadata") or {}
                    if etype == "heartbeat_end":
                        agent_stats["heartbeats"] += 1
                        score = meta.get("score")
                        if score is not None:
                            agent_stats["heartbeat_scores"].append(score)
                        dur = evt.get("duration_ms")
                        if dur:
                            agent_stats["total_duration_ms"] += dur
                    elif etype == "tool_call":
                        agent_stats["tool_calls"] += 1
                        tool_name = meta.get("tool") or meta.get("tool_name", "")
                        if tool_name:
                            agent_stats["unique_tools_used"].add(tool_name)
                    elif etype == "api_call":
                        agent_stats["api_calls"] += 1
                    elif etype == "error":
                        agent_stats["errors"] += 1

        # Compute summary
        scores = agent_stats.pop("heartbeat_scores")
        agent_stats["unique_tools_used"] = len(agent_stats["unique_tools_used"])
        if scores:
            agent_stats["avg_score"] = round(sum(scores) / len(scores), 2)
            agent_stats["min_score"] = min(scores)
            agent_stats["max_score"] = max(scores)
        else:
            agent_stats["avg_score"] = None

        total_ms = agent_stats.pop("total_duration_ms")
        if total_ms > 0:
            agent_stats["total_active_minutes"] = round(total_ms / 60000, 1)

        result["agent"] = agent_stats
    except Exception as e:
        result["agent"] = {"error": str(e)}

    return result
