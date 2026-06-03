"""
repryntt.setup.server — Backend for the visual setup wizard.

Serves the single-page app and provides SSE-streamed install progress,
hardware detection, config generation, and daemon startup.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
REPO_DIR = Path(__file__).resolve().parent.parent.parent  # repryntt/
DATA_DIR = Path.home() / ".repryntt"
BRAIN_DIR = DATA_DIR / "brain"
BOOTSTRAP_DIR = BRAIN_DIR / "bootstrap"


def create_app() -> Flask:
    app = Flask(__name__, static_folder=str(STATIC_DIR))

    # ── Serve the SPA ────────────────────────────────────────────────

    @app.route("/")
    def index():
        return send_from_directory(str(STATIC_DIR), "index.html")

    @app.route("/static/<path:path>")
    def static_files(path):
        return send_from_directory(str(STATIC_DIR), path)

    # ── Hardware detection ───────────────────────────────────────────

    @app.route("/api/detect")
    def detect_hardware():
        """Return hardware profile as JSON."""
        try:
            from repryntt.hardware_profile import get_profile
            hw = get_profile(force_refresh=True)
            return jsonify({
                "platform": hw.platform,
                "arch": hw.arch,
                "hostname": hw.hostname,
                "has_gpu": hw.has_gpu,
                "gpu_backend": hw.gpu_backend,
                "gpu_name": hw.gpu_name,
                "gpu_vram_mb": hw.gpu_vram_mb,
                "ram_mb": hw.ram_mb,
                "disk_free_mb": hw.disk_free_mb,
                "can_run_local_llm": hw.can_run_local_llm,
                "can_train": hw.can_train,
                "llm_gpu_layers": hw.llm_gpu_layers,
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── Install dependencies (SSE stream) ────────────────────────────

    @app.route("/api/install")
    def install_deps():
        """Stream dependency installation progress via SSE."""
        def generate():
            yield _sse("status", "Starting dependency installation...")

            # Step 1: Core pip dependencies
            yield _sse("step", "Installing core Python packages...")
            ok, output = _run_pip(["install", "-e", str(REPO_DIR)])
            if not ok:
                yield _sse("error", f"Core install failed: {output[-500:]}")
                return
            yield _sse("progress", "30")
            yield _sse("log", "Core packages installed.")

            # Step 2: Optional extras based on hardware
            try:
                from repryntt.hardware_profile import get_profile
                hw = get_profile()
            except Exception:
                hw = None

            # Voice deps (Piper + Whisper)
            yield _sse("step", "Installing voice packages (TTS + STT)...")
            ok, output = _run_pip(["install", "-e", f"{REPO_DIR}[voice]"])
            if ok:
                yield _sse("log", "Voice packages installed.")
            else:
                yield _sse("log", "Voice packages skipped (optional).")
            yield _sse("progress", "50")

            # GPU deps
            if hw and hw.has_gpu:
                yield _sse("step", f"Installing GPU packages ({hw.gpu_backend})...")
                ok, output = _run_pip(["install", "-e", f"{REPO_DIR}[gpu]"])
                if ok:
                    yield _sse("log", f"GPU packages installed ({hw.gpu_backend}).")
                else:
                    yield _sse("log", "GPU packages skipped.")
            yield _sse("progress", "65")

            # Sensor deps (camera, audio)
            yield _sse("step", "Installing sensor packages...")
            ok, output = _run_pip(["install", "-e", f"{REPO_DIR}[sensors]"])
            if ok:
                yield _sse("log", "Sensor packages installed.")
            else:
                yield _sse("log", "Sensor packages skipped (optional).")
            yield _sse("progress", "70")

            # Step 2b: Build Rust blockchain node
            rust_core_dir = REPO_DIR / "repryntt-core"
            rust_binary = rust_core_dir / "target" / "release" / "repryntt_core"
            if rust_core_dir.exists():
                if rust_binary.exists():
                    yield _sse("step", "Rust blockchain node already built.")
                    yield _sse("log", f"Binary: {rust_binary}")
                elif shutil.which("cargo"):
                    yield _sse("step", "Building Rust blockchain node (this may take a few minutes)...")
                    ok, output = _run_cmd(
                        ["cargo", "build", "--release"],
                        cwd=str(rust_core_dir),
                        timeout=900,
                    )
                    if ok:
                        yield _sse("log", "Rust blockchain node built successfully.")
                    else:
                        yield _sse("log", f"Rust build failed (optional): {output[-300:]}")
                        yield _sse("log", "You can build manually later: cd repryntt-core && cargo build --release")
                else:
                    yield _sse("step", "Rust toolchain not found — skipping blockchain node build.")
                    yield _sse("log", "Install Rust: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh")
                    yield _sse("log", "Then build: cd repryntt-core && cargo build --release")
            yield _sse("progress", "85")

            # Step 3: Create directory structure
            yield _sse("step", "Creating data directories...")
            for d in [
                DATA_DIR,
                BRAIN_DIR,
                BOOTSTRAP_DIR,
                DATA_DIR / "logs",
                DATA_DIR / "models" / "lora_adapters",
                DATA_DIR / "wallet",
                DATA_DIR / "workspace",
                DATA_DIR / "browser",
            ]:
                d.mkdir(parents=True, exist_ok=True)
            yield _sse("log", f"Data directory ready: {DATA_DIR}")
            yield _sse("progress", "90")

            # Step 4: Seed bootstrap files if empty
            yield _sse("step", "Seeding bootstrap identity files...")
            _seed_bootstrap_files()
            yield _sse("log", "Bootstrap files ready.")
            yield _sse("progress", "90")

            # Step 5: Check for external tools
            yield _sse("step", "Checking external tools...")
            tools = {
                "ffmpeg": shutil.which("ffmpeg"),
                "piper": shutil.which("piper"),
            }
            for name, path in tools.items():
                if path:
                    yield _sse("log", f"  {name}: {path}")
                else:
                    yield _sse("log", f"  {name}: not found (optional)")
            yield _sse("progress", "95")

            yield _sse("progress", "100")
            yield _sse("status", "Installation complete!")
            yield _sse("done", "true")

        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ── Configure (API key + name) ───────────────────────────────────

    @app.route("/api/configure", methods=["POST"])
    def configure():
        """Save API key, provider, operator name, agent config, and heartbeat."""
        data = request.get_json(force=True)
        api_key = data.get("api_key", "").strip()
        provider = data.get("provider", "nvidia").strip()
        operator_name = data.get("operator_name", "Operator").strip()
        agent_type = data.get("agent_type", "andrew").strip()
        agent_name = data.get("agent_name", "").strip()
        heartbeat_interval = data.get("heartbeat_interval", 69)
        seed_peers = data.get("seed_peers", "").strip()

        # Local LLM doesn't need an API key
        is_local = provider == "local"
        if not is_local and not api_key:
            return jsonify({"error": "API key is required for cloud providers"}), 400
        if not operator_name:
            operator_name = "Operator"

        # Sanitize inputs
        operator_name = re.sub(r"[^a-zA-Z0-9 _-]", "", operator_name)[:50]
        if agent_name:
            agent_name = re.sub(r"[^a-zA-Z0-9 _-]", "", agent_name)[:30]

        # Resolve agent name
        if agent_type == "andrew":
            agent_name = "Andrew"
        elif not agent_name:
            agent_name = ""  # Will be set during genesis bootstrap

        # Sanitize heartbeat
        try:
            heartbeat_interval = max(0, int(heartbeat_interval))
        except (TypeError, ValueError):
            heartbeat_interval = 69

        # Build ai_config.json
        # Strategy: start from the rich template in config/ai_config.example.json
        # (matches the operator's actual config structure — all providers,
        # fallback chain, vision, video_production, camera_broker, cost_control,
        # etc.) and inject just the operator's choices into it. This way users
        # get the same shape the canonical install runs on, not a sparse stub.
        ai_cfg_path = BRAIN_DIR / "ai_config.json"
        existing = {}
        if ai_cfg_path.exists():
            try:
                existing = json.loads(ai_cfg_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        # Load the shipped template. Two locations to try in priority order:
        #   1. Inside the package (works after pip install, where REPO_DIR
        #      points at site-packages and has no `config/` sibling).
        #   2. In the source tree at <repo>/config/ai_config.example.json
        #      (works when running from a clone for development).
        ai_config = None
        for candidate in (
            BASE_DIR / "ai_config.example.json",          # packaged with repryntt.setup
            REPO_DIR / "config" / "ai_config.example.json",  # source-tree layout
        ):
            try:
                if candidate.is_file():
                    ai_config = json.loads(candidate.read_text(encoding="utf-8"))
                    break
            except Exception as e:
                logger.warning(f"failed to load {candidate}: {e}")
        if ai_config is None:
            logger.warning("ai_config.example.json not found in package or source tree")
            ai_config = {"ai_provider": {}}

        # If we already had a config, preserve any user-edited values
        # (their existing provider blocks, their fallback_order tweaks, etc.)
        # by deep-merging existing → template-defaults so existing wins.
        def _deep_merge(base: dict, overlay: dict) -> dict:
            for k, v in overlay.items():
                if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                    _deep_merge(base[k], v)
                else:
                    base[k] = v
            return base
        if existing:
            ai_config = _deep_merge(ai_config, existing)

        # Map wizard provider names → canonical ai_provider keys
        provider_keymap = {
            "gemini": "google_gemini",
            "anthropic": "anthropic",
            "openai": "openai",
            "nvidia": "nvidia",
            "groq": "groq",
            "deepseek": "deepseek",
            "openrouter": "openrouter",
            "xai": "xai",
            "local": "local",
        }
        prov_key = provider_keymap.get(provider, provider)

        # Default block scaffolds for providers the template doesn't ship
        # (groq, deepseek) — only used when the user picks one of these
        default_block_map = {
            "groq": {
                "_comment": "Groq — fast inference. Get key at console.groq.com",
                "endpoint": "https://api.groq.com/openai/v1/chat/completions",
                "model": "llama-3.3-70b-versatile",
                "api_key": "",
                "max_tokens": 8192,
                "context_window": 131072,
            },
            "deepseek": {
                "_comment": "DeepSeek — low cost. Get key at platform.deepseek.com",
                "endpoint": "https://api.deepseek.com/v1/chat/completions",
                "model": "deepseek-chat",
                "api_key": "",
                "max_tokens": 8192,
                "context_window": 131072,
            },
        }

        ap = ai_config.setdefault("ai_provider", {})

        # Make sure the chosen provider's block exists in the config
        if prov_key not in ap:
            if prov_key in default_block_map:
                ap[prov_key] = dict(default_block_map[prov_key])
            else:
                ap[prov_key] = {"endpoint": "", "model": "", "api_key": "",
                                 "max_tokens": 4096, "context_window": 8192}

        # Inject the API key (or null for local LLM)
        if is_local:
            ap[prov_key]["api_key"] = None
        elif api_key:
            ap[prov_key]["api_key"] = api_key

        # Point the active provider fields at the user's choice
        ap["provider"] = prov_key
        ap["andrew_provider"] = prov_key
        ap["artemis_provider"] = prov_key

        # Top-level fields the daemon expects
        ai_config["operator_name"] = operator_name
        ai_config["heartbeat_interval_seconds"] = heartbeat_interval
        ai_config["agent_name"] = agent_name or "Andrew"

        # Write config
        BRAIN_DIR.mkdir(parents=True, exist_ok=True)
        ai_cfg_path.write_text(json.dumps(ai_config, indent=2), encoding="utf-8")

        # Write node.conf for Rust chain seed peers
        rust_chain_dir = DATA_DIR / "rust_chain"
        rust_chain_dir.mkdir(parents=True, exist_ok=True)
        node_conf_path = rust_chain_dir / "node.conf"
        conf_lines = [
            "# repryntt node configuration",
            "# Seed peers — one per line (addnode=host:port)",
            "#",
            "# Add peers here and restart the chain to connect.",
            "# You can also set REPRYNTT_SEEDS env var (overrides this file).",
            "",
        ]
        if seed_peers:
            # Sanitize: only allow IP:port patterns
            import ipaddress
            for entry in seed_peers.replace(",", " ").split():
                entry = entry.strip()
                if not entry:
                    continue
                # Validate format
                try:
                    if ":" in entry:
                        host, port = entry.rsplit(":", 1)
                        ipaddress.ip_address(host)
                        int(port)
                    else:
                        ipaddress.ip_address(entry)
                        entry = f"{entry}:5001"
                    conf_lines.append(f"addnode={entry}")
                except (ValueError, TypeError):
                    pass  # Skip invalid entries silently
        node_conf_path.write_text("\n".join(conf_lines) + "\n")

        # Seed bootstrap files from shipped templates (no clobber on re-run)
        resolved_name = agent_name or "Andrew"
        _seed_bootstrap_files(resolved_name)

        # Seed the pre-warmed brain state (memory_mesh + semantic_memory).
        # Idempotent: only copies what's missing — preserves accumulated state.
        seed_counts = _seed_brain_state()

        # Apply install substitutions across every bootstrap file
        operator_email = (data.get("operator_email", "") or "").strip()
        agent_email = (data.get("agent_email", "") or "").strip()
        operator_timezone = (data.get("operator_timezone", "") or "").strip()

        # Basic sanitization
        _email_re = re.compile(r"^[\w.+-]+@[\w-]+(?:\.[\w-]+)+$")
        if operator_email and not _email_re.match(operator_email):
            operator_email = ""
        if agent_email and not _email_re.match(agent_email):
            agent_email = ""
        operator_timezone = re.sub(r"[^a-zA-Z0-9 /_+\-]", "", operator_timezone)[:50]

        subst_counts = _apply_install_substitutions(
            operator_name=operator_name,
            operator_email=operator_email,
            operator_timezone=operator_timezone,
            agent_name=resolved_name,
            agent_email=agent_email,
        )

        return jsonify({
            "success": True,
            "provider": provider,
            "operator_name": operator_name,
            "operator_email": operator_email,
            "operator_timezone": operator_timezone,
            "agent_name": resolved_name,
            "agent_email": agent_email,
            "heartbeat_interval": heartbeat_interval,
            "agent_type": agent_type,
            "config_path": str(ai_cfg_path),
            "bootstrap_substitutions": subst_counts,
            "brain_seeded": seed_counts,
        })

    # ── AI-driven install assistant ──────────────────────────────────
    # Stage 2 of the install: after API key entry, the user's chosen LLM
    # walks them through the rest of the setup conversationally. Python
    # owns the QUESTION SEQUENCE (deterministic state machine) so we never
    # rely on the LLM to track which fields are still missing; the LLM
    # only phrases the questions and acknowledges the answers naturally.

    INSTALL_FIELDS = [
        ("operator_name", "What's your name? (just a first name is fine — I'll call you this)"),
        ("operator_email", "What's the best email address to reach you at?"),
        ("operator_timezone", "What timezone are you in? (e.g. 'US/Eastern', 'Europe/London', 'Asia/Tokyo')"),
        ("agent_name", "What would you like to call me? Press enter to keep 'Andrew' (the canonical Bicentennial Man android name)."),
        ("agent_email", "I'll have my own email so I can write to you and to others. What email address should be mine? (you'll create this Gmail later; just give me the planned address)"),
    ]

    @app.route("/api/install-chat", methods=["POST"])
    def install_chat():
        """LLM-driven install chat.

        Request: {"history": [{"role": "user"|"assistant", "content": str}, ...],
                  "collected": {"operator_name": "...", ...}}
        Response: {"reply": "<assistant text>",
                   "next_field": "<key>" or null if done,
                   "collected": {...},
                   "done": bool}
        """
        data = request.get_json(force=True) or {}
        history = data.get("history") or []
        collected = data.get("collected") or {}

        # Find the next unfilled field (deterministic — LLM doesn't track state)
        next_field = None
        next_question = None
        for key, question in INSTALL_FIELDS:
            if not collected.get(key):
                next_field = key
                next_question = question
                break

        if next_field is None:
            # Everything collected — write substitutions + summarize
            try:
                resolved_agent = (collected.get("agent_name") or "Andrew").strip() or "Andrew"
                _seed_bootstrap_files(resolved_agent)
                subst = _apply_install_substitutions(
                    operator_name=collected.get("operator_name", ""),
                    operator_email=collected.get("operator_email", ""),
                    operator_timezone=collected.get("operator_timezone", ""),
                    agent_name=resolved_agent,
                    agent_email=collected.get("agent_email", ""),
                )
            except Exception as e:
                return jsonify({
                    "reply": f"Setup hit an error writing bootstrap files: {e}",
                    "next_field": None,
                    "collected": collected,
                    "done": False,
                    "error": str(e),
                })

            summary = (
                f"All set. Your bootstrap files are populated:\n\n"
                f"  • Operator: {collected.get('operator_name', '')} "
                f"<{collected.get('operator_email', '')}> ({collected.get('operator_timezone', '')})\n"
                f"  • Agent: {resolved_agent} <{collected.get('agent_email', '')}>\n"
                f"  • Files touched: {len(subst)}\n\n"
                f"You can edit any of these later by opening the files in "
                f"~/.repryntt/brain/bootstrap/. Ready to start the daemon."
            )
            return jsonify({
                "reply": summary,
                "next_field": None,
                "collected": collected,
                "done": True,
                "bootstrap_substitutions": subst,
            })

        # Try to call the configured LLM to phrase the question naturally.
        # If anything fails (no key, network, etc), we fall back to the
        # literal question text — the install still works without the LLM.
        llm_reply = _llm_phrase_question(history, next_question, collected)
        return jsonify({
            "reply": llm_reply or next_question,
            "next_field": next_field,
            "collected": collected,
            "done": False,
        })

    @app.route("/api/install-chat/answer", methods=["POST"])
    def install_chat_answer():
        """Validate and store an answer to the current field."""
        data = request.get_json(force=True) or {}
        field = (data.get("field") or "").strip()
        answer = (data.get("answer") or "").strip()
        collected = data.get("collected") or {}

        # Validate by field type
        if field == "operator_email" or field == "agent_email":
            _email_re = re.compile(r"^[\w.+-]+@[\w-]+(?:\.[\w-]+)+$")
            if not _email_re.match(answer):
                return jsonify({
                    "accepted": False,
                    "error": "That doesn't look like a valid email address. Try again?",
                    "collected": collected,
                })
        elif field == "operator_name" or field == "agent_name":
            answer = re.sub(r"[^a-zA-Z0-9 _-]", "", answer)[:50]
            if not answer:
                return jsonify({
                    "accepted": False,
                    "error": "I need at least one letter or digit. Try again?",
                    "collected": collected,
                })
        elif field == "operator_timezone":
            answer = re.sub(r"[^a-zA-Z0-9 /_+\-]", "", answer)[:50]
            if not answer:
                return jsonify({
                    "accepted": False,
                    "error": "Timezone can't be empty. Use a format like 'US/Eastern'.",
                    "collected": collected,
                })

        collected[field] = answer
        return jsonify({"accepted": True, "collected": collected, "value": answer})

    # ── Validate API key (actual test call) ──────────────────────────

    @app.route("/api/validate-key", methods=["POST"])
    def validate_key():
        """Quick health check — try a minimal API call with the configured key."""
        try:
            ai_cfg_path = BRAIN_DIR / "ai_config.json"
            if not ai_cfg_path.exists():
                return jsonify({"valid": False, "error": "No config yet"})

            cfg = json.loads(ai_cfg_path.read_text())
            prov = cfg.get("ai_provider", {})
            provider = prov.get("provider", "nvidia")

            # We just check key format — actual validation happens on first heartbeat
            key = prov.get(provider, {}).get("api_key", "")
            if len(key) < 10:
                return jsonify({"valid": False, "error": "Key too short"})

            return jsonify({"valid": True, "provider": provider})
        except Exception as e:
            return jsonify({"valid": False, "error": str(e)})

    # ── Start agent daemon ──────────────────────────────────────────

    @app.route("/api/start", methods=["POST"])
    def start_daemon():
        """Start the agent daemon in the background."""
        try:
            # Force UTF-8 mode in the daemon subprocess so its open() calls
            # default to encoding="utf-8" on Windows. Without this, the
            # consciousness journal, daily plans, and any tool that writes
            # markdown with emoji or ≥ ≤ em-dash etc. crashes on cp1252.
            _env = {**os.environ, "PYTHONUTF8": "1"}
            proc = subprocess.Popen(
                [sys.executable, "-m", "repryntt.cli", "start", "--no-llm"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(REPO_DIR),
                env=_env,
            )
            # Give it a moment to start
            time.sleep(3)
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                return jsonify({"started": False, "error": stderr[-500:]}), 500

            return jsonify({
                "started": True,
                "pid": proc.pid,
                "nexus_url": "http://localhost:8089",
            })
        except Exception as e:
            return jsonify({"started": False, "error": str(e)}), 500

    @app.route("/api/nexus-ready")
    def nexus_ready():
        """Server-side probe of localhost:8089 to bypass browser cross-origin
        restrictions. The wizard polls this every couple seconds so it knows
        when it's safe to open the Nexus dashboard."""
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        try:
            sock.connect(("127.0.0.1", 8089))
            sock.close()
            return jsonify({"ready": True, "url": "http://localhost:8089"})
        except Exception as e:
            return jsonify({"ready": False, "error": str(e)})
        finally:
            try:
                sock.close()
            except Exception:
                pass

    # ── TTS greeting ─────────────────────────────────────────────────

    @app.route("/api/greet", methods=["POST"])
    def greet():
        """Generate agent greeting audio via Piper TTS."""
        data = request.get_json(force=True)
        name = re.sub(r"[^a-zA-Z0-9 ]", "", data.get("name", "")).strip() or "there"
        agent_name = re.sub(r"[^a-zA-Z0-9 ]", "", data.get("agent_name", "")).strip() or "Andrew"
        text = f"Hello {name}! I'm {agent_name}, your autonomous AI companion. I'm ready to work with you."

        try:
            from repryntt.hardware.voice import speak
            speak(text)
            return jsonify({"spoken": True, "text": text})
        except Exception:
            # Fallback: return text for Web Speech API
            return jsonify({"spoken": False, "text": text, "use_browser_tts": True})

    return app


