"""
Tool execution, schema building, and credit tracking.

Standalone functions extracted from BrainSystem monolith (Batch 3).
All functions are pure — no BrainSystem dependency.
"""

import inspect
import json
import logging
import os
import random
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Schema Building ─────────────────────────────────────────────────

def build_native_tool_schemas(
    available_tools: Dict[str, Callable],
    task_context: str = "",
    map_network: Any = None,
    mcp_client: Any = None,
    essential_tool_names: Optional[List[str]] = None,
    max_tools: int = 25,
) -> List[Dict[str, Any]]:
    """Build OpenAI-compatible tool/function schemas from an available_tools dict.

    Uses *map_network* (MapSyncNetwork) for relevance-based selection when
    available, otherwise falls back to a curated essential set.

    Parameters
    ----------
    available_tools : dict
        Mapping of tool-name → callable.
    task_context : str
        Current task description for relevance ranking.
    map_network : object | None
        Optional MapSyncNetwork with a ``search_tools_hybrid`` method.
    mcp_client : object | None
        Optional MCP client whose tools should be appended.
    essential_tool_names : list[str] | None
        Override list of tool names to always include.
    max_tools : int
        Maximum number of tool schemas to return.

    Returns
    -------
    list[dict]
        OpenAI-compatible ``{"type": "function", "function": {...}}`` dicts.
    """
    if not available_tools:
        return []

    tools_schema: List[Dict[str, Any]] = []

    # ── Select relevant tools ────────────────────────────────────
    selected_tools: Dict[str, Callable] = {}

    if map_network and task_context:
        try:
            relevant = map_network.search_tools_hybrid(task_context, limit=max_tools)
            if relevant:
                for name in relevant:
                    if name in available_tools:
                        selected_tools[name] = available_tools[name]
        except Exception as e:
            logger.debug(f"MapSyncNetwork search failed, using fallback: {e}")

    if not selected_tools:
        # Fallback: curated essentials
        default_essentials = [
            "search_knowledge", "brain_network_search", "grokipedia_search",
            "write_file", "read_file", "list_directory",
            "quick_research", "quick_brainstorm",
            "mcp_fetch_fetch", "get_current_time", "store_thoughts",
        ]
        essentials = essential_tool_names or default_essentials
        for name in essentials:
            if name in available_tools:
                selected_tools[name] = available_tools[name]

    # ── Build schemas ────────────────────────────────────────────
    for name, func in list(selected_tools.items())[:max_tools]:
        try:
            schema = _build_single_tool_schema(name, func)
            if schema:
                tools_schema.append(schema)
        except Exception as e:
            logger.debug(f"Failed to build schema for {name}: {e}")

    # ── Append MCP tools ─────────────────────────────────────────
    if mcp_client:
        try:
            mcp_tools = getattr(mcp_client, "tools", None) or {}
            for mcp_name, mcp_tool in mcp_tools.items():
                if len(tools_schema) >= max_tools:
                    break
                input_schema = getattr(mcp_tool, "input_schema", None) or mcp_tool.get("input_schema", {}) if isinstance(mcp_tool, dict) else {}
                tools_schema.append({
                    "type": "function",
                    "function": {
                        "name": mcp_name,
                        "description": (getattr(mcp_tool, "description", "") or (mcp_tool.get("description", "") if isinstance(mcp_tool, dict) else ""))[:200],
                        "parameters": input_schema if input_schema else {"type": "object", "properties": {}},
                    },
                })
        except Exception as e:
            logger.debug(f"MCP tool schema append failed: {e}")

    return tools_schema


