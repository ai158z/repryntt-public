"""
repryntt.onboard — guided first-run setup wizard.

Called via ``repryntt onboard`` (see cli.py).  Walks the user through:

1. Data directory creation
2. AI provider / API key
3. Local LLM model detection
4. Channel setup (Telegram / Discord)
5. Service port configuration
6. Write ai_config.json + daemon_state.json

Idempotent — safe to run again; existing values shown as defaults.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path


def _ask(prompt: str, default: str = "") -> str:
    """Prompt with an optional default."""
    suffix = f" [{default}]" if default else ""
    answer = input(f"  {prompt}{suffix}: ").strip()
    return answer or default


def _ask_yn(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = input(f"  {prompt} [{hint}]: ").strip().lower()
    if not answer:
        return default
    return answer.startswith("y")


def _header(title: str) -> None:
    print(f"\n\033[1m── {title} ──\033[0m")


def run_onboard() -> int:
    """Run the interactive onboarding wizard.  Returns 0 on success."""
    print("\033[1m")
    print("  ╔══════════════════════════════════════╗")
    print("  ║   Repryntt — First-Run Setup Wizard  ║")
    print("  ╚══════════════════════════════════════╝")
    print("\033[0m")

    # ── 1. Data directory ────────────────────────────────────────────────
    _header("1/5  Data directory")
    from repryntt.paths import get_data_dir, brain_dir, models_dir, logs_dir
    dd = get_data_dir()
    print(f"  Data directory: {dd}")
    brain = brain_dir()
    models = models_dir()
    logs_dir()
    print(f"  Created: brain/ models/ logs/")

    # Run first-boot initialization (idempotent)
    from repryntt.first_run import run_first_boot
    first_time = run_first_boot(dd)
    if first_time:
        print("  \033[32m✓\033[0m Node identity generated")
        print("  \033[32m✓\033[0m Bootstrap templates installed")
    else:
        print("  \033[32m✓\033[0m Already initialized")

    # ── 2. AI provider ──────────────────────────────────────────────────
    _header("2/5  AI provider")

    ai_cfg_path = brain / "ai_config.json"
    existing: dict = {}
    if ai_cfg_path.exists():
        try:
            existing = json.loads(ai_cfg_path.read_text())
            print("  Existing ai_config.json found — values shown as defaults.")
        except json.JSONDecodeError:
            pass

    prov_cfg = existing.get("ai_provider", {})
    current_provider = prov_cfg.get("provider", "local")

    print("\n  Available providers:")
    print("    1) local   — llama.cpp on this machine (Jetson / x86)")
    print("    2) gemini  — Google Gemini API (cloud)")
    print("    3) openai  — OpenAI API (cloud)")
    print("    4) groq    — Groq cloud inference")

    choice = _ask("Select provider (1-4)", str(
        {"local": "1", "gemini": "2", "openai": "3", "groq": "4"}.get(
            current_provider, "1")
    ))
    provider_map = {"1": "local", "2": "gemini", "3": "openai", "4": "groq"}
    provider = provider_map.get(choice, "local")

    api_key = ""
    if provider != "local":
        existing_key = prov_cfg.get(provider, {}).get("api_key", "")
        masked = f"{'*' * (len(existing_key) - 4)}{existing_key[-4:]}" if len(existing_key) > 8 else ""
        if masked:
            print(f"  Current key: {masked}")
        api_key = _ask(f"{provider} API key (paste or Enter to keep)")
        if not api_key and existing_key:
            api_key = existing_key

    prov_data: dict = {
        "provider": provider,
    }
    if provider != "local":
        prov_data[provider] = {"api_key": api_key}

    # ── 3. Local LLM ────────────────────────────────────────────────────
    _header("3/5  Local LLM")
    gguf_files = sorted(models.glob("*.gguf")) if models.exists() else []
    if gguf_files:
        print(f"  Found {len(gguf_files)} GGUF model(s):")
        for f in gguf_files:
            size_mb = f.stat().st_size // (1024 * 1024)
            print(f"    • {f.name}  ({size_mb} MB)")
    else:
        print("  No GGUF models found in models/")
        print(f"  Place .gguf files in: {models}")

    llama_path = shutil.which("llama-server")
    if llama_path:
        print(f"  llama-server: {llama_path}")
    else:
        print("  llama-server not on PATH — local inference unavailable")

    llm_port = int(_ask("LLM server port", str(
        existing.get("llm", {}).get("port", "8080"))))

    # ── 4. Channels ──────────────────────────────────────────────────────
    _header("4/5  Channel setup")
    chan_cfg = existing.get("channels", {})

    tg_cfg = chan_cfg.get("telegram", {})
    setup_tg = _ask_yn("Enable Telegram?", default=bool(tg_cfg.get("token")))
    tg_data: dict = {}
    if setup_tg:
        tg_token = _ask("Bot token from @BotFather",
                         tg_cfg.get("token", ""))
        tg_allowed = _ask("Allowed user IDs (comma-separated)",
                           ",".join(str(u) for u in tg_cfg.get("allowed_users", [])))
        allowed_list = [int(x.strip()) for x in tg_allowed.split(",") if x.strip().isdigit()]
        tg_data = {"token": tg_token, "allowed_users": allowed_list}

    dc_cfg = chan_cfg.get("discord", {})
    setup_dc = _ask_yn("Enable Discord?", default=bool(dc_cfg.get("token")))
    dc_data: dict = {}
    if setup_dc:
        dc_token = _ask("Discord bot token",
                         dc_cfg.get("token", ""))
        dc_guild = _ask("Guild ID (optional)",
                         dc_cfg.get("guild_id", ""))
        dc_data = {"token": dc_token}
        if dc_guild:
            dc_data["guild_id"] = dc_guild

    # ── 5. Service ports ─────────────────────────────────────────────────
    _header("5/5  Service ports")
    defaults = existing.get("ports", {})
    ports: dict = {}
    for name, default_port in [
        ("chat", 4000),
        ("external_api", 8081),
        ("tool_api", 8083),
        ("nexus", 8089),
        ("unified", 3000),
        ("web", 5000),
    ]:
        ports[name] = int(_ask(f"{name} port", str(
            defaults.get(name, default_port))))

    # ── Write config ─────────────────────────────────────────────────────
    config: dict = {
        "ai_provider": prov_data,
        "llm": {
            "port": llm_port,
            "host": "0.0.0.0",
        },
        "channels": {},
        "ports": ports,
    }
    if tg_data:
        config["channels"]["telegram"] = tg_data
    if dc_data:
        config["channels"]["discord"] = dc_data

    # Preserve any extra keys from existing config
    for k, v in existing.items():
        if k not in config:
            config[k] = v

    ai_cfg_path.write_text(json.dumps(config, indent=2) + "\n")
    print(f"\n  \033[32m✓\033[0m Wrote {ai_cfg_path}")

    # Daemon state (only create if missing)
    daemon_path = brain / "daemon_state.json"
    if not daemon_path.exists():
        daemon_state = {
            "daemon_running": False,
            "auto_start": False,
            "agents": [],
        }
        daemon_path.write_text(json.dumps(daemon_state, indent=2) + "\n")
        print(f"  \033[32m✓\033[0m Wrote {daemon_path}")
    else:
        print(f"  \033[32m✓\033[0m {daemon_path} already exists — kept")

    # ── Done ─────────────────────────────────────────────────────────────
    print("\n\033[1m  Setup complete!\033[0m")
    print("  Next steps:")
    print("    repryntt doctor  — verify everything is healthy")
    print("    repryntt start   — launch the evolution loop")
    print()
    return 0
