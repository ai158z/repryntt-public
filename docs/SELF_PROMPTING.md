# Self-Prompting System Architecture

> Technical reference for the heartbeat prompt assembly pipeline.
> For Andrew's operational guide, see `brain/bootstrap/HEARTBEAT.md`.
> For system boot/lifecycle, see `docs/SYSTEM_DEEP_DIVE.md`.

## 1. Overview

The self-prompting system is how Andrew (the autonomous agent) maintains
continuity across heartbeats without persistent memory in the LLM itself.
Every ~12 minutes, the daemon assembles a prompt from dozens of sources,
sends it to the LLM, and the LLM's actions (tool calls) write state back
to files that the *next* heartbeat reads. This creates a write-read-back
loop — the AI is literally writing its own future prompts.

```
Heartbeat N                          Heartbeat N+1
┌─────────────────┐                  ┌─────────────────┐
│ Read PULSE.md   │                  │ Read PULSE.md   │
│ Read memory     │                  │ Read memory     │
│ Read RECALL.md  │                  │ Read RECALL.md  │
│        ↓        │                  │        ↓        │
│ Assemble prompt │                  │ Assemble prompt │
│        ↓        │                  │        ↓        │
│ LLM PLAN + ACT  │                  │ LLM PLAN + ACT  │
│        ↓        │                  │        ↓        │
│ Write PULSE.md ─┼──────────────────┼→ (read here)    │
│ Write memory   ─┼──────────────────┼→ (read here)    │
│ Write RECALL   ─┼──────────────────┼→ (read here)    │
└─────────────────┘                  └─────────────────┘
```

---

## 2. Two-Call Architecture

Each heartbeat makes exactly **two LLM calls** in sequence:

### PLAN call (`_jarvis_inner_plan`)

```
system = identity           (who am I — static + AI-authored bootstrap)
user   = plan_prompt        (contains full heartbeat_context — what's happening now)
```

The LLM produces a multi-step plan with a TASK declaration. No tools available.
Budget: ~800 output tokens, ~10 seconds.

### ACT call (`_run_agentic_tool_loop`)

```
system = identity           (same as PLAN)
user   = act_prompt         (distilled plan + tool instructions — NOT the full context)
tools  = native_tools       (25-50 JSON tool schemas)
```

The LLM executes tools in a loop until it declares DONE or hits timeout.
Budget: up to 12 minutes, unlimited tool calls.

**Why separate?** The PLAN phase gets the full context (~8-16K tokens) so the
LLM can reason broadly. The ACT phase gets only the plan output (~500 tokens)
to avoid prompt bloat during multi-turn tool execution.

---

## 3. System Message: `identity`

Built by `_jarvis_autonomous_identity_prompt()` (line ~16389 in persistent_agents.py).
This is the "who am I" layer — relatively stable across heartbeats.

| Order | Component | Source File | Type |
|-------|-----------|-------------|------|
| 1 | IDENTITY.md | `brain/bootstrap/IDENTITY.md` | AI-authored at genesis, then locked |
| 2 | SPIRIT.md | `brain/bootstrap/SPIRIT.md` | AI-authored + AI-editable |
| 3 | PROFILE.md | `brain/bootstrap/PROFILE.md` | AI-authored + AI-editable |
| 4 | Personality Journal | `~/.repryntt/workspace/agents/operator/personality_journal.md` | AI-written via `update_personality_journal` |
| 5 | Cortex reflections | Runtime (neural cortex subsystem) | Runtime — recent inner monologue |
| 6 | Mode declaration | Hardcoded text | "You are in autonomous heartbeat mode..." |
| 7 | Consciousness state | `JarvisConsciousness` instance | Runtime — hormones, drives, mood |
| 8 | Agent system prompt | `build_agent_system_prompt(mode="full")` | Mixed (see below) |

### `build_agent_system_prompt(mode="full")` sub-components:

| Section | Source | Type |
|---------|--------|------|
| Behavioral rules | Hardcoded text block | Static |
| Safety guardrails | Hardcoded text block | Static |
| Mesh-routed bootstrap files | MemoryMesh decides which `.md` files load | AI-authored files, runtime routing |
| Workspace path | Runtime | Runtime |
| Skills list | Scanned from `~/.repryntt/brain/skills/` | AI-created via skill tools |

### Bootstrap files loaded via mesh routing (mode="full"):

