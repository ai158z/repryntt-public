#!/usr/bin/env python3
"""
Repryntt cross-platform installer (Windows / macOS / Linux / Termux).

Deterministic + idempotent. Safe to run more than once. Designed so a human OR
a coding agent (Claude Code, Cursor, Grok CLI, …) can run ONE known command
instead of inventing install steps. No sudo, no global changes.

    python install.py        (or: python3 install.py)

Steps:
  1. Verify Python >= 3.10
  2. Create a local virtualenv (.venv) and install Repryntt into it
  3. Seed ~/.repryntt/brain/ai_config.json from the example (if missing)
  4. Run `repryntt doctor` to verify

It does NOT start the blockchain (opt-in) and does NOT write your API keys —
you add those to the config file it points you to.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
IS_WIN = sys.platform == "win32"
BIN = VENV / ("Scripts" if IS_WIN else "bin")
VENV_PY = BIN / ("python.exe" if IS_WIN else "python")
VENV_REPRYNTT = BIN / ("repryntt.exe" if IS_WIN else "repryntt")
CFG = Path.home() / ".repryntt" / "brain" / "ai_config.json"
# Template can live in two places:
#   - <repo>/config/ai_config.example.json (source-tree layout — when running install.py
#     from a clone, which is the normal install path)
#   - <repo>/repryntt/setup/ai_config.example.json (same file, also kept inside the
#     package so it ships with `pip install repryntt` when the wizard runs from
#     site-packages instead of the source tree)
EXAMPLE_CANDIDATES = [
    ROOT / "config" / "ai_config.example.json",
    ROOT / "repryntt" / "setup" / "ai_config.example.json",
]
EXAMPLE = next((p for p in EXAMPLE_CANDIDATES if p.exists()), EXAMPLE_CANDIDATES[0])


def say(msg): print(f"\n==> {msg}")
def ok(msg): print(f"  OK: {msg}")
def warn(msg): print(f"  WARN: {msg}")


def main() -> int:
    # 1. Python version
    say("Checking Python")
    if sys.version_info[:2] < (3, 10):
        print(f"Python 3.10+ required (found {sys.version.split()[0]}).")
        return 1
    ok(f"Python {sys.version.split()[0]}")

    # 2. venv + install
    say("Installing Repryntt into .venv (this can take a few minutes)")
    if not VENV_PY.exists():
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    subprocess.run([str(VENV_PY), "-m", "pip", "install", "--quiet", "--upgrade", "pip"], check=True)
    subprocess.run([str(VENV_PY), "-m", "pip", "install", "--quiet", str(ROOT)], check=True)
    ok("installed")

    # 3. seed config (never overwrite an existing one)
    say("Setting up config")
    CFG.parent.mkdir(parents=True, exist_ok=True)
    if CFG.exists():
        ok(f"config exists: {CFG} (left untouched)")
    elif EXAMPLE.exists():
        shutil.copy2(EXAMPLE, CFG)
        ok(f"created {CFG} from example")
        warn(f"Add at least one provider API key (or a local LLM endpoint) to: {CFG}")
    else:
        warn(f"No example config found at {EXAMPLE}; run `repryntt setup` to configure.")

    # 4. verify
    say("Verifying install")
    if VENV_REPRYNTT.exists():
        subprocess.run([str(VENV_REPRYNTT), "doctor"], check=False)
    else:
        subprocess.run([str(VENV_PY), "-m", "repryntt.cli", "doctor"], check=False)

    # 5. Post-install instructions — three ways to invoke `repryntt`
    # depending on platform and user preference.
    activate = (f"{VENV}\\Scripts\\Activate.ps1" if IS_WIN
                else f"source {VENV}/bin/activate")
    if IS_WIN:
        invoke_examples = f"""
  Three ways to run repryntt commands (pick whichever you prefer):

    A) From the repo root, use the included shim (NO activation needed):
         .\\repryntt start
         .\\repryntt status
         .\\repryntt doctor

    B) Activate the venv once per shell session:
         {activate}
         repryntt start

    C) Call the venv exe directly (works from any directory):
         {VENV_REPRYNTT} start
"""
    else:
        invoke_examples = f"""
  Two ways to run repryntt commands:

    A) Activate the venv once per shell session:
         {activate}
         repryntt start

    B) Call the venv binary directly (works from any directory):
         {VENV_REPRYNTT} start
"""
    print(f"""
Done. Next steps:

  1. Edit your keys:
       {CFG}

  2. Run the wizard for the AI-driven setup chat:
       python -m repryntt.setup
       (opens browser at http://localhost:9090)

  3. Or start the daemon directly:
{invoke_examples}
     Blockchain stays OFF unless you enable it in the wizard or pass
     --with-blockchain.

  4. Re-check anytime:
       repryntt doctor
""")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as e:
        print(f"\nInstall step failed: {e}")
        sys.exit(1)