def _build_single_tool_schema(name: str, func: Callable) -> Optional[Dict[str, Any]]:
    """Build a single OpenAI-compatible function schema from a callable."""
    doc = (inspect.getdoc(func) or "")[:500]
    properties: Dict[str, Any] = {}
    required: List[str] = []

    # Extract per-parameter descriptions from docstring
    # Supports both "Args:" sections and "param_name:" at any indent
    _param_descs: Dict[str, str] = {}

    try:
        sig = inspect.signature(func)
        # Get real params, filter out self, *args, **kwargs
        sig_params = [p for p in sig.parameters
                      if p not in ('self', 'cls', 'brain', 'brain_system')
                      and sig.parameters[p].kind not in
                      (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)]

        if doc:
            for _line in doc.split('\n'):
                stripped = _line.strip()
                for pn in sig_params:
                    if (stripped.startswith(f'{pn}:') or
                        stripped.startswith(f'{pn} —') or
                        stripped.startswith(f'{pn} --') or
                        stripped.startswith(f'{pn} (')):
                        sep = ':' if ':' in stripped[:len(pn)+5] else ('—' if '—' in stripped else '--')
                        desc = stripped.split(sep, 1)[1].strip() if sep in stripped else ''
                        if desc:
                            _param_descs[pn] = desc[:300]

        for pname, param in sig.parameters.items():
            if pname in ('self', 'cls', 'brain', 'brain_system'):
                continue
            if param.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
                continue
            _desc = _param_descs.get(pname, pname)
            prop: Dict[str, Any] = {"type": "string", "description": _desc}
            if param.default is inspect.Parameter.empty:
                required.append(pname)
            elif isinstance(param.default, bool):
                prop["type"] = "boolean"
            elif isinstance(param.default, int):
                prop["type"] = "integer"
            elif isinstance(param.default, float):
                prop["type"] = "number"
            properties[pname] = prop
    except (ValueError, TypeError):
        pass

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": doc,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


# ── Native Tool Execution ───────────────────────────────────────────

def execute_native_tool_calls(
    tool_calls: List[Dict[str, Any]],
    available_tools: Dict[str, Callable],
) -> List[Dict[str, Any]]:
    """Execute structured tool_calls (OpenAI format) and return results.

    Each item in *tool_calls* should have::

        {"id": "...", "function": {"name": "...", "arguments": "..."}}

    Parameters
    ----------
    tool_calls : list[dict]
        OpenAI-style tool call list from an LLM response.
    available_tools : dict
        Mapping of tool-name → callable.

    Returns
    -------
    list[dict]
        ``{"role": "tool", "tool_call_id": ..., "content": ...}`` entries.
    """
    results: List[Dict[str, Any]] = []

    for call in tool_calls:
        call_id = call.get("id", f"call_{int(time.time())}")
        func_info = call.get("function", {})
        func_name = func_info.get("name", "unknown")
        raw_args = func_info.get("arguments", "{}")

        # Parse arguments
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                args = {}
        else:
            args = raw_args if isinstance(raw_args, dict) else {}

        # Look up function
        func = available_tools.get(func_name)
        if not func:
            results.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": f"Error: Tool '{func_name}' not found",
            })
            continue

        # Strip invalid parameters
        try:
            sig = inspect.signature(func)
            valid_params = set(sig.parameters.keys()) - {"self", "cls", "kwargs"}
            has_kwargs = any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            )
            if not has_kwargs:
                args = {k: v for k, v in args.items() if k in valid_params}
        except (ValueError, TypeError):
            pass

        # Execute
        try:
            result = func(**args)
            content = json.dumps(result) if not isinstance(result, str) else result
        except Exception as e:
            content = f"Error executing {func_name}: {e}"

        results.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": content[:8000],  # Cap output size
        })

    return results


# ── Tool Call Redundancy Check ───────────────────────────────────────

# Per-chain tool call history (module-level cache, cleared per chain)
_chain_tool_calls: Dict[str, List[Dict[str, Any]]] = {}


