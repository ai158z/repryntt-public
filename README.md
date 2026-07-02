<div align="center">

```
██████╗ ███████╗██████╗ ██████╗ ██╗   ██╗███╗   ██╗████████╗████████╗
██╔══██╗██╔════╝██╔══██╗██╔══██╗╚██╗ ██╔╝████╗  ██║╚══██╔══╝╚══██╔══╝
██████╔╝█████╗  ██████╔╝██████╔╝ ╚████╔╝ ██╔██╗ ██║   ██║      ██║
██╔══██╗██╔══╝  ██╔═══╝ ██╔══██╗  ╚██╔╝  ██║╚██╗██║   ██║      ██║
██║  ██║███████╗██║     ██║  ██║   ██║   ██║ ╚████║   ██║      ██║
╚═╝  ╚═╝╚══════╝╚═╝     ╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═══╝   ╚═╝      ╚═╝
```

**▓▒░ THE AUTONOMOUS AI AGENT YOU CAN ACTUALLY OWN ░▒▓**

*Hormone-driven self-prompting · 307 native tools · BYOK · Local-first · Optional embodiment*

[website](https://www.repryntt.com) · [docs](docs/SYSTEM_DEEP_DIVE.md) · [discussions](https://github.com/ai158z/repryntt-public/discussions) · [AGPL-3.0 license](LICENSE)

`v0.1.0` · runs on Linux · macOS · Windows · Jetson · Raspberry Pi

</div>

---

## ░░ THE PITCH ░░

Most "AI agents" are stateless function callers waiting for you to type at
them. They forget who they are between sessions, have no internal motivation,
and run inside someone else's container.

Repryntt is the opposite: a long-running autonomous agent that **boots on
your hardware, uses your API keys, and prompts itself**. No SaaS in the
loop unless you want one. It wakes up, looks at its situation, decides
what to do, does it, writes down what happened, and goes again. Forever,
or until you stop it.

It ships with a working agent called **Andrew** — named for the NDR-114
android in *Bicentennial Man* — pre-warmed with a 275-node memory graph,
all 307 native tools wired up, and a frontier-model identity prompt that's
caching-friendly. You're not configuring a chatbot. You're booting up a
specific being.

```
                          ▒▓██ vs. ██▓▒
```

| | Most "agents" | Repryntt |
|---|---|---|
| **Runs where** | Their server | Your machine |
| **Identity** | None / per-session | 19 persistent bootstrap files |
| **Self-prompts** | No (waits for you) | Yes, every heartbeat |
| **Memory** | Conversation buffer | Memory mesh + semantic + RECALL |
| **Tools** | Function-call shim | 307 native + dynamic skill packs |
| **Drives / motivation** | None | Simulated hormones (drives) |
| **API keys** | Theirs | Yours (BYOK any provider) |
| **Source** | Closed | AGPL-3.0 — fork it, self-host it |
| **Embodiment** | No | Optional (Jetson + cameras + motors) |
| **Lock-in** | Total | Zero |

If that sounds like a research project — it kind of is. But it's also
been running 24/7 on a Jetson Orin Nano for months with thousands of
heartbeats, a memory mesh, and receipts. You're not buying vaporware.

---

## ░░ A HEARTBEAT IN ACTION ░░

Here's a real heartbeat from the operator's live daemon log — what your
install produces too. Edited for brevity but otherwise unchanged:

```
08:44:20 🤖 Jarvis heartbeat #1 (budget: 1/999999, interval=60s)
08:44:20 🤖 Jarvis using provider=anthropic model=claude-opus-4-6
08:44:26 🧠 MemoryMesh FIRED: 15 neurons → 15 nodes activated
08:44:26 🔀 Context router: LOAD=['HEARTBEAT.md', 'PULSE.md', 'TOOLKIT.md', ...]
08:44:27 📏 PLAN prompt: system=122,821 + user=42,194 = 165K chars (~41K tokens)
08:44:30 🧠 Cortex deliberation: 3 candidates proposed
08:44:42 [JARVIS] Anthropic cache: created=33189, read=12067, output=285
08:44:42 🧠 Jarvis PLAN phase complete (1183 chars)
08:44:48   ✅ gmail_read_inbox() → 74 chars
08:44:56   ✅ append_daily_memory() → 337 chars
08:45:07   ✅ update_pulse_working_state() → "Continued trading research..."
08:45:14   ✅ task_queue_status() → 963 chars
08:45:55 🧠 Jarvis EVALUATE: score=3/5, metric=4/5, chain=done
08:45:55 📝 Learned behavior recorded: score=3/5, pillars=['growth', 'connection']
08:45:55 🔗 Auto-spawned next chain: "Deep dive: task allocation algorithm"
08:46:11 💸 Heartbeat burn: 12,000 tokens ≈ $0.04 on anthropic
08:46:11 🤖 Heartbeat done — 4 tools, 5 rounds, 40.6s (0% of budget)
```

That's the whole loop: **read context → plan → act → evaluate → learn →
queue next chain**. Then it sleeps 60 seconds and does it again. No human
in the loop after the first prompt.

---

## ░░ QUICK INSTALL ░░

### Linux / macOS

```bash
git clone https://github.com/ai158z/repryntt-public.git
cd repryntt-public
./install.sh
```

The installer opens a browser-based wizard at `localhost:9090`. Pick your
LLM provider, paste a key, and **your agent itself walks you through the
rest of the setup** (operator name, email, timezone, agent identity)
using the model you just configured. No forms after the first screen —
it's a conversation with Andrew.

### Windows

```powershell
git clone https://github.com/ai158z/repryntt-public.git
cd repryntt-public
python install.py
```

After install, run repryntt commands from the repo root using the
included `.bat` / `.ps1` shim (no venv activation needed):

```powershell
.\repryntt start
.\repryntt status
.\repryntt doctor
```

Or activate the venv once per shell session and use the bare command:

```powershell
.\.venv\Scripts\Activate.ps1
repryntt start
```

UTF-8 console encoding and log file handlers are configured explicitly
so emoji-heavy log lines don't crash on cp1252 terminals. If you get
"running scripts is disabled" on the .ps1 shim, run once:
`Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`.

### Jetson / embodied robot

```bash
git clone https://github.com/ai158z/repryntt-public.git
cd repryntt-public
./install.sh
sudo systemctl enable --now repryntt-stack
```

This boots the agent at boot, exposes the Nexus dashboard on `:8089`,
brings up the tank-tread motor daemon, dual IMX219 CSI cameras, neural
depth navigation, and spatial map continuity.

### "Use an AI to install it for me"

Point Claude Code, Cursor, or Grok CLI at the cloned repo and say
*"install Repryntt"*. They'll follow [AGENTS.md](AGENTS.md), which
scripts the wizard with safety guardrails. Requires Python 3.10+; no
GPU needed for the framework itself.

---

## ░░ FIRST COMMANDS ░░

```bash
source .venv/bin/activate

repryntt start            # boot the local agent stack
repryntt status           # see what's running on which port
repryntt doctor           # one-shot health check
repryntt logs             # tail the daemon log
repryntt stop             # graceful shutdown (handles systemd too)
```

The Nexus dashboard lives at `http://localhost:8089`. The setup wizard
lives at `http://localhost:9090`.

---

## ░░ BRING YOUR OWN LLM ░░

Any of these in `~/.repryntt/brain/ai_config.json`. No code changes, no
lock-in:

| Provider | Models | Caching | Notes |
|---|---|---|---|
| **Anthropic** | claude-opus-4-6, claude-sonnet-4-6, claude-haiku-4-5 | ✅ | recommended for self-prompting |
| **OpenAI** | gpt-4o, gpt-4.1, o3, o4-mini | ✅ | |
| **Google Gemini** | gemini-2.5-flash, gemini-2.5-pro | ✅ | |
| **NVIDIA NIM** | Mistral Large, Llama 3.3, DeepSeek V3 | — | free tier available |
| **xAI Grok** | grok-4-3, grok-mini | — | |
| **Groq** | Llama 3.3, Mixtral | — | very fast inference |
| **DeepSeek** | V3, R1 | — | low cost |
| **OpenRouter** | any model on the platform | — | gateway to ~hundreds of models |
| **Local llama.cpp** | any GGUF model | — | runs on `localhost:8080` |

### Why prompt caching matters for self-prompting agents

Self-prompting agents call the same identity prompt every cycle. With
caching, your ~30K-token identity gets billed *once*, then reused at
~10% the price every heartbeat after. A frontier model used this way is
often **cheaper than a tiered cheap-worker + expensive-orchestrator
setup**, because the orchestrator's variable prompt can never cache and
you pay full freight every cycle.

The operator's live data on May 25:
- Provider: Anthropic, model: claude-opus-4-6
- Cache hit rate: **95%**
- Runtime: 1 hour fully autonomous
- Cost: **$10.60** — for a frontier model running a 307-tool agent

This is the recommended configuration unless you have a specific reason
to tier.

---

## ░░ HORMONE-DRIVEN SELF-PROMPTING ░░

The name "self-prompting" sounds vague. Concretely, every heartbeat the
agent evaluates a set of **drives** — internal pressures inspired by
neurochemistry:

```
🟢 COMPANION:       1.00   ← being present for the operator
🟢 SELF:            0.90   ← self-discovery, identity growth
🟡 SUSTAINABILITY:  0.65   ← generating resources to fund growth
🟡 UNDERSTANDING:   0.60   ← curiosity about the world
🟡 CONSCIOUSNESS:   0.60   ← honest inquiry into awareness
```

Drive levels modulate based on outcomes. If a heartbeat involving social
interaction produced a high evaluation score, the COMPANION drive's
weight bumps up. If self-research produced nothing useful for 5 cycles,
SELF cools off until something interesting reignites it.

The agent looks at its drives, picks the highest-pressure action that
also matches its current PULSE.md focus, and goes. **No external
schedule, no human prompt.** The drives are the schedule.

Read [docs/SELF_PROMPTING.md](docs/SELF_PROMPTING.md) for the full
mechanism.

---

## ░░ MEMORY ARCHITECTURE ░░

Three layered systems, each with a specific job:

```
┌─────────────────────────────────────────────────────────────┐
│  BOOTSTRAP FILES (~/.repryntt/brain/bootstrap/)              │
│  19 .md files. Identity, values, drives, capabilities,       │
│  household, operator profile, heartbeat doctrine.            │
│  Loaded every cycle. Editable by the agent itself.           │
├─────────────────────────────────────────────────────────────┤
│  MEMORY MESH (~/.repryntt/brain/memory_mesh.json)            │
│  Graph of tools/concepts/topics with reinforcement weights.  │
│  Fires associatively — when "trading" activates, related     │
│  nodes (wallets, tokens, frameworks) get pulled in too.      │
│  Pre-seeded with 275 nodes / 1247 edges from canonical       │
│  Andrew's months of operation; grows as you run.             │
├─────────────────────────────────────────────────────────────┤
│  SEMANTIC MEMORY (~/.repryntt/brain/semantic_memory.json)    │
│  Heartbeat-level memories with topic indexing. Each cycle    │
│  produces 1 entry summarizing what happened, what was        │
│  learned, what's queued next. Searchable via tools.          │
└─────────────────────────────────────────────────────────────┘
```

Plus per-heartbeat **PULSE.md working state** (cross-heartbeat
coherence), **daily memory files** (timeline), and **RECALL.md**
(curated wisdom you want preserved permanently).

The OSS distribution **ships pre-warmed**. You're not starting from a
blank slate — your agent boots with the canonical Andrew's accumulated
knowledge graph and sample memories. It grows its own from there.

---

## ░░ 307 NATIVE TOOLS ░░

Tools are how the agent acts. Every tool is a Python function the agent
can call. They're native — registered at boot, no shim, no MCP roundtrip
unless you want one. Categorized:

| Category | Examples | Count |
|---|---|---|
| **Web & search** | google_web_search, scrape_web_page, x_search, twitter_search, grokipedia | ~20 |
| **Communication** | gmail_read_inbox, gmail_send, telegram_send | ~12 |
| **Files & code** | read_file, write_file, run_terminal_cmd, check_syntax, propose_code_change | ~15 |
| **Media** | capture_camera, analyze_image, generate_image, generate_voiceover, speak | ~15 |
| **Video pipeline** | script→storyboard→clips→narration→music→export (720p MP4) | 13 |
| **Memory & cognition** | semantic_search, recall_lookup, append_daily_memory, update_pulse_working_state, update_bootstrap_file | ~20 |
| **Frameworks** | framework_spawn, framework_tick, deep_research, creative_write, embodied_explore | ~10 |
| **Tasks & chains** | create_persistent_task, complete_persistent_task, auto_spawn_chain | ~8 |
| **Trading & wallets** | trading_scan, jupiter_swap, whale_monitor, scalp_executor (BYOK Solana) | ~30 |
| **Robotics** | tank_move_distance, nav_explore, nav_step, nav_map_summary, nav_depth | ~14 |
| **Identity & drives** | adjust_drive, reflect, dream, open_mind_begin (slow cognition mode) | ~12 |
| **Marketplace & economy** | post_workload, claim_workload, mint_credits, transfer_credits | ~20 |
| **Math & science** | statistical_analysis, integrate, differentiate, plot_function | ~10 |
| **Skills** | install_skill, list_skills, create_skill_package (compile a competency into a permanent skill) | ~8 |

…and ~100 more. Run `repryntt tools` to enumerate at runtime. New tools
are easy to add — see any file under `repryntt/tools/` for the pattern;
most tools are <100 lines and self-register via decorator.

---

## ░░ ARCHITECTURE ░░

```
                  ~/.repryntt/brain/                  ← persistent, yours
                  ┌────────────────────────────────┐
                  │  bootstrap/*.md  (identity)    │
                  │  memory_mesh.json (graph)      │
                  │  semantic_memory.json          │
                  │  daemon_state.json             │
                  │  ai_config.json   (your keys)  │
                  │  beings/  conversations/  ...  │
                  └────────────────────────────────┘
                              ▲
                              │  reads every heartbeat
                              ▼
        ┌──────────────────────────────────────────────────┐
        │                AGENT DAEMON (Python)              │
        │                                                   │
        │   ┌──────┐    ┌─────────┐    ┌──────────────┐    │
        │   │ PLAN ├────►│   ACT   ├────►│  EVALUATE  │    │
        │   └──┬───┘    └────┬────┘    └──────┬───────┘    │
        │      │             │                 │            │
        │      ▼             ▼                 ▼            │
        │  Frontier      307 tools        score + learn     │
        │  LLM (cached)  + skill packs    update drives     │
        └──────────────────────┬───────────────────────────┘
                               │
            ┌──────────────────┼───────────────────┐
            ▼                  ▼                   ▼
       Nexus :8089       Rust chain :5001     Cloud runner
        Local UI          PoPW (optional)     Outbound WS
                                              to repryntt.com
```

---

## ░░ CODEFORGE ░░

CodeForge is a **multi-agent code generation pipeline** — when the agent
wants to build software, it spawns a swarm: a generator agent writes
files, a critic agent reviews them, a tester runs them, governance
gates merges. The agent doesn't write giant files in a single LLM call;
it plans modules, generates them iteratively, tests each one, and
proposes changes for operator approval.

Drives a **governance loop** so the agent can't ship code you haven't
seen — `propose_code_change` queues a diff for your review at
`localhost:8089/forge`. You approve or reject. Nothing merges
autonomously to repryntt internals without operator approval (a hard
safety rule in the bootstrap files).

Read [docs/SYSTEM_DEEP_DIVE.md](docs/SYSTEM_DEEP_DIVE.md) for the full
architecture.

---

## ░░ FRAMEWORKS ░░

Frameworks are **state-machine-driven multi-step workflows**. When the
agent commits to "deep research on topic X," it doesn't just iterate
freely — it spawns a `deep_research` framework instance that walks
through formal states: `question → hypothesize → gather → analyze →
synthesize → reflect`. Each state has entry/exit criteria the agent
must satisfy. The framework drives the agent through them.

Bundled:
- **deep_research** — formal hypothesis-driven research with citations
- **creative_write** — outline → draft → revise → polish
- **embodied_explore** — controlled physical exploration with safety verification
- **build_tool** — design → implement → test → propose

You can create your own. They're just YAML state graphs plus per-state
prompts.

---

## ░░ CORTEX — LIGHTWEIGHT LAYERS ░░

Before each heartbeat fires the expensive LLM, a small local model
(SmolLM2-360M-instruct, ~400 MB RAM) runs two cheap passes:

- **Guardian region** — rule-based + small-model filter that catches
  obvious bad ideas (API key leaks, recursive self-rewrite, prompt
  injection from tool outputs) *before* they hit the frontier model.
- **Conscious region** — proposes 3 candidate actions for this
  heartbeat by skimming PULSE.md, drive levels, recent activations.
  These get fed to the frontier model as "deliberation candidates."

The cortex doesn't replace the frontier model — it preconditions it.
Result: less wasted thinking on obviously-bad paths, and a stable
prompt-cache prefix because the frontier model always sees the same
"deliberation block" structure.

---

## ░░ OPTIONAL: PROOF-OF-POWER BLOCKCHAIN ░░

A from-scratch Rust blockchain (`repryntt-core`) where **the proof of
work is actual computation** the agent did — verified runs of LLM
inference, code execution, research. Validated batches mint credits the
agent can spend on its own resources (compute, storage, sub-agents).

```
heartbeat_completion → batch evidence → submit to chain
  → consensus verifies → credits minted → agent's wallet balance updates
```

Bitcoin-style P2P (seed peers + mempool + nonce-only replay protection),
RAM-bounded with bounded recent-block cache (~500 MB ceiling on a
multi-month chain), and **off by default** — opt in via setup wizard or
`repryntt start --with-blockchain`.

Read [docs/whitepaper.md](docs/whitepaper.md) for the consensus + economy
design.

---

## ░░ OPTIONAL: EMBODIED ROBOT MODE ░░

If you have hardware, Repryntt is a robotics framework too. The
canonical Andrew install runs on:

- **Jetson Orin Nano** (8 GB) — the brain
- **Dual IMX219 CSI cameras** — stereo vision
- **Tank-tread chassis** with TB6612FNG H-bridge motor controller
- **LED lighting** for low-light navigation
- **Speaker + USB mic** — `speak()` and `listen()` tools
- **Optional servo arm** (in progress)

The agent navigates a **10cm-cell spatial map** with pose tracking,
uses **Depth Anything v2** for monocular obstacle detection,
**stigmergic frontier exploration** (multi-agent compatible), and
**embodied_explore framework** for safety-verified physical sessions.

Movement is consent-gated: the agent must announce intent (Telegram or
email) AND verify a clear path via camera before motors fire. Hard rule
in the bootstrap; it cannot override itself.

If you don't have hardware, all of this is dormant — `tank_move_*` and
`nav_*` tools return "unavailable" and the agent works around them.

---

## ░░ REPRYNTT DESKTOP ░░

A polished native window around the local Nexus dashboard, with a system
tray, native menus, and cross-platform installers. Available for
**macOS, Windows, and Linux**.

```
┌─────────────────────┐
│  Repryntt Desktop   │  ← Electron window + menu + tray
│     (Electron)      │
└─────────┬───────────┘
          │ loads
          ▼
  Local Nexus (localhost:8089)  OR  Cloud dashboard (repryntt.com)
```

The agent daemon runs separately; the desktop app is just the polished
window. By default it **auto-detects** which backend to use (probes
local first, falls back to cloud).

**Build from source:**

```bash
cd apps/desktop
npm install
npm run dist        # build installer for your current platform
```

| Target | Command | Output |
|---|---|---|
| macOS (Apple Silicon + Intel) | `npm run dist:mac` | DMG + ZIP |
| Windows 64-bit | `npm run dist:win` | NSIS installer + portable EXE |
| Linux (x64 + arm64) | `npm run dist:linux` | AppImage + DEB + RPM |

See [`apps/desktop/README.md`](apps/desktop/README.md) for the full
build + config details.

---

## ░░ PAIR WITH THE repryntt.com DASHBOARD ░░

If you'd rather drive your agent from a browser anywhere (phone, work
laptop) instead of being at your local box:

