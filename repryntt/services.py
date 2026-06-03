"""
repryntt.services — Process manager for all repryntt services.

Handles starting, stopping, and health-checking every component:

  Phase 1: System init (dirs, config, python env)
  Phase 2: Local LLM (llama.cpp on port 8080)
  Phase 3: Core services (web, nexus, chat, unified, APIs)
  Phase 4: Agent daemon (persistent agents + evolution loop)
  Phase 5: Trading pipeline (fetcher, monitor, trend, cleanup, dashboard)
  Phase 6: Verification (health-check all ports)

Usage:
    from repryntt.services import ServiceManager
    mgr = ServiceManager()
    mgr.start_all()          # full production startup
    mgr.stop_all()           # graceful shutdown
    mgr.status()             # check what's running
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from repryntt.paths import get_data_dir, brain_dir, models_dir, logs_dir

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

PID_DIR = get_data_dir() / "pids"
PID_DIR.mkdir(parents=True, exist_ok=True)

LOG_DIR = logs_dir()
ARCHIVE_DIR = LOG_DIR / "archive"
ARCHIVE_KEEP_DAYS = 60   # compress logs older than 1 day, delete after 60


def _archive_log(log_path: Path) -> None:
    """Archive an existing log file before truncating it on service restart.

    Moves the current log to logs/archive/<name>.YYYY-MM-DD_HH-MM-SS.log
    then gzips archives older than 1 day. Deletes archives older than
    ARCHIVE_KEEP_DAYS to keep disk usage bounded.
    """
    if not log_path.exists() or log_path.stat().st_size == 0:
        return

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    dest = ARCHIVE_DIR / f"{log_path.stem}.{ts}.log"
    try:
        shutil.move(str(log_path), str(dest))
    except Exception as e:
        logger.debug(f"Log archive move failed for {log_path.name}: {e}")
        return

    # Compress archives older than 1 day
    cutoff_compress = time.time() - 86400
    cutoff_delete   = time.time() - (ARCHIVE_KEEP_DAYS * 86400)
    for f in ARCHIVE_DIR.glob("*.log"):
        try:
            if f.stat().st_mtime < cutoff_compress:
                gz_path = f.with_suffix(".log.gz")
                with f.open("rb") as f_in, gzip.open(gz_path, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
                f.unlink()
        except Exception:
            pass
    for f in ARCHIVE_DIR.glob("*.log.gz"):
        try:
            if f.stat().st_mtime < cutoff_delete:
                f.unlink()
        except Exception:
            pass


# ── Service definition ───────────────────────────────────────────────────────

@dataclass
class ServiceDef:
    """Describes a single background service."""
    name: str
    module: str           # python -m <module>
    port: Optional[int]   # TCP port to health-check (None = no port)
    group: str            # phase grouping
    delay: int = 3        # seconds to wait after start before next
    args: List[str] = field(default_factory=list)
    optional: bool = False
    env: Dict[str, str] = field(default_factory=dict)


# Master service registry — order matters (start top-down, stop bottom-up)
# NOTE: chat-server, unified-interface, tool-api, external-api, command-center,
#       and trading-dashboard are now consolidated into the Nexus app as blueprints.
#       They are registered in nexus_app.py at startup, so only Nexus needs a port.
SERVICES: List[ServiceDef] = [
    # ── Phase 2: Core web services
    ServiceDef(
        name="web-server",
        module="repryntt.web.web_server",
        port=5000,
        group="core",
        delay=3,
    ),
    ServiceDef(
        name="nexus",
        module="repryntt.web.nexus_app",
        port=8089,
        group="core",
        delay=5,
    ),

    # ── Phase 3: Agent daemon + evolution
    ServiceDef(
        name="evolution-loop",
        module="repryntt.core.heartbeat.evolution_loop",
        port=None,
        group="agents",
        delay=5,
        optional=True,
    ),
    ServiceDef(
        name="agent-daemon",
        module="repryntt.agents.persistent_agents",
        port=None,
        group="agents",
        delay=5,
        # No --spawn arg: Andrew/JARVIS is auto-seeded by _load_state on
        # fresh installs (and loaded from saved state on subsequent boots).
        # Adding --spawn would attempt to create a 2nd agent on top of him,
        # which fails (agent_spawning: false by default) and logs noise.
        args=["--provider", "", "--interval", "120"],
    ),

    # ── Phase 3c: Stability watchdog (memory + service liveness + log staleness)
    # Always on by default. Disable with REPRYNTT_WATCHDOG_DISABLE=1.
    ServiceDef(
        name="watchdog",
        module="repryntt.core.watchdog",
        port=None,
        group="agents",
        delay=2,
        optional=True,
    ),

    # ── Phase 4: Trading pipeline
    # ── Phase 3b: Blockchain node
    ServiceDef(
        name="blockchain-node",
        module="repryntt.economy.qnode2",
        port=5001,
        group="blockchain",
        delay=5,
    ),

    # ── Phase 4: Trading pipeline
    ServiceDef(
        name="token-fetcher",
        module="repryntt.trading.token_fetcher",
        port=None,
        group="trading",
        delay=3,
    ),
    ServiceDef(
        name="token-monitor",
        module="repryntt.trading.token_monitor",
        port=None,
        group="trading",
        delay=5,
    ),
    ServiceDef(
        name="trend-agent",
        module="repryntt.trading.trend_agent",
        port=None,
        group="trading",
        delay=2,
    ),
    ServiceDef(
        name="token-cleanup",
        module="repryntt.trading.token_cleanup",
        port=None,
        group="trading",
        delay=0,
    ),
    # degen-terminal removed — now served by trading_bp blueprint on Nexus :8089 at /trading/
]


# ── Color helpers ────────────────────────────────────────────────────────────

def _ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m {msg}")

def _warn(msg: str) -> None:
    print(f"  \033[33m!\033[0m {msg}")

def _fail(msg: str) -> None:
    print(f"  \033[31m✗\033[0m {msg}")

def _phase(title: str) -> None:
    print(f"\n\033[1;34m{'─' * 50}\033[0m")
    print(f"\033[1m  {title}\033[0m")
    print(f"\033[1;34m{'─' * 50}\033[0m")

def _ts() -> str:
    return time.strftime("%H:%M:%S")


# ── Process helpers ──────────────────────────────────────────────────────────

def _pid_file(name: str) -> Path:
    return PID_DIR / f"{name}.pid"


def _is_port_open(port: int, timeout: float = 1.0) -> bool:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    except (OSError, TimeoutError):
        return False
    finally:
        s.close()


def _is_pid_live(pid: int) -> bool:
    """Return True only for live (non-zombie) processes."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError, SystemError):
        return False

    # On Linux, zombies still pass os.kill(pid, 0). Filter them out.
    if sys.platform.startswith("linux"):
        status_path = Path(f"/proc/{pid}/status")
        try:
            for line in status_path.read_text().splitlines():
                if line.startswith("State:"):
                    return "(zombie)" not in line.lower()
        except (FileNotFoundError, PermissionError, OSError):
            return False

    return True