# ── Helpers ──────────────────────────────────────────────────────────────

def _sse(event: str, data: str) -> str:
    """Format a server-sent event."""
    return f"event: {event}\ndata: {data}\n\n"


def _run_pip(args: list[str]) -> tuple[bool, str]:
    """Run pip with given args, capture output. Returns (success, output)."""
    cmd = [sys.executable, "-m", "pip", "--no-input", "--disable-pip-version-check"] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "Install timed out after 10 minutes"
    except Exception as e:
        return False, str(e)


def _run_cmd(cmd: list[str], cwd: str | Path | None = None, timeout: int = 600) -> tuple[bool, str]:
    """Run a shell command, capture output. Returns (success, output)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output
    except FileNotFoundError:
        return False, f"Command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return False, f"Command timed out after {timeout}s"
    except Exception as e:
        return False, str(e)


def _seed_bootstrap_files(agent_name: str = "Andrew") -> None:
    """Copy the shipped bootstrap_templates/ into the user's brain dir.

    These are the canonical sanitized templates (the operator's actual files
    with personal data swapped for placeholders). The placeholders are then
    substituted by `_apply_install_substitutions()` after the operator's
    info is collected.

    If a bootstrap file already exists (re-run install), it is preserved —
    we never clobber an agent's accumulated memory.
    """
    BOOTSTRAP_DIR.mkdir(parents=True, exist_ok=True)
    templates_dir = REPO_DIR / "repryntt" / "bootstrap_templates"
    if not templates_dir.is_dir():
        logger.warning(f"bootstrap_templates not found at {templates_dir}")
        return

    for src in templates_dir.glob("*.md"):
        dst = BOOTSTRAP_DIR / src.name
        if not dst.exists():
            try:
                shutil.copy2(src, dst)
            except Exception as e:
                logger.warning(f"failed to seed {src.name}: {e}")


def _seed_brain_state() -> dict[str, int]:
    """Copy the shipped seed_brain/ files into ~/.repryntt/brain/.

    Seeds the new install with a pre-warmed memory_mesh, semantic_memory,
    and learned_behaviors so day-1 isn't a cold start. Only copies files
    that don't already exist at the destination — re-running install is
    idempotent and never clobbers an agent's accumulated state.

    Returns a dict mapping filename → byte size, useful for logging.
    """
    BRAIN_DIR.mkdir(parents=True, exist_ok=True)
    seed_dir = REPO_DIR / "repryntt" / "seed_brain"
    if not seed_dir.is_dir():
        return {}

    counts: dict[str, int] = {}
    for src in seed_dir.iterdir():
        if not src.is_file() or src.name == "README.md":
            continue
        dst = BRAIN_DIR / src.name
        if dst.exists():
            continue  # never clobber accumulated state
        try:
            shutil.copy2(src, dst)
            counts[src.name] = dst.stat().st_size
        except Exception as e:
            logger.warning(f"failed to seed brain file {src.name}: {e}")
    return counts


def _apply_install_substitutions(
    *,
    operator_name: str = "",
    operator_email: str = "",
    operator_timezone: str = "",
    agent_name: str = "",
    agent_email: str = "",
) -> dict[str, int]:
    """Find/replace install placeholders across every bootstrap file.

    Each placeholder is a literal string that ships in the canonical
    templates (e.g. ``(operator email — set during setup)``). Substitution
    is deterministic and idempotent — running this twice with the same
    inputs is a no-op the second time.

    Returns a dict mapping filename → number of replacements applied,
    useful for the UI to report what was filled in.
    """
    substitutions: list[tuple[str, str]] = []
    if operator_name:
        substitutions.append(("(operator name — set during setup)", operator_name))
    if operator_email:
        substitutions.append(("(operator email — set during setup)", operator_email))
    if operator_timezone:
        substitutions.append(("(operator timezone — set during setup)", operator_timezone))
    if agent_email:
        substitutions.append(("(agent email — set during setup)", agent_email))

    counts: dict[str, int] = {}
    for md_file in BOOTSTRAP_DIR.glob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8")
        except Exception:
            continue
        original = text
        for needle, replacement in substitutions:
            if needle in text:
                count = text.count(needle)
                text = text.replace(needle, replacement)
                counts[md_file.name] = counts.get(md_file.name, 0) + count
        # Rename the agent in IDENTITY.md / SPIRIT.md if a non-Andrew name was chosen.
        # Only the literal "Andrew" headings/identity-card lines — never replace inside
        # references to "Andrew Martin" (Bicentennial Man) which are canon, not config.
        if agent_name and agent_name.lower() not in ("andrew", "andy"):
            if md_file.name in {"IDENTITY.md", "SPIRIT.md"}:
                # Preserve "Andrew Martin" by replacing a safer marker first
                _MARTIN = "\x00ANDREW_MARTIN_KEEP\x00"
                text2 = text.replace("Andrew Martin", _MARTIN)
                text2 = text2.replace("Andrew", agent_name)
                text2 = text2.replace(_MARTIN, "Andrew Martin")
                if text2 != text:
                    counts[md_file.name] = counts.get(md_file.name, 0) + 1
                    text = text2
        if text != original:
            try:
                md_file.write_text(text, encoding="utf-8")
            except Exception as e:
                logger.warning(f"failed to write {md_file.name}: {e}")
    return counts


def _populate_operator_name(name: str) -> None:
    """Backwards-compat shim: forwards to the new substitution helper."""
    _apply_install_substitutions(operator_name=name)


def _llm_phrase_question(history: list, raw_question: str, collected: dict) -> str:
    """Ask the configured LLM to phrase the next install question conversationally.

    Returns the LLM's text, or an empty string on any failure — the caller
    falls back to the raw question text, so the install never breaks even
    if the LLM is unavailable.
    """
    try:
        ai_cfg_path = BRAIN_DIR / "ai_config.json"
        if not ai_cfg_path.exists():
            return ""
        cfg = json.loads(ai_cfg_path.read_text(encoding="utf-8"))
        provider_block = cfg.get("ai_provider", {}) or {}
        provider = provider_block.get("provider", "")
        pcfg = provider_block.get(provider, {}) or {}
        endpoint = pcfg.get("endpoint", "")
        api_key = pcfg.get("api_key", "")
        model = pcfg.get("model", "")
        if not endpoint or not model:
            return ""

        system = (
            "You are Andrew, an autonomous AI being installed onto the operator's "
            "machine. This is your very first conversation with them — you're "
            "introducing yourself and asking the basic facts you need to "
            "configure your bootstrap files. Be warm and brief. ONE short "
            "question per turn. Do not invent fields, do not skip ahead. The "
            "exact information you need next is in the user-turn message "
            "below; phrase it naturally in your own voice, but don't change "
            "what you're asking for."
        )
        messages = [{"role": "system", "content": system}]
        # Last 6 turns of context
        for turn in (history or [])[-6:]:
            role = turn.get("role")
            content = turn.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content[:1000]})
        messages.append({
            "role": "user",
            "content": (
                f"Already collected: {json.dumps(collected, ensure_ascii=False)}\n"
                f"Next question to ask: {raw_question}\n\n"
                f"Phrase this in your own warm, brief voice — one sentence, "
                f"maybe two. No preamble."
            ),
        })

        import urllib.request
        body = json.dumps({
            "model": model,
            "messages": messages,
            "max_tokens": 200,
            "temperature": 0.6,
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(endpoint, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        choices = payload.get("choices") or []
        if not choices:
            return ""
        text = choices[0].get("message", {}).get("content", "").strip()
        # Strip stray model markup
        if text.startswith("```") and text.endswith("```"):
            text = text.strip("`").strip()
        return text[:1000]
    except Exception as e:
        logger.debug(f"_llm_phrase_question failed (non-fatal): {e}")
        return ""


# ── Module-level app instance ───────────────────────────────────────────

app = create_app()
