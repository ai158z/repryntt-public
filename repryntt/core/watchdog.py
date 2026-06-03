"""
repryntt.core.watchdog — 24/7 stability guard for the agent stack.

Runs as its own managed service (``python -m repryntt.core.watchdog``). Every
``POLL_INTERVAL`` seconds it performs three checks:

  1. **Memory pressure** — if MemAvailable drops below the warn threshold,
     attempt a non-destructive pagecache drop (requires a one-line sudoers
     rule, see ``docs/watchdog_sudoers.example``). Runs ``malloc_trim`` in
     this process as well.

  2. **Service liveness** — for each critical service PID file under
     ``~/.repryntt/pids/``, verify the process is alive; if not (or if it
     is a zombie) relaunch via ``ServiceManager._start_service``.

  3. **Log freshness** — for each service with a log file under
     ``~/.repryntt/logs/``, ensure the mtime is newer than
     ``STALE_LOG_MINUTES``; if a log is stale (heartbeat hung) the service
     is restarted.

This module is intentionally dependency-light: it imports ServiceManager
lazily and never touches the LLM or cortex directly.

Environment overrides:
  REPRYNTT_WATCHDOG_INTERVAL     poll seconds (default 60)
  REPRYNTT_WATCHDOG_WARN_MB      memory warn threshold (default 1200)
  REPRYNTT_WATCHDOG_DROP_MB      drop-caches threshold (default 900)
  REPRYNTT_WATCHDOG_STALE_MIN    log staleness threshold, minutes (default 15)
  REPRYNTT_WATCHDOG_DISABLE      set to 1 to exit immediately (no-op mode)
"""

from __future__ import annotations

import ctypes
import ctypes.util
import gc
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("repryntt.watchdog")


# ── Tunables ─────────────────────────────────────────────────────────────

POLL_INTERVAL = int(os.environ.get("REPRYNTT_WATCHDOG_INTERVAL", "60"))
WARN_MB = int(os.environ.get("REPRYNTT_WATCHDOG_WARN_MB", "1200"))
DROP_MB = int(os.environ.get("REPRYNTT_WATCHDOG_DROP_MB", "900"))
STALE_LOG_MIN = int(os.environ.get("REPRYNTT_WATCHDOG_STALE_MIN", "15"))

# Services we actively supervise (must match ServiceDef.name in services.py).
# Only include services whose death would break the 24/7 loop — we leave
# optional/ephemeral ones (trading helpers) to ServiceManager recovery.
SUPERVISED_SERVICES = [
    "agent-daemon",
    "evolution-loop",
    "nexus",
    "web-server",
]

# Services whose log mtime we require to advance. If no heartbeat for
# STALE_LOG_MIN minutes we assume the service is wedged and bounce it.
LOG_FRESHNESS_SERVICES = [
    "agent-daemon",
    "evolution-loop",
]


# ── Memory helpers ───────────────────────────────────────────────────────

def _read_meminfo_mb() -> dict:
    """Return MemTotal/MemFree/MemAvailable/Cached/Buffers in MB."""
    out = {}
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0].endswith(":"):
                    key = parts[0].rstrip(":")
                    try:
                        out[key] = int(parts[1]) // 1024  # kB → MB
                    except ValueError:
                        pass
    except Exception as e:
        logger.warning(f"meminfo read failed: {e}")
    return out


_LIBC = None


def _malloc_trim() -> None:
    """Ask glibc to return free heap to the OS. No-op on non-glibc systems."""
    global _LIBC
    try:
        if _LIBC is None:
            libc_name = ctypes.util.find_library("c")
            if not libc_name:
                return
            _LIBC = ctypes.CDLL(libc_name)
        if hasattr(_LIBC, "malloc_trim"):
            _LIBC.malloc_trim(0)
    except Exception:
        pass


