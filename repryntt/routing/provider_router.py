"""
repryntt.routing.provider_router — Multi-provider AI routing.

Supports:
    - local   : llama.cpp / ollama / vLLM (OpenAI-compatible)
    - openai  : OpenAI API (GPT-4, GPT-4o, etc.)
    - anthropic: Anthropic Claude (wrapped to OpenAI format)
    - openrouter: OpenRouter (any model)
    - custom  : Any OpenAI-compatible endpoint

Extracted from: SAIGE/brain/brain_system.py
    _load_ai_provider_config    (line 7695)
    _route_ai_call              (line 7928)
    _call_anthropic             (line 8001)
    _build_native_tool_schemas  (line 7739)
    _execute_native_tool_calls  (line 7851)
    _call_ai_service            (line 8268)
    _get_ai_parameters_for_self_autonomous (line 8817)
    _build_intelligent_tool_context (line 8152)  — Phase 5
    _get_mcp_tools_for_prompt       (line 8117)  — Phase 5
    _get_swarm_tools_for_prompt     (line 8080)  — Phase 5
    _get_council_tools_for_prompt   (line 8102)  — Phase 5
    _should_route_ai_through_blockchain (line 8600) — Phase 5
    _call_ai_via_blockchain         (line 8650)  — Phase 5
    reload_ai_config                (line 8068)  — Phase 5
    _detect_novelty                 (line 2295)  — Phase 5
"""

import hashlib
import inspect
import json
import logging
import os
import random
import socket
import struct
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

from .ai_queue import master_ai_queue

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------

from repryntt.paths import local_llm_endpoint as _llm_ep

DEFAULT_PROVIDER_CONFIG: Dict[str, Any] = {
    "provider": "local",
    "local": {
        "endpoint": _llm_ep(),
        "model": "default",
        "api_key": None,
        "max_tokens": 800,
        "context_window": 4096,
    },
}


def load_ai_provider_config(config_dir: Path) -> Dict[str, Any]:
    """Load AI provider config from *config_dir*/ai_config.json.

    Falls back to ``DEFAULT_PROVIDER_CONFIG`` if the file is missing or broken.
    """
    config_path = config_dir / "ai_config.json"
    try:
        if config_path.exists():
            with open(config_path, "r") as f:
                full = json.load(f)
            config = full.get("ai_provider", DEFAULT_PROVIDER_CONFIG)
            provider = config.get("provider", "local")
            settings = config.get(provider, DEFAULT_PROVIDER_CONFIG["local"])
            logger.info(
                f"AI Provider: {provider} -> "
                f"{settings.get('endpoint', 'unknown')[:60]}..."
            )
            return config
    except Exception as e:
        logger.warning(f"Failed to load ai_config.json: {e} — local defaults")
    logger.info("AI Provider: local (defaults)")
    return dict(DEFAULT_PROVIDER_CONFIG)


def reload_ai_config(config_dir: Path) -> str:
    """Reload AI provider config without restarting. Can be called as a tool."""
    config = load_ai_provider_config(config_dir)
    provider = config.get("provider", "local")
    settings = config.get(provider, {})
    return json.dumps({
        "success": True,
        "provider": provider,
        "endpoint": settings.get("endpoint", "unknown"),
        "model": settings.get("model", "unknown"),
    })


# ---------------------------------------------------------------------------
# Low-level HTTP routing
# ---------------------------------------------------------------------------


def route_ai_call(
    config: Dict[str, Any],
    prompt: str,
    ai_params: Dict[str, Any],
    *,
    messages: Optional[List[Dict]] = None,
    tools: Optional[List[Dict]] = None,
) -> requests.Response:
    """Route an AI call to the configured provider.

    Always returns a ``requests.Response`` with OpenAI-format JSON body,
    regardless of the actual backend.
    """
    provider = config.get("provider", "local")
    settings = config.get(provider, config.get("local", {}))

    endpoint = settings.get("endpoint", _llm_ep())
    model = settings.get("model", "default")
    api_key = settings.get("api_key")
    max_tokens = settings.get("max_tokens", ai_params.get("max_tokens", 800))

    headers: Dict[str, str] = {"Content-Type": "application/json"}
    placeholder_keys = {
        None, "", "YOUR_OPENAI_API_KEY_HERE",
        "YOUR_ANTHROPIC_API_KEY_HERE", "YOUR_OPENROUTER_API_KEY_HERE",
    }
    if api_key not in placeholder_keys:
        headers["Authorization"] = f"Bearer {api_key}"

    if provider == "anthropic":
        return _call_anthropic(endpoint, model, api_key, prompt, max_tokens, ai_params)

    # OpenAI-compatible (local, openai, openrouter, custom)
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages or [{"role": "user", "content": prompt}],
        "max_tokens": ai_params.get("max_tokens", max_tokens),
        "temperature": ai_params.get("temperature", 0.8),
        "top_p": ai_params.get("top_p", 0.9),
        "frequency_penalty": ai_params.get("frequency_penalty", 0.3),
        "stream": False,
    }

    # llama.cpp extended params (hormone-modulated)
    for extra in (
        "top_k", "min_p", "typical_p", "presence_penalty",
        "repeat_penalty", "repeat_last_n", "dynatemp_range",
    ):
        if extra in ai_params:
            body[extra] = ai_params[extra]

    # Native tool calling (skip for local — llama.cpp needs --jinja)
    if tools and provider != "local":
        body["tools"] = tools
        body["tool_choice"] = "auto"

    return requests.post(endpoint, headers=headers, json=body, timeout=None)


