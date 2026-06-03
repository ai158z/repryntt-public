#!/usr/bin/env python3
"""
repryntt MCP Server — Model Context Protocol bridge.

Exposes repryntt's core tools and APIs as MCP tools so Claude Desktop,
VS Code Copilot, Cursor, Windsurf, and other MCP-compatible clients can
interact with the repryntt system natively.

Usage:
    # Start the MCP server (stdio transport)
    python -m repryntt.mcp_server

    # Or directly
    python repryntt/mcp_server.py

Configure in your MCP client (e.g., Claude Desktop settings.json):
    {
        "mcpServers": {
            "repryntt": {
                "command": "python",
                "args": ["-m", "repryntt.mcp_server"],
                "env": {
                    "REPRYNTT_HOST": "http://localhost:8089",
                    "REPRYNTT_API_KEY": "your-api-key",
                    "REPRYNTT_WALLET": "your-wallet-address"
                }
            }
        }
    }

To get an API key and wallet, use the repryntt_register tool first (free),
then repryntt_faucet for 1,000 startup Credits.  CR is market-priced
(no fixed peg) — deposit SOL via the Solana bridge and buy CR on the order book.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error

# -------------------------------------------------------------------
# MCP protocol over stdio (JSON-RPC 2.0)
# Minimal implementation — no external dependencies required.
# -------------------------------------------------------------------

REPRYNTT_HOST = os.environ.get("REPRYNTT_HOST", "http://localhost:8089")
from repryntt.paths import get_data_dir as _get_data_dir
AUTH_TOKEN_PATH = str(_get_data_dir() / "auth_token")

# Credit-gated auth — set via environment or auto-registered on first use
REPRYNTT_API_KEY = os.environ.get("REPRYNTT_API_KEY", "")
REPRYNTT_WALLET = os.environ.get("REPRYNTT_WALLET", "")


def _get_auth_token() -> str | None:
    try:
        with open(AUTH_TOKEN_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def _api_call(method: str, path: str, body: dict | None = None,
              *, credit_gated: bool = False) -> dict:
    """Make an HTTP request to the repryntt Nexus API.

    Args:
        credit_gated: If True, include X-API-Key and wallet headers for
                      /ext-api/ endpoints that charge Credits.
    """
    url = f"{REPRYNTT_HOST}{path}"
    headers = {"Content-Type": "application/json"}


    token = _get_auth_token()
    if token:
        headers["X-Auth-Key"] = token

    if credit_gated and REPRYNTT_API_KEY:
        headers["X-API-Key"] = REPRYNTT_API_KEY
        headers["X-Wallet-Signature"] = "mcp"
        headers["X-Signature-Message"] = "mcp-tool-call"

    # Inject wallet_address into body for credit-gated calls
    if credit_gated and REPRYNTT_WALLET and body is not None:
        body.setdefault("wallet_address", REPRYNTT_WALLET)
    elif credit_gated and REPRYNTT_WALLET and body is None:
        body = {"wallet_address": REPRYNTT_WALLET}

    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode() if e.fp else ""
        if e.code == 402:
            return {"error": "Insufficient Credits. Fund your wallet via /gateway/deposit or /ext-api/wallet/faucet."}
        if e.code == 401:
            return {"error": "Missing or invalid API key. Set REPRYNTT_API_KEY env var or register at /ext-api/auth/register."}
        return {"error": f"HTTP {e.code}: {body_text[:500]}"}
    except urllib.error.URLError as e:
        return {"error": f"Connection failed: {e.reason}. Is repryntt running?"}
    except Exception as e:
        return {"error": str(e)}


# -------------------------------------------------------------------
# Tool definitions
# -------------------------------------------------------------------

TOOLS = [
    {
        "name": "repryntt_status",
        "description": "Get repryntt system status — agents, services, blockchain, uptime. (Free)",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "repryntt_register",
        "description": "Register for an API key and wallet to use credit-gated tools. Free — only needs to be done once.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Display name for your account"},
            },
        },
    },
    {
        "name": "repryntt_chat",
        "description": "Send a message to Artemis (the AI agent) and get a response. Costs 0.02 CR per 1k tokens.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Message to send to Artemis"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "repryntt_tool_list",
        "description": "List all 240+ registered tools with names and categories. (Free)",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "repryntt_tool_call",
        "description": "Execute any registered repryntt tool by name with parameters. Costs 0.05 CR per call.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string", "description": "Name of the tool to execute"},
                "params": {"type": "object", "description": "Tool parameters (varies by tool)"},
            },
            "required": ["tool_name"],
        },
    },
    {
        "name": "repryntt_analyze",
        "description": "Run AI analysis on data or a question. Costs 0.10 CR per request.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "data": {"type": "string", "description": "Data or question to analyze"},
            },
            "required": ["data"],
        },
    },
    {
        "name": "repryntt_gateway_status",
        "description": "Get Solana bridge status — deposit address, stats, bridge balances. (Free)",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "repryntt_gateway_deposit",
        "description": "Create a SOL/USDC deposit via the Solana bridge. Funds go to your bridge balance, then use the order book to buy CR at market price. (Free — you're adding funds)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repryntt_address": {
                    "type": "string",
                    "description": "Your repryntt wallet address (hex format)",
                },
            },
            "required": ["repryntt_address"],
        },
    },
    {
        "name": "repryntt_blockchain_health",
        "description": "Get blockchain node health — chain height, peers, mining status. (Free)",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "repryntt_trading_portfolio",
        "description": "Get trading portfolio summary — positions, P&L, balances. (Free read-only)",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "repryntt_trading_signals",
        "description": "Get active trading signals and their scores. (Free read-only)",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "repryntt_agents_list",
        "description": "List all available agents across 33 departments (230 roles). (Free)",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "repryntt_spawn_agent",
        "description": "Spawn an agent from a specific department to execute a task. Costs 0.05 CR (tool call).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "department": {"type": "string", "description": "Department name (e.g., research, trading)"},
                "task": {"type": "string", "description": "Task description for the agent"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "repryntt_compute_stats",
        "description": "Get decentralized compute marketplace stats — providers, pricing, capacity. (Free)",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "repryntt_workload_submit",
        "description": "Submit an AI workload (inference, batch, embedding, analysis) for async processing. Node's LLM does the work, you pay CR. Returns job_id for polling.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workload_type": {
                    "type": "string",
                    "description": "inference | batch | embedding | analysis",
                    "enum": ["inference", "batch", "embedding", "analysis"],
                },
                "payload": {
                    "type": "object",
                    "description": "Workload payload. inference: {prompt, max_tokens}. batch: {prompts: [...]}. embedding: {texts: [...]}. analysis: {query}.",
                },
                "max_price_cr": {
                    "type": "number",
                    "description": "Max CR you're willing to pay (0 = no limit)",
                },
            },
            "required": ["workload_type", "payload"],
        },
    },
    {
        "name": "repryntt_workload_status",
        "description": "Check the status/result of a submitted workload by job_id. Poll until status is 'completed'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job ID returned from repryntt_workload_submit"},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "repryntt_workload_list",
        "description": "List your submitted workloads. Optionally filter by status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter: pending, processing, completed, failed, cancelled"},
                "limit": {"type": "integer", "description": "Max results (default 20)"},
            },
        },
    },
    {
        "name": "repryntt_workload_cancel",
        "description": "Cancel a pending workload and get a full CR refund.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job ID to cancel"},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "repryntt_node_config",
        "description": "Get this node's workload pricing, capabilities, and queue stats. (Free)",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "repryntt_wallet_balance",
        "description": "Check your Credit (CR) balance. (Free)",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "repryntt_faucet",
        "description": "Get 1,000 free startup Credits for your wallet. (Free — one time per wallet)",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _handle_tool_call(name: str, arguments: dict) -> str:
    """Route a tool call to the appropriate API endpoint."""
    # --- Free endpoints ---
    if name == "repryntt_status":
        result = _api_call("GET", "/api/daemon/status")
    elif name == "repryntt_register":
        display = arguments.get("name", "mcp-client")
        result = _api_call("POST", "/ext-api/auth/register", {"name": display})
        if result.get("success"):
            result["next_steps"] = (
                "Set REPRYNTT_API_KEY and REPRYNTT_WALLET env vars, "
                "then call repryntt_faucet to get 1,000 free startup Credits."
            )
    elif name == "repryntt_tool_list":
        result = _api_call("GET", "/tool-api/tools")
    elif name == "repryntt_gateway_status":
        result = _api_call("GET", "/gateway/status")
    elif name == "repryntt_gateway_deposit":
        result = _api_call("POST", "/gateway/deposit", {
            "repryntt_address": arguments["repryntt_address"],
        })
    elif name == "repryntt_blockchain_health":
        result = _api_call("GET", "/ext-api/health")
    elif name == "repryntt_trading_portfolio":
        result = _api_call("GET", "/api/trading/portfolio")
    elif name == "repryntt_trading_signals":
        result = _api_call("GET", "/api/trading/signals")
    elif name == "repryntt_agents_list":
        result = _api_call("GET", "/api/daemon/agents")
    elif name == "repryntt_compute_stats":
        result = _api_call("GET", "/api/p2p/status")
    elif name == "repryntt_workload_submit":
        result = _api_call("POST", "/ext-api/workloads/submit", {
            "workload_type": arguments["workload_type"],
            "payload": arguments["payload"],
            "max_price_cr": arguments.get("max_price_cr", 0),
        }, credit_gated=True)
    elif name == "repryntt_workload_status":
        result = _api_call("GET", f"/ext-api/workloads/{arguments['job_id']}",
                           credit_gated=True)
    elif name == "repryntt_workload_list":
        qs = ""
        if arguments.get("status"):
            qs += f"?status={arguments['status']}"
        if arguments.get("limit"):
            sep = "&" if qs else "?"
            qs += f"{sep}limit={arguments['limit']}"
        result = _api_call("GET", f"/ext-api/workloads{qs}", credit_gated=True)
    elif name == "repryntt_workload_cancel":
        result = _api_call("POST", f"/ext-api/workloads/{arguments['job_id']}/cancel",
                           {}, credit_gated=True)
    elif name == "repryntt_node_config":
        result = _api_call("GET", "/ext-api/node/config", credit_gated=True)
    elif name == "repryntt_wallet_balance":
        if REPRYNTT_WALLET:
            result = _api_call("GET", f"/ext-api/wallet/{REPRYNTT_WALLET}",
                               credit_gated=True)
        else:
            result = {"error": "Set REPRYNTT_WALLET env var first."}
    elif name == "repryntt_faucet":
        result = _api_call("POST", "/ext-api/wallet/faucet", {},
                           credit_gated=True)

    # --- Credit-gated endpoints ---
    elif name == "repryntt_chat":
        result = _api_call("POST", "/ext-api/ai/chat",
                           {"message": arguments["message"]},
                           credit_gated=True)
    elif name == "repryntt_tool_call":
        result = _api_call("POST", "/ext-api/ai/tool", {
            "tool_name": arguments["tool_name"],
            "parameters": arguments.get("params", {}),
        }, credit_gated=True)
    elif name == "repryntt_analyze":
        result = _api_call("POST", "/ext-api/ai/analyze",
                           {"data": arguments["data"]},
                           credit_gated=True)
    elif name == "repryntt_spawn_agent":
        result = _api_call("POST", "/ext-api/ai/tool", {
            "tool_name": "dispatch_task",
            "parameters": {
                "department": arguments.get("department", "research"),
                "task": arguments["task"],
            },
        }, credit_gated=True)
    else:
        result = {"error": f"Unknown tool: {name}"}
    return json.dumps(result, indent=2)


# -------------------------------------------------------------------
# JSON-RPC 2.0 over stdio
# -------------------------------------------------------------------

def _send(msg: dict) -> None:
    """Write a JSON-RPC message to stdout."""
    raw = json.dumps(msg)
    sys.stdout.write(raw + "\n")
    sys.stdout.flush()


def _handle_request(req: dict) -> dict | None:
    """Process a JSON-RPC request and return a response."""
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "repryntt",
                    "version": "0.5.0",
                },
            },
        }

    if method == "notifications/initialized":
        return None  # No response needed for notifications

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS},
        }

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        try:
            text = _handle_tool_call(tool_name, arguments)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                },
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {e}"}],
                    "isError": True,
                },
            }

    # Unknown method
    if req_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


def main():
    """Run the MCP server on stdio."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle_request(req)
        if resp is not None:
            _send(resp)


if __name__ == "__main__":
    main()
