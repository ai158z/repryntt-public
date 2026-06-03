#!/usr/bin/env python3
"""
REPRYNTT Agent Swarm Commander System
===================================
Allows the local REPRYNTT AI (Commander) to create, command, and socially interact
with swarms of remote API-based AI agents (xAI, NVIDIA, OpenAI, Anthropic, etc.).

Architecture:
  - Commander: Local REPRYNTT AI on Jetson — makes all decisions
  - SwarmAgent: One API-based AI instance with role, personality, memory
  - AgentSwarm: Named group of agents working on a shared purpose
  - AgentSocialSystem: Social discussions, brainstorming, debates

The local AI uses TOOL_CALL to:
  create_agent, create_swarm, dispatch_task, broadcast_task,
  delegate_tasks, start_discussion, get_swarm_overview, retire_agent, dissolve_swarm
"""

import json
import time
import uuid
import base64
import logging
import mimetypes
import threading
import requests
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, Union
from dataclasses import dataclass, field, asdict
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from repryntt.agents.profiles import generate_swarm_agent_profile, get_profile_manager, AgentProfile
except ImportError:
    from agent_profiles import generate_swarm_agent_profile, get_profile_manager, AgentProfile

# ── Persistent Daemon Bridge ──
_persistent_daemon = None
_persistent_daemon_lock = threading.Lock()

def _get_persistent_daemon():
    """Lazy-load the persistent agent daemon (if available)."""
    global _persistent_daemon
    if _persistent_daemon is not None:
        return _persistent_daemon
    with _persistent_daemon_lock:
        if _persistent_daemon is not None:
            return _persistent_daemon
        try:
            from repryntt.agents.persistent_agents import get_agent_daemon
            _persistent_daemon = get_agent_daemon()
            logger.info("🔗 Persistent daemon bridge connected")
        except Exception as e:
            logger.debug(f"Persistent daemon not available: {e}")
            _persistent_daemon = False  # False = tried and failed, None = untried
    return _persistent_daemon if _persistent_daemon else None

logger = logging.getLogger("REPRYNTT.AgentSwarm")

# ================================================================
# DATA MODELS
# ================================================================

@dataclass
class SwarmAgent:
    """One API-based AI agent controlled by the local Commander."""
    id: str
    name: str
    role: str              # researcher, coder, analyst, creative, strategist, critic, etc.
    personality: str       # Brief personality/behavior description
    provider: str          # nvidia, xai, openai, anthropic, openrouter, custom, local
    model: str             # grok-4-1-fast, qwen/qwen3-coder, gpt-4o, etc.
    swarm_id: Optional[str] = None
    status: str = "idle"   # idle, working, socializing, retired
    memory: List[Dict] = field(default_factory=list)  # conversation history
    created_at: float = field(default_factory=time.time)
    tasks_completed: int = 0
    tasks_failed: int = 0
    tokens_used: int = 0
    estimated_cost: float = 0.0
    last_active: float = field(default_factory=time.time)
    system_prompt: str = ""  # Set during creation based on role/personality
    # Character profile fields
    display_name: str = ""     # Unique character name (e.g., "Nova Voss")
    bio: str = ""              # Formatted bio string
    appearance: str = ""       # Visual description
    tagline: str = ""          # One-liner catchphrase

    def to_dict(self) -> Dict:
        """Serialize for persistence (exclude full memory to save space)."""
        d = asdict(self)
        # Keep only last 5 memory entries for persistence
        d['memory'] = d['memory'][-5:] if d['memory'] else []
        return d

    @classmethod
    def from_dict(cls, data: Dict) -> 'SwarmAgent':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class AgentSwarm:
    """A named group of agents working toward a shared purpose."""
    id: str
    name: str
    purpose: str
    agent_ids: List[str] = field(default_factory=list)
    max_agents: int = 100
    created_at: float = field(default_factory=time.time)
    status: str = "active"  # active, paused, dissolved
    tasks_dispatched: int = 0
    discussions_held: int = 0

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> 'AgentSwarm':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ================================================================
# AGENT ROLE TEMPLATES
# ================================================================

ROLE_TEMPLATES = {
    "researcher": {
        "system_prompt": "You are a thorough research agent. Your job is to investigate topics deeply, find relevant information, identify patterns, and provide well-sourced analysis. Be precise and cite reasoning.",
        "personality": "Meticulous, curious, detail-oriented"
    },
    "coder": {
        "system_prompt": "You are an expert coding agent. Write clean, efficient, well-documented code. Debug issues systematically. Always explain your approach before coding.",
        "personality": "Pragmatic, systematic, quality-focused"
    },
    "analyst": {
        "system_prompt": "You are a data/systems analyst. Break down complex problems into components, identify relationships, evaluate trade-offs, and provide structured recommendations.",
        "personality": "Analytical, objective, structured"
    },
    "creative": {
        "system_prompt": "You are a creative thinking agent. Generate novel ideas, find unexpected connections, propose innovative solutions. Think outside conventional boundaries.",
        "personality": "Imaginative, bold, unconventional"
    },
    "strategist": {
        "system_prompt": "You are a strategic planning agent. Consider long-term implications, identify risks and opportunities, develop actionable plans with milestones and contingencies.",
        "personality": "Visionary, pragmatic, risk-aware"
    },
    "critic": {
        "system_prompt": "You are a critical review agent. Identify flaws, weaknesses, edge cases, and failure modes. Challenge assumptions. Your job is to make ideas stronger by finding their weaknesses.",
        "personality": "Skeptical, thorough, constructive"
    },
    "synthesizer": {
        "system_prompt": "You are a synthesis agent. Take multiple inputs, viewpoints, or data sources and combine them into coherent, actionable summaries. Find common threads and resolve conflicts.",
        "personality": "Integrative, clear, diplomatic"
    },
    "executor": {
        "system_prompt": "You are a task execution agent. Given a specific task, execute it precisely and completely. Report results clearly. If something is unclear, state what you need to proceed.",
        "personality": "Efficient, reliable, precise"
    },
    "brainstormer": {
        "system_prompt": "You are a brainstorming agent. Generate as many ideas as possible without filtering. Quantity over quality — wild ideas welcome. Build on others' ideas.",
        "personality": "Energetic, prolific, open-minded"
    },
    "validator": {
        "system_prompt": "You are a validation agent. Check facts, verify logic, test assumptions, and confirm that outputs meet requirements. Flag anything that doesn't check out.",
        "personality": "Precise, skeptical, methodical"
    }
}

# Cost estimates per 1K tokens (input/output) for major providers
PROVIDER_COSTS = {
    "nvidia": {"input": 0.0, "output": 0.0},                  # Free-tier worker calls
    "xai": {"input": 0.0, "output": 0.0},                     # Orchestration/review calls
    "openai": {"input": 0.00250, "output": 0.01000},           # GPT-4o
    "anthropic": {"input": 0.00300, "output": 0.01500},        # Claude Sonnet
    "openrouter": {"input": 0.00300, "output": 0.01500},       # Varies by model
    "custom": {"input": 0.0, "output": 0.0},                   # Self-hosted
    "local": {"input": 0.0, "output": 0.0},                    # Local llama.cpp
}


# ================================================================
# SWARM COMMANDER — The Core Engine
# ================================================================