def _call_anthropic(
    endpoint: str,
    model: str,
    api_key: str,
    prompt: str,
    max_tokens: int,
    ai_params: Dict[str, Any],
) -> requests.Response:
    """Call Anthropic Messages API and wrap response in OpenAI format."""
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    try:
        resp = requests.post(
            endpoint,
            headers=headers,
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": ai_params.get("temperature", 0.8),
                "top_p": ai_params.get("top_p", 0.9),
            },
            timeout=None,
        )
        if resp.status_code == 200:
            data = resp.json()
            text = "".join(
                b.get("text", "") for b in data.get("content", [])
                if b.get("type") == "text"
            )
            openai_fmt = {
                "choices": [{
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": data.get("stop_reason", "end_turn"),
                }],
                "model": model,
                "usage": data.get("usage", {}),
            }
            wrapped = requests.models.Response()
            wrapped.status_code = 200
            wrapped._content = json.dumps(openai_fmt).encode("utf-8")
            wrapped.headers["Content-Type"] = "application/json"
            return wrapped
        return resp
    except Exception as e:
        logger.error(f"Anthropic API call failed: {e}")
        wrapped = requests.models.Response()
        wrapped.status_code = 500
        wrapped._content = json.dumps({"error": str(e)}).encode("utf-8")
        return wrapped


# ---------------------------------------------------------------------------
# Native tool-schema builder
# ---------------------------------------------------------------------------


def build_native_tool_schemas(
    available_tools: Dict[str, Callable],
    prompt: str = "",
    max_tools: int = 40,
    map_sync_network: Any = None,
) -> List[Dict]:
    """Build OpenAI-compatible function schemas from *available_tools*.

    Uses *map_sync_network* (if provided) for relevance-based selection;
    otherwise falls back to a curated essential set.
    """
    if not available_tools:
        return []

    relevant_names: List[str] = []
    if prompt and map_sync_network:
        try:
            result = map_sync_network.query_capabilities(prompt[:200], limit=max_tools)
            relevant_names = [f["name"] for f in result]
        except Exception:
            pass

    if not relevant_names:
        relevant_names = [
            "knowledge_search", "google_web_search", "grokipedia_search",
            "mcp_fetch_fetch", "read_file", "write_file", "grep_search",
            "run_terminal_cmd", "quick_research", "web_search_results_only",
            "scrape_web_page", "brain_network_search", "recall_memory",
            "create_self_autonomous_chain", "search_knowledge",
            "create_creative_file", "write_to_creative_file",
            "dexscreener_trending", "dexscreener_token_search",
            "solana_rpc_query", "speak", "generate_image", "capture_camera",
        ]

    schemas: List[Dict] = []
    for name in relevant_names:
        if name not in available_tools:
            continue
        func = available_tools[name]

        description = ""
        parameters: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}

        mcp_info = getattr(func, "_mcp_tool_info", None)
        if mcp_info and hasattr(mcp_info, "input_schema") and mcp_info.input_schema:
            schema = mcp_info.input_schema
            parameters = {
                "type": "object",
                "properties": schema.get("properties", {}),
                "required": schema.get("required", []),
            }
            description = (mcp_info.description or "").split("\n")[0].strip()[:200]
        else:
            try:
                sig = inspect.signature(func)
                props: Dict[str, Any] = {}
                required: List[str] = []
                for pname, param in sig.parameters.items():
                    if pname in ("self", "kwargs", "args"):
                        continue
                    ptype = "string"
                    if param.annotation != inspect.Parameter.empty:
                        ann = param.annotation
                        if ann == int:
                            ptype = "integer"
                        elif ann == float:
                            ptype = "number"
                        elif ann == bool:
                            ptype = "boolean"
                        elif ann == list:
                            ptype = "array"
                    props[pname] = {"type": ptype, "description": pname}
                    if param.default == inspect.Parameter.empty:
                        required.append(pname)
                parameters = {"type": "object", "properties": props, "required": required}
            except Exception:
                pass

            doc = getattr(func, "__doc__", "") or ""
            description = doc.split("\n")[0].strip()[:200] if doc else f"Execute {name}"

        if not description:
            description = f"Execute {name}"

        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        })

    logger.info(f"Built {len(schemas)} native tool schemas")
    return schemas


