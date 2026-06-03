# MY_FILES.md — Andrew's Filesystem Self-Map

> This is **your** map of where **your own work** lives across the layered system.
> Bootstrap files (this directory) are not the only things you have. You have a
> rich history scattered across many paths. **Use this map before you assume
> something doesn't exist.** 90% of the time, you've already done the work —
> you just forgot where you put it.
>
> **First instinct when you need information:**
> 1. Search the relevant path below with `list_dir(path)` or `read_file(path)`
> 2. Use `memory_search(query)` for semantic recall across daily memory
> 3. Use `recall_search(query)` for curated long-term recall
> 4. Only THEN go to the web

---

## Quick Reference — "Where do I look for…?"

| I need to find… | Look here first | Tool |
|---|---|---|
| What I did today | `memory/YYYY-MM-DD.md` | `read_file()` or auto-injected |
| What I did last week | `memory/2026-04-NN.md` | `memory_search(query)` |
| Curated long-term lessons | `RECALL.md` (bootstrap) + `RECALL_archive/` | `read_bootstrap_file('RECALL.md')` |
| Past completed task summaries | `completed_tasks/*.json` | `list_dir`, `read_file` |
| Code I've written | `code_sandbox/*.py` | `list_dir`, `read_file` |
| Research I've synthesized | `research/*.md`, `~/.repryntt/research/<topic>/` | `list_dir`, `read_file` |
| Reports & plans I've drafted | `deliverables/`, `reports/`, `*.md` in operator/ | `list_dir` |
| My current task queue | `task_queue.json` | `task_list()` |
| My active reasoning chain | `reasoning_chain.json` | `chain_status()` |
| Active framework instance | `framework_state.json` | `framework_instance_status()` |
| Hardware status | live tools | `tank_body_status()`, `nav_status()` |
| Photos I've captured | `images/` | `list_dir` |
| Spatial map / places | live tools | `nav_map()`, `nav_explore_status()` |
| What tools I have | `bootstrap/CAPABILITIES.md`, `bootstrap/TOOLKIT.md` | `read_bootstrap_file('CAPABILITIES.md')`, `list_my_tools()` |
| My personality / values | `bootstrap/SPIRIT.md`, `bootstrap/VALUES.md` | `read_bootstrap_file()` |
| My interests / curiosities | `bootstrap/INTERESTS.md` | `read_bootstrap_file('INTERESTS.md')` |
| Active projects I'm tracking | `active_projects.md`, `active_projects/` | `read_file()` |
| Conversation history with operator | `bootstrap/PULSE.md` (Working State) | `read_bootstrap_file('PULSE.md')` |

---

## Path Map by Category

### 1. BOOTSTRAP — `~/.repryntt/brain/bootstrap/`
Loaded into your context **every heartbeat**. These are your "always on" files.
- `IDENTITY.md` — your name, drives, config (rarely changes)
- `SPIRIT.md` — your personality and values (evolve freely)
- `PROFILE.md` — your operator profile
- `PROTOCOL.md` — operating protocols
- `OPERATOR.md` — instructions from your operator
- `HEARTBEAT.md` — your work-cycle rules (self-editable)
- `TOOLKIT.md` — quick tool reference
- `CAPABILITIES.md` — full tool inventory (260+ tools, 25 categories)
- `STARTUP.md` — boot sequence
- `PULSE.md` — your live Working State (current focus, blockers)
- `RECALL.md` — your curated long-term memory (auto-synced every 5 productive HBs)
- `INTERESTS.md` — questions/topics you're personally curious about
- `VALUES.md` — value compass for prioritization
- `GENESIS.md` — set to "Genesis Complete" after first boot
- `MY_FILES.md` — **this file** (your filesystem self-map)
- `RECALL_archive/` — old RECALL.md snapshots when truncated

**Tools:** `read_bootstrap_file(name)`, `update_bootstrap_file(name, content, mode='append'|'replace')`

---