def should_skip_tool_call(
    chain_data: Dict[str, Any],
    tool_name: str,
    parameters: Dict[str, Any],
) -> Tuple[bool, str]:
    """Check if a tool call should be skipped due to redundancy.

    Returns (should_skip, reason).
    """
    try:
        chain_id = chain_data.get("metadata", {}).get("chain_id", "unknown")
        if chain_id not in _chain_tool_calls:
            _chain_tool_calls[chain_id] = []

        recent_calls = _chain_tool_calls[chain_id]

        if tool_name in ("search_knowledge", "analyze_topic"):
            query = parameters.get("query", "").lower().strip()

            for recent_call in recent_calls[-5:]:
                if recent_call["tool"] == tool_name:
                    recent_query = recent_call.get("query", "").lower().strip()
                    recent_words = set(recent_query.split())
                    current_words = set(query.split())

                    if recent_words and current_words:
                        overlap = len(recent_words & current_words)
                        similarity = overlap / max(len(recent_words), len(current_words))
                        if similarity > 0.6:
                            return True, f"Skipping redundant {tool_name} call - similar to recent query: '{recent_query[:50]}...'"

            recent_calls.append({
                "tool": tool_name,
                "query": query,
                "timestamp": time.time(),
            })
            _chain_tool_calls[chain_id] = recent_calls[-10:]

        return False, ""

    except Exception as e:
        logger.error(f"Error checking tool call redundancy: {e}")
        return False, ""


# ── Tool Credit Economy ──────────────────────────────────────────────

def get_tool_credit_cost(tool_name: str) -> float:
    """Get the credit cost for executing a tool.

    Returns a float in the range 0.001 – 0.5.
    """
    costs = {
        # Knowledge & Search
        "grokipedia_search": 0.05,
        "brain_network_search": 0.03,
        "search_knowledge": 0.05,
        "analyze_topic": 0.04,
        "store_semantic_memory": 0.02,
        "store_thoughts": 0.01,
        "recall_thoughts": 0.01,
        "recall_memory": 0.01,
        "search_semantic_memory": 0.03,
        "get_context_for_question": 0.02,
        "get_brain_stats": 0.001,
        "get_current_time": 0.001,
        # File Operations
        "write_file": 0.03,
        "read_file": 0.02,
        "list_directory": 0.01,
        "search_files": 0.02,
        "execute_python": 0.1,
        "execute_shell": 0.1,
        # Web / MCP
        "mcp_fetch_fetch": 0.05,
        "mcp_brave_search": 0.05,
        # Swarm & Council
        "quick_research": 0.2,
        "quick_brainstorm": 0.15,
        "start_discussion": 0.15,

        "swarm_dispatch_task": 0.1,
        "swarm_broadcast_task": 0.15,
        "swarm_delegate_tasks": 0.15,
        "swarm_create_agent": 0.05,
        "swarm_create_swarm": 0.05,
        "swarm_retire_agent": 0.01,
        "swarm_dissolve_swarm": 0.01,
        # Media
        "generate_image": 0.3,
        "analyze_image_with_gemini": 0.15,
        "capture_camera": 0.05,
        "speak": 0.02,
        "listen": 0.05,
        "post_tweet": 0.1,
        "post_tweet_autonomous": 0.1,
        "check_twitter_mentions": 0.03,
        "reply_to_twitter_mention": 0.1,
        # Chain operations
        "start_tool_chain": 0.05,
        "advance_tool_chain": 0.05,
        "get_tool_chain_status": 0.01,
        # Economy
        "blockchain_transfer": 0.5,
        "check_balance": 0.01,
        "mining_status": 0.01,
        # Personality
        "modify_personality_trait": 0.02,
        "modify_core_values": 0.02,
        # Code tools
        "read_code": 0.02,
        "write_code": 0.03,
        "analyze_code": 0.05,
        # Twitter search
        "x_search_tweets": 0.05,
        "x_search_recent": 0.05,
    }
    return costs.get(tool_name, 0.01)  # Default cost


def get_tool_credit_reward(tool_name: str) -> float:
    """Get the credit reward for successfully executing a tool.

    Rewards are smaller than costs to create a credit sink.
    """
    rewards = {
        "grokipedia_search": 0.02,
        "brain_network_search": 0.01,
        "search_knowledge": 0.02,
        "store_semantic_memory": 0.01,
        "store_thoughts": 0.005,
        "quick_research": 0.08,

        "write_file": 0.01,
        "generate_image": 0.1,
        "post_tweet_autonomous": 0.05,
        "start_discussion": 0.06,
        "analyze_topic": 0.015,
        "execute_python": 0.04,
        "blockchain_transfer": 0.2,
        "mcp_fetch_fetch": 0.02,
    }
    return rewards.get(tool_name, 0.0)