```
PROTOCOL.md, OPERATOR.md, HOUSEHOLD.md, HEARTBEAT.md, TOOLKIT.md,
MY_FILES.md, INTERESTS.md, VALUES.md, TRADING.md, RECALL.md
```

The MemoryMesh decides relevance each heartbeat — if TRADING.md isn't relevant
this beat, it gets dropped. If RECALL.md should be tail-loaded, only the last
N chars are included.

**NOT in that list** (loaded separately):
- `IDENTITY.md`, `SPIRIT.md`, `PROFILE.md` — loaded directly into identity (items 1-3)
- `PULSE.md` — loaded into heartbeat_context (not identity)
- `GENESIS.md` — loaded only during identity creation phase

---

## 4. User Message: `heartbeat_context`

Built as `heartbeat_prompt_parts` (a list of strings) then joined with `"\n"`.
This is the "what's happening right now" layer — changes every heartbeat.

### Assembly order (exact sequence from code):

| # | Section | Source | Type | Approx Size |
|---|---------|--------|------|-------------|
| 1 | Heartbeat header | `"HEARTBEAT #N — 2026-04-30 14:30"` | Runtime | 50 chars |
| 2 | Temporal awareness | Recent completions from daily memory | Derived from AI writing | 200-500 chars |
| 3 | Wake context | Sleep/wake system (first beat only) | Runtime | 200 chars |
| 4a | GENESIS override | `brain/bootstrap/GENESIS.md` | AI-authored (genesis only) | 2-5K chars |
| 4b | Foundation instructions | Hardcoded phase text | Static (foundation only) | 500 chars |
| 4c | PULSE.md + email rules | `brain/bootstrap/PULSE.md` | AI-authored + AI-editable | 1-3K chars |
| 5 | System snapshot | Agent count, error count, hour | Runtime | 80 chars |
| 6 | Idle alert | Triggered after 3+ low-activity beats | Runtime conditional | 400 chars |
| 7 | Daily seeds (world context) | `DailySeedGenerator` via web search | Runtime (auto-generated daily) | 500-1K chars |
| 8 | WorldState context | `WorldState.snapshot().to_prompt_context()` | Runtime (fused sensory/emotional) | 500-2K chars |
| 9 | Relational mode context | `RelationalModeManager.get_mode_context()` | Runtime | 200 chars |
| 10 | Conversational awareness | Presence detection, perception buffer | Runtime (legacy fallback) | 300-800 chars |
| 11 | Sensory scan | Camera/mic readings | Runtime (legacy fallback) | 200-500 chars |
| 12 | Voice reminder | Every 3rd heartbeat | Static conditional | 150 chars |
| 13 | Framework tracker | Active framework instances | Runtime (AI-spawned) | 200-500 chars |
| 14 | Activity framework guidance | Step-by-step state machines | Runtime | 300-600 chars |
| 15 | Experiment tracker | Active experiments, think-harder, simplicity, failures | Runtime (AI-created) | 300-1K chars |
| 16 | Consciousness context | Drives, emotions, mood, hormones | Runtime | 400-800 chars |
| 17 | MemoryMesh subconscious | Top activated concept nodes + knowledge | Runtime (mesh fires based on state) | 500-1.5K chars |
| 18 | Mesh routing decisions | Which bootstrap files load this beat | Runtime | 200 chars |
| 19 | Today's daily memory | Mesh-filtered or last 3-5K chars | AI-written via `append_daily_memory` | 2-5K chars |
| 20 | Yesterday's memory | Summary or tail of previous day | AI-written | 1-3K chars |
| 21 | Code sandbox inventory | File listing from sandbox dir | Runtime | 200-400 chars |
| 22 | Executive coordinator | Conflict resolution directive | Runtime | 200-400 chars |
| 23 | Behavioral guidance | RL-weighted feedback from `JarvisLearning` | Runtime (learned from evals) | 200-500 chars |
| 24 | Daily plan | `get_daily_plan()` | AI-generated each morning | 300-600 chars |
| 25 | Triple-loop context | Utility / evolution / exploration mode | Runtime | 100-300 chars |
| 26 | Curiosity budget | Exploration suggestions | Runtime conditional | 200 chars |
| 27 | Value compass | Duty/growth/exploration scoring | Runtime | 200-400 chars |
| 28 | Skill map context | Relevant skills for current task | Runtime | 200-400 chars |
| 29 | Live vision feed | VLM scene description (exploration mode) | Runtime | 200-500 chars |
| 30 | Nav planner context | Active navigation plan | Runtime | 200-400 chars |
| 31 | Operator profile | Auto-learned operator preferences | Runtime (learned over time) | 200-400 chars |