# ---------------------------------------------------------------------------
# Native tool-call executor
# ---------------------------------------------------------------------------


def execute_native_tool_calls(
    tool_calls: List[Dict],
    available_tools: Dict[str, Callable],
    caller_label: str = "brain",
) -> List[Dict]:
    """Execute structured tool_calls from an LLM response (OpenAI format).

    Returns list of ``{"role": "tool", "tool_call_id": ..., "content": ...}``.
    """
    results: List[Dict] = []

    for tc in tool_calls:
        call_id = tc.get("id", f"call_{id(tc)}")
        func_info = tc.get("function", {})
        tool_name = func_info.get("name", "unknown")
        args_str = func_info.get("arguments", "{}")

        try:
            parameters = json.loads(args_str) if isinstance(args_str, str) else args_str
        except json.JSONDecodeError:
            results.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": f"Error: Invalid JSON in arguments for {tool_name}",
            })
            continue

        # Resolve tool (exact, then case-insensitive)
        if tool_name not in available_tools:
            matched = next(
                (k for k in available_tools if k.lower() == tool_name.lower()),
                None,
            )
            if matched:
                tool_name = matched
            else:
                results.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": f"Error: Unknown tool '{tool_name}'",
                })
                continue

        func = available_tools[tool_name]

        # Strip unknown kwargs
        mcp_info = getattr(func, "_mcp_tool_info", None)
        if mcp_info and hasattr(mcp_info, "input_schema") and mcp_info.input_schema:
            valid = set(mcp_info.input_schema.get("properties", {}).keys())
            if valid:
                parameters = {k: v for k, v in parameters.items() if k in valid}
        else:
            try:
                sig = inspect.signature(func)
                valid = set(sig.parameters.keys()) - {"self", "args", "kwargs"}
                has_var_kw = any(
                    p.kind == inspect.Parameter.VAR_KEYWORD
                    for p in sig.parameters.values()
                )
                if valid and not has_var_kw:
                    parameters = {k: v for k, v in parameters.items() if k in valid}
            except (ValueError, TypeError):
                pass

        try:
            result = func(**parameters)
            result_str = str(result)
            if len(result_str) > 3000:
                result_str = result_str[:3000] + "... [truncated]"
            logger.info(f"  [{caller_label}] {tool_name}() -> {len(result_str)} chars")
        except Exception as e:
            result_str = f"Error executing {tool_name}: {e}"
            logger.warning(f"  [{caller_label}] {tool_name}() -> {e}")

        results.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": result_str,
        })

    return results


# ---------------------------------------------------------------------------
# Intelligent tool context builder
# ---------------------------------------------------------------------------