# ── AI Wallet ────────────────────────────────────────────────────────

_cached_ai_wallet: Optional[str] = None


def get_ai_wallet_address(personality_brain: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Get the AI's wallet address from personality brain or cache."""
    global _cached_ai_wallet

    if _cached_ai_wallet:
        return _cached_ai_wallet

    if personality_brain:
        try:
            wallet = (
                personality_brain
                .get("metadata", {})
                .get("economy", {})
                .get("wallet_address")
            )
            if wallet:
                _cached_ai_wallet = wallet
                return wallet
        except Exception:
            pass

    # Try loading from file
    for path in ("brain/node2040_brain.json", "node2040_brain.json"):
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                wallet = data.get("metadata", {}).get("economy", {}).get("wallet_address")
                if wallet:
                    _cached_ai_wallet = wallet
                    return wallet
            except Exception:
                continue

    return None


# ── Blockchain AI Routing ────────────────────────────────────────────

def should_route_ai_through_blockchain(
    prompt: str,
    include_tools: bool,
    use_blockchain_ai: bool = False,
    economy_manager: Any = None,
    route_percentage: int = 50,
) -> bool:
    """Decide whether to route an AI call through the blockchain economy.

    Conversational prompts stay local for responsiveness.
    Complex / tool-heavy prompts may be routed based on *route_percentage*.
    """
    if not (use_blockchain_ai and economy_manager):
        return False

    prompt_lower = prompt.lower()
    conversational = [
        "hello", "hi", "how are you", "what do you think", "tell me about",
        "explain", "describe", "what is", "how does", "why does",
        "conversation", "chat", "talk", "discuss", "respond to",
    ]
    if any(ind in prompt_lower for ind in conversational):
        return False

    complexity = [
        "analyze", "research", "investigate", "explore", "study",
        "create a", "write a", "generate", "design", "develop",
        "comprehensive", "detailed", "thorough", "extensive",
        "paper", "report", "documentation", "implementation",
    ]
    has_complexity = any(ind in prompt_lower for ind in complexity)
    is_long = len(prompt) > 500
    should_route = has_complexity or is_long or include_tools

    if should_route:
        should_route = random.random() * 100 < route_percentage

    return should_route


def call_ai_via_blockchain(
    prompt: str,
    economy_manager: Any,
    personality_brain: Optional[Dict[str, Any]] = None,
    max_tokens: int = 2000,
    temperature: float = 0.8,
    timeout: int = 120,
) -> str:
    """Route an AI inference call through the blockchain economy.

    Submits a workload to the blockchain node and polls for a miner result.
    Uses ``safe_serialize`` for wire protocol — never pickle.

    Returns the AI response text, or an error string starting with
    ``BLOCKCHAIN_ERROR:``.
    """
    import socket
    import struct

    # Import safe_serialize from the robot_economy package
    try:
        from safe_serialize import pack as safe_pack, unpack as safe_unpack
    except ImportError:
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "robot_economy"))
        from safe_serialize import pack as safe_pack, unpack as safe_unpack

    try:
        wallet = get_ai_wallet_address(personality_brain)
        if not wallet:
            return "BLOCKCHAIN_ERROR: No AI wallet available"

        node_host = "127.0.0.1"
        node_port = 5001
        if economy_manager and hasattr(economy_manager, "config"):
            node_port = economy_manager.config.get("node_port", 5001)

        logger.info(f"Connecting to blockchain node at {node_host}:{node_port}")
        fee_plancks = 100000000  # 1 Credit

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect((node_host, node_port))

            submit_message = {
                "type": "submit_ai_inference",
                "requester_address": wallet,
                "prompt": prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "fee_plancks": fee_plancks,
            }

            data = safe_pack(submit_message)
            s.sendall(struct.pack("!I", len(data)))
            s.sendall(data)

            length_bytes = s.recv(4)
            if len(length_bytes) < 4:
                return "BLOCKCHAIN_ERROR: No response from blockchain node"

            length = struct.unpack("!I", length_bytes)[0]
            response_data = b""
            while len(response_data) < length:
                packet = s.recv(min(length - len(response_data), 4096))
                if not packet:
                    break
                response_data += packet

            submit_result = safe_unpack(response_data)

        if not submit_result.get("success"):
            error = submit_result.get("error", "Unknown error")
            logger.error(f"Failed to submit blockchain AI workload: {error}")
            return f"BLOCKCHAIN_ERROR: {error}"

        workload_key = submit_result.get("workload_key")
        logger.info(f"Submitted blockchain AI workload: {workload_key[:16]}...")

        # Poll for result
        start_time = time.time()
        poll_interval = 2

        while True:
            if timeout and (time.time() - start_time) > timeout:
                logger.error(f"Blockchain AI timeout after {timeout}s")
                return f"BLOCKCHAIN_ERROR: Timeout waiting for workload completion ({timeout}s)"

            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(5)
                    s.connect((node_host, node_port))

                    get_msg = {
                        "type": "get_ai_result",
                        "workload_key": workload_key,
                        "timeout": 0,
                    }
                    data = safe_pack(get_msg)
                    s.sendall(struct.pack("!I", len(data)))
                    s.sendall(data)

                    length_bytes = s.recv(4)
                    if len(length_bytes) < 4:
                        time.sleep(poll_interval)
                        continue

                    length = struct.unpack("!I", length_bytes)[0]
                    response_data = b""
                    while len(response_data) < length:
                        packet = s.recv(min(length - len(response_data), 4096))
                        if not packet:
                            break
                        response_data += packet

                    result = safe_unpack(response_data)

                if result.get("success"):
                    ai_result = result.get("result", {})
                    if isinstance(ai_result, dict):
                        ai_text = ai_result.get("text", "")
                        tokens = ai_result.get("total_tokens", 0)
                        inf_time = ai_result.get("inference_time", 0)
                        miner = result.get("miner_address", "unknown")[:16]
                        logger.info(f"Blockchain AI result: {tokens} tokens, {inf_time:.2f}s, miner: {miner}...")
                        return ai_text
                    return str(ai_result)
                else:
                    time.sleep(poll_interval)

            except socket.timeout:
                time.sleep(poll_interval)
            except Exception as poll_error:
                logger.warning(f"Poll attempt failed: {poll_error}")
                time.sleep(poll_interval)

    except socket.timeout:
        return f"BLOCKCHAIN_ERROR: Timeout waiting for AI result"
    except Exception as e:
        logger.error(f"Blockchain AI error: {e}", exc_info=True)
        return f"BLOCKCHAIN_ERROR: {e}"