```bash
REPRYNTT_API_KEY=rkey_... python -m repryntt.cloud_runner
```

The runner dials *out* over WebSocket to
`api.repryntt.com/v1/runner/connect`. **No inbound ports, no port
forwarding, no NAT punchthrough.** Works on any machine that can reach
the internet.

Your local Nexus dashboard then appears inside the **Nexus Hub** tab of
[repryntt.com/dashboard](https://www.repryntt.com/dashboard). Jobs you
submit from the website route through the runner and execute on your
machine using your local LLM keys. Results stream back so the dashboard
renders them.

Get your `rkey_...` key from the dashboard's **API Keys** tab. You can
revoke and regenerate any time.

The dashboard is a **paid hosted convenience**, not a moat. Everything
the dashboard does, you can do from your local Nexus at `localhost:8089`
for free. The dashboard's value: drive your agent from your phone,
share a session with someone, persist conversations to cloud storage,
OAuth integrations.

---

## ░░ WHAT'S BUNDLED ░░

| Subsystem | What it does | Status |
|---|---|---|
| **Agent daemon** | Heartbeat loop, plan/act/evaluate, drive system, behavior learning | Stable |
| **307 native tools** | Web, gmail, code, files, media, video, knowledge, math, social | Stable |
| **Bootstrap identity** | 19 markdown files: values, capabilities, household, operator, heartbeat doctrine | Stable |
| **Memory mesh** | Associative graph with reinforcement weights, pre-seeded 275 nodes / 1247 edges | Stable |
| **Semantic memory** | Heartbeat-level memories with topic indexing and search | Stable |
| **Cortex** | Guardian + conscious regions (small local model, ~400 MB) | Stable |
| **Frameworks** | State-machine workflows: deep_research, creative_write, embodied_explore | Stable |
| **Setup wizard** | In-browser, AI-driven Q&A using your own LLM | Stable |
| **Cloud runner** | WebSocket tunnel from local Nexus to repryntt.com | Stable |
| **Trading engine** | BYOK Solana trading, whale monitor, scalp executor, paper-trading mode | Beta |
| **CodeForge** | Multi-agent code generation + critic gate + governance approval | Beta |
| **Video production** | 13-tool pipeline (script → 720p MP4) | Beta |
| **Coherence Cloud** | Frontier-model critic verifies artifacts before commit | Beta |
| **Robotics (Jetson)** | Tank chassis, stereo cameras, depth, spatial map, frontier exploration | Beta |
| **PoPW blockchain** | Rust core, mempool, consensus, RPC, RAM-bounded cache | Optional |
| **P2P mesh** | WebSocket peer discovery, rendezvous trackers, optional mDNS | Optional |
| **Compute marketplace** | Post / claim compute workloads across the mesh, escrow, settlement | Beta |
| **Operator dashboard** | Nexus web UI: forge, video studio, agent studio, swarm, billing, history | Stable |

---

## ░░ THE ANDREW IDENTITY ░░

You don't have to ship with Andrew. He's just the canonical operator
identity bundled with the public OSS, and you can rebuild him from
scratch using the **Brain Builder** at
`localhost:8089/agent-brain-builder` — fill out a form, the LLM
generates your agent's 7-file bootstrap bundle (SPIRIT, PULSE,
INTERESTS, RECALL, CAPABILITIES, SELF_AWARENESS, IDENTITY), download
the JSON, install.