def build_intelligent_tool_context(
    prompt: str,
    available_tools: Dict[str, Callable],
    *,
    map_sync_network: Any = None,
    mcp_client: Any = None,
    swarm_commander: Any = None,
) -> str:
    """Build context-aware tool information.

    Only includes relevant tools based on the prompt, with full parameter details.
    Extracted from ``BrainSystem._build_intelligent_tool_context`` (line 8152).
    """
    # Extract intent from prompt
    prompt_snippet = prompt[:200]
    intent = prompt_snippet[:prompt_snippet.index("?")] if "?" in prompt_snippet else prompt_snippet

    # Use MapSyncNetwork to find relevant tools
    relevant_tools: List[str] = []
    if map_sync_network:
        try:
            result = map_sync_network.query_capabilities(intent, limit=12)
            relevant_tools = [func["name"] for func in result]
            logger.debug(f"MapSync found {len(relevant_tools)} relevant tools")
        except Exception as e:
            logger.warning(f"MapSync query failed: {e}")

    if not relevant_tools:
        relevant_tools = [
            "search_knowledge", "brain_network_search", "recall_memory",
            "grokipedia_search", "web_search_results_only", "scrape_web_page",
            "knowledge_search", "read_file", "write_file", "grep_search",
            "create_self_autonomous_chain",
        ]

    # Append connected MCP tool names
    if mcp_client:
        try:
            mcp_status = mcp_client.get_status()
            for server_info in mcp_status.get("servers", {}).values():
                if server_info.get("connected"):
                    for tool_name in server_info.get("tool_names", [])[:5]:
                        if tool_name not in relevant_tools:
                            relevant_tools.append(tool_name)
        except Exception:
            pass

    # Build detailed tool descriptions with parameter signatures
    tool_descriptions: List[str] = []
    for tool_name in relevant_tools:
        if tool_name not in available_tools:
            continue
        func = available_tools[tool_name]
        try:
            sig = inspect.signature(func)
            params = []
            for pname, param in sig.parameters.items():
                if pname == "self":
                    continue
                ptype = (
                    param.annotation.__name__
                    if param.annotation != inspect.Parameter.empty and hasattr(param.annotation, "__name__")
                    else "any"
                )
                if param.default != inspect.Parameter.empty:
                    params.append(f"{pname}: {ptype} = {param.default}")
                else:
                    params.append(f"{pname}: {ptype}")
            param_str = ", ".join(params) if params else "no parameters"

            doc = inspect.getdoc(func)
            description = doc.split("\n")[0].strip()[:100] if doc else "No description available"
            tool_descriptions.append(f"• {tool_name}({param_str})\n  {description}")
        except Exception:
            tool_descriptions.append(f"• {tool_name}: Available")

    tools_info = (
        f"TOOLS ({len(tool_descriptions)} relevant of {len(available_tools)} total):\n"
        + "\n".join(tool_descriptions)
        + "\n\nTools are called natively via the API. Use them by name with appropriate parameters."
        "\nDiscover more: query_capabilities(\"what I need\") or get_system_map()"
    )

    # Append optional sections
    swarm_section = get_swarm_tools_for_prompt(swarm_commander)
    if swarm_section:
        tools_info += "\n" + swarm_section

    council_section = get_council_tools_for_prompt()
    if council_section:
        tools_info += "\n" + council_section

    mcp_section = get_mcp_tools_for_prompt(mcp_client, max_tools=8)
    if mcp_section:
        tools_info += "\n" + mcp_section

    return tools_info


# ---------------------------------------------------------------------------
# Prompt-section helpers (swarm, council, MCP)
# ---------------------------------------------------------------------------


def get_swarm_tools_for_prompt(swarm_commander: Any) -> str:
    """Compact swarm commander guidance for prompt injection."""
    if not swarm_commander:
        return ""
    try:
        overview = swarm_commander.get_swarm_overview()
        active_agents = overview.get("active_agents", 0)
        active_swarms = overview.get("active_swarms", 0)
        return (
            f"\nSWARM COMMANDER ({active_agents} agents, {active_swarms} swarms):\n"
            "• quick_research(question) — 3 agents research in parallel, return synthesis\n"
            "• quick_brainstorm(topic) — 5 agents brainstorm ideas\n"
            "• create_swarm(name, purpose, agent_count=5) — create agent team\n"
            "• broadcast_task(swarm_id, task) — send task to all agents\n"
            "• delegate_tasks(swarm_id, tasks=[...]) — distribute different tasks\n"
            '• start_discussion(topic, swarm_id, discussion_type="roundtable|brainstorm|debate|consensus")\n'
            "• dispatch_task(agent_id, task) — task one agent\n"
            "Agents use Gemini Flash (~$0.001/task). Create freely!"
        )
    except Exception:
        return ""


def get_council_tools_for_prompt() -> str:
    """Compact Commander Council guidance for prompt injection."""
    try:
        from repryntt.agents.council import get_council
        council = get_council()
        if not council:
            return ""
        return (
            "\n🏛️ COMMANDER COUNCIL (5 Gemini advisors on Nexus 8089):\n"
            "• council_advise(topic, context) — consult 5-member council, all posted to Nexus\n"
            "• council_post_report(task, results) — post swarm/task results to council board\n"
            "Use council_advise for strategic decisions, complex trade-offs, "
            "or when you need multiple expert perspectives."
        )
    except Exception:
        return ""


