"""repryntt.hardware.motor_client — Talk to the motor daemon.

Every consumer that wants to drive Andrew's motors goes through this:

    from repryntt.hardware.motor_client import session, Priority, Preempted

    try:
        with session(priority=Priority.OPERATOR, holder_label="teleop") as s:
            s.move_forward(0.6, 0.4)
            s.turn_right(0.5, 0.3)
    except Preempted:
        # A higher-priority client (safety / e-stop) took over. Wrap up.
        ...

The context manager:
  - blocks on enter until the daemon grants a lease (priority-based queue)
  - spawns a background heartbeat thread (1 Hz, well under the 3 s TTL)
  - raises Preempted on the next call after the daemon revokes the lease
  - releases on exit, even if an exception propagates

If the daemon socket isn't reachable (e.g. dev workstation, daemon not
yet started), the client falls back to direct GPIO via tank.py — set
REPRYNTT_NO_FALLBACK=1 to disable this and force a hard error instead.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import socket
import threading
import time
from typing import Any, Dict, Iterator, Optional

from repryntt.hardware.motor_lease import HEARTBEAT_TTL_S, Priority


# Set inside `session(...)` so nested callers (e.g. nav_cortex inside the
# explorer's outer session) reuse the active lease rather than queueing.
_current_session: contextvars.ContextVar[Optional["MotorSession"]] = (
    contextvars.ContextVar("repryntt_motor_current_session", default=None)
)


def current_session() -> Optional["MotorSession"]:
    """Return the active motor session for this context, or None.

    Useful for inner callers (nav_cortex._execute_action) to check
    whether they're already inside an outer session and avoid the queue.
    """
    return _current_session.get()


def daemon_status(require_daemon: bool = True) -> Dict[str, Any]:
    """Read motor daemon status without acquiring a motor lease."""
    transport = _open_transport()
    if transport is None:
        if require_daemon or os.environ.get("REPRYNTT_NO_FALLBACK") == "1":
            raise DaemonUnavailable(
                "motor daemon not reachable at any candidate socket"
            )
        return _fallback_tank().get_body_status()
    try:
        resp = transport.request({"op": "status"})
        if resp.get("ok"):
            return resp.get("data", {})
        raise MotorClientError(resp.get("error", "status failed"))
    finally:
        transport.close()

logger = logging.getLogger("repryntt.hardware.motor_client")


# ── Errors ───────────────────────────────────────────────────────────


class MotorClientError(Exception):
    """Base class for motor-client errors."""


class Preempted(MotorClientError):
    """Higher-priority client took the lease. Stop issuing motor commands."""


class NotHolder(MotorClientError):
    """Daemon says we don't own a lease (expired, revoked, never had one)."""


class DaemonUnavailable(MotorClientError):
    """Couldn't connect to the motor daemon."""


# ── Connection ───────────────────────────────────────────────────────


# Heartbeat well under the TTL so a single dropped packet doesn't kill us.
HEARTBEAT_INTERVAL_S = 1.0
SOCKET_CONNECT_TIMEOUT_S = 1.0
SOCKET_REQUEST_TIMEOUT_S = 5.0


def _candidate_socket_paths() -> list[str]:
    env = os.environ.get("REPRYNTT_MOTOR_SOCKET")
    if env:
        return [env]
    return [
        "/run/repryntt/motor.sock",
        os.path.expanduser("~/.repryntt/motor.sock"),
    ]