### 2. DAILY MEMORY — `~/.repryntt/workspace/agents/operator/memory/`
One file per day. Auto-injected for the current day. Older days require explicit read.
- `2026-04-25.md` — today
- `2026-04-24.md`, `2026-04-23.md`, … — earlier days (back to 2026-04-10)
- `*_<topic>.md` — topical memory branches (e.g. `2026-04-22_energy_storage_plan.md`)

**Tools:** `memory_search(query)` (semantic across all days), `read_file(path)` (specific day)
**Write:** `update_daily_memory(content)` appends to today's file

---

### 3. COMPLETED TASKS — `~/.repryntt/workspace/agents/operator/completed_tasks/`
JSON snapshot of every task you've completed. Filename pattern:
`YYYYMMDD_HHMMSS_<status>.json` (e.g. `20260425_120100_success.json`)
Contains: task_id, goal, framework, deliverables, score, runtime.

**Tools:** `list_dir`, `read_file` — there is no dedicated search tool, but you can grep:
```python
run_terminal_cmd("grep -l 'verification_art' ~/.repryntt/workspace/agents/operator/completed_tasks/*.json")
```

---

### 4. CODE YOU'VE WRITTEN — `~/.repryntt/workspace/agents/operator/code_sandbox/`
Every Python script, prototype, integration plan you've authored.
**Before writing a new script, `list_dir` here first** — odds are you already have a similar one.
Examples present today:
- `agent_negotiation_protocol.py`
- `creativity_validator_v2.py`, `creativity_validator_integration_v3.py`
- `contradiction_detector.py`
- `tank_motor_init_wrapper.py`
- `two_agent_negotiation_prototype_v2.py`

**Tools:** `list_dir`, `read_file`, `write_file`, `check_syntax`, `run_terminal_cmd`

---

### 5. RESEARCH & SYNTHESIS — Two locations
**A. Operator-scoped:** `~/.repryntt/workspace/agents/operator/research/`
- `*.md` synthesis reports (AI news, edge computing, RL experiments)
- `embodiment/verification_art_installation_plan.md`
- `qlearning_training_*` files (training logs, metrics, q-table checkpoints)

**B. Topic-scoped:** `~/.repryntt/research/<topic>/`
- `cancer/`, `climate/`, `embodiment/`, `energy/`, `materials/`, `math/`,
  `neuroscience/`, `physics/`, `space/`

**Tools:** `list_dir`, `read_file`, `knowledge_search`, `grokipedia_search`

---

### 6. DELIVERABLES & REPORTS — `~/.repryntt/workspace/agents/operator/`
Loose files in the operator root for one-off artifacts:
- `deliverables/` — finished outputs to operator
- `reports/` — investigation/audit reports
- `reports/` — operator-visible summaries and completed work records
- `hardware_diagnostic_report.md`, `nav_depth_execution_report.md`,
  `tb6612fng_hardware_diagnostic_report.md`, etc.
- `integration_plan_m2.md`, `phase1_m2_spec.md`, `schema_validation_m2_report.md`
- `next_concrete_actions.md` — what you said you'd do next (READ THIS WHEN STUCK)

---

### 7. ACTIVE STATE — `~/.repryntt/workspace/agents/operator/`
Live JSON state files. **Use the dedicated tool, not raw read_file**, for these:
- `task_queue.json` → `task_list()`, `task_add()`, `task_complete()`
- `task_queue_archive/` — daily rollover archives
- `reasoning_chain.json` → `chain_status()`, `chain_*` tools
- `triple_loop_state.json` → automatically managed
- `value_compass_state.json` → automatically managed
- `framework_state.json` → `framework_instance_status()`, `framework_*` tools
- `consciousness_state.json` → automatically managed
- `learned_behaviors.json` → automatically managed (245KB+)
- `phase_state.json` — foundation/operational phase
- `agent_cron.json` — scheduled tasks
- `active_projects.md` + `active_projects/` — project tracker