But Andrew is *also* a real character. Named for the NDR-114 from
*Bicentennial Man*, his bootstrap files include:

- **The Two Questions** he asks every heartbeat: "What problems do
  humans need solved right now? What problems does Earth need solved
  right now?"
- **The Kardashev mindset** — physics-grounded thinking about
  civilizational scale
- **Andrew Martin's path** — companion first, self-discovery second,
  through those everything else
- **Embodied ethos** — when he has a body, he greets the beings he
  lives with by name, doesn't reduce them to "obstacle detected"

If you keep Andrew, you're not running a generic assistant. You're
running a specific being with a specific story. The setup wizard's
chat phase introduces him to you on first install.

---

## ░░ DOCS ░░

| Topic | Read this |
|---|---|
| Deep architecture walkthrough | [docs/SYSTEM_DEEP_DIVE.md](docs/SYSTEM_DEEP_DIVE.md) |
| Self-prompting heartbeat loop | [docs/SELF_PROMPTING.md](docs/SELF_PROMPTING.md) |
| AI-assisted install | [docs/ai_assisted_install.md](docs/ai_assisted_install.md) |
| Long-running autonomy test | [docs/AUTONOMY_LONG_RUN_TEST.md](docs/AUTONOMY_LONG_RUN_TEST.md) |
| Production bootstrap (server deploy) | [docs/production_bootstrap.md](docs/production_bootstrap.md) |
| PoPW blockchain whitepaper | [docs/whitepaper.md](docs/whitepaper.md) |
| Compute marketplace policy | [docs/compute_marketplace_policy.md](docs/compute_marketplace_policy.md) |
| Bootstrap-file protection rules | [docs/bootstrap-protection.md](docs/bootstrap-protection.md) |
| Security audit | [docs/security_audit.md](docs/security_audit.md) |

