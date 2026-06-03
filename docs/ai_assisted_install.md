# AI-Assisted Installation

Repryntt supports three installation lanes:

1. **Manual CLI install** for technical users who want exact control.
2. **Visual setup wizard** for users who want a guided browser UI.
3. **AI-assisted install** for users who want help from an AI model in VS Code,
   Cursor, or another IDE chat while still running trusted Repryntt commands.

The AI-assisted lane is optional. It should make installation easier without
turning the user's machine over to arbitrary model-generated shell commands.

## Safety Model

Use the AI model as a planner, explainer, and troubleshooter. Use Repryntt's
installer commands as the executor.

Good pattern:

```text
AI model: explains what to run and why
User: approves each step
Terminal: runs known Repryntt commands
```

Avoid this pattern:

```text
AI model: invents a long sudo script
Terminal: runs it without review
```

The AI should recommend install profiles and known commands, not write
unbounded install scripts.

## Recommended Prompt For VS Code Or Cursor

Paste this into your IDE's AI chat from the repository root:

```text
You are helping me install Repryntt on this machine.

Rules:
- Do not invent arbitrary shell scripts.
- Prefer documented Repryntt commands and explain each step before I run it.
- Ask before any sudo, package-manager, firewall, service, or destructive action.
- Keep blockchain optional. Do not enable blockchain unless I explicitly ask.
- Detect my OS, CPU architecture, Python version, GPU availability, Docker
  availability, and whether Rust is installed.
- Recommend one install profile:
  1. local-agent
  2. desktop
  3. docker
  4. robot-jetson
  5. blockchain-node
- Give me commands one step at a time and wait for output before continuing.

Start by checking the environment and recommending the safest install profile.
```

## Install Profiles

### local-agent

Default profile for most users. No blockchain, no Rust build, no P2P ports.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
repryntt setup
repryntt start
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
repryntt setup
repryntt start
```

### desktop

Native desktop app. Blockchain remains off unless explicitly enabled.

```bash
pip install -e ".[desktop]"
repryntt desktop
```

### docker

Runs the Python app stack in a container. The default Docker image does not
build or start the Rust blockchain node.

```bash
docker compose -f docker/docker-compose.yml up --build
```

### robot-jetson

For Jetson/robot hosts. Install GPU/robot dependencies intentionally and keep
blockchain disabled unless the operator explicitly wants it.

```bash
pip install -e .
repryntt setup
repryntt start --no-blockchain
```

Jetson-specific PyTorch/CUDA wheels may need to be installed before optional GPU
extras. The AI assistant should inspect the device before recommending those.

### blockchain-node

Opt-in only. Requires Rust and opens chain-related ports when configured.

Linux/systemd:

```bash
cd repryntt-core
cargo build --release
cd ..
repryntt chain install
repryntt start --with-blockchain
```

macOS, Windows, WSL, and containers:

```bash
cd repryntt-core
cargo build --release
cd ..
repryntt chain start
repryntt start --with-blockchain
```

## Future Built-In AI Installer Framework

The in-product AI installer should use a typed action protocol:

```json
{
  "profile": "local-agent",
  "actions": [
    {"type": "check_python", "min_version": "3.10"},
    {"type": "create_venv", "path": ".venv"},
    {"type": "pip_install", "target": "."},
    {"type": "write_feature_config", "blockchain_enabled": false},
    {"type": "start_setup_wizard"}
  ]
}
```

The backend should validate actions against an allowlist before executing them.
The model can choose from approved actions, but it should not directly execute
free-form commands.

Minimum allowed action families:

- environment inspection
- Python virtualenv creation
- pip install for documented extras
- feature config writes
- setup wizard launch
- Docker compose launch
- optional Rust build only for blockchain profile
- optional systemd install only after explicit user approval

This gives non-technical users an AI guide while preserving predictable,
auditable installation behavior.
