#!/usr/bin/env python3
"""
SAIGE MCP Client — The Bridge to the Digital World

Connects SAIGE to the MCP (Model Context Protocol) ecosystem, allowing
autonomous discovery and use of external tools from any MCP server.

ARCHITECTURE:
    ┌─────────────────────────────────────┐
    │           SAIGE BrainSystem          │
    │  available_tools["mcp_github_..."]  │
    │  available_tools["mcp_browser_..."] │
    └──────────────┬──────────────────────┘
                   │ calls proxy function
    ┌──────────────▼──────────────────────┐
    │         MCPClientManager             │
    │  - manages persistent connections    │
    │  - translates SAIGE ↔ MCP protocol  │
    │  - auto-discovers tools              │
    │  - handles reconnection              │
    └──────────────┬──────────────────────┘
                   │ JSON-RPC 2.0
    ┌──────────────▼──────────────────────┐
    │  External MCP Servers (stdio/SSE)    │
    │  - Playwright (browser control)      │
    │  - GitHub, Slack, Discord, etc.      │
    │  - Any MCP-compatible server         │
    └─────────────────────────────────────┘

USAGE:
    from repryntt.routing.mcp_client import MCPClientManager
    
    manager = MCPClientManager(config_path="brain/mcp_servers.json")
    await manager.connect_all()
    tools = manager.get_tool_registry()  # Dict[str, callable] for BrainSystem
    brain.available_tools.update(tools)
"""

import asyncio
import json
import logging
import os
import time
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from concurrent.futures import Future

from repryntt.paths import get_data_dir

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP SDK imports — graceful fallback if not installed
# ---------------------------------------------------------------------------
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.types import Tool as MCPTool, CallToolResult, TextContent
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    logger.warning("⚠️ MCP SDK not installed — run: pip install mcp")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class MCPServerConfig:
    """Configuration for a single MCP server connection."""
    name: str                           # Human-readable name (e.g., "github")
    command: str                        # Command to start the server
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    enabled: bool = True
    tool_prefix: str = ""               # Prefix for tool names (e.g., "mcp_github_")
    description: str = ""
    auto_connect: bool = True           # Connect on startup
    timeout_seconds: float = 30.0       # Connection timeout
    retry_attempts: int = 3
    retry_delay: float = 5.0


@dataclass
class MCPToolInfo:
    """Cached information about a discovered MCP tool."""
    server_name: str
    original_name: str          # Name on the MCP server
    saige_name: str             # Name registered in SAIGE (prefixed)
    description: str
    input_schema: Dict[str, Any]
    category: str = "MCP_EXTERNAL"


@dataclass
class MCPServerConnection:
    """Tracks the state of a connection to an MCP server."""
    config: MCPServerConfig
    session: Optional[Any] = None       # ClientSession
    tools: List[MCPToolInfo] = field(default_factory=list)
    connected: bool = False
    last_connected: float = 0.0
    last_error: str = ""
    connection_attempts: int = 0
    _context_manager: Optional[Any] = None  # For cleanup


