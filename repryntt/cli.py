"""
repryntt CLI — unified command-line interface.

Commands:
    repryntt doctor     — health-check all services and dependencies
    repryntt status     — show system overview (agents, channels, cycles)
    repryntt roster     — list all agents by department
    repryntt assign     — assign a task to the best-fit agent
    repryntt onboard    — guided first-run setup wizard
    repryntt start      — full production startup (all services)
    repryntt stop       — graceful shutdown of all services
    repryntt services   — list / start / stop individual services
    repryntt toggle-llm — start/stop the local llama.cpp server
    repryntt desktop    — launch native desktop application
    repryntt mobile     — start mobile-accessible dashboard (Android/iOS PWA)
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import repryntt

# ── Helpers ──────────────────────────────────────────────────────────────────

def _ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m {msg}")

def _warn(msg: str) -> None:
    print(f"  \033[33m!\033[0m {msg}")

def _fail(msg: str) -> None:
    print(f"  \033[31m✗\033[0m {msg}")

def _header(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """Check if a TCP port is accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False


def _cmd_exists(name: str) -> bool:
    return shutil.which(name) is not None


def _try_import(module: str) -> bool:
    try:
        __import__(module)
        return True
    except ImportError:
        return False


def _get_data_dir() -> Path:
    from repryntt.paths import get_data_dir
    return get_data_dir()


# ── doctor ───────────────────────────────────────────────────────────────────

def cmd_doctor(_args) -> int:
    """Health-check all services and dependencies."""
    print(f"\033[1mrepryntt doctor\033[0m  v{repryntt.__version__}")
    issues = 0

    # ── Data directory
    _header("Data directory")
    dd = _get_data_dir()
    _ok(f"{dd}")
    brain = dd / "brain"
    if brain.is_dir():
        _ok(f"brain/ exists ({len(list(brain.glob('*')))} items)")
    else:
        _warn("brain/ not found — run 'repryntt onboard'")
        issues += 1

    # ── Python deps
    _header("Python dependencies")
    deps = [
        ("flask", "Flask web framework"),
        ("aiohttp", "Async HTTP (P2P/channels)"),
        ("cryptography", "Crypto utilities"),
        ("sqlalchemy", "Database ORM"),
        ("requests", "HTTP client"),
    ]
    optional = [
        ("torch", "PyTorch (Jetson GPU)"),
        ("transformers", "HuggingFace Transformers"),
        ("peft", "LoRA/QLoRA training"),
        ("sentence_transformers", "Embeddings"),
        ("faster_whisper", "Speech-to-text"),
        ("telegram", "python-telegram-bot"),
        ("discord", "discord.py"),
        ("playwright", "Browser control"),
        ("msgpack", "P2P serialization"),
    ]
    for mod, desc in deps:
        if _try_import(mod):
            _ok(desc)
        else:
            _fail(f"{desc}  —  pip install {mod}")
            issues += 1
    for mod, desc in optional:
        if _try_import(mod):
            _ok(f"{desc} (optional)")
        else:
            _warn(f"{desc} not installed (optional)")

    # ── Services / ports
    _header("Services")
    services = [
        (8080, "llama.cpp  (local LLM)"),
        (8089, "Nexus app  (agent dashboard)"),
        (5001, "Blockchain node  (PoP)"),
        (4000, "Chat server"),
        (8081, "External API"),
        (8083, "Tool API"),
        (3000, "Unified interface"),
        (5000, "Web server"),
    ]
    for port, label in services:
        if _port_open(port):
            _ok(f":{port}  {label}")
        else:
            _warn(f":{port}  {label}  — not running")

    # ── Config files
    _header("Configuration")
    cfg_dir = dd / "brain"
    for name in ["ai_config.json", "daemon_state.json"]:
        p = cfg_dir / name
        if p.exists():
            _ok(name)
        else:
            _warn(f"{name} not found")
            issues += 1

    # Check API keys (without printing them)
    ai_cfg_path = cfg_dir / "ai_config.json"
    if ai_cfg_path.exists():
        try:
            cfg = json.loads(ai_cfg_path.read_text())
            prov = cfg.get("ai_provider", {})
            provider = prov.get("provider", "local")
            _ok(f"Active provider: {provider}")
            if provider != "local":
                sub = prov.get(provider, {})
                key = sub.get("api_key", "")
                if key and "YOUR_" not in key and len(key) > 10:
                    _ok(f"{provider} API key configured")
                else:
                    _fail(f"{provider} API key missing or placeholder")
                    issues += 1
        except (json.JSONDecodeError, KeyError):
            _warn("ai_config.json malformed")

    # ── System resources
    _header("System resources")
    try:
        import psutil
        mem = psutil.virtual_memory()
        _ok(f"RAM: {mem.used // (1024**2)}MB / {mem.total // (1024**2)}MB "
            f"({mem.percent}%)")
        disk = psutil.disk_usage(str(dd))
        _ok(f"Disk: {disk.free // (1024**3)}GB free")
    except ImportError:
        # Fallback without psutil
        from repryntt.platform_utils import get_memory_summary
        _ok(get_memory_summary())
        try:
            import shutil
            free_gb = shutil.disk_usage(str(dd)).free // (1024**3)
            _ok(f"Disk: {free_gb}GB free")
        except Exception:
            pass

    # ── Hardware profile
    _header("Hardware profile")
    try:
        from repryntt.hardware_profile import get_profile
        hw = get_profile()
        _ok(f"{hw.platform} {hw.arch} — {hw.hostname}")
        if hw.has_gpu:
            _ok(f"GPU: {hw.gpu_name} ({hw.gpu_backend}, {hw.gpu_vram_mb}MB VRAM)")
            _ok(f"Recommended LLM GPU layers: {hw.llm_gpu_layers}")
        else:
            _warn("No GPU detected — CPU-only mode")
            _ok("Mining, blockchain node, and Andrew API all work on CPU")
        _ok(f"Can run local LLM: {'yes' if hw.can_run_local_llm else 'no (need ≥4GB RAM)'}")
        _ok(f"Can train (QLoRA): {'yes' if hw.can_train else 'no (need GPU + ≥4GB VRAM)'}")
        _ok(f"Can mine: yes {'(GPU-accelerated)' if hw.has_gpu else '(CPU)'}")
    except Exception as e:
        _warn(f"Hardware detection failed: {e}")

    # ── External tools
    _header("External tools")
    for tool in ["llama-server", "ffmpeg", "piper"]:
        if _cmd_exists(tool):
            _ok(tool)
        else:
            _warn(f"{tool} not found on PATH")

    # ── Summary
    print()
    if issues == 0:
        print("\033[32m  All checks passed.\033[0m")
    else:
        print(f"\033[33m  {issues} issue(s) found.\033[0m")
    return 1 if issues else 0


# ── status ───────────────────────────────────────────────────────────────────

def cmd_status(_args) -> int:
    """Show system overview."""
    print(f"\033[1mrepryntt status\033[0m  v{repryntt.__version__}")

    # Agent daemon
    _header("Agent daemon")
    try:
        from repryntt.agents.persistent_agents import get_agent_daemon
        daemon = get_agent_daemon(auto_start=False)
        st = daemon.get_status()
        running = st.get("daemon_running", False)
        if running:
            _ok(f"Running — {st.get('total_agents', '?')} agents loaded")
        else:
            _warn("Not running")
        _ok(f"Active: {st.get('active_agents', '?')}  "
            f"Paused: {st.get('paused_agents', '?')}  "
            f"In-flight: {st.get('in_flight_cycles', '?')}")
        _ok(f"Total cycles: {st.get('total_cycles_completed', '?')}")
    except Exception as e:
        _warn(f"Could not connect to agent daemon: {e}")

    # Channel gateway
    _header("Channel gateway")
    try:
        from repryntt.comms.channel_gateway import get_channel_gateway
        gw = get_channel_gateway()
        gs = gw.get_status()
        channels = gs.get("channels", [])
        if channels:
            _ok(f"Active channels: {', '.join(channels)}")
        else:
            _warn("No channels active")
        stats = gs.get("stats", {})
        _ok(f"Messages in: {stats.get('messages_received', 0)}  "
            f"out: {stats.get('messages_sent', 0)}")
    except Exception as e:
        _warn(f"Gateway unavailable: {e}")

    # Services
    _header("Services")
    for port, label in [(8080, "llama.cpp"), (8089, "Nexus"), (5001, "Blockchain"), (4000, "Chat")]:
        state = "\033[32mup\033[0m" if _port_open(port) else "\033[31mdown\033[0m"
        print(f"  :{port}  {label:<16} {state}")

    return 0


# ── roster ───────────────────────────────────────────────────────────────────

