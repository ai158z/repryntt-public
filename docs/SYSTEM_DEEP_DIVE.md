# repryntt System Deep Dive

## Purpose

This document is a production-grade, end-user and operator deep dive for the
repryntt platform. It explains how the system boots, how components interact,
where state lives, and how to operate and extend the platform safely.

## 1) System at a Glance

repryntt is a multi-layer autonomous AI platform composed of:

- Runtime orchestration (`repryntt start` / `repryntt stop`)
- Agent execution (`agent-daemon`)
- Continuous local loop (`evolution-loop`)
- Unified AI substrate (`ReprynttBrainSystem`)
- Native and delegated tools (`ToolRegistry`)
- Web/API control plane (Nexus)
- Rust blockchain service (`repryntt-chain`)
- Watchdog-based process and memory resilience

The system is intentionally layered. Behavior is produced by the combined effect
of queueing, reasoning chain continuity, context routing, tool constraints,
framework runtime, and evaluation/learning loops.

## 2) Startup and Lifecycle

### Command Entry

- CLI entrypoint script: `repryntt` (from `pyproject.toml`)
- Main CLI module: `repryntt/cli.py`
- Orchestrator: `repryntt/services.py`

### Startup Flow (`repryntt start`)

`ServiceManager.start_all()` executes these phases:

1. Phase 1: System initialization
2. Phase 2: Local LLM startup check/start
3. Phase 3: Core services (`web-server`, `nexus`)
4. Phase 3b: Rust blockchain health check (`repryntt-chain`)
5. Phase 4: Agent services (`evolution-loop`, `agent-daemon`, `watchdog`)
6. Phase 5: Trading pipeline (optional; disabled by default)
7. Phase 6: Health verification and endpoint summary

### Shutdown Flow (`repryntt stop`)

Stops managed service groups in reverse order:

- trading -> agents -> core -> local LLM

Important: Rust blockchain is not stopped by `repryntt stop` because it is
managed by systemd. Stop it with:

```bash
repryntt chain stop
```

### Start Flags

- `--no-llm`
- `--with-trading` (trading is off by default)
- `--no-evolution`
- `--no-blockchain`

## 3) Process Model and Responsibilities

### Managed by ServiceManager

- `web-server` (`:5000`)
- `nexus` (`:8089`)
- `evolution-loop` (background)
- `agent-daemon` (background)
- `watchdog` (background)
- local `llama.cpp` (`:8080`)

### Managed Separately (systemd)

- `repryntt-chain.service` (Rust blockchain)

Use:

```bash
repryntt chain status
repryntt chain logs
```

## 4) Data, Configuration, and Persistent State

### Base Data Root

Default runtime root:

```text
~/.repryntt/
```

Override with:

```bash
export REPRYNTT_DATA_DIR=/custom/path
```

Path resolution is centralized in `repryntt/paths.py`.

### Key Subdirectories

- `brain/` -> AI config, bootstrap docs, memory artifacts, chain/cognition files
- `workspace/` -> agent workspaces, queues, content, artifacts
- `logs/` -> service logs (`agent-daemon.log`, `nexus.log`, etc.)
- `models/` -> local model assets
- `pids/` -> service pid files
- `auth_token`, `flask_secret_key`, `jwt_secret_key` -> generated security secrets

### First Boot Initialization

`repryntt/first_run.py` handles idempotent first-run setup:

- directory scaffolding
- node identity generation
- bootstrap template installation
- default `daemon_state.json`
- auth token generation
- optional env-based `ai_config.json` generation

## 5) Brain Layer (Unified AI Substrate)

### Registration and Construction

- `repryntt/brain/bootstrap.py` registers `ReprynttBrainSystem`
- `repryntt/brain/factory.py` manages singleton access
- implementation: `repryntt/brain/brain_impl.py`

### Why It Matters

This layer unifies memory, identity, tooling, optional subsystems, and
provider configuration. All major runtime components depend on it.

### Tooling Surface

`ReprynttBrainSystem` initializes:

- Native tool registration (`register_native_tools`)
- Delegate tools for memory/personality/CoT/conversation
- Deferred MCP integration
- Prompt sync and optional subsystem adapters

Live startup currently reports:

- 303 native tools
- 40 delegate tools
- 337 total registered tools

## 6) Tool Registry and Execution Surface

### Registry

Core module: `repryntt/tools/registry.py`

Two major registration paths:

- Native registrations (`register_native_tools`): active and primary
- Delegate registrations (`register_brain_delegate_tools`): brain-bound helpers

### Category Breadth

Native tools span a large set of domains including:

- trading, social, media, web search, filesystem/code
- awareness, gmail, learning, frameworks
- codeforge/git publish, open mind, payment gateway
- task queue, robotics body control, spatial awareness, exploration

### Jarvis Tool Access Model

`persistent_agents.py` defines a curated starter tool set plus policy gates:

- starter tools for common operations
- deny list for restricted actions
- operator approval model for protected code changes
- progressive disclosure and discovery support

## 7) Agent and Reasoning Layer

### Main Runtime

Core module: `repryntt/agents/persistent_agents.py`

The daemon includes:

- persistent agent roster and scheduler
- task queue integration
- chain-of-thought and reasoning chain continuity
- PLAN -> ACT -> EVALUATE cycle
- cortex pre-filter and deliberation assists
- learning/feedback recording
- framework and memory mesh interactions

### Task Queue

Core module: `repryntt/agents/task_queue.py`

Task lifecycle:

- `queued` -> `in_progress` -> (`completed` | `failed` | `skipped`)

Priority semantics:

- lower number means higher priority
- operator tasks are highest priority (`0`)

### Reasoning Chain Continuity

