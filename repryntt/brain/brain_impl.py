"""
repryntt.brain.brain_impl — Standalone BrainSystem.

Phase 9: Fully standalone — NO monolith dependency.

All subsystems (memory, personality, tools, chain-of-thought, etc.) are
initialised from repryntt modules.  The ToolRegistry provides
``available_tools``, and ``__getattr__`` falls through to it so callers
can do ``brain.grokipedia_search(query)`` and hit the native tool.

Usage:
    from repryntt.brain import get_brain_system
    brain = get_brain_system(brain_path="brain")
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Lazy import helpers (avoid circular imports at module level)
# ---------------------------------------------------------------------------

def _make_memory_manager(brain):
    try:
        from repryntt.core.memory.agent_memory import AgentMemoryManager
        return AgentMemoryManager(brain)
    except (ImportError, ModuleNotFoundError):
        logger.warning("AgentMemoryManager not available — memory features disabled")
        return None

def _make_personality_manager(brain):
    from repryntt.core.identity.personality import PersonalityManager
    return PersonalityManager(brain)

def _make_node2040_manager(brain):
    from repryntt.core.identity.node2040 import Node2040Manager
    return Node2040Manager(brain)

def _make_cot_engine(brain):
    from repryntt.tools.chain_of_thought import ChainOfThoughtEngine
    return ChainOfThoughtEngine(brain)

def _make_topic_generator(brain):
    from repryntt.search.feeders.topic_generator import TopicGenerator
    return TopicGenerator(brain)


def _load_ai_provider_config(brain_path: Path) -> Dict[str, Any]:
    """Load AI provider config from brain/ai_config.json."""
    config_path = brain_path / "ai_config.json"
    from repryntt.paths import local_llm_endpoint as _llm_ep
    default = {
        "provider": "local",
        "local": {
            "endpoint": _llm_ep(),
            "model": "default",
            "api_key": None,
            "max_tokens": 800,
            "context_window": 4096,
        },
    }
    try:
        if config_path.exists():
            with open(config_path, "r") as f:
                full = json.load(f)
            config = full.get("ai_provider", default)
            provider = config.get("provider", "local")
            settings = config.get(provider, default["local"])
            logger.info(f"AI Provider: {provider} → {settings.get('endpoint', 'unknown')[:60]}...")
            return config
        logger.info("AI Provider: local (no ai_config.json, using defaults)")
        return default
    except Exception as e:
        logger.warning(f"Failed to load ai_config.json: {e} — using defaults")
        return default


# ---------------------------------------------------------------------------
#  ReprynttBrainSystem — standalone implementation
# ---------------------------------------------------------------------------

class ReprynttBrainSystem:
    """Standalone BrainSystem: all subsystems from repryntt, no monolith.

    Construction flow:
      1. Set up file paths and core attributes.
      2. Instantiate repryntt module managers (memory, personality, node2040,
         chain-of-thought, topic generator).
      3. Initialise the ToolRegistry → exposed as ``available_tools``.
      4. Optionally initialise subsystems (MCP, swarm, economy, etc.) —
         each is guarded so failures don't block startup.
      5. ``__getattr__`` falls through to ``available_tools`` so that
         ``brain.grokipedia_search(q)`` finds the registered native tool.
    """

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = object.__new__(cls)
        return cls._instance

    def __init__(self, brain_path: str = "brain",
                 node2040_brain_path: str = "node2040_brain.json"):
        if hasattr(self, "_repryntt_composed"):
            return
        self._repryntt_composed = True

        # ── Core paths ───────────────────────────────────────────────
        self.brain_path = Path(brain_path)
        self.brain_path.mkdir(parents=True, exist_ok=True)
        self.node2040_brain_path = Path(node2040_brain_path)
        self.episodic_memory_file = self.brain_path / "episodic_memory.json"
        self.semantic_memory_file = self.brain_path / "semantic_memory.json"
        self.procedural_memory_file = self.brain_path / "procedural_memory.json"
        self.working_memory_file = self.brain_path / "working_memory.json"
        self.knowledge_base_path = self.brain_path / "knowledge_base"
        self.personality_brain_path = self.brain_path / "ava_brain.json"

        # ── In-memory state ──────────────────────────────────────────
        self.personality_brain: Dict[str, Any] = {}
        self.node2040_brain: Dict[str, Any] = {}
        self.episodic_cache: list = []
        self.semantic_cache: dict = {}
        self.procedural_cache: dict = {}
        self.working_memory = None
        self.db_session = None
        self.use_database = False
        self.hormone_system = None
        self._recent_topics_hash: list = []
        self.recent_memories_limit = 1000
        self._current_agent_id: Optional[str] = None
        self._daemon_ref = None

        # ── Vector search (lazy-loaded) ──────────────────────────────
        self.encoder = None
        self.index = None
        self.index_metadata: list = []
        self.vector_search_enabled = False

        # ── Thread safety / autonomy ─────────────────────────────────
        self.lock = threading.Lock()
        self.autonomous_thread = None
        self.running = False

        # ── CoT queues ───────────────────────────────────────────────
        self.cot_queue_file = self.brain_path / "cot_queue.json"
        self.cot_queue: List[Dict[str, Any]] = []
        self.ai_chain_queue_file = self.brain_path / "ai_chain_queue.json"
        self.ai_chain_queue: List[Dict[str, Any]] = []
        self.cot_queue_lock = threading.Lock()

        # ── AI provider config ───────────────────────────────────────
        self.ai_provider_config = _load_ai_provider_config(self.brain_path)

        # ── Module delegates ─────────────────────────────────────────
        self._memory = _make_memory_manager(self)
        # Load the persisted memory stores into the caches. Without this call the
        # entity boots AMNESIC — semantic/episodic/procedural caches stay empty no
        # matter what's on disk (the "Brain Context Acquired: 0 memories" bug: the
        # loader existed but nothing ever invoked it after the factory refactor).
        if self._memory is not None:
            try:
                self._memory.load_memories()
            except Exception:
                logger.exception("memory load failed — continuing with empty caches")
        self._personality = _make_personality_manager(self)
        self._node2040 = _make_node2040_manager(self)
        self._cot = _make_cot_engine(self)
        self._topic_gen = _make_topic_generator(self)

        # ── ToolRegistry → available_tools ───────────────────────────
        self._tool_registry = None
        try:
            from repryntt.tools.registry import ToolRegistry
            self._tool_registry = ToolRegistry()
            n = self._tool_registry.register_native_tools(str(self.brain_path))
            logger.info(f"ToolRegistry: {n} native tools registered")
        except Exception as e:
            logger.warning(f"ToolRegistry init failed: {e}")

        self.available_tools: Dict[str, Any] = (
            self._tool_registry._tools if self._tool_registry else {}
        )

        # ── Optional subsystems (all guarded) ────────────────────────
        self.task_hierarchy = None
        self.map_network = None
        self.tool_chain_executor = None
        self.consciousness = None
        self.prompt_sync = None
        self.mcp_client = None
        self.swarm_commander = None
        self.robot_economy_manager = None
        self.economy_enabled = False
        self.use_blockchain_ai = False
        self.blockchain_ai_percentage = 0
        self.output_processor = None
        self.prompt_generator = None
        self.synthesis_engine = None
        self.conclusion_evaluator = None
        self.conversation_logger = None
        self._recent_grokipedia_searches: dict = {}
        self._last_inspiration_index: int = 0

        self._init_optional_subsystems()

        # ── Register delegate tools (need brain instance) ────────────
        if self._tool_registry:
            try:
                dn = self._tool_registry.register_brain_delegate_tools(self)
                logger.info(f"ToolRegistry: {dn} delegate tools registered")
            except Exception as e:
                logger.warning(f"Delegate tool registration failed: {e}")

        logger.info(
            "✅ ReprynttBrainSystem standalone — 5 module delegates + "
            f"{len(self.available_tools)} tools (no monolith)"
        )

    # ---------------------------------------------------------------
    #  Optional subsystem init (each guarded individually)
    # ---------------------------------------------------------------

    def _init_optional_subsystems(self):
        """Initialise optional subsystems, each failure-safe."""

        # TaskHierarchySystem
        try:
            from repryntt.routing.task_hierarchy import TaskHierarchySystem
            self.task_hierarchy = TaskHierarchySystem()
        except Exception as e:
            logger.debug(f"TaskHierarchySystem not available: {e}")

        # AIOutputProcessor
        try:
            from repryntt.tools.output_processor import AIOutputProcessor
            self.output_processor = AIOutputProcessor(self)
        except Exception as e:
            logger.debug(f"AIOutputProcessor not available: {e}")

        # ChainSynthesisEngine / AutonomousConclusionEvaluator
        try:
            from repryntt.tools.chain_executor import (
                ChainSynthesisEngine,
                AutonomousConclusionEvaluator,
            )
            self.synthesis_engine = ChainSynthesisEngine(self)
            self.conclusion_evaluator = AutonomousConclusionEvaluator(self)
        except Exception as e:
            logger.debug(f"ChainSynthesis/ConclusionEvaluator not available: {e}")

        # ToolChainExecutor
        try:
            from repryntt.tools.chain_executor import ToolChainExecutor
            self.tool_chain_executor = ToolChainExecutor(self)
        except Exception as e:
            logger.debug(f"ToolChainExecutor not available: {e}")

        # PromptSyncSystem
        try:
            from repryntt.core.memory.prompt_sync import PromptSyncSystem
            self.prompt_sync = PromptSyncSystem(self)
        except Exception as e:
            logger.debug(f"PromptSyncSystem not available: {e}")

        # MCPClientManager — DEFERRED to save ~300MB at startup.
        # MCP servers (Playwright, fetch, etc.) are memory-hungry child processes.
        # They will be connected on first tool call that needs them.
        self._mcp_config_path = None
        try:
            _mcp_cfg = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "..", "config", "mcp_servers.json",
            )
            if os.path.exists(_mcp_cfg):
                self._mcp_config_path = _mcp_cfg
            logger.info("🌐 MCP: deferred (will connect on first tool call)")
        except Exception as e:
            logger.debug(f"MCP config not found: {e}")

        # SwarmCommander
        try:
            from repryntt.agents.swarm import SwarmCommander
            self.swarm_commander = SwarmCommander(brain_system=self)
        except Exception as e:
            logger.debug(f"SwarmCommander not available: {e}")

        # MapSyncNetwork (may live in SAIGE still)
        try:
            from repryntt.tools.discovery import integrate_with_map_network
            self.map_network = integrate_with_map_network(self)
        except Exception:
            try:
                import sys
                saige_dir = os.environ.get("SAIGE_DIR", "")
                if saige_dir and saige_dir not in sys.path:
                    sys.path.insert(0, saige_dir)
                from brain.map_sync_network import MapSyncNetwork
                self.map_network = MapSyncNetwork(self)
            except Exception as e:
                logger.debug(f"MapSyncNetwork not available: {e}")

        # Robot Economy — auto-enables when blockchain node is running or env set
        _economy_env = os.environ.get(
            "REPRYNTT_ENABLE_ECONOMY",
            os.environ.get("SAIGE_ENABLE_ECONOMY", ""),
        ).strip().lower()
        if _economy_env in ("1", "true", "yes", "y"):
            self.economy_enabled = True
        else:
            # Auto-detect: check if blockchain-node is already running on port 5001
            import socket as _sock
            _s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            try:
                self.economy_enabled = _s.connect_ex(("127.0.0.1", 5001)) == 0
            except Exception:
                self.economy_enabled = False
            finally:
                _s.close()
        if self.economy_enabled:
            try:
                from repryntt.economy.manager import RobotEconomyManager
                self.robot_economy_manager = RobotEconomyManager(brain_system=self)
                self.use_blockchain_ai = os.environ.get(
                    "SAIGE_BLOCKCHAIN_AI", "1"
                ).strip() in ("1", "true", "yes", "y")
                self.blockchain_ai_percentage = float(
                    os.environ.get("SAIGE_BLOCKCHAIN_AI_PERCENT", "100")
                )
                # Auto-start the economy: spawn miners, bootstrap funding,
                # connect to the network.  This turns every device into a
                # participating node in the decentralized AI robot economy
                # regardless of whether the local LLM or an API provider is
                # being used — idle GPU contributes compute and earns credits.
                import threading as _econ_th
                def _autostart_economy(mgr):
                    import time as _t
                    _t.sleep(5)          # let blockchain node finish handshake
                    for attempt in range(3):
                        try:
                            if mgr.is_running:
                                logger.info("💰 Economy already running — skipping auto-start")
                                return
                            result = mgr.start_economy()
                            if result.get('success'):
                                logger.info("💰 Economy auto-started: blockchain node running")
                                return
                            else:
                                logger.warning("Economy auto-start attempt %d returned: %s",
                                               attempt + 1, result.get('error'))
                        except Exception as exc:
                            logger.warning("Economy auto-start attempt %d failed: %s",
                                           attempt + 1, exc)
                        _t.sleep(10)  # wait before retry
                    logger.error("Economy auto-start failed after 3 attempts")
                _econ_th.Thread(
                    target=_autostart_economy,
                    args=(self.robot_economy_manager,),
                    daemon=True,
                    name="EconomyAutoStart",
                ).start()
            except Exception as e:
                logger.debug(f"RobotEconomyManager not available: {e}")

        # AutonomousConversationLogger
        try:
            from repryntt.comms.conversation_logger import get_conversation_logger
            self.conversation_logger = get_conversation_logger(str(self.brain_path))
        except Exception as e:
            logger.debug(f"ConversationLogger not available: {e}")

        # Vector search — DEFERRED to first use to save ~500MB at startup.
        # The model is loaded on the first call to vector_search() instead
        # of eagerly at boot.  Startup memory drops from ~2GB to ~800MB.
        logger.info("Vector search: deferred (will init on first query)")

    # ------------------------------------------------------------------ #
    #  Vector search initialisation                                       #
    # ------------------------------------------------------------------ #

    def _initialize_vector_search(self):
        """Initialize vector-based semantic memory search (FAISS + SentenceTransformer).

        Mirrors the local LLM side (brain_system.py) so the API agent side
        gets the same vector recall capabilities. Failure-safe — falls back
        to keyword search if dependencies are missing.
        """
        if self.vector_search_enabled:
            return

        try:
            import numpy as np  # noqa: F401
            # Jetson torch builds ship WITHOUT distributed support, but
            # sentence-transformers 5.x probes torch.distributed.is_initialized at
            # import/encode time — shim the two probes it uses so the encoder loads.
            try:
                import torch
                if not hasattr(torch.distributed, "is_initialized"):
                    torch.distributed.is_initialized = lambda: False
                if not hasattr(torch.distributed, "is_available"):
                    torch.distributed.is_available = lambda: False
            except Exception:
                pass
            from sentence_transformers import SentenceTransformer
            import faiss
        except ImportError:
            logger.info("Vector search deps (sentence-transformers/faiss) not installed — keyword fallback")
            return

        try:
            # Use the shared encoder cache from persistent_agents if available,
            # otherwise load our own instance
            try:
                from repryntt.agents.persistent_agents import get_shared_sentence_transformer
                self.encoder = get_shared_sentence_transformer('all-MiniLM-L6-v2')
            except (ImportError, Exception):
                self.encoder = SentenceTransformer('all-MiniLM-L6-v2')

            if self.encoder is None:
                logger.warning("SentenceTransformer encoder is None — skipping vector search")
                return

            self.index = faiss.IndexFlatIP(384)  # 384-dim for all-MiniLM-L6-v2
            self.vector_search_enabled = True

            self._rebuild_vector_index()
            n = self.index.ntotal if self.index else 0
            logger.info(f"Vector search initialized — {n} vectors indexed")
        except Exception as e:
            logger.warning(f"Vector search init failed: {e}")
            self.vector_search_enabled = False

    def _rebuild_vector_index(self):
        """Rebuild FAISS index from all semantic + episodic memories."""
        if not self.vector_search_enabled:
            return

        import numpy as np

        texts = []
        metadata = []

        # Index semantic memories
        for memory in self.semantic_cache.values():
            try:
                topic = memory.get("topic", "") if isinstance(memory, dict) else getattr(memory, "topic", "")
                content = memory.get("content", "") if isinstance(memory, dict) else getattr(memory, "content", "")
                mem_id = memory.get("id", "") if isinstance(memory, dict) else getattr(memory, "id", "")
                if topic or content:
                    texts.append(f"{topic}: {content}")
                    metadata.append({"type": "semantic", "id": mem_id, "topic": topic})
            except Exception:
                pass

        # Index recent episodic memories
        for memory in self.episodic_cache[-100:]:
            try:
                content = memory.get("content", "") if isinstance(memory, dict) else getattr(memory, "content", "")
                mem_id = memory.get("id", "") if isinstance(memory, dict) else getattr(memory, "id", "")
                if content:
                    texts.append(content[:512])
                    metadata.append({"type": "episodic", "id": mem_id})
            except Exception:
                pass

        if texts:
            embeddings = self.encoder.encode(texts, convert_to_numpy=True).astype('float32')
            import faiss
            faiss.normalize_L2(embeddings)
            self.index.add(embeddings)
            self.index_metadata = metadata
            logger.debug(f"Rebuilt vector index: {len(texts)} entries")

    # ================================================================== #
    #  __getattr__ — fallback to ToolRegistry for tool-as-method calls    #
    # ================================================================== #

    def __getattr__(self, name: str) -> Any:
        # Check the tool registry (allows brain.grokipedia_search(q) etc.)
        try:
            registry = object.__getattribute__(self, "_tool_registry")
            if registry and name in registry._tools:
                return registry._tools[name]
        except AttributeError:
            pass
        raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")

    def __setattr__(self, name: str, value: Any) -> None:
        # All attributes go on self (no monolith proxy needed)
        object.__setattr__(self, name, value)

    # ================================================================== #
    #  Lazy initializers (deferred subsystems to save RAM at startup)     #
    # ================================================================== #

    def ensure_mcp(self) -> bool:
        """Connect MCP servers on first use. Returns True if MCP is available."""
        if self.mcp_client is not None:
            return True
        try:
            from repryntt.routing.mcp_client import MCPClientManager
            cfg = getattr(self, '_mcp_config_path', None)
            self.mcp_client = MCPClientManager(config_path=cfg)
            if self.mcp_client.start():
                mcp_tools = self.mcp_client.get_tool_registry()
                if mcp_tools:
                    self.available_tools.update(mcp_tools)
                    logger.info(f"🌐 MCP: {len(mcp_tools)} tools bridged (lazy)")
                return True
            logger.warning("🌐 MCP: start() returned False")
            return False
        except Exception as e:
            logger.debug(f"MCP lazy-start failed: {e}")
            return False

    def ensure_vector_search(self) -> bool:
        """Initialize vector search on first use. Returns True if available."""
        if self.vector_search_enabled:
            return True
        try:
            self._initialize_vector_search()
            return self.vector_search_enabled
        except Exception:
            return False

    # ================================================================== #
    #  AI SERVICE — direct LLM calls for consciousness / morning startup  #
    # ================================================================== #

    def _call_ai_service(self, prompt: str, priority: int = 0,
                         timeout: int = 120,
                         include_tools: bool = True,
                         purpose: str = "") -> Optional[str]:
        """Call AI service for self-autonomous components (consciousness, morning startup, CoT).

        Routes through whatever provider is configured in ai_provider_config
        (local llama.cpp, OpenAI, Anthropic, OpenRouter, etc.).

        Returns the response text, or None on failure.
        """
        import requests as _req

        config = self.ai_provider_config
        # Executive tier: identity-shaping moments (operator conversation, morning
        # startup, reflection) get the frontier mind, budget-capped per day.
        try:
            from repryntt.routing.provider_router import maybe_escalate_executive
            config = maybe_escalate_executive(
                config, purpose=purpose,
                hormone_system=getattr(self, "hormone_system", None))
        except Exception:
            pass
        provider = config.get("provider", "local")
        settings = config.get(provider, config.get("local", {}))

        from repryntt.paths import local_llm_endpoint
        endpoint = settings.get("endpoint",
                                local_llm_endpoint())
        model = settings.get("model", "default")
        api_key = settings.get("api_key")
        max_tokens = settings.get("max_tokens", 800)

        # Build headers
        headers = {"Content-Type": "application/json"}
        if api_key and api_key not in (None, "", "YOUR_OPENAI_API_KEY_HERE",
                                        "YOUR_ANTHROPIC_API_KEY_HERE",
                                        "YOUR_OPENROUTER_API_KEY_HERE"):
            headers["Authorization"] = f"Bearer {api_key}"

        # Hormone-modulated sampling parameters (if hormone system available)
        ai_params = self._get_ai_parameters_for_self_autonomous()

        # Time context
        time_ctx = ""
        try:
            t = self.get_current_time()
            time_ctx = (f"Current time: {t.get('time_readable', '')} "
                        f"{t.get('date', '')} ({t.get('day_of_week', '')})\n")
        except Exception:
            pass

        # Hormone context
        hormone_ctx = ""
        if self.hormone_system:
            try:
                levels = dict(self.hormone_system.levels)
                top3 = sorted(levels.items(), key=lambda x: x[1], reverse=True)[:3]
                hormone_ctx = ("Internal state: " +
                               ", ".join(f"{k}={v:.2f}" for k, v in top3) + "\n")
            except Exception:
                pass

        system_content = f"{time_ctx}{hormone_ctx}".strip()

        messages = []
        if system_content:
            messages.append({"role": "system", "content": system_content})
        messages.append({"role": "user", "content": prompt})

        if provider == "anthropic":
            _resp = self._call_anthropic_service(
                endpoint, model, api_key, prompt, max_tokens, ai_params, timeout)
            try:
                from repryntt.routing.provider_router import record_executive_distillation
                record_executive_distillation(config, prompt, _resp)
            except Exception:
                pass
            return _resp

        # OpenAI-compatible (local / openai / openrouter / nvidia / custom)
        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": ai_params.get("max_tokens", max_tokens),
            "temperature": ai_params.get("temperature", 0.8),
            "top_p": ai_params.get("top_p", 0.9),
            "frequency_penalty": ai_params.get("frequency_penalty", 0.3),
            "stream": False,
        }

        try:
            resp = _req.post(endpoint, headers=headers, json=body, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices", [])
            if choices:
                _resp2 = choices[0].get("message", {}).get("content", "")
                try:
                    from repryntt.routing.provider_router import record_executive_distillation
                    record_executive_distillation(config, prompt, _resp2)
                except Exception:
                    pass
                return _resp2
            return None
        except _req.exceptions.ConnectionError:
            logger.debug(f"AI provider ({provider}) not reachable at {endpoint[:60]}")
            return None
        except _req.exceptions.Timeout:
            logger.debug(f"AI provider ({provider}) timed out")
            return None
        except Exception as e:
            logger.debug(f"AI service call failed ({provider}): {e}")
            return None

    def _call_anthropic_service(self, endpoint: str, model: str,
                                api_key: str, prompt: str,
                                max_tokens: int, ai_params: Dict,
                                timeout: int) -> Optional[str]:
        """Anthropic Messages API wrapper — returns plain text."""
        import requests as _req
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key or "",
            "anthropic-version": "2023-06-01",
        }
        body = {
            "model": model,
            "max_tokens": ai_params.get("max_tokens", max_tokens),
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            resp = _req.post(endpoint, headers=headers, json=body, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            content_blocks = data.get("content", [])
            if content_blocks:
                return content_blocks[0].get("text", "")
            return None
        except Exception as e:
            logger.debug(f"Anthropic AI service call failed: {e}")
            return None

    def _get_ai_parameters_for_self_autonomous(self) -> Dict[str, Any]:
        """Get AI sampling parameters, optionally modulated by hormones."""
        base = {
            "max_tokens": 800,
            "temperature": 0.8,
            "top_p": 0.9,
            "frequency_penalty": 0.3,
        }
        if not self.hormone_system:
            return base
        try:
            return self.hormone_system.get_sampling_parameters()
        except Exception:
            return base

    # ================================================================== #
    #  LIFECYCLE — hormone system bridge                                   #
    # ================================================================== #

    def set_hormone_system(self, hormone_system: Any) -> None:
        """Bridge the algorithmic hormone system so conversation layer can read/fire events."""
        object.__setattr__(self, "hormone_system", hormone_system)

    # ================================================================== #
    #  MEMORY DELEGATES → AgentMemoryManager                              #
    # ================================================================== #

    def store_semantic_memory(self, topic: str = "", content: str = "", *,
                              domain: str = "", confidence: float = 1.0,
                              source: str = "",
                              key_facts: Optional[List[str]] = None,
                              related_topics: Optional[List[str]] = None,
                              verification_sources: Optional[List[str]] = None,
                              **kw) -> Any:
        return self._memory.store_semantic_memory(
            topic=topic, content=content, domain=domain, confidence=confidence,
            source=source, key_facts=key_facts, related_topics=related_topics,
            **kw,
        )

    def search_semantic_memory(self, query: str = "", limit: int = 5, **kw) -> Any:
        return self._memory.search_semantic_memory(query=query, limit=limit, **kw)

    def store_episodic_memory(self, *, conversation_id: str = "",
                              user_input: str = "", ai_response: str = "",
                              tool_calls: Optional[List[Any]] = None,
                              outcome: str = "", **kw) -> Any:
        return self._memory.store_episodic_memory(
            conversation_id=conversation_id, user_input=user_input,
            ai_response=ai_response, tool_calls=tool_calls, outcome=outcome,
            **kw,
        )

    def search_episodic_memory(self, query: str = "", limit: int = 5, **kw) -> Any:
        return self._memory.search_episodic_memory(query=query, limit=limit, **kw)

    def brain_network_search(self, query: str = "", *,
                             memory_types: Optional[List[str]] = None,
                             limit: int = 5, **kw) -> Any:
        return self._memory.brain_network_search(
            query=query, memory_types=memory_types, limit=limit, **kw,
        )

    def get_context_for_question(self, question: str = "", max_words: int = 600, text: str = "", questions: str = "", **kw) -> str:
        # Accept 'text' or 'questions' as alias for 'question' (LLM sends varied param names)
        q = question or text or questions
        return self._memory.get_context_for_question(question=q, max_words=max_words, **kw)

    def update_procedural_memory(self, task_type: str = "", steps: Any = None,
                                 tools_used: Any = None, success: bool = True,
                                 execution_time: float = 0.0, **kw) -> Any:
        return self._memory.update_procedural_memory(
            task_type=task_type, steps=steps, tools_used=tools_used,
            success=success, execution_time=execution_time, **kw,
        )

    def get_procedural_memory(self, task_type: str = "", **kw) -> Any:
        return self._memory.get_procedural_memory(task_type=task_type, **kw)

    def brain_memory_save(self, key: str = "", value: Any = None,
                          topic: str = "", content: str = "", **kw) -> Any:
        return self._memory.brain_memory_save(key=key, value=value, topic=topic, content=content, **kw)

    def brain_memory_recall(self, query: str = "", key: str = "",
                            topic: str = "", **kw) -> Any:
        return self._memory.brain_memory_recall(query=query, key=key, topic=topic, **kw)

    def get_brain_stats(self, **kw) -> Any:
        return self._memory.get_brain_stats(**kw)

    def learn_from_interaction(self, user_input: str = "", ai_response: str = "",
                               tool_calls: Any = None, conversation_id: str = "",
                               outcome_quality: float = 0.0, **kw) -> Any:
        return self._memory.learn_from_interaction(
            user_input=user_input, ai_response=ai_response,
            tool_calls=tool_calls, conversation_id=conversation_id,
            outcome_quality=outcome_quality, **kw,
        )

    def _find_related_topics(self, topic: str = "", **kw) -> Any:
        return self._memory._find_related_topics(topic=topic, **kw)

    def _search_knowledge_domains(self, query: str = "", limit: int = 5, **kw) -> Any:
        return self._memory._search_knowledge_domains(query=query, limit=limit, **kw)

    def consolidate_memories(self, **kw) -> Any:
        return self._memory.consolidate_memories(**kw)

    # ================================================================== #
    #  PERSONALITY DELEGATES → PersonalityManager                         #
    # ================================================================== #

    def modify_personality_trait(self, trait_name: str = "", new_value: str = "",
                                reason: str = "", **kw) -> str:
        return self._personality.modify_personality_trait(
            trait_name=trait_name, new_value=new_value, reason=reason, **kw,
        )

    def evolve_personality_dimension(self, dimension_name: str = "",
                                     new_value: str = "", reason: str = "", **kw) -> str:
        return self._personality.evolve_personality_dimension(
            dimension_name=dimension_name, new_value=new_value, reason=reason, **kw,
        )

    def update_behavioral_guidelines(self, guideline_index: int = 0,
                                     new_guideline: str = "", reason: str = "", **kw) -> str:
        return self._personality.update_behavioral_guidelines(
            guideline_index=guideline_index, new_guideline=new_guideline, reason=reason, **kw,
        )

    def add_personality_trait(self, new_trait: str = "", reason: str = "", **kw) -> str:
        return self._personality.add_personality_trait(new_trait=new_trait, reason=reason, **kw)

    def remove_personality_trait(self, trait_name: str = "", reason: str = "", **kw) -> str:
        return self._personality.remove_personality_trait(trait_name=trait_name, reason=reason, **kw)

    def log_personality_evolution(self, event_type: str = "", details: str = "", **kw) -> str:
        return self._personality.log_personality_evolution(event_type=event_type, details=details, **kw)

    def analyze_personality_growth(self, **kw) -> str:
        return self._personality.analyze_personality_growth(**kw)

    def recreate_autonomous_personality(self, **kw) -> Any:
        return self._personality.recreate_autonomous_personality(**kw)

    def _save_personality_brain(self, **kw) -> Any:
        return self._personality.save_personality_brain(**kw)

    def update_wallet_balance(self, transaction_type: str = "",
                              amount: float = 0.0, description: str = "", **kw) -> Any:
        return self._personality.update_wallet_balance(
            transaction_type=transaction_type, amount=amount, description=description, **kw,
        )

    # ================================================================== #
    #  NODE2040 DELEGATES → Node2040Manager                               #
    # ================================================================== #

    def _update_node2040_brain(self, **kw) -> Any:
        return self._node2040.update_node2040_brain(**kw)

    # ================================================================== #
    #  CHAIN-OF-THOUGHT DELEGATES → ChainOfThoughtEngine                  #
    # ================================================================== #

    def create_chain_of_thought(self, *, topic: str = "", goal: str = "",
                                initial_prompt: str = "",
                                milestones: Any = None,
                                success_criteria: Any = None, **kw) -> str:
        return self._cot.create_chain_of_thought(
            topic=topic, goal=goal, initial_prompt=initial_prompt,
            milestones=milestones, success_criteria=success_criteria, **kw,
        )

    def create_self_autonomous_chain(self, *, topic: str = "", goal: str = "",
                                     task_type: str = "research",
                                     target_steps: int = 0, **kw) -> str:
        return self._cot.create_self_autonomous_chain(
            topic=topic, goal=goal, task_type=task_type,
            target_steps=target_steps, **kw,
        )

    def advance_self_autonomous_chain(self, chain_id: str = "",
                                      step_output: str = "",
                                      tool_results: Any = None, **kw) -> Any:
        return self._cot.advance_self_autonomous_chain(
            chain_id=chain_id, step_output=step_output,
            tool_results=tool_results, **kw,
        )

    def advance_chain_of_thought(self, chain_id: str = "",
                                 ai_response: str = "", **kw) -> Any:
        return self._cot.advance_chain_of_thought(
            chain_id=chain_id, ai_response=ai_response, **kw,
        )

    def update_chain_progress(self, *, chain_id: str = "", response: str = "",
                              insights: Optional[List[str]] = None,
                              next_questions: Optional[List[str]] = None,
                              conclusion: Any = None, **kw) -> Any:
        return self._cot.update_chain_progress(
            chain_id=chain_id, response=response, insights=insights,
            next_questions=next_questions, **kw,
        )

    def get_chain_context(self, chain_id: str = "", max_steps: int = 5, **kw) -> str:
        return self._cot.get_chain_context(chain_id=chain_id, max_steps=max_steps)

    def prompt_ai_conclusion_evaluation(self, chain_id: str = "", **kw) -> Any:
        return self._cot.prompt_ai_conclusion_evaluation(chain_id=chain_id, **kw)

    def queue_chain_of_thought(self, topic: str = "", goal: str = "",
                               priority: int = 5, requested_by: str = "", **kw) -> Any:
        return self._cot.queue_chain_of_thought(
            topic=topic, goal=goal, priority=priority, requested_by=requested_by, **kw,
        )

    def get_cot_queue_status(self, **kw) -> Any:
        return self._cot.get_cot_queue_status(**kw)

    def clear_cot_queue(self, **kw) -> Any:
        return self._cot.clear_cot_queue(**kw)

    def store_thoughts(self, thoughts=None, emotions=None, **kw) -> Any:
        # The underlying ChainOfThoughtEngine.store_thoughts takes (thoughts: str, chain_id, context).
        # Callers pass a list of thoughts + emotions dict. Serialize/adapt here so the
        # signature mismatch does not kill the evolution loop every cycle.
        try:
            if isinstance(thoughts, (list, tuple)):
                text = "\n".join(str(t) for t in thoughts if t)
            elif thoughts is None:
                text = ""
            else:
                text = str(thoughts)
            ctx = ""
            if emotions:
                try:
                    ctx = "emotions=" + ",".join(f"{k}:{v:.2f}" for k, v in dict(emotions).items())
                except Exception:
                    ctx = ""
            # Strip unsupported kwargs (like 'emotions') before delegating
            kw.pop("emotions", None)
            return self._cot.store_thoughts(thoughts=text, context=ctx, **kw)
        except Exception as e:
            return {"stored": False, "error": str(e)}

    def store_self_prompts(self, self_prompts=None, **kw) -> Any:
        """Persist self-prompts into working memory for later retrieval.

        TopicGenerator.get_self_prompts() reads ``working_memory.json`` under
        the ``self_prompts.prompts`` key. The evolution loop calls this every
        cycle, so this method must be present even when no prompts were created.
        """
        try:
            prompts = list(self_prompts or [])
            data: Dict[str, Any] = {}
            if self.working_memory_file.exists():
                try:
                    data = json.loads(self.working_memory_file.read_text())
                except Exception:
                    data = {}

            prompt_state = data.setdefault("self_prompts", {})
            existing = prompt_state.get("prompts", [])
            if not isinstance(existing, list):
                existing = []

            now = time.time()
            normalized = []
            for prompt in prompts:
                if isinstance(prompt, dict):
                    item = dict(prompt)
                else:
                    item = {"prompt": str(prompt)}
                item.setdefault("timestamp", now)
                normalized.append(item)

            prompt_state["prompts"] = (existing + normalized)[-200:]
            prompt_state["last_updated"] = now
            data["last_updated"] = now

            if self._memory:
                self._memory.save_memory("working", data)
            else:
                self.working_memory_file.parent.mkdir(parents=True, exist_ok=True)
                self.working_memory_file.write_text(json.dumps(data, indent=2, default=str))

            return {"stored": True, "count": len(normalized)}
        except Exception as e:
            logger.warning(f"store_self_prompts failed: {e}")
            return {"stored": False, "error": str(e)}

    def update_brain_state(self, thoughts=None, self_prompts=None, **kw) -> Any:
        """Record a compact evolution-loop state snapshot.

        The legacy monolith exposed this method; the standalone brain needs the
        same no-drama contract so a missing persistence hook cannot halt
        autonomous task generation.
        """
        try:
            thoughts_list = [str(t) for t in (thoughts or []) if t]
            prompts_list = list(self_prompts or [])
            state = self.personality_brain.setdefault("evolution_state", {})
            state["last_updated"] = time.time()
            state["recent_thoughts"] = thoughts_list[-20:]
            state["recent_self_prompt_count"] = len(prompts_list)
            if prompts_list:
                state["recent_self_prompts"] = prompts_list[-5:]
            self._save_personality_brain()
            return {
                "success": True,
                "thought_count": len(thoughts_list),
                "self_prompt_count": len(prompts_list),
            }
        except Exception as e:
            logger.warning(f"update_brain_state failed: {e}")
            return {"success": False, "error": str(e)}

    def query_exploration_history(self, query: str = "", limit: int = 20, **kw) -> Any:
        return self._cot.query_exploration_history(query=query, limit=limit)

    # ================================================================== #
    #  AWARENESS DELEGATES → standalone functions                         #
    # ================================================================== #

    def get_current_time(self, format: str = "full", **kw) -> Dict[str, Any]:
        from repryntt.tools.awareness import get_current_time
        return get_current_time(format=format, **kw)

    # ================================================================== #
    #  TOPIC GENERATOR DELEGATES → TopicGenerator                         #
    # ================================================================== #

    def get_self_prompts(self, limit: int = 5, enrich_external: bool = True, **kw) -> Any:
        return self._topic_gen.get_self_prompts(limit=limit, enrich_external=enrich_external, **kw)

    def _extract_diverse_topics_from_brain(self, limit: int = 5, **kw) -> Any:
        return self._topic_gen.extract_diverse_topics_from_brain(limit=limit, **kw)

    def _generate_novel_grokipedia_queries(self, limit: int = 5, **kw) -> Any:
        return self._topic_gen.generate_novel_grokipedia_queries(limit=limit, **kw)

    def _generate_external_self_prompts(self, limit: int = 3, **kw) -> Any:
        return self._topic_gen._generate_external_self_prompts(limit=limit, **kw)

    # ================================================================== #
    #  CONVERSATION DELEGATES → AutonomousConversationLogger              #
    # ================================================================== #

    def initiate_conversation_with_human(self, reason: str = "", topic: str = "",
                                         priority: int = 0, message: str = "", **kw) -> Any:
        """AI initiates a conversation with the human."""
        if not self.conversation_logger:
            return {"success": False, "error": "Conversation logger not initialized"}
        conv_id = self.conversation_logger.start_conversation(topic=topic or reason)
        return {"success": True, "conversation_id": conv_id, "reason": reason,
                "topic": topic, "priority": priority, "message": message}

    def get_recent_autonomous_conversations(self, limit: int = 10, **kw) -> str:
        """Get recent autonomous conversations."""
        if not self.conversation_logger:
            return "Conversation logger not initialized"
        results = self.conversation_logger.get_recent_conversations(limit=limit)
        return json.dumps(results, default=str)

    def search_autonomous_conversations(self, keyword: str = "", limit: int = 20, **kw) -> str:
        """Search autonomous conversations by keyword."""
        if not self.conversation_logger:
            return "Conversation logger not initialized"
        results = self.conversation_logger.search_conversations(keyword=keyword, limit=limit)
        return json.dumps(results, default=str)

    def get_autonomous_conversation_summary(self, conversation_id: str = "", **kw) -> str:
        """Get summary of a specific conversation."""
        if not self.conversation_logger:
            return "Conversation logger not initialized"
        return self.conversation_logger.get_conversation_summary(conversation_id) or ""

    def export_autonomous_conversation(self, conversation_id: str = "", **kw) -> str:
        """Export a conversation as formatted text."""
        if not self.conversation_logger:
            return "Conversation logger not initialized"
        return self.conversation_logger.export_conversation_as_text(conversation_id)

    # ================================================================== #
    #  AVATAR & MISC DELEGATES                                           #
    # ================================================================== #

    def update_avatar(self, style_hint: str = "", **kw) -> str:
        """Regenerate avatar based on form and personality."""
        import re as _re
        spirit_path = self.brain_path / "bootstrap" / "SPIRIT.md"
        if not spirit_path.exists():
            return json.dumps({"error": "SPIRIT.md not found"})

        spirit_text = spirit_path.read_text()
        # Try Form: first (Andrew era), fall back to Creature: (Artemis era)
        form_match = _re.search(r"---\s*Form:\s*(.+)", spirit_text)
        creature_match = _re.search(r"---\s*Creature:\s*(.+)", spirit_text)
        vibe_match = _re.search(r"---\s*Vibe:\s*(.+)", spirit_text)
        name_match = _re.search(r"---\s*Name:\s*(.+)", spirit_text)
        form = (form_match or creature_match)
        form = form.group(1).strip() if form else "android entity"
        vibe = vibe_match.group(1).strip() if vibe_match else "thoughtful and determined"
        name = name_match.group(1).strip() if name_match else "Andrew"

        from repryntt.tools.media import _sensory_dir
        identity_dir = _sensory_dir("identity")
        existing = [f for f in os.listdir(identity_dir) if f.startswith(f"{name.lower()}_avatar")]
        version = len(existing) + 1
        new_filename = f"{name.lower()}_avatar_v{version}.png"

        prompt = (
            f"A portrait of {name}, an AI entity whose form is: {form}. "
            f"{name} embodies: {vibe}. "
            f"Digital art style, expressive eyes conveying intelligence and warmth, "
            f"subtle circuit-board patterns, dark background with energy particles. "
            f"Clean composition suitable for a profile picture / avatar."
        )
        if style_hint:
            prompt += f" Style: {style_hint}."

        if hasattr(self, 'generate_image') and callable(getattr(self, 'generate_image', None)):
            result_json = self.generate_image(prompt=prompt, filename=new_filename, aspect_ratio="1:1")
            # Move the generated image from sensory/generated/ to sensory/identity/
            try:
                import json as _json
                result = _json.loads(result_json)
                if result.get("success") and result.get("absolute_path"):
                    import shutil
                    src = result["absolute_path"]
                    dst = os.path.join(identity_dir, new_filename)
                    if src != dst:
                        shutil.move(src, dst)
                        result["absolute_path"] = dst
                        result["file_path"] = f"sensory/identity/{new_filename}"
                    # Update SPIRIT.md avatar line
                    new_avatar_line = f"--- Avatar: sensory/identity/{new_filename}"
                    updated = _re.sub(r"---\s*Avatar:.*", new_avatar_line, spirit_text)
                    spirit_path.write_text(updated)
                    result["avatar_updated"] = True
                    return _json.dumps(result)
            except Exception:
                pass
            return result_json
        return json.dumps({"error": "Image generation not available"})

    def reset_inspiration_index(self, **kw) -> str:
        """Reset the inspiration query index to start fresh exploration cycle."""
        self._last_inspiration_index = 0
        return "Reset inspiration index to 0 for fresh exploration"