class _SocketTransport:
    """Newline-delimited JSON over AF_UNIX. One lock-protected connection."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._sock: Optional[socket.socket] = None
        self._buf = b""
        self._lock = threading.Lock()

    def connect(self) -> None:
        with self._lock:
            if self._sock is not None:
                return
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(SOCKET_CONNECT_TIMEOUT_S)
            s.connect(self.path)
            s.settimeout(SOCKET_REQUEST_TIMEOUT_S)
            self._sock = s

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
                self._buf = b""

    def request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        line = (json.dumps(payload) + "\n").encode("utf-8")
        with self._lock:
            if self._sock is None:
                raise DaemonUnavailable("transport closed")
            try:
                self._sock.sendall(line)
                while b"\n" not in self._buf:
                    chunk = self._sock.recv(8192)
                    if not chunk:
                        raise DaemonUnavailable("daemon closed connection")
                    self._buf += chunk
                line, _, self._buf = self._buf.partition(b"\n")
            except (BrokenPipeError, ConnectionResetError, socket.timeout) as e:
                raise DaemonUnavailable(f"socket error: {e}") from e
        try:
            return json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise MotorClientError(f"bad daemon reply: {e}") from e


def _open_transport() -> Optional[_SocketTransport]:
    for path in _candidate_socket_paths():
        if not os.path.exists(path):
            continue
        try:
            t = _SocketTransport(path)
            t.connect()
            return t
        except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
            logger.debug("motor_client: %s unreachable: %s", path, e)
            continue
    return None


# ── Session ──────────────────────────────────────────────────────────


class MotorSession:
    """Active motor lease + helpers. Created by `session(...)` context manager."""

    def __init__(self, transport: _SocketTransport, token: str,
                 expires_at: float, priority: Priority,
                 backend: str = "daemon") -> None:
        self._transport = transport
        self._token = token
        self._expires_at = expires_at
        self._priority = priority
        self._backend = backend
        self._preempted = False
        self._closed = False
        self._hb_stop = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None
        if backend == "daemon":
            self._start_heartbeat()

    @property
    def token(self) -> str:
        return self._token

    @property
    def preempted(self) -> bool:
        return self._preempted

    # -- motor primitives ---------------------------------------------

    def move_forward(self, speed: float = 0.6, duration: float = 0.4) -> Dict[str, Any]:
        return self._cmd("forward", speed, duration)

    def move_backward(self, speed: float = 0.6, duration: float = 0.4) -> Dict[str, Any]:
        return self._cmd("backward", speed, duration)

    def turn_left(self, speed: float = 0.5, duration: float = 0.4) -> Dict[str, Any]:
        return self._cmd("turn_left", speed, duration)

    def turn_right(self, speed: float = 0.5, duration: float = 0.4) -> Dict[str, Any]:
        return self._cmd("turn_right", speed, duration)

    def drive_continuous(self, left_vel: float, right_vel: float) -> Dict[str, Any]:
        if self._backend == "fallback":
            return _fallback_tank().drive_continuous(left_vel, right_vel) or {
                "success": True,
                "command": "continuous",
            }
        return self._cmd_extra("continuous", left_vel=left_vel, right_vel=right_vel)

    def spin(self, degrees: float = 180, speed: float = 0.5) -> Dict[str, Any]:
        return self._cmd_extra("spin", speed=speed, degrees=degrees)

    def move_distance(self, distance_cm: float, speed: float = 0.5) -> Dict[str, Any]:
        return self._cmd_extra("move_distance", speed=speed, distance_cm=distance_cm)

    def turn_degrees(self, degrees: float, speed: float = 0.5) -> Dict[str, Any]:
        return self._cmd_extra("turn_degrees", speed=speed, degrees=degrees)

    def emergency_stop(self) -> Dict[str, Any]:
        return self._cmd_extra("emergency_stop")

    def reset_emergency_stop(self) -> Dict[str, Any]:
        return self._cmd_extra("reset_emergency_stop")

    def stop(self) -> Dict[str, Any]:
        # Stop is privileged — works even after preempt.
        if self._closed:
            return {"success": False, "error": "session closed"}
        if self._backend == "fallback":
            return _fallback_tank().stop()
        resp = self._transport.request({"op": "stop", "token": self._token})
        return self._unwrap(resp, allow_preempted=True)

    # -- internals ----------------------------------------------------

    def _cmd(self, action: str, speed: float, duration: float) -> Dict[str, Any]:
        return self._cmd_extra(action, speed=speed, duration=duration)

    def _cmd_extra(self, action: str, **payload: Any) -> Dict[str, Any]:
        if self._closed:
            raise MotorClientError("session closed")
        if self._preempted:
            raise Preempted("session was preempted by higher-priority client")
        if self._backend == "fallback":
            return _fallback_cmd(action, **payload)
        resp = self._transport.request({
            "op": "cmd",
            "token": self._token,
            "action": action,
            **payload,
        })
        return self._unwrap(resp)

    def status(self) -> Dict[str, Any]:
        if self._closed:
            raise MotorClientError("session closed")
        if self._backend == "fallback":
            return _fallback_tank().get_body_status()
        resp = self._transport.request({"op": "status"})
        return self._unwrap(resp)

    def _unwrap(self, resp: Dict[str, Any], allow_preempted: bool = False) -> Dict[str, Any]:
        if resp.get("ok"):
            return resp.get("data", {})
        code = resp.get("code", "")
        msg = resp.get("error", "unknown daemon error")
        if code == "PREEMPTED":
            self._preempted = True
            if allow_preempted:
                return {}
            raise Preempted(msg)
        if code in ("NOT_HOLDER", "BAD_TOKEN"):
            raise NotHolder(msg)
        raise MotorClientError(f"{code}: {msg}")

    def _start_heartbeat(self) -> None:
        def _loop():
            while not self._hb_stop.is_set():
                self._hb_stop.wait(HEARTBEAT_INTERVAL_S)
                if self._hb_stop.is_set():
                    return
                try:
                    resp = self._transport.request({
                        "op": "heartbeat", "token": self._token,
                    })
                    if not resp.get("ok"):
                        # Daemon says we lost the lease.
                        self._preempted = True
                        return
                    data = resp.get("data") or {}
                    self._expires_at = float(data.get("expires_at", 0))
                    if data.get("preempted"):
                        self._preempted = True
                except MotorClientError:
                    self._preempted = True
                    return
        self._hb_thread = threading.Thread(
            target=_loop, name="motor-heartbeat", daemon=True,
        )
        self._hb_thread.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._hb_stop.set()
        if self._backend == "daemon":
            try:
                self._transport.request({"op": "release", "token": self._token})
            except MotorClientError:
                pass
            try:
                self._transport.close()
            except Exception:
                pass


# ── Public context manager ───────────────────────────────────────────


@contextlib.contextmanager
def session(
    priority: Priority = Priority.AUTONOMOUS,
    holder_label: str = "anon",
    wait_timeout_s: float = 30.0,
    require_daemon: bool = False,
) -> Iterator[MotorSession]:
    """Acquire a motor lease, run the block, release on exit.

    Args:
        priority: which queue to enter — see motor_lease.Priority.
        holder_label: human-readable identifier for logs/status.
        wait_timeout_s: max seconds to wait in the queue before giving up.
        require_daemon: if True, raise DaemonUnavailable when the daemon
            socket isn't reachable. Default False allows direct-GPIO
            fallback for dev workstations and one-off scripts.

    Raises:
        DaemonUnavailable: socket missing AND require_daemon=True (or
            REPRYNTT_NO_FALLBACK=1 in the environment).
        TimeoutError: queue timeout exceeded.
        Preempted: higher-priority lease took over (raised on the next
            motor command after revocation, not at acquire time).
    """
    # If we're already inside an outer session in this context, just
    # re-yield it. The outer with-block owns release; we don't double-close.
    outer = _current_session.get()
    if outer is not None and not outer.preempted:
        yield outer
        return

    transport = _open_transport()
    no_fallback = os.environ.get("REPRYNTT_NO_FALLBACK") == "1"

    if transport is None:
        if require_daemon or no_fallback:
            raise DaemonUnavailable(
                "motor daemon not reachable at any candidate socket "
                "(set REPRYNTT_MOTOR_SOCKET or start "
                "`python -m repryntt.hardware.motor_daemon`)"
            )
        logger.warning(
            "motor_client: daemon unreachable — falling back to direct GPIO "
            "(only safe with a single process touching motors)"
        )
        sess = MotorSession(
            transport=None,  # type: ignore[arg-type]
            token="",
            expires_at=0,
            priority=priority,
            backend="fallback",
        )
        tok = _current_session.set(sess)
        try:
            yield sess
        finally:
            _current_session.reset(tok)
            sess.close()
        return

    try:
        resp = transport.request({
            "op": "acquire",
            "priority": priority.name,
            "holder_label": holder_label,
            "holder_pid": os.getpid(),
            "wait_timeout_s": wait_timeout_s,
        })
    except MotorClientError:
        transport.close()
        raise

    if not resp.get("ok"):
        transport.close()
        code = resp.get("code", "")
        msg = resp.get("error", "acquire failed")
        if code == "TIMEOUT":
            raise TimeoutError(msg)
        raise MotorClientError(f"{code}: {msg}")

    data = resp.get("data") or {}
    sess = MotorSession(
        transport=transport,
        token=data["token"],
        expires_at=float(data.get("expires_at", 0)),
        priority=Priority[data.get("priority", priority.name)],
        backend="daemon",
    )
    tok = _current_session.set(sess)
    try:
        yield sess
    finally:
        _current_session.reset(tok)
        sess.close()


# ── Direct-GPIO fallback (dev only) ──────────────────────────────────


def _fallback_tank():
    from repryntt.hardware.tank import get_tank_controller
    return get_tank_controller()


def _fallback_cmd(action: str, **payload: Any) -> Dict[str, Any]:
    tank = _fallback_tank()
    speed = float(payload.get("speed", 0.5))
    duration = float(payload.get("duration", 0.0))
    fn = {
        "forward": lambda: tank.move_forward(speed, duration),
        "backward": lambda: tank.move_backward(speed, duration),
        "turn_left": lambda: tank.turn_left(speed, duration),
        "turn_right": lambda: tank.turn_right(speed, duration),
        "stop": lambda: tank.stop(),
        "continuous": lambda: tank.drive_continuous(
            float(payload.get("left_vel", 0.0)),
            float(payload.get("right_vel", 0.0)),
        ) or {"success": True, "command": "continuous"},
        "spin": lambda: tank.spin(float(payload.get("degrees", 180.0)), speed),
        "move_distance": lambda: tank.move_distance(
            float(payload.get("distance_cm", 0.0)), speed,
        ),
        "turn_degrees": lambda: tank.turn_degrees(
            float(payload.get("degrees", 0.0)), speed,
        ),
        "emergency_stop": lambda: tank.emergency_stop(),
        "reset_emergency_stop": lambda: tank.reset_emergency_stop(),
    }.get(action)
    if fn is None:
        return {"success": False, "error": f"unknown action {action!r}"}
    return fn()


__all__ = [
    "session",
    "daemon_status",
    "current_session",
    "Priority",
    "MotorSession",
    "MotorClientError",
    "Preempted",
    "NotHolder",
    "DaemonUnavailable",
]
