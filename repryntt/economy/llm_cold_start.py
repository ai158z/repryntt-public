"""
LLM Cold-Start Manager
======================
Allows the local llama.cpp server to stay OFF until a workload arrives,
freeing GPU/RAM for network compute. The server is started on demand and
auto-stopped after an idle timeout.

Usage:
    from repryntt.economy.llm_cold_start import llm_manager

    # Synchronous callers (spaceminer)
    llm_manager.ensure_ready()      # blocks until server healthy
    response = requests.post(...)   # normal call
    llm_manager.mark_active()       # reset idle timer

    # Async callers (p2p bridge)
    await llm_manager.ensure_ready_async()
"""

import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("repryntt.economy.llm_cold_start")

_DEFAULT_PORT = 8080
_HEALTH_TIMEOUT = 90        # Max seconds to wait for model load
_IDLE_TIMEOUT = 300         # Seconds of inactivity before auto-stop (5 min)
_HEALTH_POLL_INTERVAL = 2   # Seconds between health checks during startup


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False


class LLMColdStartManager:
    """Manages on-demand startup and idle shutdown of the local llama.cpp server."""

    def __init__(self, port: int = _DEFAULT_PORT, idle_timeout: int = _IDLE_TIMEOUT):
        self.port = port
        self.idle_timeout = idle_timeout
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._last_active = 0.0
        self._idle_thread: Optional[threading.Thread] = None
        self._stopping = False

    # ── Public API ───────────────────────────────────────

    def is_running(self) -> bool:
        return _port_open(self.port)

    def ensure_ready(self, timeout: int = _HEALTH_TIMEOUT) -> bool:
        """Block until llama.cpp is healthy. Returns True if server is ready."""
        if self.is_running():
            self.mark_active()
            return True
        return self._start_server(timeout)

    async def ensure_ready_async(self, timeout: int = _HEALTH_TIMEOUT) -> bool:
        """Async wrapper — runs the blocking start in a thread."""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.ensure_ready, timeout)

    def mark_active(self):
        """Reset the idle timer. Call after every successful LLM request."""
        self._last_active = time.time()

    def stop(self):
        """Manually stop the llama.cpp server we started."""
        self._stopping = True
        self._kill_server()

    # ── Server Lifecycle ─────────────────────────────────

    def _start_server(self, timeout: int) -> bool:
        with self._lock:
            # Double-check after acquiring lock
            if self.is_running():
                self.mark_active()
                return True

            logger.info("🧊 Cold-starting llama.cpp server...")

            # Try llm_toggle.sh first (respects model selection)
            started = self._try_toggle_script()
            if not started:
                started = self._try_direct_launch()

            if not started:
                logger.error("❌ Cannot start llama.cpp — no binary or toggle script found")
                return False

            # Wait for health
            deadline = time.time() + timeout
            while time.time() < deadline:
                if self.is_running():
                    logger.info(f"✅ llama.cpp cold-started on :{self.port}")
                    self.mark_active()
                    self._start_idle_watchdog()
                    return True
                time.sleep(_HEALTH_POLL_INTERVAL)

            logger.error(f"❌ llama.cpp failed to become healthy within {timeout}s")
            self._kill_server()
            return False

    def _try_toggle_script(self) -> bool:
        """Start via llm_toggle.sh if it exists."""
        candidates = [
            Path(__file__).resolve().parent.parent.parent / "scripts" / "llm_toggle.sh",
            Path(os.environ.get("SAIGE_DIR", "")) / "llm_toggle.sh",
        ]
        for script in candidates:
            if script.exists():
                log_dir = Path(os.environ.get("REPRYNTT_DATA", Path.home() / ".repryntt")) / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                log_path = log_dir / "llama_server.log"
                logger.info(f"  Starting via {script.name}...")
                subprocess.Popen(
                    ["bash", str(script), "on"],
                    stdout=log_path.open("a"),
                    stderr=subprocess.STDOUT,
                )
                return True
        return False

    def _try_direct_launch(self) -> bool:
        """Start llama-server binary directly."""
        llama_bin = shutil.which("llama-server")
        if not llama_bin:
            return False

        # Find a model
        model_dirs = [
            Path(__file__).resolve().parent.parent.parent / "models",
            Path(os.environ.get("REPRYNTT_DATA", Path.home() / ".repryntt")) / "models",
        ]
        model = None
        for d in model_dirs:
            if d.exists():
                gguf_files = list(d.glob("**/*.gguf"))
                if gguf_files:
                    model = gguf_files[0]
                    break
        if not model:
            logger.warning("No .gguf model found")
            return False

        # GPU config
        ngl = "0"
        try:
            from repryntt.hardware_profile import get_profile
            hw = get_profile()
            ngl = str(hw.llm_gpu_layers)
        except Exception:
            pass

        log_dir = Path(os.environ.get("REPRYNTT_DATA", Path.home() / ".repryntt")) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "llama_server.log"

        cmd = [
            llama_bin, "-m", str(model),
            "-ngl", ngl, "-c", "4096",
            "--host", "0.0.0.0", "--port", str(self.port),
            "--no-warmup",
        ]
        logger.info(f"  Launching: {model.name} (ngl={ngl})")
        self._proc = subprocess.Popen(
            cmd,
            stdout=log_path.open("a"),
            stderr=subprocess.STDOUT,
        )
        # Write PID for service manager compatibility
        pid_dir = Path(os.environ.get("REPRYNTT_DATA", Path.home() / ".repryntt")) / "pids"
        pid_dir.mkdir(parents=True, exist_ok=True)
        (pid_dir / "llama.pid").write_text(str(self._proc.pid))
        return True

    def _kill_server(self):
        """Stop a server we started."""
        if self._proc and self._proc.poll() is None:
            logger.info("🛑 Stopping llama.cpp (freeing RAM for compute)...")
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

        # Also try toggle script stop
        toggle = Path(__file__).resolve().parent.parent.parent / "scripts" / "llm_toggle.sh"
        if toggle.exists() and sys.platform != "win32":
            subprocess.run(["bash", str(toggle), "off"], capture_output=True, timeout=10)

    # ── Idle Watchdog ────────────────────────────────────

    def _start_idle_watchdog(self):
        if self._idle_thread and self._idle_thread.is_alive():
            return  # Already watching

        self._stopping = False

        def _watchdog():
            while not self._stopping:
                time.sleep(30)  # Check every 30s
                if self._stopping:
                    return
                idle = time.time() - self._last_active
                if idle >= self.idle_timeout and self.is_running():
                    logger.info(
                        f"💤 llama.cpp idle for {int(idle)}s — stopping to free RAM"
                    )
                    self._kill_server()
                    return

        self._idle_thread = threading.Thread(
            target=_watchdog, daemon=True, name="llm-idle-watchdog"
        )
        self._idle_thread.start()


# ── Module-level singleton ───────────────────────────────
llm_manager = LLMColdStartManager()
