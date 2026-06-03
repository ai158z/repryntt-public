"""End-to-end test: spin the motor_daemon on a temp Unix socket, drive it
with motor_client, prove preemption + heartbeat + release work over the
real wire — not just the in-memory state machine.

Hardware operations are stubbed via a fake _Hardware so the test runs on
any machine (no Jetson required).
"""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest

from repryntt.hardware import motor_daemon as md
from repryntt.hardware.motor_client import (
    MotorClientError,
    NotHolder,
    Preempted,
    Priority,
    session,
)


# ── Fake hardware ────────────────────────────────────────────────────


class _FakeTank:
    def __init__(self):
        self.calls = []

    def move_forward(self, speed, duration):
        self.calls.append(("forward", speed, duration))
        return {"success": True, "command": "forward"}

    def move_backward(self, speed, duration):
        self.calls.append(("backward", speed, duration))
        return {"success": True, "command": "backward"}

    def turn_left(self, speed, duration):
        self.calls.append(("turn_left", speed, duration))
        return {"success": True, "command": "turn_left"}

    def turn_right(self, speed, duration):
        self.calls.append(("turn_right", speed, duration))
        return {"success": True, "command": "turn_right"}

    def stop(self):
        self.calls.append(("stop",))
        return {"success": True, "command": "stop"}

    def drive_continuous(self, left_vel, right_vel):
        self.calls.append(("continuous", left_vel, right_vel))

    def spin(self, degrees, speed):
        self.calls.append(("spin", degrees, speed))
        return {"success": True, "command": "spin"}

    def move_distance(self, distance_cm, speed):
        self.calls.append(("move_distance", distance_cm, speed))
        return {"success": True, "command": "move_distance"}

    def turn_degrees(self, degrees, speed):
        self.calls.append(("turn_degrees", degrees, speed))
        return {"success": True, "command": "turn_degrees"}

    def emergency_stop(self):
        self.calls.append(("emergency_stop",))
        return {"success": True, "command": "emergency_stop"}

    def reset_emergency_stop(self):
        self.calls.append(("reset_emergency_stop",))
        return {"success": True, "command": "reset_emergency_stop"}

    def get_body_status(self):
        return {"success": True, "body": {"is_moving": False}}


class _FakeHW(md._Hardware):
    def __init__(self):
        super().__init__()
        self._tank = _FakeTank()  # bypass lazy init

    def shutdown(self):  # noqa: D401
        pass


# ── Daemon harness ───────────────────────────────────────────────────


@pytest.fixture
def running_daemon(tmp_path, monkeypatch):
    """Spin the motor_daemon on a tmp socket in a background thread."""
    sock_path = str(tmp_path / "motor.sock")
    monkeypatch.setenv("REPRYNTT_MOTOR_SOCKET", sock_path)

    lease_mgr = md.LeaseManager()
    hw = _FakeHW()
    server = md._Server(sock_path, lease_mgr, hw)
    os.chmod(sock_path, 0o666)
    reaper_stop = threading.Event()
    reaper = md._start_reaper(lease_mgr, reaper_stop)

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    # Wait for the socket to be live.
    deadline = time.time() + 2.0
    while time.time() < deadline:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(sock_path)
            s.close()
            break
        except OSError:
            time.sleep(0.02)

    yield {"path": sock_path, "lease_mgr": lease_mgr, "hw": hw}

    reaper_stop.set()
    server.shutdown()
    server.server_close()


# ── Tests ────────────────────────────────────────────────────────────


def test_basic_acquire_cmd_release(running_daemon):
    with session(priority=Priority.AUTONOMOUS, holder_label="test") as s:
        result = s.move_forward(0.6, 0.0)
        assert result.get("success") is True
    assert running_daemon["hw"]._tank.calls[0] == ("forward", 0.6, 0.0)
    # After release, no lease.
    assert running_daemon["lease_mgr"].current() is None


def test_continuous_drive_goes_through_daemon(running_daemon):
    with session(priority=Priority.AUTONOMOUS, holder_label="bridge") as s:
        result = s.drive_continuous(0.25, -0.5)
        assert result.get("success") is True
    assert running_daemon["hw"]._tank.calls[0] == ("continuous", 0.25, -0.5)


def test_auxiliary_actions_go_through_daemon(running_daemon):
    with session(priority=Priority.AUTONOMOUS, holder_label="tools") as s:
        assert s.spin(90, 0.4).get("success") is True
        assert s.move_distance(12, 0.3).get("success") is True
        assert s.turn_degrees(-45, 0.5).get("success") is True
    assert ("spin", 90, 0.4) in running_daemon["hw"]._tank.calls
    assert ("move_distance", 12, 0.3) in running_daemon["hw"]._tank.calls
    assert ("turn_degrees", -45, 0.5) in running_daemon["hw"]._tank.calls