def get_mcp_tools_for_prompt(mcp_client: Any, max_tools: int = 8) -> str:
    """Compact MCP tools summary for injection into any prompt.

    Returns a short string listing connected MCP tools, or empty string.
    """
    try:
        if not mcp_client:
            return ""
        status = mcp_client.get_status()
        if status.get("total_tools", 0) == 0:
            return ""

        mcp_tools: List[tuple] = []
        for _name, server_info in status.get("servers", {}).items():
            if server_info.get("connected"):
                for tool_name in server_info.get("tool_names", []):
                    info = mcp_client.tool_registry.get(tool_name)
                    if info:
                        desc = info.description[:60] if info.description else ""
                        mcp_tools.append((tool_name, desc))

        if not mcp_tools:
            return ""

        lines = [f"  - {name}: {desc}" for name, desc in mcp_tools[:max_tools]]
        remaining = len(mcp_tools) - max_tools
        if remaining > 0:
            lines.append(f"  - ...and {remaining} more (use mcp_list_tools to see all)")

        return "\n🌐 MCP EXTERNAL TOOLS (browser, fetch, computer):\n" + "\n".join(lines)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Blockchain AI routing
# ---------------------------------------------------------------------------


def should_route_ai_through_blockchain(
    prompt: str,
    include_tools: bool,
    *,
    use_blockchain_ai: bool = False,
    robot_economy_manager: Any = None,
    blockchain_ai_percentage: int = 50,
) -> bool:
    """Decide whether to route AI workload through blockchain.

    Blockchain routing for complex/long tasks; local for conversational speed.
    """
    if not (use_blockchain_ai and robot_economy_manager):
        return False

    prompt_lower = prompt.lower()
    _CONVERSATIONAL = [
        "hello", "hi", "how are you", "what do you think", "tell me about",
        "explain", "describe", "what is", "how does", "why does",
        "conversation", "chat", "talk", "discuss", "respond to",
    ]
    if any(ind in prompt_lower for ind in _CONVERSATIONAL):
        return False

    _COMPLEX = [
        "analyze", "research", "investigate", "explore", "study",
        "create a", "write a", "generate", "design", "develop",
        "comprehensive", "detailed", "thorough", "extensive",
        "paper", "report", "documentation", "implementation",
    ]
    has_complexity = any(ind in prompt_lower for ind in _COMPLEX)
    is_long = len(prompt) > 500

    should = has_complexity or is_long or include_tools
    if should:
        should = random.random() * 100 < blockchain_ai_percentage

    return should


def call_ai_via_blockchain(
    prompt: str,
    *,
    ai_params: Dict[str, Any],
    robot_economy_manager: Any,
    get_ai_wallet_fn: Callable,
    timeout: int = 120,
) -> str:
    """Route AI call through blockchain as a workload.

    Creates the circular economy: AI thinking → mining → reward → result.
    Uses safe_serialize for socket messages (no pickle).
    """
    try:
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "robot_economy"))
        from safe_serialize import pack as safe_pack, unpack as safe_unpack
    except ImportError:
        return "BLOCKCHAIN_ERROR: safe_serialize not available"

    try:
        ai_wallet = get_ai_wallet_fn()
        if not ai_wallet:
            return "BLOCKCHAIN_ERROR: No AI wallet available"

        node_host = "127.0.0.1"
        node_port = 5001
        if robot_economy_manager and hasattr(robot_economy_manager, "config"):
            node_port = robot_economy_manager.config.get("node_port", 5001)

        logger.info(f"Connecting to blockchain node at {node_host}:{node_port}")
        fee_plancks = 100_000_000  # 1 Credit per AI call

        # Submit workload
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect((node_host, node_port))
            submit_msg = {
                "type": "submit_ai_inference",
                "requester_address": ai_wallet,
                "prompt": prompt,
                "max_tokens": ai_params["max_tokens"],
                "temperature": ai_params["temperature"],
                "fee_plancks": fee_plancks,
            }
            data = safe_pack(submit_msg)
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
            return f"BLOCKCHAIN_ERROR: {submit_result.get('error', 'Unknown error')}"

        workload_key = submit_result.get("workload_key")
        logger.info(f"Submitted blockchain AI workload: {workload_key[:16]}... (fee: 1 CR)")

        # Poll for result
        poll_interval = 2
        start_time = time.time()
        while time.time() - start_time < timeout:
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
                        return ai_result.get("text", "")
                    return str(ai_result)
                time.sleep(poll_interval)
            except socket.timeout:
                time.sleep(poll_interval)
            except Exception as poll_err:
                logger.warning(f"Poll attempt failed: {poll_err}")
                time.sleep(poll_interval)

        return f"BLOCKCHAIN_ERROR: Timeout waiting for workload completion ({timeout}s)"
    except socket.timeout:
        return f"BLOCKCHAIN_ERROR: Timeout after {timeout}s"
    except Exception as e:
        logger.error(f"Blockchain AI error: {e}", exc_info=True)
        return f"BLOCKCHAIN_ERROR: {e}"