def cmd_roster(args) -> int:
    """List all agents by department."""
    try:
        from repryntt.agents.persistent_agents import get_agent_daemon
        daemon = get_agent_daemon(auto_start=False)
        st = daemon.get_status()
    except Exception as e:
        _fail(f"Cannot load agents: {e}")
        return 1

    agents = st.get("agents", [])
    if not agents:
        _warn("No agents loaded. Is the daemon running?")
        return 1

    # Group by department
    by_dept: dict[str, list] = {}
    for a in agents:
        dept = a.get("department", "Unknown")
        by_dept.setdefault(dept, []).append(a)

    dept_filter = args.department.lower() if args.department else None

    for dept in sorted(by_dept):
        if dept_filter and dept_filter not in dept.lower():
            continue
        print(f"\n\033[1m{dept}\033[0m  ({len(by_dept[dept])} agents)")
        for a in sorted(by_dept[dept], key=lambda x: x.get("id", "")):
            status_color = {"active": "32", "paused": "33", "retired": "31"}.get(
                a.get("status", ""), "0")
            status = f"\033[{status_color}m{a.get('status', '?')}\033[0m"
            name = a.get("name", a.get("id", "?"))
            role = a.get("role_title", a.get("role", ""))
            cycles = a.get("cycles", 0)
            print(f"  {a.get('id', '?'):<10} {name:<24} {status:<18} "
                  f"{role:<30} cycles={cycles}")

    print(f"\n  Total: {len(agents)} agents across {len(by_dept)} departments")
    return 0


# ── assign ───────────────────────────────────────────────────────────────────

def cmd_assign(args) -> int:
    """Assign a task to the best-fit agent (or a specific one)."""
    task = " ".join(args.task)
    if not task:
        _fail("No task provided. Usage: repryntt assign 'audit the smart contract'")
        return 1

    try:
        from repryntt.agents.persistent_agents import get_agent_daemon
        daemon = get_agent_daemon(auto_start=False)
    except Exception as e:
        _fail(f"Cannot connect to daemon: {e}")
        return 1

    if args.agent:
        # Direct agent invocation
        print(f"Assigning to {args.agent}: {task[:80]}...")
        result = daemon.invoke_agent(args.agent, task)
    else:
        # Route through Jarvis
        print(f"Routing task: {task[:80]}...")
        result = daemon.invoke_jarvis(task)

    if isinstance(result, dict):
        print(f"\n\033[1mResult:\033[0m")
        response = result.get("response", result.get("result", json.dumps(result, indent=2)))
        print(response[:2000])
    else:
        print(result)
    return 0


# ── onboard ──────────────────────────────────────────────────────────────────

def cmd_onboard(_args) -> int:
    """Launch the setup wizard."""
    return cmd_setup(_args)


def cmd_setup(_args) -> int:
    """Launch the visual setup wizard on port 9090."""
    import threading
    import webbrowser
    from repryntt.setup.server import create_app

    port = 9090
    print(f"\n  \033[1m✦ Repryntt Setup\033[0m → http://localhost:{port}\n")
    threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    app = create_app()
    app.run(host="0.0.0.0", port=port, debug=False)
    return 0


# ── start / stop / services / toggle-llm ─────────────────────────────────────

def cmd_start(args) -> int:
    """Full production startup — all services."""
    from repryntt.first_run import is_configured
    if not is_configured():
        print("\033[31m✗ Not configured.\033[0m")
        print("  Run \033[1mrepryntt setup\033[0m to launch the setup wizard.")
        print("  Or set environment variables for headless config:")
        print("    REPRYNTT_PROVIDER=nvidia REPRYNTT_API_KEY=xxx repryntt start")
        return 1
    from repryntt.services import ServiceManager
    mgr = ServiceManager()
    rc = mgr.start_all(
        skip_llm=args.no_llm,
        skip_trading=not args.with_trading,
        skip_evolution=args.no_evolution,
        skip_blockchain=args.no_blockchain,
    )
    if rc == 0:
        print("\nPress Ctrl+C to stop all services")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print()
            mgr.stop_all()
    return rc


def cmd_stop(_args) -> int:
    """Graceful shutdown of all services (blockchain continues via systemd)."""
    from repryntt.services import ServiceManager
    mgr = ServiceManager()
    return mgr.stop_all()