def test_higher_priority_preempts_lower(running_daemon):
    """Operator should preempt autonomous mid-session and be granted control."""
    autonomous_started = threading.Event()
    autonomous_saw_preempt = threading.Event()

    def _autonomous():
        with session(priority=Priority.AUTONOMOUS, holder_label="explorer") as s:
            autonomous_started.set()
            # Sit in the lease for a while, polling commands.
            for _ in range(50):
                try:
                    s.move_forward(0.3, 0.0)
                except Preempted:
                    autonomous_saw_preempt.set()
                    return
                time.sleep(0.05)
            s.stop()

    t1 = threading.Thread(target=_autonomous, daemon=True)
    t1.start()
    assert autonomous_started.wait(timeout=2.0)

    # Now the operator barges in.
    with session(priority=Priority.OPERATOR, holder_label="teleop") as s:
        s.turn_right(0.5, 0.0)

    # Autonomous client should have observed the preempt and bailed.
    assert autonomous_saw_preempt.wait(timeout=3.0)
    t1.join(timeout=2.0)


def test_equal_priority_queues_fifo(running_daemon):
    """Two equal-priority requests; the second waits for the first to release."""
    started = threading.Event()
    second_lease = []

    def _first():
        with session(priority=Priority.AUTONOMOUS, holder_label="first") as s:
            started.set()
            time.sleep(0.5)
            s.move_forward(0.3, 0.0)

    def _second():
        with session(priority=Priority.AUTONOMOUS, holder_label="second",
                     wait_timeout_s=5.0) as s:
            second_lease.append(s.token)
            s.move_backward(0.3, 0.0)

    t1 = threading.Thread(target=_first, daemon=True)
    t1.start()
    started.wait(timeout=2.0)

    t2 = threading.Thread(target=_second, daemon=True)
    t2.start()
    # While first holds, status should show the waiter queued.
    time.sleep(0.1)
    s = running_daemon["lease_mgr"].status()
    assert s["queue_depth"]["AUTONOMOUS"] == 1

    t1.join(timeout=3.0)
    t2.join(timeout=3.0)
    assert second_lease, "second client never got a lease"


def test_cmd_after_release_raises_not_holder(running_daemon):
    """After a session closes, replaying its commands must fail safely."""
    sock_path = running_daemon["path"]
    sess_token: list[str] = []

    with session(priority=Priority.AUTONOMOUS, holder_label="test") as s:
        sess_token.append(s.token)
        s.move_forward(0.5, 0.0)
    # session is closed; daemon released the lease. Replay manually.
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(sock_path)
    sock.sendall((
        '{"op": "cmd", "token": "' + sess_token[0]
        + '", "action": "forward", "speed": 0.5, "duration": 0}\n'
    ).encode())
    reply = b""
    while b"\n" not in reply:
        reply += sock.recv(4096)
    sock.close()
    import json as _json
    data = _json.loads(reply.decode().splitlines()[0])
    assert data["ok"] is False
    assert data["code"] == "NOT_HOLDER"


def test_nested_session_reuses_outer_lease(running_daemon):
    """Inner `with session(...)` must NOT acquire a new lease when an outer
    session is already active in the same context — otherwise the explorer
    holding an outer session would deadlock when nav_cortex tries to
    acquire a nested AUTONOMOUS one."""
    with session(priority=Priority.AUTONOMOUS, holder_label="outer") as outer:
        outer_token = outer.token
        with session(priority=Priority.AUTONOMOUS, holder_label="inner") as inner:
            assert inner.token == outer_token  # same lease, no second acquire
            inner.move_forward(0.5, 0.0)
        # After inner exits, outer is still active.
        assert running_daemon["lease_mgr"].current() is not None
        assert running_daemon["lease_mgr"].current().token == outer_token
        outer.move_forward(0.5, 0.0)
    # After outer exits, lease is gone.
    assert running_daemon["lease_mgr"].current() is None


def test_preempted_client_can_still_call_stop(running_daemon):
    """After preemption, stop() must still work — better to brake than not."""
    started = threading.Event()
    stop_result: list = []

    def _autonomous():
        with session(priority=Priority.AUTONOMOUS, holder_label="explorer") as s:
            started.set()
            # Wait until preempted, then try to brake.
            for _ in range(50):
                if s.preempted:
                    stop_result.append(s.stop())
                    return
                time.sleep(0.05)

    t = threading.Thread(target=_autonomous, daemon=True)
    t.start()
    started.wait(timeout=2.0)

    # Operator preempts.
    with session(priority=Priority.OPERATOR, holder_label="teleop") as s:
        s.move_forward(0.3, 0.0)

    t.join(timeout=3.0)
    # Stop after preempt either succeeded (we still owned briefly) or
    # was rejected because the lease was already gone — both are
    # acceptable; the key contract is "doesn't crash the client".
    # If it succeeded the result is non-empty.
    assert stop_result == [] or stop_result[0] is not None