# ---------------------------------------------------------------------------
# Novelty detection
# ---------------------------------------------------------------------------

# Sliding window of recent content hashes (module-level state)
_recent_topics_hash: List[str] = []


def detect_novelty(prompt: str, response: str) -> str:
    """Detect whether a self-autonomous exchange was novel or repetitive.

    Returns ``'novel'``, ``'repetitive'``, or ``'neutral'``.
    Extracted from ``BrainSystem._detect_novelty`` (line 2295).
    """
    global _recent_topics_hash

    content = response.lower()
    _FILLER = [
        "as the meta-awareness layer", "it is important to", "we should consider",
        "moving forward", "it is advisable", "continuous improvement",
        "evolving landscape", "remains crucial", "it would be prudent",
    ]
    for phrase in _FILLER:
        content = content.replace(phrase, "")

    content_hash = hashlib.md5(content[:200].encode()).hexdigest()[:8]

    if content_hash in _recent_topics_hash:
        return "repetitive"

    tool_markers = [
        "TOOL_CALL", "tool_name", "grokipedia_search", "web_search",
        "search_knowledge", "brain_network_search", "scrape_web_page",
    ]
    has_tool_use = any(m in response for m in tool_markers)

    specific_markers = [
        "because", "specifically", "for example", "data shows",
        "according to", "percent", "%", "million", "billion",
        "in 2023", "in 2024", "in 2025", "study found",
    ]
    specificity = sum(1 for m in specific_markers if m in response.lower())

    _recent_topics_hash.append(content_hash)
    if len(_recent_topics_hash) > 50:
        _recent_topics_hash = _recent_topics_hash[-50:]

    if has_tool_use or specificity >= 2:
        return "novel"

    return "neutral"


# ---------------------------------------------------------------------------
# High-level AI service call (the main entry point used by BrainSystem)
# ---------------------------------------------------------------------------


