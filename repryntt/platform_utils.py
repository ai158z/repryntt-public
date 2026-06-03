"""
repryntt.platform_utils — cross-platform helpers.

Provides platform-agnostic replacements for Unix-only operations:
  - Process management (kill by name, check if running)
  - Filesystem sync
  - Memory / hardware info
  - File permissions (secure-restrict-to-owner)
  - Machine identity fingerprinting
  - Audio playback/recording dispatch
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import io
from pathlib import Path
from typing import Optional

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def fix_windows_encoding() -> None:
    """Reconfigure stdout/stderr to UTF-8 on Windows.

    Windows console defaults to cp1252 which cannot encode emoji characters,
    causing UnicodeEncodeError in the logging module.  Call this once at
    process startup before any log output.
    """
    if not IS_WINDOWS:
        return
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
                continue
        except Exception:
            pass
        try:
            buffer = getattr(stream, "buffer", None)
            if buffer is not None:
                wrapped = io.TextIOWrapper(
                    buffer,
                    encoding="utf-8",
                    errors="replace",
                    line_buffering=True,
                )
                setattr(sys, stream_name, wrapped)
        except Exception:
            pass  # frozen / piped / non-TextIO — ignore

# ── Process management ───────────────────────────────────────────────────


def kill_process_by_name(name: str, *, signal: str = "TERM") -> bool:
    """Kill processes matching *name*.  Returns True if the command ran."""
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/IM", f"{name}*"],
                capture_output=True, timeout=10,
            )
        else:
            flag = "-9" if signal == "KILL" else "-15"
            subprocess.run(
                ["pkill", flag, "-f", name],
                capture_output=True, timeout=10,
            )
        return True
    except Exception:
        return False


def is_process_running(name: str) -> bool:
    """Check if a process matching *name* exists."""
    try:
        if IS_WINDOWS:
            r = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}*"],
                capture_output=True, text=True, timeout=10,
            )
            return name.lower() in r.stdout.lower()
        else:
            r = subprocess.run(
                ["pgrep", "-f", name],
                capture_output=True, timeout=10,
            )
            return r.returncode == 0
    except Exception:
        return False


# ── Filesystem ───────────────────────────────────────────────────────────


def sync_filesystem() -> None:
    """Flush filesystem buffers (no-op on Windows)."""
    if IS_WINDOWS:
        return
    try:
        subprocess.run(["sync"], capture_output=True, timeout=30)
    except Exception:
        pass


# ── Memory info ──────────────────────────────────────────────────────────


def get_ram_mb() -> int:
    """Return total RAM in MiB, cross-platform."""
    try:
        import psutil
        return psutil.virtual_memory().total // (1024 * 1024)
    except ImportError:
        pass
    if IS_LINUX:
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal"):
                        return int(line.split()[1]) // 1024
        except Exception:
            pass
    if IS_MACOS:
        try:
            r = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5,
            )
            return int(r.stdout.strip()) // (1024 * 1024)
        except Exception:
            pass
    if IS_WINDOWS:
        try:
            import ctypes
            class MEMSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = MEMSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return stat.ullTotalPhys // (1024 * 1024)
        except Exception:
            pass
    return 0


def get_memory_summary() -> str:
    """Return a human-readable memory summary (replaces `free -h`)."""
    try:
        import psutil
        vm = psutil.virtual_memory()
        total = vm.total // (1024 * 1024)
        used = vm.used // (1024 * 1024)
        avail = vm.available // (1024 * 1024)
        return f"RAM: {used}MiB used / {total}MiB total ({avail}MiB available, {vm.percent}% used)"
    except ImportError:
        ram = get_ram_mb()
        if ram:
            return f"RAM: {ram}MiB total (detailed stats require psutil)"
        return "RAM: unknown (install psutil for memory info)"


# ── File permissions ─────────────────────────────────────────────────────


def secure_file(path: str | Path) -> None:
    """Restrict file to owner-only access (best-effort on Windows)."""
    path = Path(path)
    if IS_WINDOWS:
        try:
            # Remove inherited ACLs and set owner-only
            subprocess.run(
                ["icacls", str(path), "/inheritance:r",
                 "/grant:r", f"{os.environ.get('USERNAME', 'SYSTEM')}:(R,W)"],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass
    else:
        try:
            os.chmod(str(path), 0o600)
        except Exception:
            pass


def secure_file_readonly(path: str | Path) -> None:
    """Make file read-only, owner-only (best-effort on Windows)."""
    path = Path(path)
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["icacls", str(path), "/inheritance:r",
                 "/grant:r", f"{os.environ.get('USERNAME', 'SYSTEM')}:(R)"],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass
    else:
        try:
            os.chmod(str(path), 0o400)
        except Exception:
            pass


# ── Machine identity ─────────────────────────────────────────────────────


def get_machine_fingerprint_parts() -> list[str]:
    """Gather platform-specific identity parts for fingerprinting."""
    parts = []
    if IS_LINUX:
        for path in ["/etc/machine-id", "/var/lib/dbus/machine-id"]:
            try:
                with open(path) as f:
                    parts.append(f.read().strip())
                    break
            except Exception:
                pass
    elif IS_MACOS:
        try:
            r = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5,
            )
            for line in r.stdout.splitlines():
                if "IOPlatformUUID" in line:
                    parts.append(line.split('"')[-2])
                    break
        except Exception:
            pass
    elif IS_WINDOWS:
        try:
            r = subprocess.run(
                ["wmic", "csproduct", "get", "UUID"],
                capture_output=True, text=True, timeout=5,
            )
            lines = [l.strip() for l in r.stdout.splitlines() if l.strip() and l.strip() != "UUID"]
            if lines:
                parts.append(lines[0])
        except Exception:
            pass

    parts.append(platform.node())
    parts.append(platform.machine())
    parts.append(platform.system())
    return parts


def detect_device_type() -> str:
    """Detect the device type string for node naming."""
    arch = platform.machine()
    if IS_LINUX and os.path.exists("/proc/device-tree/model"):
        try:
            model = Path("/proc/device-tree/model").read_text().strip("\x00").strip()
            if "jetson" in model.lower():
                return "jetson"
            elif "raspberry" in model.lower():
                return "rpi"
            return model.split()[0].lower()[:12]
        except Exception:
            pass
    if IS_MACOS:
        return "mac"
    if IS_WINDOWS:
        return "win"
    if "arm" in arch.lower() or "aarch" in arch.lower():
        return "arm"
    return "pc"


# ── GPU detection helpers ────────────────────────────────────────────────


def has_nvidia_device() -> bool:
    """Check for NVIDIA GPU without torch."""
    if IS_LINUX:
        return os.path.exists("/dev/nvidia0")
    if IS_WINDOWS:
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=10,
            )
            return r.returncode == 0 and bool(r.stdout.strip())
        except Exception:
            return False
    if IS_MACOS:
        return False  # No NVIDIA on modern macOS
    return False


def has_amd_gpu() -> bool:
    """Check for AMD GPU without torch."""
    if IS_LINUX:
        return os.path.exists("/dev/dri/renderD128")
    if IS_WINDOWS:
        # Could check dxdiag but not reliable
        return False
    return False


# ── Audio dispatch ───────────────────────────────────────────────────────


def get_audio_backend() -> str:
    """Determine the audio backend for this platform."""
    if IS_LINUX:
        # Check for PulseAudio, then ALSA
        try:
            r = subprocess.run(["pactl", "info"], capture_output=True, timeout=5)
            if r.returncode == 0:
                return "pulse"
        except Exception:
            pass
        return "alsa"
    if IS_MACOS:
        return "coreaudio"
    if IS_WINDOWS:
        return "wasapi"
    return "unknown"


def play_audio_file(path: str | Path, *, device: str = "", blocking: bool = True) -> Optional[subprocess.Popen]:
    """Play an audio file using the platform's native tool."""
    path = str(path)
    try:
        if IS_LINUX:
            cmd = ["aplay"]
            if device:
                cmd.extend(["-D", device])
            cmd.append(path)
        elif IS_MACOS:
            cmd = ["afplay", path]
        elif IS_WINDOWS:
            # Use PowerShell's built-in audio player
            cmd = ["powershell", "-c",
                   f"(New-Object Media.SoundPlayer '{path}').PlaySync()"]
        else:
            return None

        if blocking:
            subprocess.run(cmd, capture_output=True, timeout=60)
            return None
        else:
            return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return None


def get_venv_python(venv_dir: str | Path) -> Path:
    """Return the python executable inside a virtualenv, cross-platform."""
    venv_dir = Path(venv_dir)
    if IS_WINDOWS:
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"