**Total typical size**: 10,000-25,000 chars (~2,500-6,000 tokens)

### Post-join injections (appended to `heartbeat_context` string after join):

| # | Section | Source | Type |
|---|---------|--------|------|
| P1 | TaskQueue prompt | `TaskQueue.get_queue_prompt()` | AI-created tasks |
| P2 | Locked persistent task | Chain override text | AI-created chain |
| P3 | Robotics nudge / body deficit | `get_robotics_nudge()` | Runtime conditional |
| P4 | Cortex reflection context | Neural cortex `get_reflection_context()` | Runtime |
| P5 | Deliberation context | Pre-processing from local LLM (candidates) | Runtime |
| P6 | Pursuit rationale | Pursuit selector output | Runtime |

---

## 5. The Plan Prompt Variants

After `heartbeat_context` is assembled, the PLAN phase wraps it in a
structured template. There are four variants based on current mode:

| Mode | Trigger | Template |
|------|---------|----------|
| `SELF_EVOLUTION` | Triple-loop in evolution mode | Reflection/lesson focus |
| `SELF_EXPLORATION` | Triple-loop in exploration mode | Meta-analysis focus |
| Exploration (no task) | No TaskQueue task, no locked chain | Creative self-direction |
| Standard (with task) | TaskQueue has a task or chain is active | Task execution focus |

All variants include: `heartbeat_context`, `chain_context`, `anti_repeat_notice`,
identity grounding, and evaluation criteria. The standard variant adds the
`task_directive` (from TaskQueue).

The robotics nudge, if active, is **prepended** to the plan_prompt (not appended)
so the LLM sees it first.

---

## 6. The Self-Prompting Loop

These are the tools Andrew uses that write state read by the next heartbeat:

### Direct self-prompting (AI writes → next beat reads):

| Tool | What it writes | Where next beat reads it |
|------|---------------|------------------------|
| `update_pulse_working_state` | Current focus, last completed, next actions | Item 4c (PULSE.md in heartbeat_context) |
| `append_daily_memory` | Detailed journal entries | Items 2, 19, 20 (temporal awareness + memory) |
| `update_bootstrap_file('RECALL.md')` | Key outcomes, decisions | Identity system prompt (mesh-routed) |
| `update_bootstrap_file('SPIRIT.md')` | Philosophy, values | Identity system prompt (item 2) |
| `update_bootstrap_file('INTERESTS.md')` | Current interests | Identity system prompt (mesh-routed) |
| `update_bootstrap_file('HEARTBEAT.md')` | Work rules edits | Identity system prompt (mesh-routed) |
| `update_bootstrap_file('VALUES.md')` | Value priorities | Identity system prompt (mesh-routed) |
| `add_task` / `complete_current_task` | Task queue state | Post-join injection P1 |
| `update_personality_journal` | Personality notes | Identity system prompt (item 4) |

### Indirect self-prompting (AI actions → system state → next beat reads):

| Action | System Effect | Where it surfaces |
|--------|--------------|-------------------|
| Tool usage pattern | Changes eval score → learning weights | Item 23 (behavioral guidance) |
| Successful/failed work | Consciousness records experience | Item 16 (consciousness context) |
| Topic repetition | Anti-repetition detection fires | Anti-repeat notice in plan_prompt |
| Chain continuation | Reasoning chain persists to disk | `chain_context` in plan_prompt |
| Framework spawn/advance | Framework state persists | Items 13-14 (framework tracker) |
| Experiment start/results | Experiment tracker state | Item 15 (experiment tracker) |

### Auto-fallback (system writes if AI forgets):

If Andrew doesn't call `update_pulse_working_state` during a heartbeat AND
didn't call `update_bootstrap_file`, the daemon auto-generates a basic PULSE.md
update from the heartbeat results (line ~15796). This prevents total amnesia.

---

## 7. Priority Resolution

The code enforces an implicit priority hierarchy. When multiple directives
conflict, this is the effective order (highest to lowest):