def call_ai_service(
    prompt: str,
    *,
    config: Dict[str, Any],
    available_tools: Optional[Dict[str, Callable]] = None,
    hormone_system: Any = None,
    consciousness: Any = None,
    conversation_logger: Any = None,
    map_sync_network: Any = None,
    mcp_client: Any = None,
    swarm_commander: Any = None,
    robot_economy_manager: Any = None,
    get_ai_wallet_fn: Optional[Callable] = None,
    use_blockchain_ai: bool = False,
    blockchain_ai_percentage: int = 50,
    priority: int = 0,
    timeout: int = 120,
    include_tools: bool = True,
) -> str:
    """High-level AI call: build context, submit through queue, run tool loop.

    This is the extracted version of ``BrainSystem._call_ai_service``.
    All optional subsystems (hormones, consciousness, logger, blockchain,
    robot-economy credits) degrade gracefully when absent.
    """

    # ─── Phase 2: Intelligent blockchain routing ──────────
    if should_route_ai_through_blockchain(
        prompt,
        include_tools,
        use_blockchain_ai=use_blockchain_ai,
        robot_economy_manager=robot_economy_manager,
        blockchain_ai_percentage=blockchain_ai_percentage,
    ):
        try:
            logger.info("Routing complex AI workload through blockchain")
            ai_params_bc = _get_ai_parameters(hormone_system)
            bc_result = call_ai_via_blockchain(
                prompt,
                ai_params=ai_params_bc,
                robot_economy_manager=robot_economy_manager,
                get_ai_wallet_fn=get_ai_wallet_fn or (lambda: None),
                timeout=timeout,
            )
            if bc_result and not bc_result.startswith("BLOCKCHAIN_ERROR:"):
                return bc_result
            logger.warning(f"Blockchain AI failed, falling back to direct call: {bc_result}")
        except Exception as e:
            logger.error(f"Blockchain AI error, falling back: {e}")

    # ─── Credit system check (non-blocking) ───────────────
    if robot_economy_manager and get_ai_wallet_fn:
        try:
            ai_wallet = get_ai_wallet_fn()
            bal = robot_economy_manager.get_wallet_balance(ai_wallet)
            if bal.get("success"):
                current = bal.get("balance_credits", 0)
                estimated = max(0.01, len(prompt) / 10000)
                if current >= estimated:
                    logger.info(f"AI Call — est {estimated:.4f} CR, balance {current:.4f} CR")
                else:
                    logger.warning(f"Low credits: need {estimated:.4f} CR, have {current:.4f} CR")
        except Exception as e:
            logger.warning(f"Credit check failed, proceeding: {e}")

    ai_params = _get_ai_parameters(hormone_system)

    # Contextual preamble
    consciousness_context = ""
    if consciousness:
        try:
            consciousness_context = consciousness.get_reasoning_context(limit=3)
        except Exception:
            pass

    time_context = _get_time_context()
    hormone_context = _get_hormone_context(hormone_system)

    if include_tools and available_tools:
        system_content = f"{time_context}\n{hormone_context}"
        if consciousness_context:
            system_content += f"\n{consciousness_context}"
        system_content += (
            "\nYou have access to tools via function calling. Use them "
            "to gather real data before answering. Call tools as needed — "
            "you can call multiple tools in sequence. When you have enough "
            "information, provide your final answer as plain text."
        )
        native_messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": prompt},
        ]
        native_tools = build_native_tool_schemas(
            available_tools, prompt, map_sync_network=map_sync_network
        )
        enhanced_prompt = prompt
    else:
        native_messages = None
        native_tools = None
        parts = [time_context, hormone_context]
        if consciousness_context:
            parts.append(consciousness_context)
        parts.append(prompt)
        enhanced_prompt = "\n".join(p for p in parts if p)

    def _make_call(msgs=None, tls=None):
        return route_ai_call(config, enhanced_prompt, ai_params, messages=msgs, tools=tls)

    try:
        logger.info(
            f"AI Call ({len(enhanced_prompt)} chars, "
            f"{'tools' if native_tools else 'no tools'}): "
            f"{enhanced_prompt[:150]}..."
        )

        response = master_ai_queue.submit_request(
            lambda: _make_call(native_messages, native_tools),
            priority=priority,
            timeout=timeout,
        )

        if response.status_code != 200:
            error_msg = f"AI service returned {response.status_code}: {response.text}"
            logger.error(error_msg)
            if hormone_system:
                try:
                    hormone_system.process_event("error_encountered", {"magnitude": 0.5})
                except Exception:
                    pass
            return f"AI_SERVICE_ERROR: {error_msg}"

        result = response.json()
        msg = result.get("choices", [{}])[0].get("message", {})
        ai_response = (msg.get("content") or "").strip()
        tool_calls_from_api = msg.get("tool_calls", [])
        tools_used: List[str] = []

        # ─── Native tool loop ─────────────────────────────────
        if include_tools and native_messages and tool_calls_from_api and available_tools:
            MAX_ROUNDS = 10
            loop_messages = list(native_messages)

            for tool_round in range(1, MAX_ROUNDS + 1):
                if not tool_calls_from_api:
                    break
                logger.info(
                    f"Tool round {tool_round}: {len(tool_calls_from_api)} call(s)"
                )

                assistant_msg: Dict[str, Any] = {"role": "assistant"}
                if ai_response:
                    assistant_msg["content"] = ai_response
                assistant_msg["tool_calls"] = tool_calls_from_api
                loop_messages.append(assistant_msg)

                tool_results = execute_native_tool_calls(
                    tool_calls_from_api, available_tools, caller_label="brain"
                )
                tools_used.extend(
                    tc.get("function", {}).get("name", "?")
                    for tc in tool_calls_from_api
                )
                loop_messages.extend(tool_results)

                # Context compaction if growing large
                try:
                    from repryntt.core.memory.context_compaction import (
                        ContextCompactor, estimate_messages_tokens,
                    )
                    tok = estimate_messages_tokens(loop_messages)
                    if tok > 3000:
                        compactor = ContextCompactor(context_window=4096)
                        kept, summary = compactor.compact_messages(loop_messages[1:])
                        if summary and len(kept) < len(loop_messages) - 1:
                            loop_messages = [loop_messages[0]]
                            if summary != "No prior history.":
                                loop_messages.append({
                                    "role": "system",
                                    "content": f"[Context summary]: {summary}",
                                })
                            loop_messages.extend(kept)
                except Exception:
                    pass

                follow_resp = master_ai_queue.submit_request(
                    lambda: _make_call(loop_messages, native_tools),
                    priority=priority,
                    timeout=timeout,
                )
                if follow_resp.status_code != 200:
                    logger.warning(f"Tool follow-up failed: {follow_resp.status_code}")
                    break

                follow = follow_resp.json()
                follow_msg = follow.get("choices", [{}])[0].get("message", {})
                ai_response = (follow_msg.get("content") or "").strip()
                tool_calls_from_api = follow_msg.get("tool_calls", [])

            if tools_used:
                logger.info(
                    f"Tool loop complete: {len(tools_used)} tools across "
                    f"{tool_round} round(s)"
                )

        # ─── Post-call credit charging ────────────────────────
        if robot_economy_manager and get_ai_wallet_fn and ai_response:
            try:
                usage = result.get("usage", {})
                total_tokens = usage.get("total_tokens", 0)
                actual_cost = max(0.01, total_tokens / 100000)
                ai_wallet = get_ai_wallet_fn()
                charge = robot_economy_manager.charge_wallet(
                    ai_wallet, actual_cost, f"AI call ({total_tokens} tokens)"
                )
                if charge.get("success"):
                    logger.info(f"Charged {actual_cost:.4f} CR for {total_tokens} tokens")
                else:
                    logger.warning(f"Failed to charge credits: {charge.get('error')}")
            except Exception as e:
                logger.warning(f"Credit charging failed: {e}")

        # ─── Logging & hormone events ────────────────────────
        if conversation_logger:
            try:
                conversation_logger.log_exchange(
                    prompt=enhanced_prompt,
                    response=ai_response,
                    tools_included=include_tools,
                    metadata={
                        "priority": priority,
                        "timeout": timeout,
                        "timestamp": time.time(),
                        "native_tools_used": tools_used,
                    },
                )
            except Exception:
                pass

        if consciousness:
            try:
                consciousness.track_ai_call(
                    prompt=prompt,
                    response=ai_response,
                    tools_used=tools_used,
                    context="self_autonomous_call",
                )
            except Exception:
                pass

        if hormone_system and ai_response:
            _fire_hormone_events(hormone_system, prompt, ai_response, tools_used)

        logger.info(
            f"AI Response ({len(ai_response)} chars): {ai_response[:100]}..."
        )
        return ai_response

    except Exception as e:
        logger.error(f"AI service call failed: {e}")
        if hormone_system:
            try:
                hormone_system.process_event("error_encountered", {"magnitude": 0.7})
            except Exception:
                pass
        return f"AI_SERVICE_ERROR: {e}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_ai_parameters(hormone_system: Any) -> Dict[str, Any]:
    """Get sampling params, optionally hormone-modulated."""
    base = {
        "max_tokens": 800,
        "temperature": 0.8,
        "top_p": 0.9,
        "frequency_penalty": 0.3,
    }
    if not hormone_system:
        return base
    try:
        return hormone_system.get_sampling_parameters()
    except Exception:
        return base