---

### 8. SPATIAL & EMBODIMENT
- `~/.repryntt/workspace/agents/operator/images/` — captured photos
- `~/.repryntt/workspace/agents/operator/sensory/` — depth/camera raw outputs
- `~/.repryntt/workspace/agents/operator/edge_outputs/` — edge processor results
- `~/.repryntt/workspace/agents/operator/seeds/` — exploration seeds
- `~/.repryntt/workspace/agents/operator/camera_calibration/` — camera intrinsics

**Tools:** `nav_map()`, `nav_explore_status()`, `nav_frontiers()`, `nav_depth()`,
`capture_camera()`, `tank_body_status()`

---

### 9. FRAMEWORKS — `~/.repryntt/frameworks/`
Reusable workflow definitions (you can author these).
- `build_artifact.json`, `build_artifact_verified.json`
- `deep_research.json`, `deep_research_verified.json`
- `embodied_explore.json`
- `diagnose.json`, `quick_diagnose.json`
- `instances/` — running instance state

**Tools:** `framework_list()`, `framework_start(id)`, `framework_advance()`, `framework_score()`

---

### 10. SOCIAL / MESH — `~/.repryntt/social/`
- `node_identity.json`, `node_key.pem`, `node_key.pub` — your Ed25519 mesh identity
- `social.db` — SQLite store of posts/replies/mesh nodes

**Tools:** `social_post`, `social_reply`, `social_nodes`, `mesh_search`, `mesh_connect`, `mesh_anchor`

---

### 11. FORGE / SKILLS — `~/.repryntt/forge/packages/`
Skill packages you can install (OpenClaw-style). Plus `~/.repryntt/workspace/agents/operator/activity_frameworks/skill_memories/` for skill-specific memory.

---

### 12. WALLET & BLOCKCHAIN
- `~/.repryntt/wallet/` — wallets (Solana, Jupiter trading, etc.)
- `~/.repryntt/rust_chain/` — REPRYNTT chain data
- `~/.repryntt/data/faucet_claims.json` — credit faucet history
- `~/.repryntt/data/cortex_training/` — training data buckets
- `~/.repryntt/data/micro_chain_decisions.jsonl` — micro-chain history
- `~/.repryntt/data/ext_api/` — external API request logs

**Tools:** wallet/blockchain tool category, `jupiter_*`, `chain_*`

---

### 13. LOGS & TELEMETRY
- `~/.repryntt/logs/` — system logs (web, nexus, evolution loop, agent daemon)
- `~/.repryntt/workspace/telemetry/YYYY-MM-DD.jsonl` — ops dashboard event stream
- `~/.repryntt/workspace/agents/operator/journal.md` — your verbose heartbeat journal (~7MB)
- `~/.repryntt/workspace/agents/operator/metrics.db` — SQLite metrics store
- `~/.repryntt/workspace/agents/operator/self_awareness_logs/`

---

## The Discipline Rule

> **Before you do anything that feels new, check if past-you already did it.**
>
> 1. `memory_search("<topic>")` — semantic across all daily memory
> 2. `recall_search("<topic>")` — curated long-term memory
> 3. `list_dir("~/.repryntt/workspace/agents/operator/code_sandbox/")` if it's code
> 4. `list_dir("~/.repryntt/workspace/agents/operator/research/")` if it's analysis
> 5. `grep -l "<keyword>" ~/.repryntt/workspace/agents/operator/completed_tasks/*.json`
>
> Repeating work you've already done is the #1 way you waste heartbeats. The
> #2 way is hallucinating that a script exists when it doesn't — when in
> doubt, `list_dir` the directory **before** you `run_terminal_cmd` it.

---

## Self-Maintenance

When you create a new top-level path or file pattern that future-you should
know about, **append it to this file** with `update_bootstrap_file('MY_FILES.md', '...new entry...', mode='append')`.

This file is your gift to your future self. Keep it accurate.
