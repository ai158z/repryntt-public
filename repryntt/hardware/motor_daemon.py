"""repryntt.hardware.motor_daemon — sole owner of motor / camera / sonar.

This is the production answer to the multi-process GPIO contention found
on 2026-05-10: two Python processes (agent daemon + nexus_app) were
both calling get_tank_controller() and racing for the chardev locks.

The daemon owns the hardware. Every other process talks to it through
the Unix socket at SOCKET_PATH. Lease policy is enforced by motor_lease.

Wire format: line-delimited JSON over AF_UNIX SOCK_STREAM.
  Request:   {"op": "<verb>", "token": "<lease>", ...}
  Response:  {"ok": true,  "data": {...}}
             {"ok": false, "error": "<msg>", "code": "<symbol>"}

Verbs:
  acquire   — {priority, holder_label, holder_pid, wait_timeout_s} → lease info
  heartbeat — {token} → refreshed expires_at
  release   — {token} → ok
    cmd       — {token, action, speed, duration, left_vel, right_vel, ...}
                            → tank result
  stop      — {token} → tank stop result (no preempt check; allowed even
              if your lease was preempted, so callers can still brake)
  status    — {} → lease status snapshot + hw availability flags

Lifecycle:
  systemd unit (or `python -m repryntt.hardware.motor_daemon`) keeps it
  running. atexit + SIGTERM stop motors and remove the socket file.

Run:
  python -m repryntt.hardware.motor_daemon [--socket /run/repryntt/motor.sock]
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import signal
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from repryntt.hardware.motor_lease import (
    GRACE_PERIOD_S,
    HEARTBEAT_TTL_S,
    LeaseManager,
    Priority,
)

logger = logging.getLogger("repryntt.hardware.motor_daemon")

DEFAULT_SOCKET_PATH = "/run/repryntt/motor.sock"
USER_FALLBACK_SOCKET_PATH = str(Path.home() / ".repryntt" / "motor.sock")
REAPER_INTERVAL_S = 0.5
RECV_BUFFER_BYTES = 8192


# ── Hardware façade ──────────────────────────────────────────────────


class _Hardware:
    """Lazy holder for the singletons the daemon owns. Everything else
    in the program talks to these through the lease/cmd protocol."""

    def __init__(self) -> None:
        self._tank = None
        self._lock = threading.Lock()

    def tank(self):
        with self._lock:
            if self._tank is None:
                from repryntt.hardware.tank import get_tank_controller
                t = get_tank_controller()
                t.initialize()
                self._tank = t
            return self._tank

    def shutdown(self) -> None:
        with self._lock:
            if self._tank is not None:
                try:
                    self._tank.shutdown()
                except Exception as e:
                    logger.debug("tank shutdown failed: %s", e)


# ── Request handler ──────────────────────────────────────────────────


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, socket_path: str, lease_mgr: LeaseManager, hw: _Hardware):
        self.socket_path = socket_path
        self.lease_mgr = lease_mgr
        self.hw = hw
        super().__init__(socket_path, _Handler)


class _Handler(socketserver.StreamRequestHandler):
    """One thread per client connection. Reads line-delimited JSON requests."""

    def handle(self) -> None:
        # Identify the peer by PID for logging (SO_PEERCRED is Linux-only;
        # falls back to "?" on other platforms).
        peer_pid = _peer_pid(self.connection)
        logger.debug("motor_daemon: client connected (pid=%s)", peer_pid)
        try:
            for line in self.rfile:
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except json.JSONDecodeError as e:
                    self._send({"ok": False, "error": f"bad json: {e}",
                                "code": "BAD_JSON"})
                    continue
                resp = self._dispatch(req, peer_pid)
                self._send(resp)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logger.exception("motor_daemon: handler crashed")
        finally:
            logger.debug("motor_daemon: client disconnected (pid=%s)", peer_pid)

    def _send(self, obj: Dict[str, Any]) -> None:
        try:
            self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _dispatch(self, req: Dict[str, Any], peer_pid: int) -> Dict[str, Any]:
        op = req.get("op", "")
        srv: _Server = self.server  # type: ignore[assignment]
        try:
            handler = _OPS.get(op)
            if handler is None:
                return {"ok": False, "error": f"unknown op {op!r}",
                        "code": "BAD_OP"}
            return handler(srv, req, peer_pid)
        except PermissionError as e:
            return {"ok": False, "error": str(e), "code": "BAD_TOKEN"}
        except TimeoutError as e:
            return {"ok": False, "error": str(e), "code": "TIMEOUT"}
        except Exception as e:
            logger.exception("motor_daemon: op %s failed", op)
            return {"ok": False, "error": str(e)[:200], "code": "INTERNAL"}


# ── Op handlers ──────────────────────────────────────────────────────


def _op_acquire(srv: _Server, req: Dict[str, Any], peer_pid: int) -> Dict[str, Any]:
    priority_name = req.get("priority", "AUTONOMOUS")
    try:
        priority = Priority[priority_name]
    except KeyError:
        return {"ok": False, "error": f"unknown priority {priority_name!r}",
                "code": "BAD_PRIORITY"}
    holder_label = str(req.get("holder_label", "anon"))[:64]
    holder_pid = int(req.get("holder_pid", peer_pid))
    wait = float(req.get("wait_timeout_s", 30.0))
    lease = srv.lease_mgr.acquire(priority, holder_label, holder_pid, wait_timeout_s=wait)
    logger.info("motor_daemon: granted lease pri=%s holder=%s pid=%s",
                priority.name, holder_label, holder_pid)
    return {"ok": True, "data": {
        "token": lease.token,
        "expires_at": lease.expires_at,
        "ttl_s": HEARTBEAT_TTL_S,
        "granted_at": lease.granted_at,
        "priority": lease.priority.name,
    }}


def _op_heartbeat(srv: _Server, req: Dict[str, Any], peer_pid: int) -> Dict[str, Any]:
    token = req.get("token", "")
    lease = srv.lease_mgr.heartbeat(token)
    return {"ok": True, "data": {
        "expires_at": lease.expires_at,
        "preempted": lease.preempt_event.is_set(),
    }}


def _op_release(srv: _Server, req: Dict[str, Any], peer_pid: int) -> Dict[str, Any]:
    token = req.get("token", "")
    srv.lease_mgr.release(token)
    return {"ok": True, "data": {}}


def _op_cmd(srv: _Server, req: Dict[str, Any], peer_pid: int) -> Dict[str, Any]:
    token = req.get("token", "")
    active = srv.lease_mgr.current()
    if active is None or active.token != token:
        return {"ok": False, "error": "no active lease for this token",
                "code": "NOT_HOLDER"}
    if active.preempt_event.is_set():
        return {"ok": False, "error": "your lease was preempted",
                "code": "PREEMPTED"}

    action = str(req.get("action", "stop"))
    speed = float(req.get("speed", 0.5))
    duration = float(req.get("duration", 0.0))
    tank = srv.hw.tank()
    fn = {
        "forward": lambda: tank.move_forward(speed, duration),
        "backward": lambda: tank.move_backward(speed, duration),
        "turn_left": lambda: tank.turn_left(speed, duration),
        "turn_right": lambda: tank.turn_right(speed, duration),
        "stop": lambda: tank.stop(),
        "continuous": lambda: _drive_continuous(tank, req),
        "spin": lambda: tank.spin(float(req.get("degrees", 180.0)), speed),
        "move_distance": lambda: tank.move_distance(
            float(req.get("distance_cm", 0.0)), speed,
        ),
        "turn_degrees": lambda: tank.turn_degrees(
            float(req.get("degrees", 0.0)), speed,
        ),
        "emergency_stop": lambda: tank.emergency_stop(),
        "reset_emergency_stop": lambda: tank.reset_emergency_stop(),
    }.get(action)
    if fn is None:
        return {"ok": False, "error": f"unknown action {action!r}",
                "code": "BAD_ACTION"}
    result = fn()
    return {"ok": bool(result.get("success", True)), "data": result}


def _drive_continuous(tank, req: Dict[str, Any]) -> Dict[str, Any]:
    left_vel = float(req.get("left_vel", 0.0))
    right_vel = float(req.get("right_vel", 0.0))
    tank.drive_continuous(left_vel, right_vel)
    return {
        "success": True,
        "command": "continuous",
        "left_vel": round(left_vel, 3),
        "right_vel": round(right_vel, 3),
    }


def _op_stop(srv: _Server, req: Dict[str, Any], peer_pid: int) -> Dict[str, Any]:
    """Stop is allowed even on preempted leases — better to brake than not."""
    token = req.get("token", "")
    active = srv.lease_mgr.current()
    if active is None or active.token != token:
        return {"ok": False, "error": "no active lease for this token",
                "code": "NOT_HOLDER"}
    result = srv.hw.tank().stop()
    return {"ok": bool(result.get("success", True)), "data": result}


def _op_status(srv: _Server, req: Dict[str, Any], peer_pid: int) -> Dict[str, Any]:
    data = {"lease": srv.lease_mgr.status()}
    if srv.hw._tank is not None:
        data["body"] = srv.hw._tank.get_body_status()
    else:
        data["body"] = {
            "success": True,
            "body": {"gpio_initialized": False, "is_moving": False},
            "hardware": {"controller_initialized": False},
        }
    return {"ok": True, "data": data}


_OPS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "acquire": _op_acquire,
    "heartbeat": _op_heartbeat,
    "release": _op_release,
    "cmd": _op_cmd,
    "stop": _op_stop,
    "status": _op_status,
}


# ── Reaper thread ────────────────────────────────────────────────────


def _start_reaper(lease_mgr: LeaseManager, stop: threading.Event) -> threading.Thread:
    def _loop():
        while not stop.is_set():
            stop.wait(REAPER_INTERVAL_S)
            try:
                revoked = lease_mgr.reap_expired()
                if revoked is not None:
                    logger.info(
                        "motor_daemon: reaped lease holder=%s pid=%s state=%s",
                        revoked.holder_label, revoked.holder_pid,
                        revoked.state.value,
                    )
            except Exception:
                logger.exception("motor_daemon: reaper crashed (continuing)")
    t = threading.Thread(target=_loop, name="motor-lease-reaper", daemon=True)
    t.start()
    return t


# ── Daemon entry point ───────────────────────────────────────────────


def _peer_pid(conn: socket.socket) -> int:
    try:
        import struct
        SO_PEERCRED = 17
        creds = conn.getsockopt(socket.SOL_SOCKET, SO_PEERCRED, struct.calcsize("3i"))
        _pid, _uid, _gid = struct.unpack("3i", creds)
        return _pid
    except Exception:
        return -1


def _resolve_socket_path(requested: Optional[str]) -> str:
    if requested:
        return requested
    # /run/repryntt is preferred (systemd-managed RuntimeDirectory) but
    # falls back to a user-writable path for development.
    parent = Path(DEFAULT_SOCKET_PATH).parent
    if parent.exists() and os.access(parent, os.W_OK):
        return DEFAULT_SOCKET_PATH
    Path(USER_FALLBACK_SOCKET_PATH).parent.mkdir(parents=True, exist_ok=True)
    return USER_FALLBACK_SOCKET_PATH


def serve(socket_path: Optional[str] = None) -> None:
    """Run the daemon. Blocks until SIGINT/SIGTERM."""
    path = _resolve_socket_path(socket_path)
    # Remove a stale socket from a prior run.
    if os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass

    lease_mgr = LeaseManager()
    hw = _Hardware()
    server = _Server(path, lease_mgr, hw)
    # World-rw so non-root processes (nexus_app, agent daemon) can connect.
    # Same-host only by kernel design — no LAN exposure.
    os.chmod(path, 0o666)

    reaper_stop = threading.Event()
    reaper = _start_reaper(lease_mgr, reaper_stop)

    def _cleanup(*_args):
        logger.info("motor_daemon: shutting down")
        reaper_stop.set()
        try:
            server.shutdown()
        except Exception:
            pass
        hw.shutdown()
        try:
            os.unlink(path)
        except OSError:
            pass

    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, lambda *_: (_cleanup(), sys.exit(0)))
    signal.signal(signal.SIGINT, lambda *_: (_cleanup(), sys.exit(0)))

    logger.info("motor_daemon: listening at %s", path)
    try:
        server.serve_forever()
    finally:
        _cleanup()


def main() -> int:
    ap = argparse.ArgumentParser(description="repryntt motor daemon")
    ap.add_argument("--socket", help="Unix socket path "
                    f"(default: {DEFAULT_SOCKET_PATH} or "
                    f"{USER_FALLBACK_SOCKET_PATH})")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    serve(socket_path=args.socket)
    return 0


if __name__ == "__main__":
    sys.exit(main())