def _read_pid(name: str) -> Optional[int]:
    pf = _pid_file(name)
    if not pf.exists():
        return None
    try:
        pid = int(pf.read_text().strip())
        # Keep PID only if process is truly alive (not zombie).
        if not _is_pid_live(pid):
            pf.unlink(missing_ok=True)
            return None
        return pid
    except (ValueError, ProcessLookupError, PermissionError, OSError, SystemError):
        pf.unlink(missing_ok=True)
        return None


def _kill_pid(pid: int, timeout: int = 5) -> bool:
    """Send SIGTERM then SIGKILL if needed. Returns True if killed."""
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OSError, SystemError):
        return True

    for _ in range(timeout):
        time.sleep(1)
        if not _is_pid_live(pid):
            return True

    # Force kill (SIGKILL not available on Windows)
    _sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
    try:
        os.kill(pid, _sigkill)
        time.sleep(0.5)
    except (ProcessLookupError, OSError, SystemError):
        pass
    return True


# ── ServiceManager ───────────────────────────────────────────────────────────

class ServiceManager:
    """Orchestrates starting/stopping all repryntt services."""

    def __init__(self) -> None:
        self.data_dir = get_data_dir()
        self.brain = brain_dir()
        self.models = models_dir()
        self.logs = logs_dir()
        self._started: List[str] = []

    # ── Phase 1: System init ─────────────────────────────────────────────

    def check_prerequisites(self) -> bool:
        """Verify system is ready. Returns True if OK."""
        _phase("Phase 1: System Initialization")
        ok = True

        # First-run bootstrap (idempotent — safe on every startup)
        from repryntt.first_run import run_first_boot
        first_time = run_first_boot(self.data_dir)
        if first_time:
            _ok("First-run initialization complete")
            _ok("  → Node identity generated")
            _ok("  → Bootstrap templates installed")
        else:
            _ok("Data directory verified")

        # Config check
        ai_cfg = self.brain / "ai_config.json"
        if ai_cfg.exists():
            try:
                cfg = json.loads(ai_cfg.read_text())
                provider = cfg.get("ai_provider", {}).get("provider", "local")
                _ok(f"ai_config.json loaded (provider: {provider})")
            except json.JSONDecodeError:
                _warn("ai_config.json malformed — defaults will be used")
        else:
            _warn("ai_config.json not found — run 'repryntt setup'")

        # Python environment
        for mod in ("flask", "requests", "aiohttp"):
            try:
                __import__(mod)
            except ImportError:
                _fail(f"Missing: {mod} — pip install {mod}")
                ok = False
        if ok:
            _ok("Python environment verified")

        # Dirs
        for d in [self.logs, PID_DIR]:
            d.mkdir(parents=True, exist_ok=True)
        _ok(f"Data: {self.data_dir}")

        return ok

    # ── Phase 2: LLM ────────────────────────────────────────────────────

    def start_llm(self) -> bool:
        """Start the local llama.cpp server if not already running."""
        _phase("Phase 2: Local LLM")

        # Check disabled flag
        disabled_flag = self.data_dir / ".llm_disabled"
        if disabled_flag.exists():
            _warn("LLM disabled by toggle — skipping")
            return False

        if _is_port_open(8080):
            _ok("llama.cpp already running on :8080")
            return True

        # Try llm_toggle.sh first (has model selection logic)
        toggle_sh = self.data_dir / "llm_toggle.sh"
        if not toggle_sh.exists():
            # Fallback: check SAIGE_DIR env or default location
            saige_dir = Path(os.environ.get("SAIGE_DIR", ""))
            if saige_dir.is_dir():
                saige_toggle = saige_dir / "llm_toggle.sh"
                if saige_toggle.exists():
                    toggle_sh = saige_toggle

        if toggle_sh.exists():
            print(f"  [{_ts()}] Starting LLM via {toggle_sh.name}...")
            log_path = self.logs / "llama_server.log"
            subprocess.Popen(
                ["bash", str(toggle_sh), "on"],
                stdout=log_path.open("a", encoding="utf-8", errors="replace"),
                stderr=subprocess.STDOUT,
            )
            # Wait for health
            print(f"  [{_ts()}] Waiting for model load", end="", flush=True)
            for _ in range(30):
                time.sleep(2)
                if _is_port_open(8080):
                    print()
                    _ok("llama.cpp healthy on :8080")
                    return True
                print(".", end="", flush=True)
            print()
            _fail("llama.cpp failed to start within 60s")
            return False

        # Direct llama-server fallback
        import shutil
        llama_bin = shutil.which("llama-server")
        if not llama_bin:
            _warn("llama-server not found — LLM unavailable")
            return False

        gguf_files = list(self.models.glob("**/*.gguf")) if self.models.exists() else []
        if not gguf_files:
            _warn("No .gguf models found — LLM unavailable")
            return False

        model = gguf_files[0]
        from repryntt.hardware_profile import get_profile
        hw = get_profile()
        ngl = str(hw.llm_gpu_layers)
        mode = "GPU" if hw.has_gpu else "CPU-only"
        print(f"  [{_ts()}] Starting llama-server with {model.name} ({mode}, ngl={ngl})...")
        log_path = self.logs / "llama_server.log"
        _archive_log(log_path)
        # Smaller context + mmap-friendly flags reduce RAM pressure so GGML compute
        # allocations do not abort the daemon under memory stress. --mlock is
        # intentionally NOT used: locking model pages in RAM prevents the kernel
        # from swapping them out, which has caused hard aborts on low-RAM hosts
        # (GGML_ASSERT(buffer) failed). Opt in with REPRYNTT_LLAMA_MLOCK=1.
        ctx_len = os.environ.get("REPRYNTT_LLAMA_CTX", "2048")
        cmd = [llama_bin, "-m", str(model), "-ngl", ngl, "-c", ctx_len,
               "--host", "0.0.0.0", "--port", "8080", "--no-warmup"]
        if os.environ.get("REPRYNTT_LLAMA_MLOCK", "").strip() in ("1", "true", "yes"):
            cmd.append("--mlock")
        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(
            cmd,
            stdout=log_path.open("w", encoding="utf-8", errors="replace"),
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
        )
        _pid_file("llama").write_text(str(proc.pid))

        print(f"  [{_ts()}] Waiting for model load", end="", flush=True)
        for _ in range(30):
            time.sleep(2)
            if _is_port_open(8080):
                print()
                _ok(f"llama.cpp healthy on :8080 (PID {proc.pid})")
                return True
            print(".", end="", flush=True)
        print()
        _fail("llama.cpp failed to start within 60s")
        return False

    # ── Nav stack (ROS2) ─────────────────────────────────────────────────

    def _start_nav_stack(self) -> None:
        """Start or verify the andrew-nav systemd service (ROS2 nav stack)."""
        nav_enabled = os.environ.get("REPRYNTT_ENABLE_NAV", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "y",
        )
        if not nav_enabled:
            _warn("Nav stack skipped (set REPRYNTT_ENABLE_NAV=1 on robot hosts)")
            return
        if not sys.platform.startswith("linux") or not shutil.which("systemctl"):
            _warn("Nav stack requires a Linux systemd host")
            return
        try:
            rc = subprocess.call(
                ["systemctl", "is-active", "--quiet", "andrew-nav"],
                timeout=5,
            )
            if rc == 0:
                _ok("Andrew nav stack running (systemd: andrew-nav)")
                return
        except Exception:
            pass

        # Try to start it
        try:
            rc = subprocess.call(["sudo", "systemctl", "start", "andrew-nav"], timeout=15)
            if rc == 0:
                _ok("Andrew nav stack started")
            else:
                _warn("andrew-nav service not installed — run: repryntt chain install --nav")
                _warn("  or manually: sudo systemctl start andrew-nav")
        except Exception as e:
            _warn(f"Could not start andrew-nav: {e}")

    def _stop_nav_stack(self) -> None:
        """Stop the andrew-nav systemd service."""
        try:
            rc = subprocess.call(["sudo", "systemctl", "stop", "andrew-nav"], timeout=10)
            if rc == 0:
                _ok("Andrew nav stack stopped")
        except Exception:
            pass

    # ── Service start/stop ───────────────────────────────────────────────

    def _start_service(self, svc: ServiceDef) -> bool:
        """Start a single service as a background process."""
        # Already running?
        existing_pid = _read_pid(svc.name)
        if existing_pid:
            if svc.port and _is_port_open(svc.port):
                _warn(f"{svc.name} already running (PID {existing_pid})")
                return True
            elif svc.port is None:
                _warn(f"{svc.name} already running (PID {existing_pid})")
                return True

        # Check port conflict
        if svc.port and _is_port_open(svc.port):
            _warn(f"{svc.name}: port {svc.port} already in use — skipping")
            return True

        log_path = self.logs / f"{svc.name}.log"
        _archive_log(log_path)
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        # Force Python UTF-8 mode (PEP 540) in every spawned service.
        # Without this, Nexus and other Flask services boot in cp1252 on
        # Windows and crash silently on the first open() of a file with
        # an emoji or special char (templates, configs, etc).
        env["PYTHONUTF8"] = "1"
        # Tell child services they're managed — prevents nexus from
        # auto-starting an in-process agent daemon (ServiceManager starts
        # agent-daemon as a separate process).
        env["REPRYNTT_MANAGED"] = "1"
        env.update(svc.env)

        cmd = [sys.executable, "-m", svc.module] + svc.args

        # On Windows, use CREATE_NEW_PROCESS_GROUP so child doesn't get parent's CTRL_C
        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=log_path.open("w", encoding="utf-8", errors="replace"),
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=creation_flags,
            )
        except Exception as e:
            _fail(f"{svc.name}: failed to launch — {e}")
            return False

        # Verify process didn't die immediately
        time.sleep(1)
        if proc.poll() is not None:
            _fail(f"{svc.name}: died on start (exit {proc.returncode}) — check {log_path}")
            return False

        _pid_file(svc.name).write_text(str(proc.pid))
        self._started.append(svc.name)

        # Wait for port if applicable
        if svc.port:
            for _ in range(svc.delay * 2):
                time.sleep(0.5)
                if _is_port_open(svc.port):
                    _ok(f"{svc.name} started (PID {proc.pid}, :{svc.port})")
                    return True
            _warn(f"{svc.name} started (PID {proc.pid}) but :{svc.port} not yet responding")
            return True
        else:
            if svc.delay:
                time.sleep(svc.delay)
            _ok(f"{svc.name} started (PID {proc.pid})")
            return True

    def _stop_service(self, name: str) -> bool:
        """Stop a single service by name."""
        pid = _read_pid(name)
        if not pid:
            return True  # already stopped

        _kill_pid(pid)
        _pid_file(name).unlink(missing_ok=True)
        _ok(f"{name} stopped (PID {pid})")
        return True

    # ── Group operations ─────────────────────────────────────────────────

    def start_group(self, group: str) -> int:
        """Start all services in a group. Returns count of successful starts."""
        svcs = [s for s in SERVICES if s.group == group]
        if not svcs:
            _warn(f"No services in group '{group}'")
            return 0

        count = 0
        for svc in svcs:
            if self._start_service(svc):
                count += 1
        return count

    def stop_group(self, group: str) -> None:
        """Stop all services in a group (reverse order)."""
        svcs = [s for s in SERVICES if s.group == group]
        for svc in reversed(svcs):
            self._stop_service(svc.name)

    # ── Full lifecycle ───────────────────────────────────────────────────

    def start_all(
        self,
        skip_llm: bool = False,
        skip_trading: bool = False,
        skip_evolution: bool = False,
        skip_blockchain: bool = False,
    ) -> int:
        """Full production startup. Returns exit code (0 = success)."""
        print("\033[1m🚀 Starting Repryntt Production System\033[0m")
        print("=" * 50)

        # Phase 1: Prerequisites
        if not self.check_prerequisites():
            _fail("Prerequisites check failed — aborting")
            return 1

        # Phase 2: LLM
        llm_healthy = False
        if not skip_llm:
            llm_healthy = self.start_llm()
        else:
            _phase("Phase 2: Local LLM")
            _warn("Skipped (--no-llm)")

        # Phase 2.5: Andrew nav stack (ROS2 Nav2 + slam_toolbox)
        _phase("Phase 2.5: Andrew Nav Stack (ROS2)")
        self._start_nav_stack()

        # Phase 3: Core services
        _phase("Phase 3: Core Services")
        core_count = self.start_group("core")

        # Phase 3b: Blockchain (Rust — systemd on Linux, direct process on Windows)
        _phase("Phase 3b: Blockchain Node (Rust)")
        if skip_blockchain:
            _warn("Blockchain check skipped (--no-blockchain)")
        else:
            _is_win = sys.platform == "win32"
            chain_running = False
            if _is_win:
                # On Windows check if the RPC port is reachable or a PID file exists
                import socket as _sock
                try:
                    with _sock.create_connection(("127.0.0.1", 9332), timeout=1):
                        chain_running = True
                except Exception:
                    pass
                if chain_running:
                    _ok("Rust blockchain running (process)")
                else:
                    _warn("Rust blockchain not running")
                    _warn("Start with: repryntt chain start")
            else:
                try:
                    import subprocess as _sp
                    rc = _sp.call(
                        ["systemctl", "is-active", "--quiet", "repryntt-chain"],
                        timeout=5,
                    )
                    if rc == 0:
                        chain_running = True
                        _ok("Rust blockchain running (systemd: repryntt-chain)")
                    else:
                        _warn("Rust blockchain not running")
                        _warn("Install with: repryntt chain install")
                        _warn("Or start with: repryntt chain start")
                except Exception:
                    _warn("Cannot check systemd — run 'repryntt chain install' to set up")

        # Phase 4: Agents / Evolution
        _phase("Phase 4: Agents & Evolution")
        if skip_evolution:
            _warn("Evolution loop skipped (--no-evolution)")
            # Still start agent daemon even without evolution loop
            self._start_service(
                next(s for s in SERVICES if s.name == "agent-daemon"))
        else:
            self.start_group("agents")

        # Phase 5: Trading pipeline
        _phase("Phase 5: Trading Pipeline")
        if skip_trading:
            _warn("Trading pipeline skipped (--no-trading)")
        else:
            self.start_group("trading")

        # Phase 6: Verification
        self._verify()

        # Write master PID
        master_pid_file = self.data_dir / "repryntt.pid"
        master_pid_file.write_text(str(os.getpid()))

        return 0

    def stop_all(self) -> int:
        """Graceful shutdown of everything EXCEPT the blockchain.

        The Rust blockchain node runs independently via systemd (repryntt-chain)
        and is NOT stopped by 'repryntt stop'. Use 'repryntt chain stop' to
        stop the blockchain explicitly.
        """
        print("\033[1m🛑 Stopping Repryntt Production System\033[0m")
        print("=" * 50)

        # ── First: if the stack is running as a systemd unit, stop the
        # unit via systemctl. Otherwise systemd's Restart=always sees us
        # killing the process directly as a crash and respawns it after
        # 15s — the symptom: "repryntt stop" appears to succeed but the
        # daemon comes back. systemctl stop tells systemd we mean it.
        if sys.platform.startswith("linux") and shutil.which("systemctl"):
            try:
                r = subprocess.run(
                    ["systemctl", "is-active", "--quiet", "repryntt-stack"],
                    timeout=5,
                )
                if r.returncode == 0:
                    _phase("Stopping: systemd unit repryntt-stack")
                    rc = subprocess.call(
                        ["sudo", "systemctl", "stop", "repryntt-stack"],
                        timeout=30,
                    )
                    if rc == 0:
                        _ok("systemd repryntt-stack stopped (won't auto-restart)")
                    else:
                        _warn(
                            "Failed to stop systemd unit. Run manually:\n"
                            "    sudo systemctl stop repryntt-stack"
                        )
            except Exception as e:
                _warn(f"systemd stop check failed (non-fatal): {e}")

        # Stop nav stack first (safe to do before agents)
        _phase("Stopping: Andrew nav stack")
        self._stop_nav_stack()

        # Stop in reverse group order — skip blockchain (runs via systemd)
        for group in ["trading", "agents", "core"]:
            _phase(f"Stopping: {group}")
            self.stop_group(group)

        # Also stop legacy Python blockchain-node if somehow still running
        legacy_pid = _read_pid("blockchain-node")
        if legacy_pid:
            _phase("Stopping: legacy Python blockchain")
            _kill_pid(legacy_pid)
            _pid_file("blockchain-node").unlink(missing_ok=True)
            _ok("Legacy Python blockchain stopped")

        if sys.platform == "win32":
            _ok("⛓  Rust blockchain is managed separately — use 'repryntt chain stop' to stop it")
        else:
            _ok("⛓  Rust blockchain continues running (systemd) — use 'repryntt chain stop' to stop")

        # Stop LLM
        _phase("Stopping: LLM")
        llm_pid = _read_pid("llama")
        if llm_pid:
            _kill_pid(llm_pid)
            _pid_file("llama").unlink(missing_ok=True)
            _ok(f"llama.cpp stopped (PID {llm_pid})")
        else:
            _ok("llama.cpp: not managed by repryntt")

        # Cleanup orphans by known process patterns
        _phase("Orphan Cleanup")
        patterns = [
            "repryntt.web.", "repryntt.trading.",
            "repryntt.core.heartbeat", "llama-server",
        ]
        for pat in patterns:
            from repryntt.platform_utils import kill_process_by_name
            kill_process_by_name(pat)
        _ok("Orphan processes cleaned")

        # Remove master PID
        master_pid = self.data_dir / "repryntt.pid"
        master_pid.unlink(missing_ok=True)

        # Clean PID dir
        for pf in PID_DIR.glob("*.pid"):
            pf.unlink(missing_ok=True)

        print(f"\n\033[32m✓ Repryntt shutdown complete\033[0m")
        return 0

    def status(self) -> Dict[str, dict]:
        """Check status of all services. Returns dict of name → info."""
        results = {}
        for svc in SERVICES:
            pid = _read_pid(svc.name)
            port_ok = _is_port_open(svc.port) if svc.port else None
            running = pid is not None
            results[svc.name] = {
                "group": svc.group,
                "pid": pid,
                "port": svc.port,
                "port_ok": port_ok,
                "running": running,
                "healthy": running and (port_ok is True or port_ok is None),
            }
        # LLM is special
        llm_pid = _read_pid("llama")
        results["llama.cpp"] = {
            "group": "llm",
            "pid": llm_pid,
            "port": 8080,
            "port_ok": _is_port_open(8080),
            "running": llm_pid is not None or _is_port_open(8080),
            "healthy": _is_port_open(8080),
        }
        return results

    # ── Verification ─────────────────────────────────────────────────────

    def _verify(self) -> None:
        """Phase 6: Health-check all started services."""
        _phase("Phase 6: Verification")

        # Brief settle time
        time.sleep(3)

        running = 0
        failed = 0

        # LLM
        if _is_port_open(8080):
            _ok(":8080  llama.cpp")
            running += 1
        else:
            _warn(":8080  llama.cpp — not running")

        # All services
        for svc in SERVICES:
            pid = _read_pid(svc.name)
            if pid is None:
                continue  # wasn't started

            if svc.port:
                if _is_port_open(svc.port):
                    _ok(f":{svc.port}  {svc.name}")
                    running += 1
                else:
                    _warn(f":{svc.port}  {svc.name} — port not responding")
                    failed += 1
            else:
                # No port to check — verify PID is alive
                try:
                    if not _is_pid_live(pid):
                        raise ProcessLookupError
                    _ok(f"       {svc.name} (PID {pid})")
                    running += 1
                except (ProcessLookupError, OSError, SystemError):
                    _fail(f"       {svc.name} — process died")
                    failed += 1

        # Summary
        print()
        if failed == 0:
            print(f"\033[32m  🎉 Repryntt Production System ACTIVE "
                  f"— {running} services running\033[0m")
        else:
            print(f"\033[33m  ⚠️ {running} services running, "
                  f"{failed} failed\033[0m")

        # Access points
        print()
        print("\033[1m  Access Points:\033[0m")
        endpoints = [
            (8080, "LLM API", "http://localhost:8080"),
            (5000, "Web Interface", "http://localhost:5000"),
            (8089, "Nexus Dashboard", "http://localhost:8089"),
            (4000, "Chat Server", "http://localhost:4000"),
            (3000, "Unified Interface", "http://localhost:3000"),
            (8083, "Tool API", "http://localhost:8083"),
            (8081, "External API", "http://localhost:8081"),
            (8888, "Trading Dashboard", "http://localhost:8888"),
        ]
        for port, label, url in endpoints:
            if _is_port_open(port):
                print(f"    \033[32m●\033[0m {label:<22} {url}")

    # ── Single service control ───────────────────────────────────────────

    def start_one(self, name: str) -> bool:
        """Start a single named service."""
        for svc in SERVICES:
            if svc.name == name:
                return self._start_service(svc)
        if name in ("llm", "llama", "llama.cpp"):
            return self.start_llm()
        _fail(f"Unknown service: {name}")
        return False

    def stop_one(self, name: str) -> bool:
        """Stop a single named service."""
        if name in ("llm", "llama", "llama.cpp"):
            pid = _read_pid("llama")
            if pid:
                _kill_pid(pid)
                _pid_file("llama").unlink(missing_ok=True)
                _ok(f"llama.cpp stopped")
                return True
            return True
        return self._stop_service(name)

    def list_services(self) -> None:
        """Print all available services."""
        print("\033[1mAvailable services:\033[0m\n")
        groups = {}
        for svc in SERVICES:
            groups.setdefault(svc.group, []).append(svc)

        for group, svcs in groups.items():
            print(f"  \033[1m{group}\033[0m")
            for svc in svcs:
                port_str = f":{svc.port}" if svc.port else "     "
                opt = " (optional)" if svc.optional else ""
                print(f"    {svc.name:<24} {port_str}  python -m {svc.module}{opt}")
            print()
        print("  \033[1mllm\033[0m")
        print(f"    llama.cpp                :8080  Local LLM server")