Reasoning chains persist across heartbeats and can continue independently of
single-task prompt instructions. This is by design and is one of the key
"deep layers" in behavior outcomes.

Key facts:

- active chain is loaded at heartbeat start
- locked chains inject explicit override context
- chain completion and queue advancement are coupled but separate operations
- stale-chain and anti-repetition guards exist

Practical effect: operator task text alone may not dominate if higher-level
reasoning continuity or framework state still directs execution.

## 8) Evolution Loop vs Agent Daemon

repryntt runs two autonomous loops with different responsibilities:

1. `evolution-loop` (`repryntt/core/heartbeat/evolution_loop.py`)
2. `agent-daemon` (`repryntt/agents/persistent_agents.py`)

They share the broader brain/tool substrate but are distinct control loops.
For operations, treat them as separate contributors to system behavior.

## 9) AI Request Serialization

Module: `repryntt/routing/ai_queue.py`

`MasterAIQueue` provides singleton, thread-safe request serialization.
It intentionally uses one worker for llama.cpp-facing safety and predictable
LLM request ordering.

## 10) Web and API Control Plane (Nexus)

### Main App

- module: `repryntt/web/nexus_app.py`
- port: `8089`

Nexus consolidates a broad route surface and blueprint registrations.

### Notable Built-in API Groups

- daemon control (`/api/daemon/*`)
- jarvis endpoints (`/api/jarvis/*`)
- conversation/session/memory controls
- trading and commerce APIs
- hooks/integrations
- p2p status and connectivity

### Blueprint Consolidation

Nexus registers multiple blueprints at import-time, including:

- Ops dashboard (`/ops`)
- Tool API (`/tool-api`)
- External API (`/ext-api`)
- Trading (`/trading`)
- Blockchain explorer (`/chain`)
- System and agent dashboards

## 11) External API (Credit-Gated Surface)

Module: `repryntt/web/external_api.py` (mounted under `/ext-api`)

Capabilities include:

- registration/auth and wallet operations
- local faucet
- AI chat/tool/analyze endpoints
- workload marketplace submission and polling
- node config/stats endpoints
- analytics and monitoring endpoints

Security controls include API keys, wallet signature checks, JWT settings, and
rate limiting logic.

## 12) Blockchain and Economy

### Rust Chain Service

- service: `repryntt-chain.service`
- binary: `repryntt-core/target/release/repryntt_core`
- lifecycle: `repryntt chain <action>` wrappers in CLI

### Runtime Integration

- startup checks chain liveness via systemd
- web and external APIs expose wallet, pricing, and market endpoints
- tool surface includes economy and gateway operations

## 13) Resilience and Self-Healing

### Watchdog

Module: `repryntt/core/watchdog.py`

Checks every interval:

- memory pressure and optional page cache drop actions
- supervised process liveness using pid files
- log staleness (service appears alive but stuck)

Can restart supervised services when needed.

## 14) Observability and Operations

### Core Checks

```bash
repryntt doctor
repryntt status
repryntt services status
repryntt chain status
```

### Logs

```text
~/.repryntt/logs/
```

Important files typically include:

- `agent-daemon.log`
- `nexus.log`
- `saige_evolution.log`
- service-specific logs (`web-server.log`, etc.)

### Operations Dashboard

If loaded, ops telemetry is available via the `/ops` blueprint in Nexus.

## 15) Security Model (Practical)

- API key enforcement for sensitive routes
- optional wallet-signature gates for paid API actions
- JWT-based auth support for external API flows
- persisted secret key generation for Flask and JWT if missing
- operator approval gates for selected high-risk file modification tools

## 16) Extension Points

### Add a New Tool

1. Implement tool callable in the relevant module
2. Register in `register_native_tools` inside `repryntt/tools/registry.py`
3. If agent-visible on startup is required, include in starter/discovery policy

### Add a Service

1. Add `ServiceDef` entry in `repryntt/services.py`
2. Place it in proper group (`core`, `agents`, `trading`, etc.)
3. Ensure health checks and logs are coherent

### Add Web Surface

1. Create/extend blueprint module
2. Register in `nexus_app.py` blueprint registration section
3. Document route and auth/rate-limit model

## 17) Known Operational Realities

- The system is layered; behavior can be influenced by more than task queue
  entries (reasoning chains, framework state, context routing, and learning).
- `repryntt status` may initialize daemon internals for inspection if scheduler
  is not already running; this is expected behavior in this architecture.
- Rust blockchain service is independent of service-manager stop flow.

## 18) Recommended Production Runbook

1. Configure with `repryntt setup` or env vars
2. Start with `repryntt start`
3. Verify health (`doctor`, `services status`, `chain status`)
4. Monitor logs and `/ops` dashboard
5. Use `repryntt stop` for managed services and `repryntt chain stop` only when
   blockchain shutdown is intentionally required

## 19) Reference File Map

Core files for operators and developers:

- `repryntt/cli.py`
- `repryntt/services.py`
- `repryntt/first_run.py`
- `repryntt/paths.py`
- `repryntt/agents/persistent_agents.py`
- `repryntt/agents/task_queue.py`
- `repryntt/brain/bootstrap.py`
- `repryntt/brain/factory.py`
- `repryntt/brain/brain_impl.py`
- `repryntt/tools/registry.py`
- `repryntt/routing/ai_queue.py`
- `repryntt/web/nexus_app.py`
- `repryntt/web/external_api.py`
- `repryntt/core/watchdog.py`
- `repryntt/core/heartbeat/evolution_loop.py`

## 20) Final Summary

repryntt is not a single-loop chatbot service. It is a coordinated operating
stack with service orchestration, autonomous cognition loops, tool ecosystems,
web/API planes, and economic infrastructure. Reliable operation depends on
understanding layer interactions, not just a single queue or prompt.