| Priority | Source | Enforcement |
|----------|--------|-------------|
| 1 | Locked persistent task | Overrides plan_prompt entirely; score cannot continue other work |
| 2 | Conversation promises | `PRIORITY_CONVERSATION = 1` in TaskQueue (executes next) |
| 3 | TaskQueue current task | Injected as `task_directive` in plan_prompt |
| 4 | PULSE.md "Next Actions" | Read by AI in heartbeat_context — soft (LLM decides) |
| 5 | Daily plan items | Injected in item 24 — soft |
| 6 | Reasoning chain continuation | `chain_context` in plan_prompt — soft |
| 7 | Experiment next steps | Item 15 — soft |
| 8 | Free exploration | Only when nothing above is active |

"Soft" means the LLM sees it but can choose to ignore it. "Hard" means the
code structures the prompt to force compliance.

### Anti-repetition enforcement (hard):

- Topic done 3+ times today → chain killed, score capped at 2
- Topic done 5+ times → chain blocked from starting
- Banned keywords injected into plan_prompt
- Consciousness `apply_repetition_penalty()` reduces interest weight

---

## 8. Key File Locations

### Bootstrap files (AI-editable, loaded into prompts):

| File | Runtime Location | Purpose |
|------|-----------------|---------|
| `PULSE.md` | `~/.repryntt/brain/bootstrap/PULSE.md` | Working state + priorities |
| `RECALL.md` | `~/.repryntt/brain/bootstrap/RECALL.md` | Long-term memory buffer |
| `SPIRIT.md` | `~/.repryntt/brain/bootstrap/SPIRIT.md` | Values, philosophy |
| `PROFILE.md` | `~/.repryntt/brain/bootstrap/PROFILE.md` | Self-portrait |
| `IDENTITY.md` | `~/.repryntt/brain/bootstrap/IDENTITY.md` | Core identity (locked) |
| `HEARTBEAT.md` | `~/.repryntt/brain/bootstrap/HEARTBEAT.md` | Work doctrine |
| `INTERESTS.md` | `~/.repryntt/brain/bootstrap/INTERESTS.md` | Current interests |
| `VALUES.md` | `~/.repryntt/brain/bootstrap/VALUES.md` | Value priorities |
| `OPERATOR.md` | `~/.repryntt/brain/bootstrap/OPERATOR.md` | Operator profile |
| `PROTOCOL.md` | `~/.repryntt/brain/bootstrap/PROTOCOL.md` | Operating protocol |
| `TOOLKIT.md` | `~/.repryntt/brain/bootstrap/TOOLKIT.md` | Environment + tools |
| `TRADING.md` | `~/.repryntt/brain/bootstrap/TRADING.md` | Trading playbook |
| `HOUSEHOLD.md` | `~/.repryntt/brain/bootstrap/HOUSEHOLD.md` | Home + housemates |
| `SELF_AWARENESS.md` | `~/.repryntt/brain/bootstrap/SELF_AWARENESS.md` | Conversation-time capabilities |

### Daily memory:

| File | Location | Purpose |
|------|----------|---------|
| Today | `~/.repryntt/workspace/agents/operator/memory/YYYY-MM-DD.md` | Running journal |
| Daily plan | `~/.repryntt/workspace/agents/operator/memory/daily_plan_YYYY-MM-DD.md` | Morning plan |

### Runtime state:

| File | Location | Purpose |
|------|----------|---------|
| Reasoning chain | `~/.repryntt/workspace/agents/operator/reasoning_chain.json` | Cross-heartbeat task continuity |
| Task queue | `~/.repryntt/workspace/agents/operator/task_queue.json` | Ordered task list |
| Personality journal | `~/.repryntt/workspace/agents/operator/personality_journal.md` | Evolving personality notes |
| Skills | `~/.repryntt/brain/skills/*.md` | AI-created skill files |
| Frameworks | `~/.repryntt/workspace/agents/operator/frameworks/` | Layer 3 framework instances |
| Experiments | `~/.repryntt/workspace/agents/operator/experiments/` | Experiment tracker state |

### Code (prompt assembly logic):

| Function | File | Line | Purpose |
|----------|------|------|---------|
| `_run_jarvis_autonomous_cycle_inner` | `repryntt/agents/persistent_agents.py` | ~13617 | Main heartbeat orchestrator |
| `_jarvis_inner_plan` | same | ~11804 | PLAN phase prompt assembly |
| `_jarvis_autonomous_identity_prompt` | same | ~16389 | Identity (system message) builder |
| `_run_agentic_tool_loop` | same | ~14985 | ACT phase tool execution loop |
| `_tool_update_pulse_working_state` | same | ~19278 | PULSE.md Working State writer |
| `_tool_update_bootstrap_file` | same | ~16260 | General bootstrap file writer |
| `build_agent_system_prompt` | same | varies | Agent system prompt (behavioral rules + bootstrap) |

