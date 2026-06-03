"""
repryntt.first_run — Automatic first-run initialization.

Called by ServiceManager.check_prerequisites() on every startup.
All steps are idempotent — safe to run repeatedly.

Handles:
  1. Data directory structure (~/.repryntt/brain, social, logs, etc.)
  2. Node identity generation (Ed25519 keypair + node_id)
  3. Bootstrap template files (GENESIS.md, PROTOCOL.md, etc.)
  4. Default daemon_state.json (if missing)
  5. P2P auth token generation
  6. Environment-variable config for headless/Docker deploys

NOTE: ai_config.json is NOT created here. It is created by:
  - The setup wizard (python -m repryntt.setup)
  - Environment variables (REPRYNTT_PROVIDER + REPRYNTT_API_KEY)
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
from pathlib import Path

logger = logging.getLogger("repryntt.first_run")


def is_configured(data_dir: Path | None = None) -> bool:
    """Check if the system has been configured (ai_config.json exists
    and has a real provider set, not just the env-var placeholder).

    Returns True if ready to run, False if setup wizard needed."""
    if data_dir is None:
        data_dir = Path.home() / ".repryntt"
    ai_cfg_path = data_dir / "brain" / "ai_config.json"
    if not ai_cfg_path.exists():
        # Check if env vars can configure it
        if os.environ.get("REPRYNTT_PROVIDER"):
            return True
        return False
    try:
        cfg = json.loads(ai_cfg_path.read_text())
        provider = cfg.get("ai_provider", {}).get("provider", "")
        # "unconfigured" is the sentinel we use for fresh installs
        return bool(provider) and provider != "unconfigured"
    except (json.JSONDecodeError, KeyError):
        return False


def configure_from_env(data_dir: Path) -> bool:
    """Create ai_config.json from environment variables.
    Used for Docker / headless deploys where no wizard is available.

    Env vars:
        REPRYNTT_PROVIDER   — provider name (nvidia, gemini, openai, groq,
                              anthropic, deepseek, openrouter, local)
        REPRYNTT_API_KEY    — API key (not needed for local)
        REPRYNTT_MODEL      — model name (optional, uses provider default)
        REPRYNTT_AGENT_NAME — agent name (default: Andrew)
        REPRYNTT_HEARTBEAT  — heartbeat interval in seconds (default: 69)
        REPRYNTT_OPERATOR   — operator name (default: Operator)

    Returns True if config was created, False if env vars not set.
    """
    provider = os.environ.get("REPRYNTT_PROVIDER", "").strip().lower()
    if not provider:
        return False

    api_key = os.environ.get("REPRYNTT_API_KEY", "").strip()
    model = os.environ.get("REPRYNTT_MODEL", "").strip()
    agent_name = os.environ.get("REPRYNTT_AGENT_NAME", "Andrew").strip()
    heartbeat = int(os.environ.get("REPRYNTT_HEARTBEAT", "69"))
    operator = os.environ.get("REPRYNTT_OPERATOR", "Operator").strip()

    if provider != "local" and not api_key:
        logger.warning("REPRYNTT_PROVIDER set but REPRYNTT_API_KEY missing — skipping env config")
        return False

    # Provider defaults
    provider_defaults = {
        "local": {"endpoint": "http://127.0.0.1:8080/v1/chat/completions", "model": "local"},
        "nvidia": {"endpoint": "https://integrate.api.nvidia.com/v1/chat/completions",
                   "model": "mistralai/mistral-large-3-675b-instruct-2512"},
        "gemini": {"endpoint": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                   "model": "gemini-2.0-flash"},
        "openai": {"endpoint": "https://api.openai.com/v1/chat/completions", "model": "gpt-4o"},
        "groq": {"endpoint": "https://api.groq.com/openai/v1/chat/completions",
                 "model": "llama-3.3-70b-versatile"},
        "anthropic": {"endpoint": "https://api.anthropic.com/v1/messages",
                      "model": "claude-sonnet-4-20250514"},
        "deepseek": {"endpoint": "https://api.deepseek.com/v1/chat/completions",
                     "model": "deepseek-chat"},
        "openrouter": {"endpoint": "https://openrouter.ai/api/v1/chat/completions",
                       "model": "meta-llama/llama-3.3-70b-instruct"},
    }

    defaults = provider_defaults.get(provider, provider_defaults["nvidia"])
    # Map gemini to google_gemini config key
    config_key = "google_gemini" if provider == "gemini" else provider
    prov_config = {
        "endpoint": defaults["endpoint"],
        "model": model or defaults["model"],
        "max_tokens": 8192,
        "context_window": 131072,
    }
    if api_key:
        prov_config["api_key"] = api_key

    ai_config = {
        "ai_provider": {
            "provider": config_key,
            config_key: prov_config,
        },
        "operator_name": operator,
        "agent_name": agent_name,
        "heartbeat_interval": heartbeat,
    }

    brain_dir = data_dir / "brain"
    brain_dir.mkdir(parents=True, exist_ok=True)
    ai_cfg_path = brain_dir / "ai_config.json"
    ai_cfg_path.write_text(json.dumps(ai_config, indent=2) + "\n")
    logger.info(f"Created ai_config.json from env vars (provider: {provider})")
    return True


def run_first_boot(data_dir: Path) -> bool:
    """Run all first-boot initialization steps. Returns True if this was
    a fresh install (first time), False if already initialized."""
    first_time = False

    # ── 1. Directory structure ───────────────────────────────────────────
    dirs = [
        data_dir / "brain",
        data_dir / "brain" / "bootstrap",
        data_dir / "brain" / "chains",
        data_dir / "brain" / "conversations",
        data_dir / "brain" / "consolidation",
        data_dir / "brain" / "skills",
        data_dir / "brain" / "open_mind",
        data_dir / "social",
        data_dir / "logs",
        data_dir / "models",
        data_dir / "models" / "lora_adapters",
        data_dir / "data",
        data_dir / "workspace",
        data_dir / "workspace" / "projects",
        data_dir / "workspace" / "reports",
        data_dir / "pids",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    # ── 2. Node identity (Ed25519) ───────────────────────────────────────
    identity_marker = data_dir / "social" / "node_identity.json"
    if not identity_marker.exists():
        first_time = True
        try:
            from repryntt.social.identity import get_node_identity
            nid = get_node_identity()
            logger.info(f"Node identity created: {nid.node_id}")
        except Exception as e:
            logger.warning(f"Could not generate node identity: {e}")
            # Non-fatal — identity will be created on demand later

    # ── 3. Bootstrap templates ───────────────────────────────────────────
    bootstrap_dest = data_dir / "brain" / "bootstrap"
    _install_bootstrap_templates(bootstrap_dest)

    # ── 4. Config from environment variables (Docker/headless) ─────────
    ai_cfg_path = data_dir / "brain" / "ai_config.json"
    if not ai_cfg_path.exists():
        first_time = True
        if os.environ.get("REPRYNTT_PROVIDER"):
            configured = configure_from_env(data_dir)
            if configured:
                logger.info("Configured from environment variables")
        # If no env vars, ai_config.json is NOT created.
        # User must run 'repryntt setup' to configure.

    # ── 5. Default daemon_state.json ─────────────────────────────────────
    daemon_path = data_dir / "brain" / "daemon_state.json"
    if not daemon_path.exists():
        daemon_state = {
            "daemon_running": False,
            "auto_start": False,
            "agents": [],
        }
        daemon_path.write_text(json.dumps(daemon_state, indent=2) + "\n")
        logger.info("Created default daemon_state.json")

    # ── 6. P2P auth token ────────────────────────────────────────────────
    auth_token_path = data_dir / "auth_token"
    if not auth_token_path.exists():
        token = secrets.token_hex(32)
        auth_token_path.write_text(token)
        from repryntt.platform_utils import secure_file
        secure_file(auth_token_path)
        logger.info("Generated P2P auth token")

    if first_time:
        logger.info("First-run initialization complete")

    return first_time


def _install_bootstrap_templates(dest: Path) -> None:
    """Copy bootstrap .md templates into dest/ if not already present."""
    # Find the templates bundled with the package
    templates_dir = Path(__file__).parent / "bootstrap_templates"

    if not templates_dir.is_dir():
        logger.debug("No bootstrap_templates directory in package")
        return

    for src_file in templates_dir.glob("*.md"):
        if src_file.name == "README.md":
            continue
        dest_file = dest / src_file.name
        if not dest_file.exists():
            shutil.copy2(src_file, dest_file)
            logger.info(f"Installed bootstrap template: {src_file.name}")