# ---------------------------------------------------------------------------
# Main MCP Client Manager
# ---------------------------------------------------------------------------
class MCPClientManager:
    """
    Manages connections to multiple MCP servers and exposes their tools
    to SAIGE's BrainSystem as regular callable functions.
    
    Thread-safe: All MCP calls are routed through a dedicated asyncio
    event loop running in a background thread, so synchronous SAIGE code
    can call MCP tools without blocking.
    """

    def __init__(self, config_path: str = None, base_dir: str = None):
        self.base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
        self.config_path = config_path or os.path.join(self.base_dir, "mcp_servers.json")
        
        # Server connections
        self.servers: Dict[str, MCPServerConnection] = {}
        
        # Tool registry: saige_name → MCPToolInfo
        self.tool_registry: Dict[str, MCPToolInfo] = {}
        
        # Async event loop in background thread
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()  # Signals when event loop is running
        self._started = False
        
        # Connection state tracking
        self._reconnect_task: Optional[asyncio.Task] = None
        
        logger.info("🌐 MCP Client Manager initialized")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        """Start the MCP client manager — spins up async loop and connects to servers."""
        if not MCP_AVAILABLE:
            logger.error("❌ MCP SDK not available — cannot start MCP client")
            return False
        
        if self._started:
            logger.debug("MCP client already started")
            return True
        
        # Load server configs
        configs = self._load_configs()
        if not configs:
            logger.info("📋 No MCP servers configured — creating default config")
            self._create_default_config()
            configs = self._load_configs()
        
        # Start the async event loop in a background thread
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_event_loop,
            name="mcp-client-loop",
            daemon=True
        )
        self._thread.start()
        
        # Wait for event loop to be running before submitting work
        if not self._loop_ready.wait(timeout=5):
            logger.error("MCP event loop failed to start within 5 seconds")
            return False
        
        # Connect to all enabled servers
        auto_connect = [c for c in configs if c.enabled and c.auto_connect]
        if auto_connect:
            future = self._submit_async(self._connect_servers(auto_connect))
            try:
                results = future.result(timeout=60)
                connected = sum(1 for r in results if r)
                logger.info(f"🌐 MCP: Connected to {connected}/{len(auto_connect)} servers")
            except Exception as e:
                logger.error(f"❌ MCP startup connection error: {e}")
        
        self._started = True
        
        # Start background reconnection monitor
        self._submit_async(self._reconnection_loop())
        
        return True

    def stop(self):
        """Gracefully shut down all MCP connections."""
        if not self._started:
            return
        
        logger.info("🛑 Shutting down MCP client connections...")
        
        try:
            future = self._submit_async(self._disconnect_all())
            future.result(timeout=10)
        except Exception as e:
            logger.error(f"Error during MCP shutdown: {e}")
        
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        
        self._started = False
        logger.info("🛑 MCP client shut down")

    # ------------------------------------------------------------------
    # Tool Registry — what BrainSystem consumes
    # ------------------------------------------------------------------
    def get_tool_registry(self) -> Dict[str, callable]:
        """
        Returns a dict of {tool_name: callable} ready to be merged into
        brain.available_tools. Each callable is a sync wrapper that routes
        to the MCP server's async call_tool.
        """
        registry = {}
        for saige_name, info in self.tool_registry.items():
            registry[saige_name] = self._make_tool_proxy(info)
        return registry

    def get_tool_categories(self) -> Dict[str, Any]:
        """
        Returns MCP tools organized by server for the tool discovery system.
        Format matches TOOL_CATEGORIES in tool_discovery_system.py.
        """
        categories = {}
        
        for server_name, conn in self.servers.items():
            if not conn.connected or not conn.tools:
                continue
            
            cat_key = f"MCP_{server_name.upper()}"
            categories[cat_key] = {
                "emoji": "🌐",
                "description": f"External tools from {server_name} MCP server: {conn.config.description}",
                "tools": [t.saige_name for t in conn.tools],
                "use_when": f"Need to use {server_name} capabilities via MCP"
            }
        
        return categories

    def get_tool_details(self) -> Dict[str, Dict[str, Any]]:
        """
        Returns per-tool detail dicts for tool_discovery_system TOOL_DETAILS.
        """
        details = {}
        for saige_name, info in self.tool_registry.items():
            # Convert JSON Schema parameters to readable format
            params = {}
            schema_props = info.input_schema.get("properties", {})
            required = info.input_schema.get("required", [])
            
            for param_name, param_schema in schema_props.items():
                param_type = param_schema.get("type", "any")
                param_desc = param_schema.get("description", "")
                is_required = param_name in required
                params[param_name] = f"({'required' if is_required else 'optional'}, {param_type}) {param_desc}"
            
            details[saige_name] = {
                "description": info.description or f"MCP tool from {info.server_name}",
                "parameters": params,
                "example": f'Call {saige_name} with appropriate parameters',
                "source": f"MCP server: {info.server_name}",
                "chain_next": []
            }
        
        return details

    def get_status(self) -> Dict[str, Any]:
        """Get status of all MCP connections for monitoring."""
        return {
            "started": self._started,
            "mcp_available": MCP_AVAILABLE,
            "servers": {
                name: {
                    "connected": conn.connected,
                    "tools_count": len(conn.tools),
                    "tool_names": [t.saige_name for t in conn.tools],
                    "last_connected": conn.last_connected,
                    "last_error": conn.last_error,
                    "attempts": conn.connection_attempts
                }
                for name, conn in self.servers.items()
            },
            "total_tools": len(self.tool_registry),
            "total_connected": sum(1 for c in self.servers.values() if c.connected)
        }

    # ------------------------------------------------------------------
    # Connection Management (async internals)
    # ------------------------------------------------------------------
    async def _connect_servers(self, configs: List[MCPServerConfig]) -> List[bool]:
        """Connect to multiple servers (called from async context)."""
        results = []
        for config in configs:
            success = await self._connect_server(config)
            results.append(success)
        return results

    async def _connect_server(self, config: MCPServerConfig) -> bool:
        """Connect to a single MCP server and discover its tools."""
        server_name = config.name
        
        for attempt in range(1, config.retry_attempts + 1):
            try:
                logger.info(f"🔌 MCP: Connecting to '{server_name}' (attempt {attempt}/{config.retry_attempts})...")
                
                # Build environment: merge current env with server-specific env
                env = dict(os.environ)
                env.update(config.env)
                
                server_params = StdioServerParameters(
                    command=config.command,
                    args=config.args,
                    env=env if config.env else None,
                    cwd=config.cwd
                )
                
                # Create the stdio client context manager
                # We need to keep it alive for the connection duration
                ctx = stdio_client(server_params)
                streams = await ctx.__aenter__()
                
                # Create session
                session = ClientSession(*streams)
                await session.__aenter__()
                
                # Initialize the MCP session
                init_result = await asyncio.wait_for(
                    session.initialize(),
                    timeout=config.timeout_seconds
                )
                
                logger.info(f"✅ MCP: Connected to '{server_name}' — protocol: {init_result.protocolVersion}")
                
                # Discover tools
                tools_result = await session.list_tools()
                tools = []
                
                for mcp_tool in tools_result.tools:
                    prefix = config.tool_prefix or f"mcp_{server_name}_"
                    saige_name = f"{prefix}{mcp_tool.name}"
                    
                    tool_info = MCPToolInfo(
                        server_name=server_name,
                        original_name=mcp_tool.name,
                        saige_name=saige_name,
                        description=mcp_tool.description or "",
                        input_schema=mcp_tool.inputSchema or {},
                    )
                    tools.append(tool_info)
                    self.tool_registry[saige_name] = tool_info
                
                # Store connection
                conn = MCPServerConnection(
                    config=config,
                    session=session,
                    tools=tools,
                    connected=True,
                    last_connected=time.time(),
                    _context_manager=ctx
                )
                self.servers[server_name] = conn
                
                tool_names = [t.saige_name for t in tools]
                logger.info(f"🔧 MCP: Discovered {len(tools)} tools from '{server_name}': {tool_names[:10]}{'...' if len(tools) > 10 else ''}")
                
                return True
                
            except asyncio.TimeoutError:
                logger.warning(f"⏱️ MCP: Timeout connecting to '{server_name}' (attempt {attempt})")
            except FileNotFoundError as e:
                logger.error(f"❌ MCP: Server command not found for '{server_name}': {config.command} — {e}")
                # Don't retry if the binary doesn't exist
                break
            except Exception as e:
                logger.error(f"❌ MCP: Error connecting to '{server_name}' (attempt {attempt}): {type(e).__name__}: {e}")
            
            if attempt < config.retry_attempts:
                await asyncio.sleep(config.retry_delay)
        
        # Record failure
        if server_name not in self.servers:
            self.servers[server_name] = MCPServerConnection(config=config)
        self.servers[server_name].connected = False
        self.servers[server_name].last_error = f"Failed after {config.retry_attempts} attempts"
        self.servers[server_name].connection_attempts += config.retry_attempts
        
        return False

    async def _disconnect_all(self):
        """Disconnect from all servers."""
        for name, conn in self.servers.items():
            try:
                if conn.session:
                    await conn.session.__aexit__(None, None, None)
                if conn._context_manager:
                    await conn._context_manager.__aexit__(None, None, None)
                conn.connected = False
                logger.info(f"🔌 MCP: Disconnected from '{name}'")
            except Exception as e:
                logger.debug(f"Error disconnecting from '{name}': {e}")

    async def _reconnection_loop(self):
        """Background loop that monitors and reconnects failed servers."""
        while True:
            await asyncio.sleep(60)  # Check every 60 seconds
            
            for name, conn in self.servers.items():
                if not conn.connected and conn.config.enabled and conn.config.auto_connect:
                    logger.info(f"🔄 MCP: Attempting reconnection to '{name}'...")
                    # Remove stale tools
                    for tool in conn.tools:
                        self.tool_registry.pop(tool.saige_name, None)
                    conn.tools = []
                    
                    success = await self._connect_server(conn.config)
                    if success:
                        logger.info(f"✅ MCP: Reconnected to '{name}'!")

    # ------------------------------------------------------------------
    # Tool Execution
    # ------------------------------------------------------------------
    async def _call_tool_async(self, server_name: str, tool_name: str, 
                                arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call an MCP tool asynchronously."""
        conn = self.servers.get(server_name)
        if not conn or not conn.connected or not conn.session:
            return {
                "success": False,
                "error": f"MCP server '{server_name}' not connected"
            }
        
        try:
            result: CallToolResult = await asyncio.wait_for(
                conn.session.call_tool(tool_name, arguments),
                timeout=conn.config.timeout_seconds
            )
            
            # Extract content from MCP response
            if result.isError:
                error_text = ""
                for content in result.content:
                    if hasattr(content, 'text'):
                        error_text += content.text
                return {
                    "success": False,
                    "error": error_text or "MCP tool returned an error"
                }
            
            # Parse successful response
            response_parts = []
            for content in result.content:
                if hasattr(content, 'text'):
                    response_parts.append(content.text)
                elif hasattr(content, 'data'):
                    response_parts.append(f"[{content.type}: {len(content.data)} bytes]")
                else:
                    response_parts.append(str(content))
            
            response_text = "\n".join(response_parts) if response_parts else str(result)
            
            # Try to parse as JSON if it looks like JSON
            if response_text.strip().startswith('{') or response_text.strip().startswith('['):
                try:
                    parsed = json.loads(response_text)
                    return {"success": True, "result": parsed}
                except json.JSONDecodeError:
                    pass
            
            return {"success": True, "result": response_text}
            
        except asyncio.TimeoutError:
            logger.warning(f"⏱️ MCP tool timeout: {server_name}/{tool_name}")
            return {
                "success": False,
                "error": f"MCP tool '{tool_name}' timed out after {conn.config.timeout_seconds}s"
            }
        except Exception as e:
            logger.error(f"❌ MCP tool error: {server_name}/{tool_name}: {e}")
            # Check if connection is broken
            if "broken" in str(e).lower() or "closed" in str(e).lower():
                conn.connected = False
                conn.last_error = str(e)
            return {
                "success": False,
                "error": f"MCP tool error: {type(e).__name__}: {e}"
            }

    def call_tool(self, server_name: str, tool_name: str, 
                   arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Synchronous wrapper to call an MCP tool. Safe to call from
        SAIGE's synchronous BrainSystem code.
        """
        if not self._loop or not self._started:
            return {"success": False, "error": "MCP client not started"}
        
        future = self._submit_async(
            self._call_tool_async(server_name, tool_name, arguments)
        )
        
        try:
            return future.result(timeout=60)
        except Exception as e:
            return {"success": False, "error": f"MCP call failed: {e}"}

    def _make_tool_proxy(self, tool_info: MCPToolInfo) -> callable:
        """
        Create a synchronous callable that proxies to an MCP tool.
        This is what gets registered in brain.available_tools.
        """
        manager = self
        server_name = tool_info.server_name
        original_name = tool_info.original_name
        
        def mcp_tool_proxy(**kwargs) -> Dict[str, Any]:
            """Proxy function that calls an MCP tool through the MCP client."""
            start_time = time.time()
            result = manager.call_tool(server_name, original_name, kwargs)
            elapsed = time.time() - start_time
            
            if result.get("success"):
                logger.info(f"🌐 MCP tool '{original_name}' on '{server_name}' completed in {elapsed:.1f}s")
            else:
                logger.warning(f"🌐 MCP tool '{original_name}' on '{server_name}' failed ({elapsed:.1f}s): {result.get('error', 'unknown')}")
            
            return result
        
        # Attach metadata so BrainSystem can introspect
        mcp_tool_proxy.__doc__ = tool_info.description or f"MCP tool: {original_name} (from {server_name})"
        mcp_tool_proxy._mcp_tool_info = tool_info
        mcp_tool_proxy._is_mcp_tool = True
        
        return mcp_tool_proxy

    # ------------------------------------------------------------------
    # Async Event Loop Management
    # ------------------------------------------------------------------
    def _run_event_loop(self):
        """Run the asyncio event loop in a background thread."""
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._loop_ready.set)
        self._loop.run_forever()

    def _submit_async(self, coro) -> Future:
        """Submit a coroutine to the background event loop, return a Future."""
        if not self._loop or not self._loop.is_running():
            raise RuntimeError("MCP event loop not running")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    def _load_configs(self) -> List[MCPServerConfig]:
        """Load MCP server configurations from JSON file."""
        if not os.path.exists(self.config_path):
            return []
        
        try:
            with open(self.config_path, 'r') as f:
                data = json.load(f)
            
            configs = []
            servers = data.get("servers", data) if isinstance(data, dict) else data
            
            if isinstance(servers, dict):
                # Format: {"server_name": {config...}, ...}
                for name, cfg in servers.items():
                    if isinstance(cfg, dict):
                        cfg["name"] = cfg.get("name", name)
                        configs.append(MCPServerConfig(**cfg))
            elif isinstance(servers, list):
                # Format: [{config...}, ...]
                for cfg in servers:
                    if isinstance(cfg, dict):
                        configs.append(MCPServerConfig(**cfg))
            
            logger.info(f"📋 MCP: Loaded {len(configs)} server configs from {self.config_path}")
            return configs
            
        except Exception as e:
            logger.error(f"❌ MCP: Error loading config from {self.config_path}: {e}")
            return []

    def _create_default_config(self):
        """Create a default MCP servers config file with examples."""
        default_config = {
            "_comment": "SAIGE MCP Server Configuration — Add MCP servers here for SAIGE to connect to",
            "_docs": "Each server entry needs: command (executable), args (list), and optionally env, tool_prefix, enabled",
            "servers": {
                "filesystem": {
                    "name": "filesystem",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", str(get_data_dir())],
                    "description": "File system access via MCP",
                    "tool_prefix": "mcp_fs_",
                    "enabled": False,
                    "auto_connect": True,
                    "timeout_seconds": 15
                },
                "brave_search": {
                    "name": "brave_search",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-brave-search"],
                    "env": {"BRAVE_API_KEY": "YOUR_API_KEY_HERE"},
                    "description": "Web search via Brave Search API",
                    "tool_prefix": "mcp_brave_",
                    "enabled": False,
                    "auto_connect": True,
                    "timeout_seconds": 30
                },
                "memory": {
                    "name": "memory",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-memory"],
                    "description": "Persistent memory/knowledge graph via MCP",
                    "tool_prefix": "mcp_memory_",
                    "enabled": False,
                    "auto_connect": True,
                    "timeout_seconds": 15
                }
            }
        }
        
        try:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, 'w') as f:
                json.dump(default_config, f, indent=2)
            logger.info(f"📋 MCP: Created default config at {self.config_path}")
        except Exception as e:
            logger.error(f"❌ MCP: Error creating default config: {e}")

    # ------------------------------------------------------------------
    # Dynamic Server Management (runtime add/remove)
    # ------------------------------------------------------------------
    def add_server(self, config: MCPServerConfig) -> bool:
        """Add and connect to a new MCP server at runtime."""
        if not self._started:
            logger.error("MCP client not started — call start() first")
            return False
        
        try:
            future = self._submit_async(self._connect_server(config))
            success = future.result(timeout=config.timeout_seconds + 10)
            
            if success:
                # Save to config file
                self._save_server_config(config)
            
            return success
        except Exception as e:
            logger.error(f"❌ MCP: Error adding server '{config.name}': {e}")
            return False

    def remove_server(self, server_name: str):
        """Disconnect and remove an MCP server."""
        conn = self.servers.get(server_name)
        if conn:
            # Remove tools from registry
            for tool in conn.tools:
                self.tool_registry.pop(tool.saige_name, None)
            
            # Disconnect
            if conn.session:
                try:
                    future = self._submit_async(conn.session.__aexit__(None, None, None))
                    future.result(timeout=5)
                except Exception:
                    pass
            
            del self.servers[server_name]
            logger.info(f"🗑️ MCP: Removed server '{server_name}'")

    def _save_server_config(self, config: MCPServerConfig):
        """Save a server config to the JSON file."""
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r') as f:
                    data = json.load(f)
            else:
                data = {"servers": {}}
            
            servers = data.get("servers", {})
            servers[config.name] = {
                "name": config.name,
                "command": config.command,
                "args": config.args,
                "env": config.env,
                "description": config.description,
                "tool_prefix": config.tool_prefix,
                "enabled": config.enabled,
                "auto_connect": config.auto_connect,
                "timeout_seconds": config.timeout_seconds
            }
            data["servers"] = servers
            
            with open(self.config_path, 'w') as f:
                json.dump(data, f, indent=2)
                
        except Exception as e:
            logger.error(f"❌ MCP: Error saving config: {e}")

    # ------------------------------------------------------------------
    # Tool Listing for AI Discovery
    # ------------------------------------------------------------------
    def list_available_tools(self) -> str:
        """
        Returns a human-readable listing of all MCP tools.
        Useful for the AI to understand what external tools are available.
        """
        if not self.tool_registry:
            return "No MCP external tools currently connected."
        
        lines = ["🌐 === MCP External Tools ==="]
        
        by_server = {}
        for info in self.tool_registry.values():
            by_server.setdefault(info.server_name, []).append(info)
        
        for server_name, tools in by_server.items():
            conn = self.servers.get(server_name)
            status = "🟢 Connected" if conn and conn.connected else "🔴 Disconnected"
            lines.append(f"\n📦 {server_name} ({status}):")
            
            for tool in tools:
                desc = tool.description[:80] if tool.description else "No description"
                lines.append(f"  • {tool.saige_name}: {desc}")
        
        lines.append(f"\nTotal: {len(self.tool_registry)} external tools from {len(by_server)} servers")
        return "\n".join(lines)

    def search_tools(self, query: str) -> List[Dict[str, Any]]:
        """Search MCP tools by name or description keyword."""
        query_lower = query.lower()
        results = []
        
        for saige_name, info in self.tool_registry.items():
            name_match = query_lower in saige_name.lower()
            desc_match = query_lower in (info.description or "").lower()
            
            if name_match or desc_match:
                results.append({
                    "name": saige_name,
                    "server": info.server_name,
                    "description": info.description,
                    "parameters": info.input_schema.get("properties", {})
                })
        
        return results