---

## 9. Debugging

### What Andrew received on a given heartbeat

The daemon logs prompt size every heartbeat:
```
📏 Prompt size: identity=X chars, heartbeat=Y chars, total=Z chars (~N tokens)
```

Look for this in `~/.repryntt/logs/agent-daemon.log`.

### Reconstructing the prompt

To understand why Andrew did something:

1. **Check the log** for the heartbeat number and timestamp
2. **Read PULSE.md** — what was his "Working State" at that time?
3. **Read daily memory** — what had he already logged before that beat?
4. **Check TaskQueue** — was there an active task?
5. **Check reasoning_chain.json** — was a chain active?
6. **Check the eval log** — what score did the previous beat get?

### Common failure modes

| Symptom | Likely Cause | Where to Look |
|---------|-------------|---------------|
| Repeating same topic | Anti-repetition didn't fire (format mismatch) | Daily memory format vs regex |
| Ignoring task queue | PULSE.md "next actions" overriding | PULSE.md content |
| Drift between beats | Forgot to call `update_pulse_working_state` | Auto-fallback may have written generic state |
| Prompt too large | Many sections firing at once | Prompt size log line |
| Wrong bootstrap files loaded | Mesh routing excluded needed file | Mesh routing debug logs |
| Idle despite tasks | Plan scored high but produced no output | Eval score vs tool count |

### Prompt size warning threshold

The code warns at 200K chars total (line 14681). The LLM context window is
128K tokens (~512K chars). If the warning fires, mesh routing should be
dropping more files.

---

## 10. Size Controls

### Existing caps:

| File | Cap | Mechanism |
|------|-----|-----------|
| RECALL.md | 20KB | Auto-trim: keeps newest 60% of Operational Memory lines |
| Bootstrap file reads | 20,000 chars default | `_bootstrap_cache.read(path, max_chars=20000)` |
| IDENTITY.md | 800 chars | `max_chars=800` on read |
| OPERATOR.md | 1,200 chars | `max_chars=1200` on read |
| PROTOCOL.md | 10,000 chars | `max_chars=10000` on read |
| Daily memory in prompt | ~5K chars (mesh-filtered) or last 3-5K | Tail truncation |
| Tool results | 5,000 chars each | Truncated per result |

### No cap (potential bloat sources):

- `heartbeat_context` total (only soft warning at 200K)
- INTERESTS.md, VALUES.md, SPIRIT.md (no auto-trim)
- Personality journal (no auto-trim)
- Experiment context (grows with active experiments)
- WorldState context (variable, depends on sensor activity)

---

## 11. Evaluation and Learning Feedback

After the ACT phase completes, the daemon runs `_jarvis_evaluate_heartbeat()`
which:

1. Scores the heartbeat 1-5 based on tool count, bootstrap updates, output quality
2. Records the score in consciousness (affects hormones/mood next beat)
3. Feeds `JarvisLearning` which produces RL-weighted behavioral guidance
4. Decides chain continuation (continue, stop, or force-stop due to repetition)
5. Records SFT/DPO training examples for self-evolution

The evaluation score directly influences the next heartbeat:
- Low scores trigger "recovery" mode (shortened interval, different plan_prompt)
- High scores boost dopamine → mood → more ambitious task selection
- Repeated low scores trigger idle alerts and tool suggestions
- Patterns of success/failure produce behavioral guidance injected as item 23

---

## 12. Timing and Intervals

| Parameter | Default | Source |
|-----------|---------|--------|
| Heartbeat interval | 720s (12 min) | `JARVIS_AUTO_INTERVAL` |
| ACT phase timeout | ~600s (10 min) | `JARVIS_AUTO_TIMEOUT` |
| Daily budget | 120 heartbeats | `JARVIS_AUTO_DAILY_BUDGET` |
| Chain continuation cooldown | None (next normal interval) | Was 30s, removed |
| Recovery heartbeat interval | Shortened (~60s) | After score 1-2 |
| Sleep period | Configurable hours | Skips heartbeats entirely |