def _drop_pagecache() -> bool:
    """Try to drop page cache (safe, non-destructive). Returns True on success.

    Prefers a passwordless sudo rule (see docs). Without it, this is a no-op.
    """
    # Prefer a pre-installed helper, if any.
    helper = Path.home() / ".repryntt" / "bin" / "drop_caches.sh"
    if helper.exists() and os.access(helper, os.X_OK):
        try:
            subprocess.run(["sudo", "-n", str(helper)], check=True,
                           timeout=10, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    # Direct sysctl via passwordless sudo (requires sudoers rule).
    try:
        subprocess.run(
            ["sudo", "-n", "sysctl", "-q", "vm.drop_caches=1"],
            check=True, timeout=10,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def memory_pressure_action() -> Optional[str]:
    """Evaluate memory state and take non-destructive action if needed.

    Returns a short status string describing what was done (or None if no
    action was needed).
    """
    mem = _read_meminfo_mb()
    avail = mem.get("MemAvailable", 0)
    if not avail:
        return None

    if avail < DROP_MB:
        gc.collect()
        _malloc_trim()
        dropped = _drop_pagecache()
        msg = (
            f"🧹 Low memory ({avail}MB avail) — gc+trim run"
            + (", pagecache dropped" if dropped else ", drop_caches unavailable")
        )
        logger.warning(msg)
        return msg

    if avail < WARN_MB:
        gc.collect()
        _malloc_trim()
        logger.info(f"⚠️ Memory pressure ({avail}MB avail) — gc+trim run")
        return f"gc+trim at {avail}MB"

    return None


# ── Service liveness helpers ─────────────────────────────────────────────

def _pid_dir() -> Path:
    return Path.home() / ".repryntt" / "pids"


def _log_dir() -> Path:
    return Path.home() / ".repryntt" / "logs"


def _read_pid(name: str) -> Optional[int]:
    p = _pid_dir() / f"{name}.pid"
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip() or "0") or None
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    """True if pid exists AND is not a zombie."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours — treat as alive
    except Exception:
        return False
    # Zombie check via /proc/<pid>/status
    try:
        with open(f"/proc/{pid}/status", "r") as f:
            for line in f:
                if line.startswith("State:"):
                    state = line.split()[1]
                    return state not in ("Z", "X")
    except Exception:
        pass
    return True


def _restart_service(name: str) -> bool:
    """Restart a managed service using ServiceManager."""
    try:
        from repryntt.services import ServiceManager, SERVICES
    except Exception as e:
        logger.warning(f"Cannot import ServiceManager: {e}")
        return False

    svc = next((s for s in SERVICES if s.name == name), None)
    if svc is None:
        logger.warning(f"Unknown service '{name}' — cannot restart")
        return False

    mgr = ServiceManager()
    # Stop first (clears pid file + kills any stray process)
    try:
        mgr._stop_service(name)
    except Exception as e:
        logger.debug(f"{name}: stop pre-restart raised {e} (non-fatal)")
    time.sleep(1)
    try:
        ok = mgr._start_service(svc)
        if ok:
            logger.info(f"🔁 Watchdog restarted '{name}'")
        else:
            logger.warning(f"🔁 Watchdog failed to restart '{name}'")
        return ok
    except Exception as e:
        logger.error(f"🔁 Watchdog restart error for '{name}': {e}")
        return False


def check_liveness() -> int:
    """Return number of services restarted this pass."""
    restarted = 0
    for name in SUPERVISED_SERVICES:
        pid = _read_pid(name)
        if pid is None:
            # No pid file — either never started this run or already cleaned up.
            # Only restart if the service is supposed to be up, i.e. its log
            # file exists (indicating a prior launch in this session).
            if (_log_dir() / f"{name}.log").exists():
                logger.warning(f"💀 {name}: pid file missing — restarting")
                if _restart_service(name):
                    restarted += 1
            continue
        if not _pid_alive(pid):
            logger.warning(f"💀 {name}: pid {pid} dead/zombie — restarting")
            if _restart_service(name):
                restarted += 1
    return restarted


def check_log_freshness() -> int:
    """Restart services whose logs have not advanced recently."""
    restarted = 0
    cutoff = time.time() - (STALE_LOG_MIN * 60)
    for name in LOG_FRESHNESS_SERVICES:
        pid = _read_pid(name)
        if pid is None or not _pid_alive(pid):
            continue  # liveness check will handle it
        log_path = _log_dir() / f"{name}.log"
        if not log_path.exists():
            continue
        try:
            mtime = log_path.stat().st_mtime
        except Exception:
            continue
        if mtime < cutoff:
            age_min = int((time.time() - mtime) / 60)
            logger.warning(
                f"🐌 {name}: log stale ({age_min}m, threshold={STALE_LOG_MIN}m) — bouncing"
            )
            if _restart_service(name):
                restarted += 1
    return restarted


# ── Main loop ────────────────────────────────────────────────────────────

_STOP = False


def _handle_signal(signum, frame):
    global _STOP
    _STOP = True


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if os.environ.get("REPRYNTT_WATCHDOG_DISABLE", "").strip() in ("1", "true", "yes"):
        logger.info("Watchdog disabled via REPRYNTT_WATCHDOG_DISABLE — exiting")
        return 0

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info(
        "👁️ Watchdog starting (interval=%ds, warn=%dMB, drop=%dMB, stale=%dm)",
        POLL_INTERVAL, WARN_MB, DROP_MB, STALE_LOG_MIN,
    )

    # Small initial delay so ServiceManager can finish initial launch.
    for _ in range(15):
        if _STOP:
            return 0
        time.sleep(1)

    while not _STOP:
        try:
            memory_pressure_action()
        except Exception as e:
            logger.warning(f"memory check failed: {e}")
        try:
            check_liveness()
        except Exception as e:
            logger.warning(f"liveness check failed: {e}")
        try:
            check_log_freshness()
        except Exception as e:
            logger.warning(f"freshness check failed: {e}")

        for _ in range(POLL_INTERVAL):
            if _STOP:
                break
            time.sleep(1)

    logger.info("👁️ Watchdog stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