# ── Task-Specific Tool Config ───────────────────────────────────────

def get_task_specific_tools(
    task_description: str,
    task_hierarchy: Any = None,
) -> Dict[str, Any]:
    """Get task-specific tool configuration for AI guidance.

    If *task_hierarchy* is provided (TaskHierarchy instance), uses it for
    classification. Otherwise returns sensible defaults.
    """
    if task_hierarchy:
        try:
            task_config = task_hierarchy.classify_task(task_description)
            return {
                "task_type": task_config.name,
                "preferred_tools": task_config.preferred_tools,
                "tool_priorities": task_config.priority_tools,
                "evaluation_criteria": task_config.evaluation_criteria,
                "success_metrics": task_config.success_metrics,
                "guidance": f"For {task_config.name.lower()} tasks, prioritize: {', '.join(task_config.preferred_tools[:3])}",
            }
        except Exception as e:
            logger.error(f"Error getting task-specific tools: {e}")

    return {
        "task_type": "unknown",
        "preferred_tools": ["search_knowledge", "brain_network_search"],
        "tool_priorities": {},
        "evaluation_criteria": ["completeness", "accuracy"],
        "success_metrics": ["information quality"],
        "guidance": "Use general knowledge search tools",
    }


# ── Brain Network Status (top-level helper) ─────────────────────────

def get_brain_network_status(brain: Any) -> Dict[str, Any]:
    """Get status of entire brain network for AI awareness."""
    if hasattr(brain, "get_brain_stats"):
        return brain.get_brain_stats()
    return {"status": "unknown", "error": "brain has no get_brain_stats method"}