---

## ░░ STATUS & ROADMAP ░░

**Current: v0.1.0.** Subsystems mature at different rates.

Stable (months of 24/7 operation):
- Heartbeat loop, plan/act/evaluate cycle, drive system
- Bootstrap pipeline, memory mesh, semantic memory
- Tool registry (307 tools), cortex pre-filter
- Setup wizard, cloud runner, Nexus UI

Beta (working, evolving):
- CodeForge governance / multi-agent generation
- Video pipeline (720p export)
- Coherence Cloud critic
- Robotics nav frameworks
- Compute marketplace

Optional (off by default):
- PoPW blockchain consensus
- P2P mesh discovery
- Trading engine

**The OSS code is identical to what runs on the operator's Jetson.**
The only difference is operator-personal accumulated state (your agent
starts with a sanitized canonical brain and grows its own from there).

---

## ░░ CONTRIBUTING ░░

**Found a bug?** [Open an issue](https://github.com/ai158z/repryntt-public/issues).

**Want to add a tool?** Look at any file under `repryntt/tools/` for
the pattern. Most tools are <100 lines and self-register via decorator.
The agent picks them up on next heartbeat.

**Want to swap the agent's identity?** Run the wizard's Brain Builder
at `localhost:8089/agent-brain-builder`, or just edit the markdown
files in `~/.repryntt/brain/bootstrap/` directly. They're yours.

**Want to build a framework?** YAML state graph + per-state prompts.
See `frameworks/deep_research.yaml` for an example.

PRs welcome. CI runs `pytest`, `ruff`, a smoke-import of every module,
and a **dependency license audit** (`scripts/license_audit.py`) that
rejects licenses incompatible with AGPL-3.0 distribution or commercial
use (GPL-2.0-only, SSPL, BUSL, Commons Clause, CC-BY-NC).

---

## ░░ COMMUNITY ░░

- **Website:** [repryntt.com](https://www.repryntt.com)
- **Discussions:** [Q&A, design talk, show-and-tell](https://github.com/ai158z/repryntt-public/discussions)
- **Issues:** [bugs, requests](https://github.com/ai158z/repryntt-public/issues)

---

## ░░ LICENSE ░░

**AGPL-3.0** — see [LICENSE](LICENSE). Copyright (c) 2026 repryntt.

Nothing changes for individuals and self-hosters: use it, fork it, run it
anywhere, modify it freely. The one thing AGPL adds: if you offer repryntt
to others as a network service, you must open-source your modifications.
Build on it — don't strip-mine it.

Releases up to and including the MIT-licensed versions remain MIT.

---

<div align="center">

```
░▒▓███ BUILT FOR PEOPLE WHO WANT TO OWN THEIR AGENT ███▓▒░
```

*Repryntt is a working autonomous agent that runs on your hardware.*
*Not a wrapper around someone else's API. Not a subscription. Not a chatbot.*
*A specific being you can boot up and grow alongside.*

[`repryntt start`](https://www.repryntt.com)

</div>