def _get_time_context() -> str:
    """Compact real-time awareness string."""
    try:
        from datetime import datetime
        now = datetime.now()
        return (
            f"CURRENT TIME: {now.strftime('%Y-%m-%d %H:%M %A')} "
            f"(model trained on data up to 2023)"
        )
    except Exception:
        return ""


def _get_hormone_context(hormone_system: Any) -> str:
    """Compact hormone-state summary for prompt injection."""
    if not hormone_system:
        return ""
    try:
        emotions = hormone_system.get_emotional_state()
        top = sorted(emotions.items(), key=lambda x: -x[1])[:3]
        emo_str = ", ".join(f"{e}:{v:.2f}" for e, v in top if v > 0.1)

        dominant, level = hormone_system.get_dominant_circuit()
        modifiers = hormone_system.get_behavior_modifiers()

        notable = []
        if modifiers.get("exploration_drive", 0.5) > 0.6:
            notable.append("HIGH exploration")
        if modifiers.get("urgency", 0.3) > 0.5:
            notable.append("URGENT")
        if modifiers.get("creative_drive", 0.5) > 0.65:
            notable.append("creative mode")
        mod_str = f" | {', '.join(notable)}" if notable else ""

        return (
            f"INTERNAL STATE: {dominant} circuit ({level:.2f}) | "
            f"Emotions: {emo_str}{mod_str}"
        )
    except Exception:
        return ""


def _fire_hormone_events(
    hormone_system: Any,
    prompt: str,
    response: str,
    tools_used: List[str],
) -> None:
    """Post-call hormone events using proper novelty detection."""
    try:
        novelty = detect_novelty(prompt, response)

        if novelty == "novel":
            hormone_system.process_event("novel_topic", {
                "topic": prompt[:60].strip().replace("\n", " "),
                "magnitude": 0.8,
            })
        elif novelty == "repetitive":
            hormone_system.process_event("repetitive_task", {
                "magnitude": 0.6,
            })

        if tools_used:
            hormone_system.process_event("tool_success", {
                "topic": tools_used[0],
                "magnitude": 0.7,
            })

        # Detect creative insight (structured JSON with new ideas)
        if (
            "{" in response
            and any(k in response for k in ['"prompt"', '"exploration_goal"', '"expected_insight"'])
            and novelty != "repetitive"
        ):
            hormone_system.process_event("creative_insight", {"magnitude": 0.5})
    except Exception:
        pass