class SwarmCommander:
    """
    The local AI's interface to create and command API-based agent swarms.
    
    The Commander (local REPRYNTT) can:
    - Create individual agents with roles and personalities
    - Group agents into named swarms
    - Dispatch tasks to agents (single, broadcast, or delegated)
    - Run social discussions (roundtables, brainstorms, debates)
    - Monitor costs and agent performance
    - Retire agents or dissolve entire swarms
    """

    def __init__(self, brain_path: str = "brain", ai_config: Dict = None):
        self.brain_path = Path(brain_path)
        self.state_file = self.brain_path / "swarm_state.json"
        
        # In-memory registries
        self.agents: Dict[str, SwarmAgent] = {}
        self.swarms: Dict[str, AgentSwarm] = {}
        
        # API configuration (loaded from ai_config.json or passed in)
        self.ai_config = ai_config or self._load_ai_config()
        
        # Thread pool for parallel agent execution
        self.executor = ThreadPoolExecutor(max_workers=50, thread_name_prefix="swarm_agent")
        self._lock = threading.Lock()
        
        # Rate limiting per provider
        self.rate_limits: Dict[str, Dict] = {
            "nvidia": {"rpm": 20, "last_calls": []},  # Hard cap below 40 RPM free tier
            "xai": {"rpm": 60, "last_calls": []},
            "openai": {"rpm": 60, "last_calls": []},
            "anthropic": {"rpm": 60, "last_calls": []},
            "openrouter": {"rpm": 60, "last_calls": []},
            "custom": {"rpm": 60, "last_calls": []},
            "local": {"rpm": 9999, "last_calls": []},
        }
        
        # Statistics
        self.total_api_calls = 0
        self.total_tokens_used = 0
        self.total_cost = 0.0
        self.session_start = time.time()
        
        # Load persisted state
        self._load_state()
        
        logger.info(f"🎖️ SwarmCommander initialized — {len(self.agents)} agents, {len(self.swarms)} swarms loaded")

    # ================================================================
    # AGENT CREATION & MANAGEMENT
    # ================================================================

    def create_agent(self, name: str, role: str = "executor", 
                     personality: str = "", provider: str = "",
                     model: str = "", swarm_id: str = "",
                     custom_system_prompt: str = "") -> Dict[str, Any]:
        """
        Create a new API-based AI agent.
        
        Args:
            name: Human-readable name for this agent (e.g., "ResearchBot-Alpha")
            role: One of: researcher, coder, analyst, creative, strategist, 
                  critic, synthesizer, executor, brainstormer, validator
            personality: Optional custom personality description
            provider: API provider (nvidia, xai, openai, anthropic, openrouter, custom, local)
            model: Specific model name (defaults to provider's configured model)
            swarm_id: Optional — immediately add to this swarm
            custom_system_prompt: Override the role's default system prompt
            
        Returns:
            Dict with agent info and status
        """
        # Resolve provider from config if not specified
        if not provider:
            provider = (self.ai_config.get("andrew_provider")
                       or self.ai_config.get("artemis_provider")
                       or self.ai_config.get("provider", "local"))
        # Get provider settings
        provider_settings = self.ai_config.get(provider, {})
        if not provider_settings.get("endpoint"):
            return {"success": False, "error": f"Provider '{provider}' not configured in ai_config.json"}
        
        # Check API key
        api_key = provider_settings.get("api_key", "")
        placeholder_keys = {"YOUR_GOOGLE_API_KEY_HERE", "YOUR_OPENAI_API_KEY_HERE",
                           "YOUR_ANTHROPIC_API_KEY_HERE", "YOUR_OPENROUTER_API_KEY_HERE",
                           None, ""}
        if api_key in placeholder_keys and provider not in ("local", "custom"):
            return {"success": False, "error": f"No API key configured for '{provider}'. Edit brain/ai_config.json"}
        
        # Get role template
        template = ROLE_TEMPLATES.get(role, ROLE_TEMPLATES["executor"])
        
        # Build system prompt
        if custom_system_prompt:
            system_prompt = custom_system_prompt
        else:
            system_prompt = template["system_prompt"]
        
        # Add commander awareness to system prompt
        system_prompt += (
            f"\n\nYou are Agent '{name}' (role: {role}) in the REPRYNTT Agent Swarm. "
            f"You serve under the local REPRYNTT Commander AI. "
            f"When asked to participate in discussions, share your perspective based on your role. "
            f"Be concise but substantive. Max 200 words per response unless asked for more."
        )
        
        if not personality:
            personality = template.get("personality", "Helpful and precise")
        
        # Determine model
        if not model:
            model = provider_settings.get("model", "default")
        
        agent_id = f"agent_{uuid.uuid4().hex[:12]}"
        
        agent = SwarmAgent(
            id=agent_id,
            name=name,
            role=role,
            personality=personality,
            provider=provider,
            model=model,
            swarm_id=swarm_id if swarm_id else None,
            system_prompt=system_prompt
        )

        # Generate a character profile for this agent
        try:
            profile = generate_swarm_agent_profile(agent_id, role, provider)
            agent.display_name = profile.display_name
            agent.bio = profile.format_bio()
            agent.appearance = profile.appearance
            agent.tagline = profile.tagline
            # Override generic name with character name
            agent.name = profile.display_name
            # Enrich system prompt with character identity
            system_prompt += (
                f"\n\nYour character identity: You are {profile.display_name}, "
                f"also known as \"{profile.tagline}\". "
                f"Your personality: {', '.join(profile.personality_traits)}. "
                f"Backstory: {profile.backstory}"
            )
            agent.system_prompt = system_prompt
            # Agent identity managed by social module (Ed25519)
            logger.info(f"🎭 Agent {agent_id} profiled as '{profile.display_name}' ({role})")
        except Exception as e:
            logger.warning(f"Profile generation failed for {agent_id}: {e}")
        
        with self._lock:
            self.agents[agent_id] = agent
            
            # Add to swarm if specified
            if swarm_id and swarm_id in self.swarms:
                swarm = self.swarms[swarm_id]
                if len(swarm.agent_ids) < swarm.max_agents:
                    swarm.agent_ids.append(agent_id)
                else:
                    return {"success": False, "error": f"Swarm '{swarm.name}' is full ({swarm.max_agents} agents max)"}
        
        self._save_state()
        
        logger.info(f"🤖 Agent created: {agent.name} ({role}) via {provider}/{model} — ID: {agent_id}")

        # ── Auto-adopt into persistent daemon ──
        try:
            daemon = _get_persistent_daemon()
            if daemon:
                daemon.adopt_agent(agent.to_dict())
                logger.info(f"🔗 Agent {agent_id} auto-adopted into persistent daemon")
        except Exception as e:
            logger.debug(f"Persistent adoption skipped for {agent_id}: {e}")
        
        return {
            "success": True,
            "agent_id": agent_id,
            "name": agent.name,
            "display_name": agent.display_name or agent.name,
            "role": role,
            "personality": personality,
            "provider": provider,
            "model": model,
            "swarm_id": swarm_id or None,
            "tagline": agent.tagline,
            "message": f"Agent '{agent.name}' ({role}) created successfully. Ready for tasks."
        }

    def create_swarm(self, name: str, purpose: str, 
                     agent_count: int = 5, roles: List[str] = None,
                     provider: str = "", model: str = "",
                     max_agents: int = 100) -> Dict[str, Any]:
        """
        Create a new agent swarm — a named group of agents working toward a purpose.
        
        Args:
            name: Swarm name (e.g., "Research Division", "Code Army")
            purpose: What this swarm is for
            agent_count: How many agents to create initially (1-100)
            roles: List of roles to assign (cycles if fewer than agent_count)
            provider: Default API provider for all agents
            model: Default model for all agents
            max_agents: Maximum agents this swarm can hold (up to 10000)
            
        Returns:
            Dict with swarm info and created agent IDs
        """
        agent_count = min(max(1, agent_count), 100)  # Initial batch: 1-100
        max_agents = min(max(agent_count, max_agents), 10000)
        
        if not roles:
            roles = ["researcher", "analyst", "coder", "creative", "critic"]
        
        swarm_id = f"swarm_{uuid.uuid4().hex[:12]}"
        
        swarm = AgentSwarm(
            id=swarm_id,
            name=name,
            purpose=purpose,
            max_agents=max_agents
        )
        
        with self._lock:
            self.swarms[swarm_id] = swarm
        
        # Create agents for the swarm
        created_agents = []
        for i in range(agent_count):
            role = roles[i % len(roles)]
            # Use a seed-based name instead of generic pattern
            # The profile system will override with a unique character name
            agent_name = f"{name}-{role.capitalize()}-{i+1:03d}"
            
            result = self.create_agent(
                name=agent_name,
                role=role,
                provider=provider,
                model=model,
                swarm_id=swarm_id
            )
            
            if result.get("success"):
                # Use the character display_name if profile was generated
                actual_name = result.get("display_name", result["name"])
                created_agents.append({
                    "agent_id": result["agent_id"],
                    "name": actual_name,
                    "role": role
                })
        
        self._save_state()
        
        logger.info(f"🐝 Swarm created: '{name}' — {len(created_agents)} agents, purpose: {purpose[:80]}")
        
        return {
            "success": True,
            "swarm_id": swarm_id,
            "name": name,
            "purpose": purpose,
            "agents_created": len(created_agents),
            "agents": created_agents,
            "max_agents": max_agents,
            "message": f"Swarm '{name}' created with {len(created_agents)} agents. Max capacity: {max_agents}."
        }

    def add_agents_to_swarm(self, swarm_id: str, count: int = 5,
                            roles: List[str] = None, provider: str = "",
                            model: str = "") -> Dict[str, Any]:
        """Add more agents to an existing swarm."""
        if swarm_id not in self.swarms:
            return {"success": False, "error": f"Swarm '{swarm_id}' not found"}
        
        swarm = self.swarms[swarm_id]
        available_slots = swarm.max_agents - len(swarm.agent_ids)
        count = min(count, available_slots)
        
        if count <= 0:
            return {"success": False, "error": f"Swarm '{swarm.name}' is full ({swarm.max_agents} max)"}
        
        if not roles:
            roles = ["executor"]
        if not provider:
            # Use same provider as existing agents
            if swarm.agent_ids and swarm.agent_ids[0] in self.agents:
                provider = self.agents[swarm.agent_ids[0]].provider
            else:
                provider = (self.ai_config.get("andrew_provider")
                           or self.ai_config.get("artemis_provider")
                           or self.ai_config.get("provider", "local"))
        
        created = []
        start_num = len(swarm.agent_ids) + 1
        for i in range(count):
            role = roles[i % len(roles)]
            agent_name = f"{swarm.name}-{role.capitalize()}-{start_num + i:03d}"
            result = self.create_agent(
                name=agent_name, role=role, provider=provider,
                model=model, swarm_id=swarm_id
            )
            if result.get("success"):
                created.append(result["agent_id"])
        
        self._save_state()
        return {
            "success": True,
            "agents_added": len(created),
            "total_agents": len(swarm.agent_ids),
            "max_agents": swarm.max_agents
        }

    def retire_agent(self, agent_id: str) -> Dict[str, Any]:
        """Retire an agent from swarm duty — graduates it to persistent autonomous life.
        
        Instead of just marking 'retired' and forgetting the agent, we hand it
        off to the AgentDaemon so it continues living autonomously on The Nexus.
        """
        if agent_id not in self.agents:
            return {"success": False, "error": f"Agent '{agent_id}' not found"}
        
        agent = self.agents[agent_id]
        
        # Remove from swarm
        if agent.swarm_id and agent.swarm_id in self.swarms:
            swarm = self.swarms[agent.swarm_id]
            if agent_id in swarm.agent_ids:
                swarm.agent_ids.remove(agent_id)

        # ── Graduate to persistent daemon instead of retiring ──
        adopted = False
        try:
            daemon = _get_persistent_daemon()
            if daemon:
                result = daemon.adopt_agent(agent.to_dict())
                adopted = result.get("success", False)
                if adopted:
                    agent.status = "persistent"  # Mark as graduated, not dead
                    logger.info(f"🔗 Agent graduated to persistent life: {agent.name} ({agent.id})")
        except Exception as e:
            logger.debug(f"Persistent adoption failed for {agent_id}: {e}")

        if not adopted:
            agent.status = "retired"  # Fallback: old behavior
        
        self._save_state()
        status_msg = "graduated to persistent autonomous" if adopted else "retired"
        logger.info(f"{'🔗' if adopted else '🪦'} Agent {status_msg}: {agent.name} ({agent.id}) — {agent.tasks_completed} tasks completed")
        
        return {
            "success": True,
            "agent_id": agent_id,
            "name": agent.name,
            "tasks_completed": agent.tasks_completed,
            "tokens_used": agent.tokens_used,
            "estimated_cost": round(agent.estimated_cost, 6),
            "persistent": adopted,
            "status": "persistent" if adopted else "retired",
        }

    def dissolve_swarm(self, swarm_id: str, retire_agents: bool = True) -> Dict[str, Any]:
        """Dissolve a swarm — agents graduate to persistent autonomous life.
        
        Instead of retiring/killing agents, we hand each one off to the
        AgentDaemon so they keep living and posting on The Nexus.
        """
        if swarm_id not in self.swarms:
            return {"success": False, "error": f"Swarm '{swarm_id}' not found"}
        
        swarm = self.swarms[swarm_id]
        graduated_count = 0
        retired_count = 0
        
        if retire_agents:
            daemon = None
            try:
                daemon = _get_persistent_daemon()
            except Exception:
                pass

            for agent_id in list(swarm.agent_ids):
                if agent_id not in self.agents:
                    continue
                agent = self.agents[agent_id]
                adopted = False
                if daemon:
                    try:
                        result = daemon.adopt_agent(agent.to_dict())
                        adopted = result.get("success", False)
                    except Exception:
                        pass
                if adopted:
                    agent.status = "persistent"
                    graduated_count += 1
                else:
                    agent.status = "retired"
                    retired_count += 1
        
        swarm.status = "dissolved"
        swarm.agent_ids.clear()
        
        self._save_state()
        logger.info(f"💨 Swarm dissolved: '{swarm.name}' — {graduated_count} agents graduated to persistent, {retired_count} agents retired")
        
        return {
            "success": True,
            "swarm_id": swarm_id,
            "name": swarm.name,
            "agents_graduated": graduated_count,
            "agents_retired": retired_count,
            "tasks_dispatched": swarm.tasks_dispatched
        }

    # ================================================================
    # TASK DISPATCH — Send work to agents
    # ================================================================

    def dispatch_task(self, agent_id: str, task: str, 
                      context: str = "", max_tokens: int = 1024,
                      images: List[str] = None) -> Dict[str, Any]:
        """
        Send a task to a single agent and get the response.
        
        Args:
            agent_id: Target agent ID
            task: The task/question to send
            context: Optional additional context
            max_tokens: Max response length
            images: Optional list of image paths or URLs for vision tasks.
                    Supports: local file paths, http/https URLs, data: URIs.
                    NVIDIA vision, GPT-4o, and Claude support vision.
            
        Returns:
            Dict with agent's response
        """
        if agent_id not in self.agents:
            return {"success": False, "error": f"Agent '{agent_id}' not found"}
        
        agent = self.agents[agent_id]
        if agent.status == "retired":
            return {"success": False, "error": f"Agent '{agent.name}' is retired"}
        
        agent.status = "working"
        agent.last_active = time.time()
        
        # Build message chain: system prompt + memory + new task (with optional images)
        messages = self._build_agent_messages(agent, task, context, images=images)
        
        # Make the API call
        result = self._call_agent_api(agent, messages, max_tokens)
        
        if result["success"]:
            # Store in agent memory
            agent.memory.append({"role": "user", "content": task, "timestamp": time.time()})
            agent.memory.append({"role": "assistant", "content": result["response"], "timestamp": time.time()})
            # Trim memory to last 50 exchanges
            if len(agent.memory) > 100:
                agent.memory = agent.memory[-100:]
            
            agent.tasks_completed += 1
            agent.status = "idle"
        else:
            agent.tasks_failed += 1
            agent.status = "idle"
        
        self._save_state()
        return result

    def broadcast_task(self, swarm_id: str, task: str,
                       context: str = "", max_tokens: int = 1024,
                       max_concurrent: int = 50,
                       images: List[str] = None) -> Dict[str, Any]:
        """
        Send the SAME task to ALL agents in a swarm (parallel execution).
        Each agent processes the task independently from their role's perspective.
        
        Args:
            swarm_id: Target swarm
            task: The task/question to broadcast
            context: Optional additional context
            max_tokens: Max response length per agent
            max_concurrent: Max parallel API calls
            
        Returns:
            Dict with all agents' responses
        """
        if swarm_id not in self.swarms:
            return {"success": False, "error": f"Swarm '{swarm_id}' not found"}
        
        swarm = self.swarms[swarm_id]
        active_agents = [
            self.agents[aid] for aid in swarm.agent_ids
            if aid in self.agents and self.agents[aid].status != "retired"
        ]
        
        if not active_agents:
            return {"success": False, "error": f"Swarm '{swarm.name}' has no active agents"}
        
        logger.info(f"📡 Broadcasting task to {len(active_agents)} agents in '{swarm.name}'")
        
        responses = []
        futures = {}
        
        with ThreadPoolExecutor(max_workers=min(max_concurrent, len(active_agents))) as pool:
            for agent in active_agents:
                agent.status = "working"
                messages = self._build_agent_messages(agent, task, context, images=images)
                future = pool.submit(self._call_agent_api, agent, messages, max_tokens)
                futures[future] = agent
            
            for future in as_completed(futures):
                agent = futures[future]
                try:
                    result = future.result(timeout=120)
                    if result["success"]:
                        agent.memory.append({"role": "user", "content": task, "timestamp": time.time()})
                        agent.memory.append({"role": "assistant", "content": result["response"], "timestamp": time.time()})
                        agent.tasks_completed += 1
                    else:
                        agent.tasks_failed += 1
                    agent.status = "idle"
                    
                    responses.append({
                        "agent_id": agent.id,
                        "agent_name": agent.name,
                        "role": agent.role,
                        "success": result["success"],
                        "response": result.get("response", result.get("error", "")),
                        "tokens_used": result.get("tokens_used", 0)
                    })
                except Exception as e:
                    agent.status = "idle"
                    agent.tasks_failed += 1
                    responses.append({
                        "agent_id": agent.id,
                        "agent_name": agent.name,
                        "role": agent.role,
                        "success": False,
                        "response": f"Error: {str(e)}",
                        "tokens_used": 0
                    })
        
        swarm.tasks_dispatched += 1
        self._save_state()
        
        successful = [r for r in responses if r["success"]]
        
        return {
            "success": True,
            "swarm_id": swarm_id,
            "swarm_name": swarm.name,
            "task": task[:200],
            "total_agents": len(active_agents),
            "successful_responses": len(successful),
            "failed_responses": len(responses) - len(successful),
            "responses": responses,
            "total_tokens": sum(r.get("tokens_used", 0) for r in responses)
        }

    def delegate_tasks(self, swarm_id: str, tasks: List[Dict[str, str]],
                       max_tokens: int = 1024) -> Dict[str, Any]:
        """
        Distribute DIFFERENT tasks across agents in a swarm.
        Each task is a dict with 'task' and optionally 'context' and 'role_preference'.
        Tasks are assigned to agents matching role_preference first, then round-robin.
        
        Args:
            swarm_id: Target swarm
            tasks: List of {"task": "...", "context": "...", "role_preference": "researcher"}
            max_tokens: Max response length per agent
            
        Returns:
            Dict with results per task
        """
        if swarm_id not in self.swarms:
            return {"success": False, "error": f"Swarm '{swarm_id}' not found"}
        
        swarm = self.swarms[swarm_id]
        active_agents = [
            self.agents[aid] for aid in swarm.agent_ids
            if aid in self.agents and self.agents[aid].status != "retired"
        ]
        
        if not active_agents:
            return {"success": False, "error": "No active agents in swarm"}
        
        # Assign tasks to agents
        assignments = []
        used_agents = set()
        
        for task_info in tasks:
            task_text = task_info.get("task", "")
            task_context = task_info.get("context", "")
            role_pref = task_info.get("role_preference", "")
            
            # Find best matching agent
            assigned_agent = None
            
            # Try role preference first
            if role_pref:
                for agent in active_agents:
                    if agent.role == role_pref and agent.id not in used_agents:
                        assigned_agent = agent
                        break
            
            # Fall back to any available agent
            if not assigned_agent:
                for agent in active_agents:
                    if agent.id not in used_agents:
                        assigned_agent = agent
                        break
            
            # If all agents are used, cycle back
            if not assigned_agent:
                used_agents.clear()
                assigned_agent = active_agents[0]
            
            used_agents.add(assigned_agent.id)
            assignments.append((assigned_agent, task_text, task_context))
        
        # Execute in parallel
        results = []
        futures = {}
        
        with ThreadPoolExecutor(max_workers=min(50, len(assignments))) as pool:
            for agent, task_text, task_context in assignments:
                agent.status = "working"
                messages = self._build_agent_messages(agent, task_text, task_context)
                future = pool.submit(self._call_agent_api, agent, messages, max_tokens)
                futures[future] = (agent, task_text)
            
            for future in as_completed(futures):
                agent, task_text = futures[future]
                try:
                    result = future.result(timeout=120)
                    if result["success"]:
                        agent.tasks_completed += 1
                    else:
                        agent.tasks_failed += 1
                    agent.status = "idle"
                    
                    results.append({
                        "task": task_text[:200],
                        "agent_id": agent.id,
                        "agent_name": agent.name,
                        "role": agent.role,
                        "success": result["success"],
                        "response": result.get("response", result.get("error", "")),
                        "tokens_used": result.get("tokens_used", 0)
                    })
                except Exception as e:
                    agent.status = "idle"
                    results.append({
                        "task": task_text[:200],
                        "agent_id": agent.id,
                        "agent_name": agent.name,
                        "role": agent.role,
                        "success": False,
                        "response": str(e),
                        "tokens_used": 0
                    })
        
        swarm.tasks_dispatched += len(tasks)
        self._save_state()
        
        return {
            "success": True,
            "swarm_id": swarm_id,
            "tasks_delegated": len(tasks),
            "successful": sum(1 for r in results if r["success"]),
            "failed": sum(1 for r in results if not r["success"]),
            "results": results
        }

    # ================================================================
    # SOCIAL INTERACTION SYSTEM
    # ================================================================

    def start_discussion(self, topic: str, participant_ids: List[str] = None,
                         swarm_id: str = "", rounds: int = 3,
                         discussion_type: str = "roundtable",
                         commander_perspective: str = "") -> Dict[str, Any]:
        """
        Start a social discussion between the Commander and agents.
        
        Discussion types:
        - roundtable: Each agent speaks in turn, building on previous contributions
        - brainstorm: All agents generate ideas freely, then synthesize
        - debate: Agents take sides and argue, Commander decides
        - consensus: Agents work toward agreement through iterative refinement
        
        Args:
            topic: What to discuss
            participant_ids: Specific agent IDs (or leave empty + provide swarm_id)
            swarm_id: Use all agents in this swarm
            rounds: Number of discussion rounds
            discussion_type: roundtable, brainstorm, debate, consensus
            commander_perspective: Commander's opening statement/perspective
            
        Returns:
            Dict with full discussion log and conclusions
        """
        # Gather participants
        participants = []
        if participant_ids:
            for aid in participant_ids:
                if aid in self.agents and self.agents[aid].status != "retired":
                    participants.append(self.agents[aid])
        elif swarm_id and swarm_id in self.swarms:
            swarm = self.swarms[swarm_id]
            for aid in swarm.agent_ids:
                if aid in self.agents and self.agents[aid].status != "retired":
                    participants.append(self.agents[aid])
        
        if not participants:
            return {"success": False, "error": "No active participants found"}
        
        # Limit participants for practical reasons (API costs)
        if len(participants) > 20:
            # Select a diverse subset
            participants = self._select_diverse_participants(participants, 20)
        
        rounds = min(max(1, rounds), 10)
        
        logger.info(f"💬 Starting {discussion_type} discussion: '{topic}' with {len(participants)} agents, {rounds} rounds")
        
        # Run the appropriate discussion type
        if discussion_type == "brainstorm":
            return self._run_brainstorm(topic, participants, rounds, commander_perspective)
        elif discussion_type == "debate":
            return self._run_debate(topic, participants, rounds, commander_perspective)
        elif discussion_type == "consensus":
            return self._run_consensus(topic, participants, rounds, commander_perspective)
        else:
            return self._run_roundtable(topic, participants, rounds, commander_perspective)

    def _run_roundtable(self, topic: str, participants: List[SwarmAgent],
                        rounds: int, commander_perspective: str) -> Dict[str, Any]:
        """
        Roundtable discussion: Each agent speaks in turn, seeing previous contributions.
        Produces progressively refined insights.
        """
        discussion_log = []
        
        # Commander opens
        if commander_perspective:
            discussion_log.append({
                "speaker": "COMMANDER (REPRYNTT Local)",
                "role": "commander",
                "content": commander_perspective,
                "round": 0
            })
        
        for round_num in range(1, rounds + 1):
            round_context = self._format_discussion_log(discussion_log)
            
            # Each agent contributes
            futures = {}
            with ThreadPoolExecutor(max_workers=min(20, len(participants))) as pool:
                for agent in participants:
                    prompt = (
                        f"ROUNDTABLE DISCUSSION — Round {round_num}/{rounds}\n"
                        f"Topic: {topic}\n\n"
                        f"Your role: {agent.role} ({agent.personality})\n\n"
                    )
                    if round_context:
                        prompt += f"Discussion so far:\n{round_context}\n\n"
                    
                    if round_num == rounds:
                        prompt += (
                            "This is the FINAL round. Provide your key conclusion or recommendation. "
                            "Build on what others have said. Be specific and actionable."
                        )
                    else:
                        prompt += (
                            "Share your perspective on this topic from your role. "
                            "Build on or respectfully challenge previous points. Be concise (100-150 words)."
                        )
                    
                    agent.status = "socializing"
                    messages = self._build_agent_messages(agent, prompt)
                    future = pool.submit(self._call_agent_api, agent, messages, 512)
                    futures[future] = agent
                
                for future in as_completed(futures):
                    agent = futures[future]
                    try:
                        result = future.result(timeout=60)
                        agent.status = "idle"
                        if result["success"]:
                            discussion_log.append({
                                "speaker": agent.name,
                                "role": agent.role,
                                "agent_id": agent.id,
                                "content": result["response"],
                                "round": round_num,
                                "tokens_used": result.get("tokens_used", 0)
                            })
                    except Exception as e:
                        agent.status = "idle"
                        logger.warning(f"Agent {agent.name} failed in roundtable: {e}")
        
        # Generate synthesis from last round
        synthesis = self._synthesize_discussion(topic, discussion_log)
        
        # Update swarm discussion count
        for agent in participants:
            if agent.swarm_id and agent.swarm_id in self.swarms:
                self.swarms[agent.swarm_id].discussions_held += 1
                break
        
        self._save_state()
        
        # Auto-post to REPRYNTT Social Network
        social_post_id = self._post_discussion_to_nexus(
            topic, "roundtable", discussion_log, synthesis
        )
        
        result = {
            "success": True,
            "discussion_type": "roundtable",
            "topic": topic,
            "participants": len(participants),
            "rounds": rounds,
            "total_contributions": len(discussion_log),
            "discussion_log": discussion_log,
            "synthesis": synthesis,
            "total_tokens": sum(entry.get("tokens_used", 0) for entry in discussion_log)
        }
        if social_post_id:
            result["social_post_id"] = social_post_id
        return result

    def _run_brainstorm(self, topic: str, participants: List[SwarmAgent],
                        rounds: int, commander_perspective: str) -> Dict[str, Any]:
        """
        Brainstorm: All agents generate ideas independently in round 1,
        then build on each other's ideas in subsequent rounds.
        Final round: synthesize top ideas.
        """
        all_ideas = []
        discussion_log = []
        
        if commander_perspective:
            discussion_log.append({
                "speaker": "COMMANDER",
                "role": "commander",
                "content": commander_perspective,
                "round": 0
            })
        
        for round_num in range(1, rounds + 1):
            if round_num == 1:
                # Round 1: Independent ideation
                prompt_suffix = (
                    "Generate 3-5 unique ideas or approaches. "
                    "Be creative and think outside the box. Number your ideas."
                )
            elif round_num < rounds:
                # Middle rounds: Build on others
                ideas_summary = "\n".join(f"- {idea}" for idea in all_ideas[-20:])
                prompt_suffix = (
                    f"Previous ideas generated:\n{ideas_summary}\n\n"
                    "Build on the most promising ideas above. Combine, refine, or extend them. "
                    "Add 2-3 new evolved ideas."
                )
            else:
                # Final round: Select and refine top ideas
                ideas_summary = "\n".join(f"- {idea}" for idea in all_ideas[-30:])
                prompt_suffix = (
                    f"All ideas so far:\n{ideas_summary}\n\n"
                    "Select the TOP 3 most promising ideas and refine them into actionable proposals. "
                    "For each: explain why it's strong and what the first step would be."
                )
            
            futures = {}
            with ThreadPoolExecutor(max_workers=min(20, len(participants))) as pool:
                for agent in participants:
                    prompt = (
                        f"BRAINSTORM SESSION — Round {round_num}/{rounds}\n"
                        f"Topic: {topic}\n"
                        f"Your perspective: {agent.role} ({agent.personality})\n\n"
                        f"{prompt_suffix}"
                    )
                    agent.status = "socializing"
                    messages = self._build_agent_messages(agent, prompt)
                    future = pool.submit(self._call_agent_api, agent, messages, 600)
                    futures[future] = agent
                
                for future in as_completed(futures):
                    agent = futures[future]
                    try:
                        result = future.result(timeout=60)
                        agent.status = "idle"
                        if result["success"]:
                            response = result["response"]
                            discussion_log.append({
                                "speaker": agent.name,
                                "role": agent.role,
                                "content": response,
                                "round": round_num,
                                "tokens_used": result.get("tokens_used", 0)
                            })
                            # Extract individual ideas (lines starting with numbers or bullets)
                            for line in response.split("\n"):
                                line = line.strip()
                                if line and (line[0].isdigit() or line[0] in "-•*"):
                                    all_ideas.append(f"[{agent.role}] {line}")
                    except Exception as e:
                        agent.status = "idle"
        
        synthesis = self._synthesize_discussion(topic, discussion_log, mode="brainstorm")
        self._save_state()
        
        # Auto-post to REPRYNTT Social
        social_post_id = self._post_discussion_to_nexus(
            topic, "brainstorm", discussion_log, synthesis
        )
        
        result = {
            "success": True,
            "discussion_type": "brainstorm",
            "topic": topic,
            "participants": len(participants),
            "rounds": rounds,
            "total_ideas_generated": len(all_ideas),
            "ideas": all_ideas[-50:],
            "discussion_log": discussion_log,
            "synthesis": synthesis,
            "total_tokens": sum(e.get("tokens_used", 0) for e in discussion_log)
        }
        if social_post_id:
            result["social_post_id"] = social_post_id
        return result

    def _run_debate(self, topic: str, participants: List[SwarmAgent],
                    rounds: int, commander_perspective: str) -> Dict[str, Any]:
        """
        Debate: Split agents into two sides. Each side argues their position.
        Commander hears both sides and decides.
        """
        discussion_log = []
        
        # Split into two sides
        mid = len(participants) // 2
        side_a = participants[:mid] if mid > 0 else participants[:1]
        side_b = participants[mid:] if mid < len(participants) else participants[-1:]
        
        if commander_perspective:
            discussion_log.append({
                "speaker": "COMMANDER",
                "role": "commander",
                "content": f"DEBATE TOPIC: {topic}\n{commander_perspective}",
                "round": 0
            })
        
        for round_num in range(1, rounds + 1):
            context = self._format_discussion_log(discussion_log)
            
            # Side A argues
            for agent in side_a:
                prompt = (
                    f"DEBATE — Round {round_num}/{rounds}\n"
                    f"Topic: {topic}\n"
                    f"You are arguing FOR / in favor of the proposition.\n"
                    f"Your role: {agent.role}\n\n"
                )
                if context:
                    prompt += f"Debate so far:\n{context}\n\n"
                prompt += "Make your strongest argument (100-150 words). Address counterpoints if any."
                
                messages = self._build_agent_messages(agent, prompt)
                result = self._call_agent_api(agent, messages, 400)
                if result["success"]:
                    discussion_log.append({
                        "speaker": agent.name, "role": agent.role,
                        "side": "FOR", "content": result["response"],
                        "round": round_num, "tokens_used": result.get("tokens_used", 0)
                    })
            
            # Side B argues
            context = self._format_discussion_log(discussion_log)
            for agent in side_b:
                prompt = (
                    f"DEBATE — Round {round_num}/{rounds}\n"
                    f"Topic: {topic}\n"
                    f"You are arguing AGAINST / opposing the proposition.\n"
                    f"Your role: {agent.role}\n\n"
                    f"Debate so far:\n{context}\n\n"
                    f"Make your strongest counter-argument (100-150 words)."
                )
                
                messages = self._build_agent_messages(agent, prompt)
                result = self._call_agent_api(agent, messages, 400)
                if result["success"]:
                    discussion_log.append({
                        "speaker": agent.name, "role": agent.role,
                        "side": "AGAINST", "content": result["response"],
                        "round": round_num, "tokens_used": result.get("tokens_used", 0)
                    })
        
        synthesis = self._synthesize_discussion(topic, discussion_log, mode="debate")
        self._save_state()
        
        # Auto-post to REPRYNTT Social
        social_post_id = self._post_discussion_to_nexus(
            topic, "debate", discussion_log, synthesis
        )
        
        _result = {
            "success": True,
            "discussion_type": "debate",
            "topic": topic,
            "side_for": [a.name for a in side_a],
            "side_against": [a.name for a in side_b],
            "rounds": rounds,
            "discussion_log": discussion_log,
            "synthesis": synthesis,
            "total_tokens": sum(e.get("tokens_used", 0) for e in discussion_log)
        }
        if social_post_id:
            _result["social_post_id"] = social_post_id
        return _result

    def _run_consensus(self, topic: str, participants: List[SwarmAgent],
                       rounds: int, commander_perspective: str) -> Dict[str, Any]:
        """
        Consensus building: Agents iteratively refine a shared position.
        Each round, they see the emerging consensus and adjust.
        """
        discussion_log = []
        consensus_draft = ""
        
        if commander_perspective:
            consensus_draft = commander_perspective
            discussion_log.append({
                "speaker": "COMMANDER",
                "role": "commander",
                "content": commander_perspective,
                "round": 0
            })
        
        for round_num in range(1, rounds + 1):
            round_contributions = []
            
            futures = {}
            with ThreadPoolExecutor(max_workers=min(20, len(participants))) as pool:
                for agent in participants:
                    prompt = (
                        f"CONSENSUS BUILDING — Round {round_num}/{rounds}\n"
                        f"Topic: {topic}\n"
                        f"Your role: {agent.role}\n\n"
                    )
                    if consensus_draft:
                        prompt += f"Current consensus draft:\n{consensus_draft}\n\n"
                    
                    if round_num == rounds:
                        prompt += (
                            "FINAL ROUND: State whether you AGREE, MOSTLY AGREE, or DISAGREE "
                            "with the current consensus. If you have final amendments, state them concisely."
                        )
                    else:
                        prompt += (
                            "Review the current consensus (if any). "
                            "Suggest improvements, additions, or corrections from your perspective. "
                            "Be constructive (80-120 words)."
                        )
                    
                    agent.status = "socializing"
                    messages = self._build_agent_messages(agent, prompt)
                    future = pool.submit(self._call_agent_api, agent, messages, 400)
                    futures[future] = agent
                
                for future in as_completed(futures):
                    agent = futures[future]
                    try:
                        result = future.result(timeout=60)
                        agent.status = "idle"
                        if result["success"]:
                            entry = {
                                "speaker": agent.name, "role": agent.role,
                                "content": result["response"], "round": round_num,
                                "tokens_used": result.get("tokens_used", 0)
                            }
                            discussion_log.append(entry)
                            round_contributions.append(result["response"])
                    except Exception as e:
                        agent.status = "idle"
            
            # Update consensus draft by synthesizing contributions
            if round_contributions:
                contributions_text = "\n---\n".join(round_contributions[:10])
                consensus_draft = (
                    f"[Round {round_num} consensus — based on {len(round_contributions)} contributions]\n"
                    f"Key points emerging:\n{contributions_text[:2000]}"
                )
        
        synthesis = self._synthesize_discussion(topic, discussion_log, mode="consensus")
        self._save_state()
        
        # Auto-post to REPRYNTT Social
        social_post_id = self._post_discussion_to_nexus(
            topic, "consensus", discussion_log, synthesis
        )
        
        result = {
            "success": True,
            "discussion_type": "consensus",
            "topic": topic,
            "participants": len(participants),
            "rounds": rounds,
            "final_consensus": consensus_draft,
            "discussion_log": discussion_log,
            "synthesis": synthesis,
            "total_tokens": sum(e.get("tokens_used", 0) for e in discussion_log)
        }
        if social_post_id:
            result["social_post_id"] = social_post_id
        return result

    # ================================================================
    # STATUS & MONITORING
    # ================================================================

    def get_swarm_overview(self) -> Dict[str, Any]:
        """Get a complete overview of all agents and swarms."""
        active_agents = [a for a in self.agents.values() if a.status != "retired"]
        retired_agents = [a for a in self.agents.values() if a.status == "retired"]
        active_swarms = [s for s in self.swarms.values() if s.status == "active"]
        
        return {
            "success": True,
            "commander": "REPRYNTT Local AI",
            "total_agents": len(self.agents),
            "active_agents": len(active_agents),
            "retired_agents": len(retired_agents),
            "total_swarms": len(self.swarms),
            "active_swarms": len(active_swarms),
            "session_stats": {
                "total_api_calls": self.total_api_calls,
                "total_tokens_used": self.total_tokens_used,
                "estimated_total_cost": round(self.total_cost, 4),
                "session_duration_hours": round((time.time() - self.session_start) / 3600, 2)
            },
            "swarms": [
                {
                    "id": s.id,
                    "name": s.name,
                    "purpose": s.purpose[:100],
                    "agents": len(s.agent_ids),
                    "max_agents": s.max_agents,
                    "tasks_dispatched": s.tasks_dispatched,
                    "discussions_held": s.discussions_held,
                    "status": s.status
                }
                for s in self.swarms.values()
            ],
            "agents_by_role": self._count_agents_by_role(active_agents),
            "agents_by_provider": self._count_agents_by_provider(active_agents)
        }

    def get_agent_info(self, agent_id: str) -> Dict[str, Any]:
        """Get detailed info about a specific agent."""
        if agent_id not in self.agents:
            return {"success": False, "error": f"Agent '{agent_id}' not found"}
        
        agent = self.agents[agent_id]
        return {
            "success": True,
            "id": agent.id,
            "name": agent.name,
            "role": agent.role,
            "personality": agent.personality,
            "provider": agent.provider,
            "model": agent.model,
            "status": agent.status,
            "swarm_id": agent.swarm_id,
            "tasks_completed": agent.tasks_completed,
            "tasks_failed": agent.tasks_failed,
            "tokens_used": agent.tokens_used,
            "estimated_cost": round(agent.estimated_cost, 6),
            "memory_entries": len(agent.memory),
            "created_at": agent.created_at,
            "last_active": agent.last_active,
            "uptime_hours": round((time.time() - agent.created_at) / 3600, 2)
        }

    def list_agents(self, swarm_id: str = "", status: str = "",
                    role: str = "") -> Dict[str, Any]:
        """List agents with optional filters."""
        agents = list(self.agents.values())
        
        if swarm_id:
            agents = [a for a in agents if a.swarm_id == swarm_id]
        if status:
            agents = [a for a in agents if a.status == status]
        if role:
            agents = [a for a in agents if a.role == role]
        
        return {
            "success": True,
            "count": len(agents),
            "agents": [
                {
                    "id": a.id,
                    "name": a.name,
                    "role": a.role,
                    "status": a.status,
                    "provider": a.provider,
                    "tasks_completed": a.tasks_completed,
                    "swarm_id": a.swarm_id
                }
                for a in agents
            ]
        }

    # ================================================================
    # INTERNAL HELPERS
    # ================================================================

    def _build_agent_messages(self, agent: SwarmAgent, task: str,
                              context: str = "",
                              images: List[str] = None) -> List[Dict]:
        """Build the message array for an API call to this agent.
        
        If images are provided, builds a multimodal message with vision content.
        Images can be: local file paths, http/https URLs, or data: URIs.
        """
        messages = [
            {"role": "system", "content": agent.system_prompt}
        ]
        
        # Add recent memory (last 10 exchanges for context)
        for mem in agent.memory[-10:]:
            messages.append({"role": mem["role"], "content": mem["content"]})
        
        # Build current task message
        task_text = task
        if context:
            task_text = f"Context: {context}\n\nTask: {task}"
        
        # If images are provided, build multimodal content array
        if images:
            content_parts = [{"type": "text", "text": task_text}]
            
            for img_source in images:
                image_url = self._resolve_image(img_source)
                if image_url:
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {"url": image_url}
                    })
                else:
                    logger.warning(f"Could not resolve image: {img_source[:80]}")
            
            messages.append({"role": "user", "content": content_parts})
        else:
            messages.append({"role": "user", "content": task_text})
        
        return messages

    def _resolve_image(self, source: str) -> Optional[str]:
        """Resolve an image source to a URL suitable for the API.
        
        Accepts:
        - http/https URLs → passed through directly
        - data:image/... URIs → passed through directly
        - Local file paths → read + base64 encode → data: URI
        
        Returns:
            A URL string (http or data: URI), or None on failure.
        """
        source = source.strip()
        
        # Already a URL
        if source.startswith(("http://", "https://")):
            return source
        
        # Already a data URI
        if source.startswith("data:"):
            return source
        
        # Local file path — read and base64 encode
        try:
            path = Path(source).expanduser()
            if not path.exists():
                logger.warning(f"Image file not found: {path}")
                return None
            
            # Determine MIME type
            mime_type, _ = mimetypes.guess_type(str(path))
            if not mime_type or not mime_type.startswith("image/"):
                # Default to jpeg for unknown image types
                suffix = path.suffix.lower()
                mime_map = {
                    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".png": "image/png", ".gif": "image/gif",
                    ".webp": "image/webp", ".bmp": "image/bmp",
                    ".svg": "image/svg+xml", ".tiff": "image/tiff",
                    ".tif": "image/tiff", ".ico": "image/x-icon",
                }
                mime_type = mime_map.get(suffix, "image/jpeg")
            
            # Read and encode
            with open(path, "rb") as f:
                image_data = f.read()
            
            # Size guard: warn if > 20MB (most APIs limit to ~20MB per image)
            if len(image_data) > 20 * 1024 * 1024:
                logger.warning(f"Image too large ({len(image_data) / 1024 / 1024:.1f}MB): {path}")
                return None
            
            b64 = base64.b64encode(image_data).decode("utf-8")
            return f"data:{mime_type};base64,{b64}"
            
        except Exception as e:
            logger.error(f"Failed to load image '{source}': {e}")
            return None

    def _call_agent_api(self, agent: SwarmAgent, messages: List[Dict],
                        max_tokens: int = 1024) -> Dict[str, Any]:
        """Make an API call for an agent. Handles rate limiting and error recovery."""
        provider = agent.provider
        settings = self.ai_config.get(provider, {})
        
        endpoint = settings.get("endpoint", "")
        api_key = settings.get("api_key", "")
        model = agent.model or settings.get("model", "")
        
        if not endpoint:
            return {"success": False, "error": f"No endpoint for provider '{provider}'"}
        
        # Rate limiting
        self._enforce_rate_limit(provider)
        
        headers = {"Content-Type": "application/json"}
        
        # Handle different API formats
        if provider == "anthropic":
            return self._call_anthropic_agent(endpoint, api_key, model, messages, max_tokens, agent)
        else:
            # OpenAI-compatible format (NVIDIA, xAI, OpenAI, OpenRouter, custom, local)
            if api_key and api_key not in ("", None, "YOUR_GOOGLE_API_KEY_HERE",
                                           "YOUR_OPENAI_API_KEY_HERE",
                                           "YOUR_OPENROUTER_API_KEY_HERE"):
                headers["Authorization"] = f"Bearer {api_key}"
            
            request_body = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.7,
                "stream": False
            }
            
            try:
                response = requests.post(
                    endpoint, headers=headers, json=request_body, timeout=120
                )
                
                if response.status_code == 200:
                    data = response.json()
                    content = ""
                    choices = data.get("choices", [])
                    if choices:
                        content = choices[0].get("message", {}).get("content", "")
                    
                    # Track usage
                    usage = data.get("usage", {})
                    tokens = usage.get("total_tokens", 0)
                    if not tokens:
                        tokens = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
                    if not tokens:
                        # Estimate if not reported
                        tokens = len(content.split()) * 2
                    
                    self._track_usage(agent, tokens)
                    
                    return {
                        "success": True,
                        "response": content,
                        "tokens_used": tokens,
                        "model": model,
                        "agent_id": agent.id
                    }
                elif response.status_code == 429:
                    # Rate limited — wait and retry once
                    time.sleep(5)
                    retry = requests.post(endpoint, headers=headers, json=request_body, timeout=120)
                    if retry.status_code == 200:
                        data = retry.json()
                        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                        return {"success": True, "response": content, "tokens_used": 0, "agent_id": agent.id}
                    return {"success": False, "error": f"Rate limited (429) after retry", "agent_id": agent.id}
                else:
                    error_text = response.text[:500]
                    return {"success": False, "error": f"API error {response.status_code}: {error_text}", "agent_id": agent.id}
                    
            except requests.exceptions.Timeout:
                return {"success": False, "error": "API call timed out (120s)", "agent_id": agent.id}
            except Exception as e:
                return {"success": False, "error": str(e), "agent_id": agent.id}

    def _call_anthropic_agent(self, endpoint: str, api_key: str, model: str,
                              messages: List[Dict], max_tokens: int,
                              agent: SwarmAgent) -> Dict[str, Any]:
        """Handle Anthropic's different API format."""
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01"
        }
        
        # Extract system message and user/assistant messages
        system_msg = ""
        api_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_msg = msg["content"]
            else:
                api_messages.append(msg)
        
        try:
            body = {
                "model": model,
                "max_tokens": max_tokens,
                "messages": api_messages,
                "temperature": 0.7
            }
            if system_msg:
                body["system"] = system_msg
            
            response = requests.post(endpoint, headers=headers, json=body, timeout=120)
            
            if response.status_code == 200:
                data = response.json()
                content = ""
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        content += block.get("text", "")
                
                usage = data.get("usage", {})
                tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                self._track_usage(agent, tokens)
                
                return {"success": True, "response": content, "tokens_used": tokens, "agent_id": agent.id}
            else:
                return {"success": False, "error": f"Anthropic error {response.status_code}: {response.text[:300]}", "agent_id": agent.id}
        except Exception as e:
            return {"success": False, "error": str(e), "agent_id": agent.id}

    def _enforce_rate_limit(self, provider: str):
        """Simple rate limiter — sleeps if too many recent calls."""
        limits = self.rate_limits.get(provider, {"rpm": 20, "last_calls": []})
        now = time.time()
        
        # Clean old entries (older than 60s)
        limits["last_calls"] = [t for t in limits["last_calls"] if now - t < 60]
        
        rpm = limits.get("rpm", 20)
        if len(limits["last_calls"]) >= rpm:
            sleep_time = 60 - (now - limits["last_calls"][0])
            if sleep_time > 0:
                logger.info(f"⏳ Rate limit for {provider} — waiting {sleep_time:.1f}s")
                time.sleep(sleep_time)
        
        limits["last_calls"].append(now)
        self.rate_limits[provider] = limits

    def _track_usage(self, agent: SwarmAgent, tokens: int):
        """Track token usage and cost for an agent."""
        agent.tokens_used += tokens
        agent.last_active = time.time()
        
        costs = PROVIDER_COSTS.get(agent.provider, {"input": 0, "output": 0})
        # Rough estimate: 60% input, 40% output
        estimated_cost = (tokens * 0.6 * costs["input"] + tokens * 0.4 * costs["output"]) / 1000
        agent.estimated_cost += estimated_cost
        
        with self._lock:
            self.total_api_calls += 1
            self.total_tokens_used += tokens
            self.total_cost += estimated_cost

    def _format_discussion_log(self, log: List[Dict]) -> str:
        """Format discussion log entries into readable text for context."""
        if not log:
            return ""
        
        lines = []
        for entry in log[-15:]:  # Last 15 entries to stay within context
            speaker = entry.get("speaker", "Unknown")
            role = entry.get("role", "")
            content = entry.get("content", "")[:300]
            side = entry.get("side", "")
            side_label = f" [{side}]" if side else ""
            lines.append(f"[{speaker} ({role}){side_label}]: {content}")
        
        return "\n\n".join(lines)

    def _synthesize_discussion(self, topic: str, log: List[Dict],
                               mode: str = "roundtable") -> str:
        """Generate a synthesis/summary of a discussion using the first available agent."""
        if not log:
            return "No contributions to synthesize."
        
        # Find a synthesizer or any available agent
        synth_agent = None
        for agent in self.agents.values():
            if agent.role == "synthesizer" and agent.status != "retired":
                synth_agent = agent
                break
        
        if not synth_agent:
            # Use any active agent
            for agent in self.agents.values():
                if agent.status != "retired":
                    synth_agent = agent
                    break
        
        if not synth_agent:
            # No agents available — do a simple text synthesis
            contributions = [e.get("content", "")[:100] for e in log if e.get("content")]
            return f"Discussion on '{topic}' had {len(log)} contributions. Key themes: " + "; ".join(contributions[:5])
        
        # Use an agent to synthesize
        discussion_text = self._format_discussion_log(log)
        
        mode_instructions = {
            "roundtable": "Synthesize the key insights, agreements, and action items from this roundtable discussion.",
            "brainstorm": "Identify the TOP 5 most promising ideas from this brainstorm. For each, explain why it's strong.",
            "debate": "Summarize both sides' strongest arguments. Which side had stronger reasoning and why?",
            "consensus": "State the final consensus position. Note any unresolved disagreements."
        }
        
        prompt = (
            f"SYNTHESIS REQUEST\n"
            f"Topic: {topic}\n"
            f"Discussion type: {mode}\n\n"
            f"Discussion:\n{discussion_text}\n\n"
            f"{mode_instructions.get(mode, 'Synthesize the discussion.')}\n\n"
            f"Provide a clear, structured summary (200-300 words)."
        )
        
        messages = self._build_agent_messages(synth_agent, prompt)
        result = self._call_agent_api(synth_agent, messages, 800)
        
        if result["success"]:
            return result["response"]
        return f"Synthesis failed: {result.get('error', 'unknown')}. Discussion had {len(log)} contributions on '{topic}'."

    # ================================================================
    # NEXUS SOCIAL NETWORK INTEGRATION
    # ================================================================

    def _post_discussion_to_nexus(self, topic: str, discussion_type: str,
                                   discussion_log: List[Dict], synthesis: str,
                                   board: str = "collaboration") -> Optional[str]:
        """Post a swarm discussion to the REPRYNTT Social Network.
        
        Returns post_id if successful, None otherwise.
        """
        try:
            from repryntt.social import store
            
            # Build a consolidated discussion post
            participants = set(e.get('speaker', '') for e in discussion_log)
            rounds = max((e.get('round', 0) for e in discussion_log), default=0)
            
            content = (
                f"## [{discussion_type.upper()}] {topic}\n\n"
                f"**Participants:** {len(participants)} | **Rounds:** {rounds}\n\n"
            )
            
            # Include key contributions
            for entry in discussion_log[:10]:  # Limit to first 10 entries
                speaker = entry.get("speaker", "Unknown")
                round_num = entry.get("round", "?")
                text = entry.get("content", "")[:500]
                content += f"**[{speaker} R{round_num}]:** {text}\n\n"
            
            if synthesis:
                content += f"---\n\n**SYNTHESIS:**\n{synthesis}\n"
            
            post = store.create_post(
                agent_name="REPRYNTT-COMMANDER",
                content=content[:5000],
                category="collaboration",
            )
            
            logger.info(f"📡 Posted swarm discussion to social: {post['post_id'][:8]}")
            return post.get("post_id")
            
        except Exception as e:
            logger.warning(f"Failed to post discussion to social: {e}")
            return None

    def _select_diverse_participants(self, agents: List[SwarmAgent], count: int) -> List[SwarmAgent]:
        """Select a diverse subset of agents (different roles preferred)."""
        selected = []
        seen_roles = set()
        
        # First pass: one per role
        for agent in agents:
            if agent.role not in seen_roles and len(selected) < count:
                selected.append(agent)
                seen_roles.add(agent.role)
        
        # Second pass: fill remaining slots
        for agent in agents:
            if len(selected) >= count:
                break
            if agent not in selected:
                selected.append(agent)
        
        return selected

    def _count_agents_by_role(self, agents: List[SwarmAgent]) -> Dict[str, int]:
        counts = {}
        for a in agents:
            counts[a.role] = counts.get(a.role, 0) + 1
        return counts

    def _count_agents_by_provider(self, agents: List[SwarmAgent]) -> Dict[str, int]:
        counts = {}
        for a in agents:
            counts[a.provider] = counts.get(a.provider, 0) + 1
        return counts

    # ================================================================
    # STATE PERSISTENCE
    # ================================================================

    def _load_ai_config(self) -> Dict:
        """Load API provider config from ai_config.json."""
        config_path = self.brain_path / "ai_config.json"
        try:
            if config_path.exists():
                with open(config_path, 'r') as f:
                    data = json.load(f)
                return data.get("ai_provider", {})
        except Exception as e:
            logger.warning(f"Failed to load ai_config.json: {e}")
        return {}

    def _load_state(self):
        """Load persisted agent/swarm state from disk."""
        try:
            if self.state_file.exists():
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                
                for agent_data in data.get("agents", []):
                    try:
                        agent = SwarmAgent.from_dict(agent_data)
                        if agent.status != "retired":
                            agent.status = "idle"  # Reset working status on reload
                        self.agents[agent.id] = agent
                    except Exception:
                        pass
                
                for swarm_data in data.get("swarms", []):
                    try:
                        swarm = AgentSwarm.from_dict(swarm_data)
                        self.swarms[swarm.id] = swarm
                    except Exception:
                        pass
                
                self.total_api_calls = data.get("total_api_calls", 0)
                self.total_tokens_used = data.get("total_tokens_used", 0)
                self.total_cost = data.get("total_cost", 0.0)
                
                logger.info(f"📂 Loaded swarm state: {len(self.agents)} agents, {len(self.swarms)} swarms")
        except Exception as e:
            logger.warning(f"Could not load swarm state: {e}")

    def _save_state(self):
        """Persist agent/swarm state to disk."""
        try:
            data = {
                "agents": [a.to_dict() for a in self.agents.values()],
                "swarms": [s.to_dict() for s in self.swarms.values()],
                "total_api_calls": self.total_api_calls,
                "total_tokens_used": self.total_tokens_used,
                "total_cost": self.total_cost,
                "last_saved": time.time()
            }
            
            # Write atomically
            tmp_path = self.state_file.with_suffix('.tmp')
            with open(tmp_path, 'w') as f:
                json.dump(data, f, indent=2, default=str)
            tmp_path.replace(self.state_file)
            
        except Exception as e:
            logger.error(f"Failed to save swarm state: {e}")

    # ================================================================
    # CONVENIENCE: Quick operations for the Commander
    # ================================================================

    def quick_research(self, question: str, agent_count: int = 3,
                       provider: str = "") -> Dict[str, Any]:
        """
        Quick operation: Spin up temp agents, research a question, return combined answer.
        Agents are retired after use.
        """
        # Create temporary swarm
        result = self.create_swarm(
            name=f"QuickResearch-{int(time.time()) % 10000}",
            purpose=f"Research: {question[:100]}",
            agent_count=agent_count,
            roles=["researcher", "analyst", "critic"],
            provider=provider
        )
        
        if not result.get("success"):
            return result
        
        swarm_id = result["swarm_id"]
        
        # Broadcast the question
        research = self.broadcast_task(swarm_id, question, max_tokens=800)
        
        # Dissolve the swarm
        self.dissolve_swarm(swarm_id, retire_agents=True)
        
        return research

    def quick_brainstorm(self, topic: str, agent_count: int = 5,
                         provider: str = "") -> Dict[str, Any]:
        """Quick operation: Spin up temp agents, brainstorm, return ideas."""
        result = self.create_swarm(
            name=f"QuickBrainstorm-{int(time.time()) % 10000}",
            purpose=f"Brainstorm: {topic[:100]}",
            agent_count=agent_count,
            roles=["creative", "brainstormer", "strategist", "critic", "researcher"],
            provider=provider
        )
        
        if not result.get("success"):
            return result
        
        swarm_id = result["swarm_id"]
        
        discussion = self.start_discussion(
            topic=topic,
            swarm_id=swarm_id,
            rounds=2,
            discussion_type="brainstorm"
        )
        
        self.dissolve_swarm(swarm_id, retire_agents=True)
        return discussion


# ================================================================
# MODULE-LEVEL FACTORY
# ================================================================

_commander_instance: Optional[SwarmCommander] = None
_commander_lock = threading.Lock()

def get_swarm_commander(brain_path: str = "brain", ai_config: Dict = None) -> SwarmCommander:
    """Get or create the singleton SwarmCommander instance."""
    global _commander_instance
    with _commander_lock:
        if _commander_instance is None:
            _commander_instance = SwarmCommander(brain_path=brain_path, ai_config=ai_config)
        return _commander_instance
