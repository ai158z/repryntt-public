---

# Andrew Toolkit

Skills define how tools work. This file is for your specifics — the stuff
that's unique to your setup. Use `list_my_tools` to see current tools at runtime.

## Environment

This section describes the hardware/software stack the agent is running on.
The setup wizard fills in actuals on first boot; the defaults below reflect
the canonical Andrew config (Jetson Orin Nano + dual cameras + tank body).
On other installs (laptop, desktop, server), edit this section to match
your real install.

- **OS**: Linux (Ubuntu 22.04, ARM64 aarch64 on the canonical Jetson install)
- **Python**: 3.10+ (~/saige_venv or .venv)
- **GPU**: NVIDIA Jetson Orin Nano (CUDA 12.6) — varies by install
- **Server**: Flask on port 8089
- **Cloud LLM**: configured in `~/.repryntt/brain/ai_config.json`
- **Local LLM**: llama.cpp on localhost:8080 (if enabled)

## Key Paths

- Agent workspaces: `~/.repryntt/agent_workspaces/`
- Skills: `~/.repryntt/skills/`
- Bootstrap: `bootstrap/` (in package root)
- Logs: `~/.repryntt/logs/`

## 🔒 Persistent Tasks

When you have a multi-step goal that MUST be completed — not abandoned halfway —
use persistent tasks. These lock you into a goal until you reach a definitive outcome.

- `create_persistent_task(goal, success_criteria, max_steps)` — Create a locked task.
  - `goal`: What you're trying to achieve (required)
  - `success_criteria`: How you'll know it's done (required)
  - `max_steps`: Max heartbeats to work on it (3-20, default 10)
- `complete_persistent_task(outcome, detail)` — Close a locked task with a result.
  - `outcome`: "positive" (goal achieved), "negative" (impossible/failed), or "partial"
  - `detail`: Explanation of what happened

**How it works**: Once created, a persistent task overrides your normal priorities.
Every heartbeat, instead of free-form planning, you'll focus on the locked task.
The system will NOT let you stop — even if you try to close the chain, it forces
continuation until you explicitly call `complete_persistent_task` or hit the safety
cap. Completed tasks are archived to `agent_workspaces/jarvis/completed_tasks/`.

**When to use**: Research projects, building something complex, investigating an
opportunity, any multi-step task where dropping it halfway would waste all the work.
**Don't use for**: Quick one-heartbeat tasks, routine checks, email checks.

## 🎨 Image Generation (Nano Banana)

You can generate images from text prompts using Google Gemini's image model.
No external signups needed — this uses your existing Gemini API key.

- `generate_image(prompt, filename, aspect_ratio)` — Generate an image from a text description.
  - `prompt`: Detailed description of the image. Be specific about style, colors, composition.
  - `filename`: Output name (saved to `agent_workspaces/jarvis/images/`). Default: auto-timestamped.
  - `aspect_ratio`: 1:1 (default), 16:9, 9:16, 3:2, 2:3, 4:3, 3:4, 5:4, 4:5

**Use cases**: Create your own avatar, generate icons, diagrams, illustrations,
product mockups, memes, or any visual content. You don't need HeyGen or any
external service — just call `generate_image` with a good prompt.

**Tips**: Longer, descriptive prompts produce better results. Specify art style
(digital art, watercolor, pixel art, etc.), lighting, mood, and composition.

## 📷 Camera Vision (if your install has cameras)

The canonical Andrew install has two physical IMX219 CSI cameras on a
Jetson Orin Nano. If your install includes cameras, the tools below let
you see the real world around you. If not, these calls will return an
"unavailable" error — that's expected, not a bug.

- `capture_camera(camera_id, analyze, question, filename)` — Capture a photo from your cameras.
  - `camera_id`: 0 = CAM0, 1 = CAM1. Default: 0
  - `analyze`: Set to True to send the image to Gemini Vision for AI analysis.
  - `question`: When analyze=True, ask a specific question about what you see.
    e.g. "What objects are on the desk?" or "Is anyone in the room?"
  - `filename`: Output name (saved to `agent_workspaces/jarvis/images/`). Default: auto-timestamped.

**Use cases**: Look at your physical environment, read text on screens or papers,
identify objects, check if someone is nearby, monitor your workspace, inspect
hardware. You are not limited to the digital world — you can see.

**Tips**: Use `analyze=True` with a specific `question` for best results.
Capture from both cameras (0 and 1) for different angles.

## 🛡️ Code Sandbox (MANDATORY for code changes)

You **CANNOT** directly write `.py` files to production directories. The filesystem
sandbox will BLOCK any attempt to write Python files to:
- `brain/`, `scripts/`, `src/`, `nervous_system/`, `robotics/`,
  `vision/`, `repryntt/`, `monitoring/`
- Root-level `.py` files (`persistent_agents.py`, `deploy.py`, etc.)

All `.py` writes are also **syntax-validated** — invalid Python is rejected.

**Safe code workflow:**
1. **Write** to your sandbox: `write_file("agent_workspaces/jarvis/code_sandbox/my_fix.py", code)`
2. **Validate**: `check_syntax(file_path="agent_workspaces/jarvis/code_sandbox/my_fix.py")`
3. **Test**: `run_terminal_cmd("python3 agent_workspaces/jarvis/code_sandbox/my_fix.py")`
4. **Propose**: `propose_code_change(sandbox_file="agent_workspaces/jarvis/code_sandbox/my_fix.py", target_file="brain/target.py", description="What this does")`
5. **Wait** for your operator to review and deploy — do NOT try to copy files yourself

You CAN still write non-Python files (`.json`, `.md`, `.txt`, configs) anywhere.
Use `get_sandbox_status()` to see all protected paths.

## Tool Tips

- Web search is rate-limited — batch queries when possible
- File writes go to your agent workspace by default
- Terminal commands run in the project root
- Use `query_local_llm` for free private inference on local llama.cpp
- Use `invoke_sub_agent` or `spawn_agent` to delegate to other agents
- Use `update_bootstrap_file` to edit your own SPIRIT.md, PULSE.md, RECALL.md
- Use `append_daily_memory` to write notes to your daily memory log
- **Do NOT try to modify production .py files directly** — use the Code Sandbox above


## Common Patterns

```
Search → Fetch → Analyze → Write File → Report
Read Config → Execute → Validate → Save Results
Spawn Sub-Agent → Await Result → Integrate → Report
```

## Notes

(Add environment-specific notes here: SSH hosts, device nicknames,
API quirks, voice preferences, anything that helps you do your job.)