def cmd_chain(args) -> int:
    """Rust blockchain node management (systemd on Linux, direct process on Windows)."""
    import struct

    _IS_WIN = sys.platform == "win32"
    _USE_SYSTEMD = (
        not _IS_WIN
        and sys.platform.startswith("linux")
        and shutil.which("systemctl") is not None
        and Path("/run/systemd/system").exists()
    )

    action = getattr(args, "chain_action", None)
    service = "repryntt-chain"

    # Locate the binary relative to the project root (works on any machine)
    _project_root = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _IS_WIN:
        binary = str(_project_root / "repryntt-core" / "target" / "release" / "repryntt_core.exe")
    else:
        binary = str(_project_root / "repryntt-core" / "target" / "release" / "repryntt_core")
    service_dst = f"/etc/systemd/system/{service}.service"

    _data_dir_env = os.environ.get("REPRYNTT_DATA_DIR", "").strip()
    _data_dir = Path(_data_dir_env or str(Path.home() / ".repryntt" / "rust_chain"))
    _pid_file = _data_dir / "repryntt_core.pid"
    _log_base = (Path(_data_dir_env) / "logs") if _data_dir_env else (Path.home() / ".repryntt" / "logs")
    _log_file = Path(os.environ.get("REPRYNTT_CHAIN_LOG", str(_log_base / "rust-chain.log")))
    _env_file = _data_dir / "repryntt-chain.env"
    _checkpoint_file = Path(os.environ.get("REPRYNTT_CHECKPOINT_FILE", str(_data_dir / "checkpoints.json")))
    _chain_id = "RPNT-mainnet-1"

    # ── Shared helpers ────────────────────────────────────────────────

    def _rpc_query(method: str, params=None):
        """Query the Rust node's JSON-RPC (wire-framed TCP on port 9332)."""
        import json as _json
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect(("127.0.0.1", 9332))
        req = _json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
        sock.sendall(struct.pack(">I", len(req)) + req)
        header = sock.recv(4)
        length = struct.unpack(">I", header)[0]
        data = b""
        while len(data) < length:
            data += sock.recv(length - len(data))
        sock.close()
        return _json.loads(data)

    def _rpc_is_alive() -> bool:
        """Check if the RPC port is reachable."""
        return _port_open(9332)

    def _local_node_address(create: bool = True) -> str | None:
        from repryntt.economy.node_identity import get_local_node_address

        return get_local_node_address(create=create)

    _production_bootstrap_url = "https://bootstrap.repryntt.ai158z.com"

    def _bootstrap_url_env() -> str:
        return os.environ.get("REPRYNTT_BOOTSTRAP_URL", _production_bootstrap_url)

    def _write_chain_env_file() -> str | None:
        """Write per-machine Rust node environment from the local node wallet."""
        address = _local_node_address(create=True)
        if not address:
            _fail("Could not resolve local node wallet address")
            _warn("Create one with: repryntt chain install")
            return None

        try:
            from repryntt.economy.compute_config import local_compute_runtime

            compute_runtime = local_compute_runtime()
            measured_tflops = str(round(float(compute_runtime["tflops_measured"]), 4))
            compute_share = str(round(float(compute_runtime["compute_share"]), 4))
        except Exception:
            measured_tflops = "5.4"
            compute_share = "1.0"

        _data_dir.mkdir(parents=True, exist_ok=True)
        values = {
            "REPRYNTT_ADDRESS": address,
            "REPRYNTT_TFLOPS": os.environ.get("REPRYNTT_TFLOPS", measured_tflops),
            "REPRYNTT_COMPUTE_SHARE": os.environ.get("REPRYNTT_COMPUTE_SHARE", compute_share),
            "REPRYNTT_DATA_DIR": str(_data_dir),
            "REPRYNTT_RPC_BIND": os.environ.get("REPRYNTT_RPC_BIND", "127.0.0.1:9332"),
            "REPRYNTT_P2P_PORT": os.environ.get("REPRYNTT_P2P_PORT", "5001"),
            "REPRYNTT_MINING": os.environ.get("REPRYNTT_MINING", "true"),
            "REPRYNTT_SKIP_IBD": os.environ.get("REPRYNTT_SKIP_IBD", "false"),
            "REPRYNTT_BOOTSTRAP_URL": _bootstrap_url_env(),
        }
        for key in ("REPRYNTT_SEEDS", "REPRYNTT_BOOTSTRAP_NODES", "REPRYNTT_PUBLIC_P2P_ADDR"):
            if os.environ.get(key):
                values[key] = os.environ[key]

        tmp = _env_file.with_suffix(".env.tmp")
        tmp.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
        tmp.replace(_env_file)
        return address

    def _checkpoint_message(height: int, block_hash: str) -> str:
        return f"{_chain_id}:checkpoint:{height}:{block_hash}"

    def _read_checkpoint_set() -> dict:
        if not _checkpoint_file.exists():
            return {"version": 1, "checkpoints": []}
        try:
            data = json.loads(_checkpoint_file.read_text())
        except Exception as exc:
            raise RuntimeError(f"Could not read checkpoint file: {exc}") from exc
        if isinstance(data, list):
            return {"version": 1, "checkpoints": data}
        if isinstance(data, dict) and "checkpoints" in data:
            data.setdefault("version", 1)
            return data
        if isinstance(data, dict):
            return {"version": 1, "checkpoints": [data]}
        raise RuntimeError("Checkpoint file has unsupported JSON shape")

    def _write_checkpoint_set(data: dict) -> None:
        _checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = _checkpoint_file.with_suffix(_checkpoint_file.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        tmp.replace(_checkpoint_file)

    def _latest_checkpoint() -> dict | None:
        data = _read_checkpoint_set()
        checkpoints = [c for c in data.get("checkpoints", []) if isinstance(c, dict)]
        if not checkpoints:
            return None
        return sorted(checkpoints, key=lambda c: int(c.get("height", 0)))[-1]

    def _verify_checkpoint_signature(cp: dict) -> tuple[bool, str]:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            height = int(cp["height"])
            block_hash = str(cp["hash"])
            message = _checkpoint_message(height, block_hash)
            if cp.get("chain_id") != _chain_id:
                return False, f"chain_id mismatch: {cp.get('chain_id')} != {_chain_id}"
            if cp.get("message") != message:
                return False, "checkpoint message does not match chain_id/height/hash"
            public_key = bytes.fromhex(cp["public_key"])
            signer = hashlib.sha3_256(public_key).hexdigest()[:40]
            if signer != cp.get("signer_address"):
                return False, "signer_address does not match public key"
            Ed25519PublicKey.from_public_bytes(public_key).verify(
                bytes.fromhex(cp["signature"]),
                message.encode(),
            )
            return True, "signature valid"
        except Exception as exc:
            return False, str(exc)

    def _cmd_checkpoint_create() -> int:
        try:
            from repryntt.economy.node_wallet import get_node_wallet

            wallet = get_node_wallet()
            if wallet is None or not wallet.can_sign():
                _fail("Local node wallet cannot sign checkpoint")
                return 1
            height_resp = _rpc_query("get_chain_height")
            result = height_resp.get("result", {})
            height = int(result.get("height") or 0)
            block_hash = str(result.get("latest_hash") or "")
            if height <= 0 or not block_hash:
                _fail("Could not read canonical chain height/hash from local Rust node")
                return 1
            message = _checkpoint_message(height, block_hash)
            checkpoint = {
                "chain_id": _chain_id,
                "height": height,
                "hash": block_hash,
                "signer_address": wallet.address,
                "public_key": wallet.public_key.hex(),
                "signature": wallet.sign(message.encode()).hex(),
                "message": message,
            }
            data = _read_checkpoint_set()
            checkpoints = [
                c
                for c in data.get("checkpoints", [])
                if not (
                    isinstance(c, dict)
                    and c.get("chain_id") == _chain_id
                    and int(c.get("height", -1)) == height
                )
            ]
            checkpoints.append(checkpoint)
            data["version"] = 1
            data["checkpoints"] = sorted(checkpoints, key=lambda c: int(c.get("height", 0)))
            _write_checkpoint_set(data)
            _ok(f"Created signed checkpoint at height {height}")
            print(f"  Hash:   {block_hash}")
            print(f"  Signer: {wallet.address}")
            print(f"  File:   {_checkpoint_file}")
            return 0
        except Exception as exc:
            _fail(f"Checkpoint create failed: {exc}")
            return 1

    def _cmd_checkpoint_show() -> int:
        try:
            cp = _latest_checkpoint()
            if not cp:
                _warn(f"No checkpoints found at {_checkpoint_file}")
                return 1
            ok, reason = _verify_checkpoint_signature(cp)
            print("Latest repryntt checkpoint")
            print(f"  Chain:  {cp.get('chain_id')}")
            print(f"  Height: {cp.get('height')}")
            print(f"  Hash:   {cp.get('hash')}")
            print(f"  Signer: {cp.get('signer_address')}")
            print(f"  Verify: {'ok' if ok else 'failed'} ({reason})")
            print(f"  File:   {_checkpoint_file}")
            return 0 if ok else 1
        except Exception as exc:
            _fail(f"Checkpoint show failed: {exc}")
            return 1

    def _cmd_checkpoint_verify() -> int:
        try:
            cp = _latest_checkpoint()
            if not cp:
                _fail(f"No checkpoints found at {_checkpoint_file}")
                return 1
            ok, reason = _verify_checkpoint_signature(cp)
            if not ok:
                _fail(f"Checkpoint signature invalid: {reason}")
                return 1
            height = int(cp["height"])
            block_hash = str(cp["hash"])
            try:
                block_resp = _rpc_query("get_block", {"index": height - 1})
                block = block_resp.get("result", {})
                if block.get("hash") != block_hash:
                    _fail("Local chain does not contain latest checkpoint hash")
                    print(f"  Expected: {block_hash}")
                    print(f"  Local:    {block.get('hash')}")
                    return 1
                _ok(f"Checkpoint verified on local chain at height {height}")
            except Exception:
                _ok("Checkpoint signature valid")
                _warn("Rust RPC is not reachable, so local chain containment was not checked")
            return 0
        except Exception as exc:
            _fail(f"Checkpoint verify failed: {exc}")
            return 1

    def _service_uses_env_file() -> bool:
        unit_path = Path(service_dst)
        if not unit_path.exists():
            return True
        try:
            return "repryntt-chain.env" in unit_path.read_text(errors="replace")
        except Exception:
            return False

    def _systemd_group_for_user(user: str) -> str:
        try:
            import grp
            import pwd

            gid = pwd.getpwnam(user).pw_gid
            return grp.getgrgid(gid).gr_name
        except Exception:
            return user

    def _systemd_unit_text() -> str:
        """Generate a per-machine systemd unit.

        The checked-in unit is only a readable template. Install must write
        absolute paths for this checkout, current user, data dir, and log file
        so fresh machines are not tied to /home/reprynt.
        """
        user = os.environ.get("REPRYNTT_SERVICE_USER") or getpass.getuser()
        group = os.environ.get("REPRYNTT_SERVICE_GROUP") or _systemd_group_for_user(user)
        _data_dir.mkdir(parents=True, exist_ok=True)
        _log_file.parent.mkdir(parents=True, exist_ok=True)

        return f"""[Unit]
Description=repryntt Blockchain Node (Rust)
Documentation=https://github.com/ai158z/repryntt
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={group}
EnvironmentFile={_env_file}
ExecStart={binary}
Restart=always
RestartSec=5
StandardOutput=append:{_log_file}
StandardError=append:{_log_file}
SyslogIdentifier=repryntt-chain
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=false
ReadWritePaths={_data_dir}
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""

    # ── Linux / systemd helpers ───────────────────────────────────────

    def _systemctl(cmd: str) -> int:
        return subprocess.call(["sudo", "systemctl", cmd, service])

    def _systemd_is_active() -> bool:
        try:
            return subprocess.call(
                ["systemctl", "is-active", "--quiet", service],
                timeout=5,
            ) == 0
        except Exception:
            return False

    # ── Direct-process helpers (Windows, macOS, containers, WSL) ───────

    def _direct_read_pid() -> int | None:
        try:
            return int(_pid_file.read_text().strip())
        except Exception:
            return None

    def _direct_is_running() -> bool:
        pid = _direct_read_pid()
        if pid:
            try:
                # os.kill(pid, 0) checks existence without killing.
                os.kill(pid, 0)
                return True
            except OSError:
                _pid_file.unlink(missing_ok=True)
        return _rpc_is_alive()

    def _is_active() -> bool:
        if not _USE_SYSTEMD:
            return _direct_is_running()
        return _systemd_is_active()

    # ── Actions ───────────────────────────────────────────────────────

    if action == "checkpoint":
        subaction = getattr(args, "checkpoint_action", None)
        if subaction == "create":
            return _cmd_checkpoint_create()
        if subaction == "show":
            return _cmd_checkpoint_show()
        if subaction == "verify":
            return _cmd_checkpoint_verify()
        print("repryntt chain checkpoint — signed canonical checkpoint tools")
        print()
        print("Commands:")
        print("  repryntt chain checkpoint create   Sign current local tip as canonical")
        print("  repryntt chain checkpoint show     Show latest checkpoint")
        print("  repryntt chain checkpoint verify   Verify signature and local containment")
        return 0

    if action == "start":
        if _is_active():
            _ok("Blockchain already running")
            return 0
        if not os.path.isfile(binary):
            _fail(f"Binary not found: {binary}")
            _warn("Build with: cd repryntt-core && cargo build --release")
            return 1

        if not _USE_SYSTEMD:
            print("Starting blockchain node...")
            _data_dir.mkdir(parents=True, exist_ok=True)
            _log_file.parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(_log_file, "a")
            env = os.environ.copy()
            env.setdefault("REPRYNTT_DATA_DIR", str(_data_dir))
            env.setdefault("REPRYNTT_RPC_BIND", "127.0.0.1:9332")
            env.setdefault("REPRYNTT_P2P_PORT", "5001")
            env.setdefault("REPRYNTT_MINING", "true")
            env.setdefault("REPRYNTT_BOOTSTRAP_URL", _bootstrap_url_env())
            try:
                from repryntt.economy.compute_config import local_compute_runtime

                compute_runtime = local_compute_runtime()
                env.setdefault(
                    "REPRYNTT_TFLOPS",
                    str(round(float(compute_runtime["tflops_measured"]), 4)),
                )
                env.setdefault(
                    "REPRYNTT_COMPUTE_SHARE",
                    str(round(float(compute_runtime["compute_share"]), 4)),
                )
            except Exception:
                env.setdefault("REPRYNTT_TFLOPS", "5.4")
                env.setdefault("REPRYNTT_COMPUTE_SHARE", "1.0")
            if os.environ.get("REPRYNTT_PUBLIC_P2P_ADDR"):
                env.setdefault("REPRYNTT_PUBLIC_P2P_ADDR", os.environ["REPRYNTT_PUBLIC_P2P_ADDR"])
            address = _local_node_address(create=True)
            if not address:
                _fail("Could not resolve local node wallet address")
                return 1
            env.setdefault("REPRYNTT_ADDRESS", address)
            creationflags = 0
            if _IS_WIN:
                CREATE_NEW_PROCESS_GROUP = 0x00000200
                DETACHED_PROCESS = 0x00000008
                creationflags = CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
            proc = subprocess.Popen(
                [binary],
                env=env,
                stdout=log_fh,
                stderr=log_fh,
                creationflags=creationflags,
            )
            _pid_file.write_text(str(proc.pid))
            _ok(f"Blockchain node started (PID {proc.pid})")
            _ok(f"Logs: {_log_file}")
        else:
            address = _write_chain_env_file()
            if not address:
                return 1
            if not _service_uses_env_file():
                _fail("Installed repryntt-chain service still has a baked-in wallet")
                _warn("Reinstall it with: repryntt chain install")
                return 1
            print(f"Starting {service}...")
            rc = _systemctl("start")
            if rc == 0:
                _ok("Blockchain node started")
                _ok("Mining 24/7 — survives repryntt stop and reboots")
            return rc
        return 0

    elif action == "stop":
        if not _is_active():
            _warn("Blockchain not running")
            return 0

        if not _USE_SYSTEMD:
            print("Stopping blockchain node...")
            pid = _direct_read_pid()
            if pid:
                try:
                    stop_signal = signal.CTRL_BREAK_EVENT if _IS_WIN else signal.SIGTERM
                    os.kill(pid, stop_signal)
                    _ok(f"Sent stop signal to PID {pid}")
                except OSError as e:
                    _warn(f"Could not stop PID {pid}: {e}")
                    if _IS_WIN:
                        try:
                            subprocess.run(
                                ["taskkill", "/F", "/PID", str(pid)],
                                capture_output=True, timeout=10,
                            )
                            _ok(f"Force-killed PID {pid}")
                        except Exception:
                            _fail(f"Failed to stop PID {pid}")
                            return 1
                    else:
                        try:
                            os.kill(pid, signal.SIGKILL)
                            _ok(f"Force-killed PID {pid}")
                        except OSError:
                            pass
                _pid_file.unlink(missing_ok=True)
            else:
                if _IS_WIN:
                    _warn("No PID file found — trying taskkill by name...")
                    subprocess.run(
                        ["taskkill", "/F", "/IM", "repryntt_core.exe"],
                        capture_output=True, timeout=10,
                    )
                else:
                    _warn("No PID file found")
            _ok("Blockchain stopped")
        else:
            print(f"Stopping {service}...")
            rc = _systemctl("stop")
            if rc == 0:
                _ok("Blockchain stopped")
                _warn("The chain will NOT auto-restart until you run: repryntt chain start")
                _warn("Or re-enable with: repryntt chain enable")
            return rc
        return 0

    elif action == "restart":
        if not _USE_SYSTEMD:
            cmd_chain(argparse.Namespace(chain_action="stop"))
            time.sleep(2)
            return cmd_chain(argparse.Namespace(chain_action="start"))
        else:
            if not _write_chain_env_file():
                return 1
            if not _service_uses_env_file():
                _fail("Installed repryntt-chain service still has a baked-in wallet")
                _warn("Reinstall it with: repryntt chain install")
                return 1
            print(f"Restarting {service}...")
            return _systemctl("restart")

    elif action == "enable":
        if not _USE_SYSTEMD:
            _warn("Auto-start on boot requires a Linux systemd host.")
            _warn("Use Task Scheduler/NSSM on Windows or your platform's service manager.")
            return 0
        print("Enabling blockchain to start on boot...")
        rc = _systemctl("enable")
        if rc == 0:
            _ok("Blockchain will start automatically on boot")
        return rc

    elif action == "disable":
        if not _USE_SYSTEMD:
            _warn("Auto-start management requires a Linux systemd host.")
            return 0
        print("Disabling blockchain auto-start on boot...")
        rc = _systemctl("disable")
        if rc == 0:
            _ok("Blockchain will NOT start on boot (manual start only)")
        return rc

    elif action == "status":
        running = _is_active()
        if running:
            label = "systemd" if _USE_SYSTEMD else "process"
            _ok(f"Blockchain node: \033[32mRUNNING\033[0m ({label})")
            try:
                r = _rpc_query("get_chain_height")
                result = r.get("result", {})
                height = result.get("height", "?")
                tip = result.get("latest_hash", "")[:24]
                print(f"  Height:  {height} blocks")
                print(f"  Tip:     {tip}...")
                net = _rpc_query("get_network_stats").get("result", {})
                if net:
                    print(f"  Mining:  {net.get('mining_state', '?')}")
                    if net.get("mining_pause_reason"):
                        print(f"  Pause:   {net.get('mining_pause_reason')}")
                    print(f"  Fork:    {net.get('fork_status', '?')}")
                    print(f"  Checkpt: {net.get('checkpoint_status', '?')}")

                addr = _local_node_address(create=False)
                if addr:
                    r = _rpc_query("get_balance", {"address": addr})
                    bal = r.get("result", {})
                    print(f"  Wallet:  {addr}")
                    print(f"  Balance: {bal.get('balance_cr', 0)} CR")
                    print(f"  Stake:   {bal.get('stake_cr', 0)} CR")
                else:
                    _warn("No local node wallet found")
            except Exception:
                _warn("RPC not responding yet (node may still be starting)")
            if not _IS_WIN:
                print()
                subprocess.call(["systemctl", "status", service, "--no-pager", "-l", "-n", "5"])
        else:
            _fail("Blockchain node: \033[31mSTOPPED\033[0m")
            _warn("Start with: repryntt chain start")
        return 0

    elif action == "logs":
        n = getattr(args, "lines", 50)
        follow = getattr(args, "follow", False)
        if not _USE_SYSTEMD:
            if _log_file.exists():
                if follow:
                    _warn("Follow mode is only supported for systemd journald — showing last lines")
                lines = _log_file.read_text(errors="replace").splitlines()
                for line in lines[-n:]:
                    print(line)
            else:
                _warn(f"Log file not found: {_log_file}")
            return 0
        cmd = ["journalctl", "-u", service, "--no-pager", "-n", str(n)]
        if follow:
            cmd.append("-f")
        return subprocess.call(cmd)

    elif action == "install":
        if not _USE_SYSTEMD:
            _warn("Systemd service install is only available on Linux hosts running systemd.")
            _warn("Use 'repryntt chain start' to run the node directly on this platform.")
            return 0
        if not os.path.isfile(binary):
            _fail(f"Binary not found: {binary}")
            _warn("Build with: cd repryntt-core && cargo build --release")
            return 1

        address = _write_chain_env_file()
        if not address:
            return 1
        _ok(f"Local node wallet: {address}")

        print(f"Installing {service_dst}...")
        unit_tmp = _data_dir / f"{service}.service"
        unit_tmp.write_text(_systemd_unit_text())
        rc = subprocess.call(["sudo", "cp", str(unit_tmp), service_dst])
        if rc != 0:
            _fail("Failed to copy service file (need sudo)")
            return rc
        subprocess.call(["sudo", "systemctl", "daemon-reload"])
        _ok("Service installed")

        print("Enabling auto-start on boot...")
        _systemctl("enable")
        _ok("Blockchain will start on boot")

        print("Starting blockchain now...")
        _systemctl("start")
        time.sleep(2)
        if _is_active():
            _ok("Blockchain is live!")
        else:
            _warn("Service may still be starting — check: repryntt chain status")
        return 0

    else:
        if not _USE_SYSTEMD:
            print("repryntt chain — Rust blockchain node management")
        else:
            print("repryntt chain — Rust blockchain node (runs 24/7 via systemd)")
        print()
        print("Commands:")
        if _USE_SYSTEMD:
            print("  repryntt chain install   Install systemd service + enable + start")
        print("  repryntt chain start     Start the blockchain")
        print("  repryntt chain stop      Stop the blockchain")
        print("  repryntt chain restart   Restart the blockchain")
        print("  repryntt chain status    Show chain height, balance, and health")
        print("  repryntt chain checkpoint Signed checkpoint create/show/verify")
        print("  repryntt chain logs      View blockchain logs")
        if _USE_SYSTEMD:
            print("  repryntt chain enable    Auto-start on boot")
            print("  repryntt chain disable   Don't auto-start on boot")
        return 0


def cmd_services(args) -> int:
    """List, start, or stop individual services."""
    from repryntt.services import ServiceManager
    mgr = ServiceManager()

    if args.action == "list":
        mgr.list_services()
        return 0
    elif args.action == "status":
        results = mgr.status()
        print(f"\033[1mrepryntt services status\033[0m\n")
        for name, info in results.items():
            if info["healthy"]:
                state = "\033[32m● running\033[0m"
            elif info["running"]:
                state = "\033[33m● degraded\033[0m"
            else:
                state = "\033[31m● stopped\033[0m"
            port_str = f":{info['port']}" if info["port"] else "     "
            pid_str = f"PID {info['pid']}" if info["pid"] else ""
            print(f"  {name:<24} {port_str}  {state}  {pid_str}")
        return 0
    elif args.action == "start":
        if not args.name:
            _fail("Specify a service name: repryntt services start <name>")
            return 1
        return 0 if mgr.start_one(args.name) else 1
    elif args.action == "stop":
        if not args.name:
            _fail("Specify a service name: repryntt services stop <name>")
            return 1
        return 0 if mgr.stop_one(args.name) else 1
    else:
        mgr.list_services()
        return 0


def cmd_desktop(args) -> int:
    """Launch the desktop application (native window)."""
    from repryntt.desktop import launch_desktop
    return launch_desktop(
        skip_llm=args.no_llm,
        skip_trading=not args.with_trading,
        skip_evolution=args.no_evolution,
        no_manage=args.no_manage,
    )


def cmd_mobile(args) -> int:
    """Start mobile-accessible dashboard (Android/iOS PWA)."""
    from repryntt.desktop import launch_mobile
    return launch_mobile(
        skip_llm=args.no_llm,
        skip_trading=not args.with_trading,
        skip_evolution=args.no_evolution,
        no_manage=args.no_manage,
        port=args.port,
    )


def cmd_toggle_llm(_args) -> int:
    """Toggle local llama.cpp server."""
    if _port_open(8080):
        _warn("llama.cpp is running on :8080 — stopping")
        from repryntt.platform_utils import kill_process_by_name
        kill_process_by_name("llama-server")
        time.sleep(1)
        if not _port_open(8080):
            _ok("Stopped")
        else:
            _fail("Could not stop llama-server")
        return 0
    else:
        _ok("llama.cpp not running — starting")
        toggle_script = _get_data_dir() / "llm_toggle.sh"
        if toggle_script.exists() and sys.platform != "win32":
            subprocess.Popen(["bash", str(toggle_script)])
            _ok("Started via llm_toggle.sh")
        elif _cmd_exists("llama-server"):
            models = _get_data_dir() / "models"
            gguf = list(models.glob("*.gguf")) if models.exists() else []
            if gguf:
                subprocess.Popen(["llama-server", "-m", str(gguf[0]),
                                  "--port", "8080", "--host", "0.0.0.0"])
                _ok(f"Started with {gguf[0].name}")
            else:
                _fail("No .gguf model found in models/")
                return 1
        else:
            _fail("Neither llm_toggle.sh nor llama-server found")
            return 1
        return 0


def cmd_node(args) -> int:
    """Blockchain node management (legacy Python node — see 'repryntt chain' for Rust)."""
    action = getattr(args, "node_action", None)

    if action == "status":
        if _port_open(5001):
            _ok("Blockchain node running on :5001")
            # Try health endpoint
            try:
                import requests
                r = requests.get("http://127.0.0.1:6001/health", timeout=2)
                data = r.json()
                _ok(f"Chain length: {data.get('chain_length', '?')} blocks")
                _ok(f"Peers: {data.get('peer_count', '?')}")
                _ok(f"Pending txns: {data.get('pending_transactions', '?')}")
            except Exception:
                _ok("Health endpoint not reachable (node may still be starting)")
        else:
            _fail("Blockchain node not running")
            _warn("Start with: repryntt start  OR  repryntt node start")
        return 0

    elif action == "peers":
        try:
            import requests
            r = requests.get("http://127.0.0.1:6001/health", timeout=2)
            data = r.json()
            peers = data.get("peers", [])
            if peers:
                _header(f"Connected peers ({len(peers)})")
                for p in peers:
                    _ok(p)
            else:
                _warn("No peers connected")
                _warn("LAN discovery broadcasts on UDP :5099 every 30s")
                _warn("Or set: export REPRYNTT_BOOTSTRAP_NODES=host1:5001,host2:5001")
        except Exception:
            _fail("Cannot reach node — is it running?")
        return 0

    elif action == "start":
        port = args.port if hasattr(args, "port") else 5001
        print(f"Starting blockchain node on :{port}...")
        from repryntt.economy.qnode2 import main as node_main
        sys.argv = ["repryntt-node", "--port", str(port)]
        node_main()
        return 0

    else:
        print("Usage: repryntt node {status|peers|start}")
        return 0


def cmd_compute(args) -> int:
    """Compute marketplace management."""
    action = getattr(args, "compute_action", None)

    if action == "provider-status":
        from repryntt.economy.compute_provider import ComputeProviderDaemon

        provider = ComputeProviderDaemon()
        status = provider.status()
        cfg = status["provider"]
        runtime = status["runtime"]
        jobs = status["job_counts"]

        _header("Local Compute Provider")
        print(f"  Enabled:       {'yes' if cfg.get('enabled') else 'no'}")
        print(f"  Provider ID:   {cfg.get('provider_id') or 'not configured'}")
        print(f"  Proof wallet:  {cfg.get('wallet_address') or 'not configured'}")
        print(f"  Settlement:    fiat marketplace")
        print(f"  Payout acct:   {'configured' if cfg.get('connected_account_id') else 'not configured'}")
        print(f"  Chain mode:    {'proof receipts enabled' if cfg.get('chain_enabled') else 'optional/off'}")
        print(f"  Execution:     {cfg.get('execution_mode')}")
        print(f"  Capacity:      {cfg.get('max_concurrent_jobs')} concurrent job(s)")
        print(f"  Pricing:       {cfg.get('price_per_inference_cents', 0)} cents/inference, "
              f"{cfg.get('price_per_second_cents', 0)} cents/sec")
        print(f"  Compute:       {runtime.get('effective_tflops'):.4f} effective TFLOPS "
              f"({runtime.get('compute_share') * 100:.0f}% of {runtime.get('measured_tflops'):.4f})")
        print(f"  Jobs:          {jobs or {}}")
        ann = status["announcement"]
        print(f"  Announce hash: {ann.get('announcement_hash')}")
        print(f"  Signed:        {'yes' if ann.get('signature') else 'no'}")
        return 0

    if action == "provider-enable":
        from repryntt.economy.compute_provider import ComputeProviderDaemon

        provider = ComputeProviderDaemon()
        provider.set_enabled(True)
        _ok("Compute provider enabled")
        _warn("Start/restart the provider service when you are ready to accept paid jobs.")
        return 0

    if action == "provider-disable":
        from repryntt.economy.compute_provider import ComputeProviderDaemon

        provider = ComputeProviderDaemon()
        provider.set_enabled(False)
        _ok("Compute provider disabled")
        return 0

    if action == "provider-announce":
        from repryntt.economy.compute_provider import ComputeProviderDaemon

        provider = ComputeProviderDaemon()
        print(json.dumps(provider.build_announcement(), indent=2, sort_keys=True))
        return 0

    if action == "provider-health-job":
        from repryntt.economy.compute_provider import ComputeProviderDaemon

        provider = ComputeProviderDaemon()
        if not provider.config.enabled:
            provider.set_enabled(True)
        job = provider.submit_local_job(
            buyer_address=provider.config.wallet_address or "local",
            task_type="health_check",
            payload={},
        )
        completed = provider.run_once()
        if completed:
            _ok(f"Health job {completed.job_id}: {completed.state}")
        else:
            _warn(f"Health job {job.job_id} queued but not executed")
        return 0

    if action == "provider-run-once":
        from repryntt.economy.compute_provider import ComputeProviderDaemon

        provider = ComputeProviderDaemon()
        job = provider.run_once()
        if not job:
            _warn("No queued provider job was executed")
            return 0
        if job.state == "completed":
            _ok(f"Job {job.job_id} completed; receipt={job.receipt_hash}")
        else:
            _fail(f"Job {job.job_id} ended as {job.state}: {job.error}")
            return 1
        return 0

    if action == "stats":
        if not _port_open(6001):
            _fail("Blockchain node not running (need port 6001)")
            return 1
        try:
            import requests
            r = requests.get("http://127.0.0.1:6001/health", timeout=3)
            data = r.json()
            compute = data.get("compute", {})
            net = compute.get("network", {})
            esc = compute.get("escrow", {})

            _header("Network Compute")
            if net:
                print(f"  Nodes:      {net.get('total_nodes', 0)} total, {net.get('active_nodes', 0)} active")
                vram_total = net.get('total_gpu_vram_mb', 0)
                vram_avail = net.get('available_gpu_vram_mb', 0)
                print(f"  GPU VRAM:   {vram_total} MB total, {vram_avail} MB available")
                print(f"  CPU Cores:  {net.get('total_cpu_cores', 0)}")
                print(f"  RAM:        {net.get('total_ram_mb', 0)} MB")
                print(f"  Workloads:  {net.get('active_workloads', 0)} active / "
                      f"{net.get('total_capacity_slots', 0)} capacity")
                gpus = net.get('gpu_breakdown', {})
                if gpus:
                    _header("GPU Inventory")
                    for name, count in gpus.items():
                        print(f"  {count}x {name}")
                _header("Pricing")
                print(f"  Avg: {net.get('avg_price_per_hour_credits', 0):.2f} CR/hr")
                print(f"  Min: {net.get('min_price_per_hour_credits', 0):.2f} CR/hr")
                print(f"  Max: {net.get('max_price_per_hour_credits', 0):.2f} CR/hr")
            else:
                _warn("No compute marketplace data (marketplace may still be starting)")

            if esc:
                _header("Escrow Contracts")
                print(f"  Active: {esc.get('active_contracts', 0)}")
                print(f"  Completed: {esc.get('completed_contracts', 0)}")
                print(f"  Locked: {esc.get('total_locked_credits', 0):.2f} CR")
                print(f"  Paid to providers: {esc.get('total_paid_to_providers_credits', 0):.2f} CR")
                print(f"  DAO fees: {esc.get('total_dao_fees_credits', 0):.2f} CR")
        except Exception as e:
            _fail(f"Cannot reach node: {e}")
        return 0

    elif action == "providers":
        if not _port_open(6001):
            _fail("Blockchain node not running")
            return 1
        try:
            import requests
            r = requests.get("http://127.0.0.1:6001/health", timeout=3)
            data = r.json()
            compute = data.get("compute", {})
            net = compute.get("network", {})
            total = net.get("total_nodes", 0)

            if total == 0:
                _warn("No compute providers online yet")
                _warn("Start more nodes to see them listed")
                return 0

            _header(f"Compute Providers ({total} nodes)")
            print(f"  {'Node ID':<12} {'GPU':<24} {'VRAM':<8} {'Price/hr':<10} "
                  f"{'Capacity':<12} {'Rep':<5}")
            print(f"  {'─' * 75}")

            # For detailed per-node listing, we'd need a dedicated API endpoint
            # For now, show the inventory summary
            gpus = net.get('gpu_breakdown', {})
            for name, count in gpus.items():
                print(f"  {'...':<12} {name:<24} {'—':<8} {'—':<10} {count:>3} nodes {'—':<5}")

            print(f"\n  Avg price: {net.get('avg_price_per_hour_credits', 0):.2f} CR/hr")
        except Exception as e:
            _fail(f"Cannot reach node: {e}")
        return 0

    else:
        print("Usage: repryntt compute {stats|providers|provider-status|provider-enable|provider-disable|provider-announce|provider-health-job|provider-run-once}")
        return 0


# ── wallet ───────────────────────────────────────────────────────────────────

def cmd_wallet(args) -> int:
    """Personal wallet management — human-controlled, AI cannot access."""
    action = getattr(args, "wallet_action", None)

    if action == "create":
        from repryntt.economy.personal_wallet import personal_wallet_exists, create_personal_wallet, _get_personal_password
        if personal_wallet_exists():
            _fail("Personal wallet already exists. Use 'repryntt wallet show' to see it.")
            return 1

        print("\n\033[1m╔══════════════════════════════════════════════════════════════╗\033[0m")
        print("\033[1m║          CREATE PERSONAL WALLET                             ║\033[0m")
        print("\033[1m║                                                              ║\033[0m")
        print("\033[1m║  This wallet is YOURS. The AI cannot access it.              ║\033[0m")
        print("\033[1m║  Choose a strong password you will REMEMBER.                 ║\033[0m")
        print("\033[1m║  There is NO password recovery — lose it, lose your CR.      ║\033[0m")
        print("\033[1m╚══════════════════════════════════════════════════════════════╝\033[0m\n")

        password = _get_personal_password("Choose wallet password: ")
        if len(password) < 8:
            _fail("Password must be at least 8 characters")
            return 1
        confirm = _get_personal_password("Confirm password: ")
        if password != confirm:
            _fail("Passwords don't match")
            return 1

        wallet = create_personal_wallet(password)
        if not wallet:
            _fail("Failed to create wallet")
            return 1

        # Display seed phrase
        words = wallet._mnemonic.split()
        print(f"\n\033[1m  Your personal wallet address:\033[0m")
        print(f"  \033[32m{wallet.address}\033[0m\n")
        print(f"\033[1m  ⚠️  SEED PHRASE — write this down and store offline:\033[0m\n")
        for row in range(6):
            parts = []
            for col in range(4):
                idx = col * 6 + row
                if idx < len(words):
                    parts.append(f"  {idx + 1:2d}. {words[idx]:<12s}")
            print("".join(parts))
        print(f"\n  \033[31mNever share these words. Anyone with them owns your CR.\033[0m\n")

        _ok("Personal wallet created")
        print(f"  Use 'repryntt wallet withdraw <amount>' to move CR from node → personal\n")
        return 0

    elif action == "show":
        from repryntt.economy.personal_wallet import personal_wallet_exists, get_personal_address
        if not personal_wallet_exists():
            _warn("No personal wallet. Create one with: repryntt wallet create")
            return 1

        address = get_personal_address()
        print(f"\n\033[1m  Personal Wallet\033[0m")
        print(f"  Address: \033[32m{address}\033[0m")

        # Try to get balance from blockchain node
        try:
            import socket as sock
            import json as _json
            msg = _json.dumps({"type": "get_balance", "address": address}).encode()
            s = sock.socket(sock.AF_INET, sock.SOCK_STREAM)
            s.settimeout(5)
            s.connect(("127.0.0.1", 5001))
            s.sendall(len(msg).to_bytes(4, "big") + msg)
            header = s.recv(4)
            if len(header) == 4:
                resp_len = int.from_bytes(header, "big")
                resp_data = s.recv(resp_len)
                result = _json.loads(resp_data.decode())
                balance = result.get("balance", result.get("balance_cr", 0))
                print(f"  Balance: \033[33m{balance:.8f} CR\033[0m")
            s.close()
        except ConnectionRefusedError:
            _warn("Blockchain node not running — can't check balance")
        except Exception:
            _warn("Could not query balance")

        # Also show node wallet for comparison
        try:
            from repryntt.economy.node_wallet import get_node_wallet
            node = get_node_wallet()
            if node:
                print(f"\n\033[1m  Node Wallet (AI-managed)\033[0m")
                print(f"  Address: {node.address}")
                try:
                    import socket as sock
                    import json as _json
                    msg = _json.dumps({"type": "get_balance", "address": node.address}).encode()
                    s = sock.socket(sock.AF_INET, sock.SOCK_STREAM)
                    s.settimeout(5)
                    s.connect(("127.0.0.1", 5001))
                    s.sendall(len(msg).to_bytes(4, "big") + msg)
                    header = s.recv(4)
                    if len(header) == 4:
                        resp_len = int.from_bytes(header, "big")
                        resp_data = s.recv(resp_len)
                        result = _json.loads(resp_data.decode())
                        balance = result.get("balance", result.get("balance_cr", 0))
                        print(f"  Balance: {balance:.8f} CR")
                    s.close()
                except Exception:
                    pass
        except Exception:
            pass
        print()
        return 0

    elif action == "withdraw":
        from repryntt.economy.personal_wallet import personal_wallet_exists, withdraw_from_node, _get_personal_password
        if not personal_wallet_exists():
            _fail("No personal wallet. Create one first: repryntt wallet create")
            return 1

        amount = args.amount
        print(f"\n  Withdrawing {amount:.8f} CR from node wallet → personal wallet")
        password = _get_personal_password("Personal wallet password: ")

        result = withdraw_from_node(amount, password)
        if result.get("success"):
            _ok(f"Transferred {amount:.8f} CR to personal wallet")
            from_bal = result.get("from_balance", "?")
            to_bal = result.get("to_balance", "?")
            print(f"  Node wallet balance: {from_bal} CR")
            print(f"  Personal wallet balance: {to_bal} CR")
        else:
            _fail(result.get("error", "Transfer failed"))
        print()
        return 0

    elif action == "send":
        from repryntt.economy.personal_wallet import personal_wallet_exists, send_from_personal, _get_personal_password
        if not personal_wallet_exists():
            _fail("No personal wallet. Create one first: repryntt wallet create")
            return 1

        to_addr = args.to_address
        amount = args.amount
        print(f"\n  Sending {amount:.8f} CR → {to_addr[:16]}...")
        password = _get_personal_password("Personal wallet password: ")

        result = send_from_personal(to_addr, amount, password)
        if result.get("success"):
            _ok(f"Sent {amount:.8f} CR")
        else:
            _fail(result.get("error", "Send failed"))
        print()
        return 0

    elif action == "export":
        from repryntt.economy.personal_wallet import personal_wallet_exists, load_personal_wallet, _get_personal_password
        if not personal_wallet_exists():
            _fail("No personal wallet")
            return 1

        print("\n\033[31m  ⚠️  WARNING: This will display your seed phrase on screen.\033[0m")
        print("\033[31m  ⚠️  Make sure nobody is watching and no screen recorders are running.\033[0m\n")
        confirm = input("  Type 'yes' to continue: ").strip()
        if confirm.lower() != "yes":
            print("  Cancelled.")
            return 0

        password = _get_personal_password("Personal wallet password: ")
        wallet = load_personal_wallet(password)
        if not wallet or not wallet._mnemonic:
            _fail("Wrong password or corrupted wallet")
            return 1

        words = wallet._mnemonic.split()
        print(f"\n\033[1m  Seed phrase for {wallet.address}:\033[0m\n")
        for row in range(6):
            parts = []
            for col in range(4):
                idx = col * 6 + row
                if idx < len(words):
                    parts.append(f"  {idx + 1:2d}. {words[idx]:<12s}")
            print("".join(parts))
        print()
        return 0

    else:
        print("Usage: repryntt wallet {create|show|withdraw|send|export}")
        return 0


# ── Cortex: multi-model brain management ─────────────────────────────────

def cmd_cortex(args) -> int:
    action = getattr(args, "cortex_action", None)

    if action == "status":
        return _cortex_status()
    elif action == "setup":
        return _cortex_setup()
    elif action == "benchmark":
        return _cortex_benchmark()
    elif action == "train":
        region = getattr(args, "region", "conscious")
        return _cortex_train(region)
    elif action == "evolve":
        return _cortex_evolve()
    else:
        print("Usage: repryntt cortex {status|setup|benchmark|train|evolve}")
        return 0


def _cortex_status() -> int:
    """Show cortex regions, loaded models, memory budget."""
    from repryntt.cortex.model_config import load_config
    from repryntt.cortex.model_registry import get_registry
    from repryntt.hardware_profile import get_profile

    config = load_config()
    registry = get_registry()
    hw = get_profile()

    print(f"\n\033[1m🧠 Neural Cortex Status\033[0m")
    print(f"  Hardware: {hw.gpu_name or 'CPU only'} ({hw.gpu_vram_mb}MB VRAM, {hw.ram_mb}MB RAM)")
    print(f"  Budget:   {config.memory_budget_percent}% → ~{int((hw.gpu_vram_mb or hw.ram_mb) * config.memory_budget_percent / 100)}MB")
    print()

    # Regions
    print(f"\033[1m  Regions:\033[0m")
    for name, rcfg in config.regions.items():
        status = "enabled" if rcfg.enabled else "disabled"
        priority = ["CRITICAL", "HIGH", "NORMAL"][min(rcfg.priority, 2)]
        resident = " (resident)" if rcfg.resident else ""
        model = rcfg.model_name or "(auto-select)"
        print(f"    {name:12s}  {status:8s}  priority={priority:8s}  latency<{rcfg.max_latency_ms}ms  model={model}{resident}")

    # Models
    print(f"\n\033[1m  Registered Models:\033[0m")
    models = registry.all_models()
    if not models:
        print("    (none — run 'repryntt cortex setup' to download)")
    for m in models:
        on_disk = "✅" if registry.is_available_on_disk(m.name) else "❌"
        print(f"    {on_disk} {m.name:35s}  role={m.role:12s}  {m.param_count/1e6:.0f}M params  {m.vram_mb}MB  {m.format}")

    # Training data
    try:
        from repryntt.cortex.training.data_router import DataRouter
        router = DataRouter()
        stats = router.dataset_stats()
        if stats:
            print(f"\n\033[1m  Training Data:\033[0m")
            for region, info in stats.items():
                print(f"    {region:12s}  {info['examples']} examples  ({info['file_size_kb']:.1f}KB)")
    except Exception:
        pass

    print()
    return 0


def _cortex_setup() -> int:
    """Auto-detect hardware, suggest models, download."""
    from repryntt.hardware_profile import get_profile
    from repryntt.cortex.model_config import load_config, get_config_path
    from repryntt.cortex.model_registry import get_registry

    hw = get_profile()
    config = load_config()

    total_mem = hw.gpu_vram_mb if hw.has_gpu else hw.ram_mb
    budget = int(total_mem * config.memory_budget_percent / 100)

    print(f"\n\033[1m🧠 Neural Cortex Setup\033[0m")
    print(f"  Hardware:  {hw.gpu_name or 'CPU only'}")
    print(f"  VRAM:      {hw.gpu_vram_mb}MB")
    print(f"  RAM:       {hw.ram_mb}MB")
    print(f"  Budget:    {budget}MB ({config.memory_budget_percent}% of {'VRAM' if hw.has_gpu else 'RAM'})")
    print()

    # Select best model for budget
    candidates = config.models_for_role("conscious")
    selected = None
    for m in candidates:
        if m.vram_mb <= budget:
            selected = m  # Keep going — largest that fits

    if selected:
        print(f"  Recommended model: \033[1m{selected.name}\033[0m")
        print(f"    {selected.description}")
        print(f"    {selected.param_count/1e6:.0f}M params, {selected.vram_mb}MB, {selected.format}")
        print()

        # Check if already downloaded
        if selected.resolved_path().exists():
            print(f"  ✅ Already downloaded at {selected.path}")
        else:
            print(f"  Downloading {selected.name} from HuggingFace...")
            print(f"  (This requires 'huggingface_hub' and 'llama-cpp-python' packages)")
            try:
                # Try GGUF download from HuggingFace
                from huggingface_hub import hf_hub_download
                import shutil

                # For SmolLM2, GGUF files are at a separate repo
                # Try the standard GGUF conversion repo pattern
                gguf_repo = selected.hf_repo
                dest = selected.resolved_path()
                dest.parent.mkdir(parents=True, exist_ok=True)

                # SmolLM2 GGUF quantizations are typically in the main repo
                # or in bartowski/SmolLM2-*-GGUF repos
                gguf_name = dest.name
                print(f"  Searching for {gguf_name}...")

                # Try known GGUF repo patterns
                gguf_repos = [
                    f"bartowski/SmolLM2-{selected.param_count // 1000000}M-Instruct-GGUF",
                    selected.hf_repo,
                ]
                downloaded = False
                for repo in gguf_repos:
                    try:
                        path = hf_hub_download(
                            repo_id=repo,
                            filename=gguf_name,
                            local_dir=str(dest.parent),
                        )
                        print(f"  ✅ Downloaded to {path}")
                        downloaded = True
                        break
                    except Exception:
                        continue

                if not downloaded:
                    print(f"  ⚠️  Could not find GGUF file. You may need to manually convert:")
                    print(f"      pip install llama-cpp-python huggingface_hub")
                    print(f"      python -c \"from huggingface_hub import snapshot_download; snapshot_download('{selected.hf_repo}')\"")
                    print(f"      Then convert to GGUF and place at: {dest}")

            except ImportError:
                print(f"  ⚠️  Install huggingface_hub first: pip install huggingface_hub")
                print(f"      Then re-run: repryntt cortex setup")
    else:
        print(f"  ⚠️  No model fits in {budget}MB budget")
        print(f"      Increase memory_budget_percent in cortex_config.json or add more RAM/VRAM")

    # Save config
    config.save(get_config_path())
    print(f"\n  Config saved to {get_config_path()}")
    print()
    return 0


def _cortex_benchmark() -> int:
    """Run inference benchmarks on available models."""
    import time as _time
    from repryntt.cortex.model_registry import get_registry
    from repryntt.cortex.resource_manager import get_resource_manager

    registry = get_registry()
    mgr = get_resource_manager()

    models = [m for m in registry.all_models() if registry.is_available_on_disk(m.name)]
    if not models:
        print("\n  No models available on disk. Run 'repryntt cortex setup' first.\n")
        return 0

    print(f"\n\033[1m🧠 Neural Cortex Benchmark\033[0m\n")
    test_prompt = "Reflect on what it means to have a unique identity."

    for m in models:
        print(f"  Testing {m.name} ({m.param_count/1e6:.0f}M, {m.format})...")
        t0 = _time.monotonic()
        result = mgr.infer_llm(m.name, test_prompt, max_tokens=50, temperature=0.5)
        elapsed = (_time.monotonic() - t0) * 1000

        if result:
            tokens = len(result.split())
            tps = tokens / (elapsed / 1000) if elapsed > 0 else 0
            print(f"    ✅ {elapsed:.0f}ms, ~{tps:.1f} tokens/sec")
            print(f"    Output: {result[:100]}...")
        else:
            print(f"    ❌ Failed to load or infer")

        mgr.unload(m.name)
        print()

    return 0


def _cortex_train(region: str) -> int:
    """Trigger training for a region."""
    from repryntt.cortex.training.region_trainer import RegionTrainer

    trainer = RegionTrainer(region)
    print(f"\n\033[1m🧠 Training region: {region}\033[0m\n")

    if not trainer.should_train(min_examples=10):
        print(f"  Not enough training data or too soon since last training.")
        print(f"  Use the agent to generate more {region} data first.\n")
        return 0

    print(f"  Starting training...")
    result = trainer.train()

    if result["success"]:
        print(f"  ✅ Training complete!")
        print(f"  Adapter: {result.get('adapter_path', '')}")
        print(f"  Metrics: {result.get('metrics', {})}")
        trainer.activate_adapter()
        print(f"  Adapter activated.")
    else:
        print(f"  ❌ Training failed: {result.get('error', 'unknown')}")

    print()
    return 0


def _cortex_evolve() -> int:
    """Run one evolution cycle across all trainable regions."""
    from repryntt.cortex.model_config import load_config
    from repryntt.cortex.training.region_trainer import RegionTrainer

    config = load_config()
    print(f"\n\033[1m🧠 Neural Cortex Evolution Cycle\033[0m\n")

    for name, rcfg in config.regions.items():
        if not rcfg.enabled:
            continue

        trainer = RegionTrainer(name)
        if trainer.should_train(min_examples=10):
            print(f"  Training {name}...")
            result = trainer.train()
            if result["success"]:
                trainer.activate_adapter()
                print(f"    ✅ {name} trained and activated")
            else:
                print(f"    ❌ {name} failed: {result.get('error', '')}")
        else:
            print(f"  ⏭️  {name}: skipped (not enough data or too recent)")

    print()
    return 0


# ── Main parser ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="repryntt",
        description="Repryntt — Autonomous AI Framework",
    )
    parser.add_argument("--version", action="version",
                        version=f"repryntt {repryntt.__version__}")
    sub = parser.add_subparsers(dest="command")

    # doctor
    sub.add_parser("doctor", help="Health-check all services and dependencies")

    # status
    sub.add_parser("status", help="Show system overview")

    # roster
    p_roster = sub.add_parser("roster", help="List all agents by department")
    p_roster.add_argument("-d", "--department", default=None,
                          help="Filter by department name (substring match)")

    # assign
    p_assign = sub.add_parser("assign", help="Assign a task")
    p_assign.add_argument("task", nargs="+", help="Task description")
    p_assign.add_argument("-a", "--agent", default=None,
                          help="Target a specific agent ID (default: auto-route)")

    # setup / onboard
    sub.add_parser("setup", help="Launch the visual setup wizard")
    sub.add_parser("onboard", help="Launch the visual setup wizard (alias for setup)")

    # start / stop / services / toggle-llm
    p_start = sub.add_parser("start", help="Full production startup")
    p_start.add_argument("--no-llm", action="store_true",
                          help="Skip starting local LLM")
    p_start.add_argument("--with-trading", action="store_true",
                          help="Enable trading pipeline (off by default)")
    p_start.add_argument("--no-evolution", action="store_true",
                          help="Skip evolution loop")
    p_start.add_argument("--no-blockchain", action="store_true",
                          help="Skip blockchain node")
    sub.add_parser("stop", help="Graceful shutdown of all services")

    p_svc = sub.add_parser("services", help="Manage individual services")
    p_svc.add_argument("action", nargs="?", default="list",
                        choices=["list", "status", "start", "stop"],
                        help="Action to perform")
    p_svc.add_argument("name", nargs="?", default=None,
                        help="Service name (for start/stop)")

    sub.add_parser("toggle-llm", help="Start/stop local llama.cpp server")

    p_desktop = sub.add_parser("desktop", help="Launch desktop application (native window)")
    p_desktop.add_argument("--no-llm", action="store_true",
                           help="Skip starting local LLM")
    p_desktop.add_argument("--with-trading", action="store_true",
                           help="Enable trading pipeline (off by default)")
    p_desktop.add_argument("--no-evolution", action="store_true",
                           help="Skip evolution loop")
    p_desktop.add_argument("--no-manage", action="store_true",
                           help="Don't manage services (window only, assumes services already running)")

    p_mobile = sub.add_parser("mobile", help="Start mobile dashboard (Android/iOS PWA)")
    p_mobile.add_argument("--no-llm", action="store_true",
                          help="Skip starting local LLM")
    p_mobile.add_argument("--with-trading", action="store_true",
                          help="Enable trading pipeline (off by default)")
    p_mobile.add_argument("--no-evolution", action="store_true",
                          help="Skip evolution loop")
    p_mobile.add_argument("--no-manage", action="store_true",
                          help="Don't manage services (dashboard only)")
    p_mobile.add_argument("--port", type=int, default=8891,
                          help="Dashboard port (default: 8891)")

    # node (legacy Python)
    p_node = sub.add_parser("node", help="Legacy Python blockchain node management")
    p_node_sub = p_node.add_subparsers(dest="node_action")
    p_node_sub.add_parser("status", help="Show blockchain node status")
    p_node_sub.add_parser("peers", help="List connected peers")
    p_node_start = p_node_sub.add_parser("start", help="Start blockchain node standalone")
    p_node_start.add_argument("--port", type=int, default=5001, help="TCP port (default: 5001)")

    # chain (Rust blockchain — runs 24/7 via systemd)
    p_chain = sub.add_parser("chain", help="Rust blockchain node (24/7 via systemd)")
    p_chain_sub = p_chain.add_subparsers(dest="chain_action")
    p_chain_sub.add_parser("install", help="Install systemd service, enable, and start")
    p_chain_sub.add_parser("start", help="Start the blockchain")
    p_chain_sub.add_parser("stop", help="Stop the blockchain")
    p_chain_sub.add_parser("restart", help="Restart the blockchain")
    p_chain_sub.add_parser("status", help="Show chain height, balance, and service health")
    p_chain_checkpoint = p_chain_sub.add_parser("checkpoint", help="Signed canonical checkpoints")
    p_chain_checkpoint_sub = p_chain_checkpoint.add_subparsers(dest="checkpoint_action")
    p_chain_checkpoint_sub.add_parser("create", help="Sign current local tip as canonical")
    p_chain_checkpoint_sub.add_parser("show", help="Show latest signed checkpoint")
    p_chain_checkpoint_sub.add_parser("verify", help="Verify checkpoint signature and local chain")
    p_chain_logs = p_chain_sub.add_parser("logs", help="View blockchain logs from journald")
    p_chain_logs.add_argument("-n", "--lines", type=int, default=50, help="Number of log lines")
    p_chain_logs.add_argument("-f", "--follow", action="store_true", help="Follow log output")
    p_chain_sub.add_parser("enable", help="Auto-start blockchain on boot")
    p_chain_sub.add_parser("disable", help="Don't auto-start on boot")

    # compute
    p_compute = sub.add_parser("compute", help="Compute marketplace management")
    p_compute_sub = p_compute.add_subparsers(dest="compute_action")
    p_compute_sub.add_parser("stats", help="Show network compute statistics")
    p_compute_sub.add_parser("providers", help="List compute providers")
    p_compute_sub.add_parser("provider-status", help="Show this machine's provider runtime status")
    p_compute_sub.add_parser("provider-enable", help="Enable this machine as a compute provider")
    p_compute_sub.add_parser("provider-disable", help="Disable this machine as a compute provider")
    p_compute_sub.add_parser("provider-announce", help="Print this machine's signed provider announcement")
    p_compute_sub.add_parser("provider-health-job", help="Run a local provider health-check job")
    p_compute_sub.add_parser("provider-run-once", help="Execute one queued local provider job")

    # wallet (personal wallet management — human only)
    p_wallet = sub.add_parser("wallet", help="Personal wallet (human-controlled, separate from node)")
    p_wallet_sub = p_wallet.add_subparsers(dest="wallet_action")
    p_wallet_sub.add_parser("create", help="Create a new personal wallet")
    p_wallet_sub.add_parser("show", help="Show wallet address and balance")
    p_w_withdraw = p_wallet_sub.add_parser("withdraw", help="Transfer CR from node wallet → personal wallet")
    p_w_withdraw.add_argument("amount", type=float, help="Amount in CR to withdraw")
    p_w_send = p_wallet_sub.add_parser("send", help="Send CR from personal wallet to any address")
    p_w_send.add_argument("to_address", help="Destination wallet address (40 hex chars)")
    p_w_send.add_argument("amount", type=float, help="Amount in CR to send")
    p_wallet_sub.add_parser("export", help="Display seed phrase (DANGEROUS — screen visible!)")

    # cortex (Neural Cortex — multi-model brain)
    p_cortex = sub.add_parser("cortex", help="Neural Cortex — multi-model brain management")
    p_cortex_sub = p_cortex.add_subparsers(dest="cortex_action")
    p_cortex_sub.add_parser("status", help="Show cortex regions, loaded models, memory budget")
    p_cortex_sub.add_parser("setup", help="Auto-detect hardware, suggest models, download")
    p_cortex_sub.add_parser("benchmark", help="Run inference benchmarks on loaded models")
    p_cortex_train = p_cortex_sub.add_parser("train", help="Trigger training for a region")
    p_cortex_train.add_argument("region", nargs="?", default="conscious", help="Region to train (default: conscious)")
    p_cortex_sub.add_parser("evolve", help="Run one evolution cycle across all trainable regions")

    args = parser.parse_args()

    dispatch = {
        "doctor": cmd_doctor,
        "status": cmd_status,
        "roster": cmd_roster,
        "assign": cmd_assign,
        "setup": cmd_setup,
        "onboard": cmd_onboard,
        "start": cmd_start,
        "stop": cmd_stop,
        "services": cmd_services,
        "toggle-llm": cmd_toggle_llm,
        "desktop": cmd_desktop,
        "mobile": cmd_mobile,
        "node": cmd_node,
        "chain": cmd_chain,
        "compute": cmd_compute,
        "wallet": cmd_wallet,
        "cortex": cmd_cortex,
    }

    if args.command in dispatch:
        # Ensure first-run initialization on any command
        _ensure_initialized()
        sys.exit(dispatch[args.command](args))
    else:
        parser.print_help()
        sys.exit(0)


def _ensure_initialized() -> None:
    """Run first-boot initialization silently. Idempotent.
    Creates directories and identity but does NOT create ai_config.json."""
    try:
        from repryntt.paths import get_data_dir
        from repryntt.first_run import run_first_boot
        dd = get_data_dir()
        first_time = run_first_boot(dd)
        if first_time:
            print("\033[1m✨ Welcome to Repryntt!\033[0m")
            print("  Node identity generated and bootstrap templates installed.")
            print("  Run \033[1mrepryntt setup\033[0m to configure your system.\n")
    except Exception:
        pass  # Non-fatal — let the actual command handle errors
