"""repryntt.hardware.motor_lease — Lease state machine for motor access.

Pure-Python policy module: who is allowed to drive Andrew right now,
how does control change hands, what happens when a holder dies.

The transport (Unix socket / HTTP) is in motor_daemon.py.
The client-side ergonomics (`with session(...) as s`) are in motor_client.py.
This module is the rulebook those two depend on.

Rules (derived from operator decisions on 2026-05-10):
  - Exactly ONE lease may be active at a time.
  - Each lease has a priority: SAFETY > OPERATOR > AUTONOMOUS > SCRIPT.
  - A request at strictly-higher priority PREEMPTS the active lease:
      * incumbent gets a preempt event raised on its next status check
      * incumbent has GRACE_PERIOD_S to release; after that it's force-revoked
      * preempting client receives the new lease only after the grace window
        (so it never collides motor commands with the outgoing client)
  - A request at equal-or-lower priority QUEUES (FIFO within priority).
  - Heartbeat TTL: 3 s. If no heartbeat arrives, lease auto-expires and the
    next queued request is granted.
  - SAFETY priority is reserved for in-process safety reflexes (sonar/e-stop)
    and bypasses the queue entirely.

This module has no I/O — give it a clock, give it a way to signal events,
it returns decisions.
"""

from __future__ import annotations

import enum
import secrets
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional


class Priority(enum.IntEnum):
    """Higher value = higher priority. Comparison is meaningful."""
    SCRIPT = 10
    AUTONOMOUS = 20
    OPERATOR = 30
    SAFETY = 40


# Default TTLs. The transport layer can override per request.
HEARTBEAT_TTL_S = 3.0
GRACE_PERIOD_S = 1.0    # how long an incumbent has to release after preempt
QUEUE_WAIT_TIMEOUT_S = 30.0  # max time a queued request waits before giving up


# ── Result types ─────────────────────────────────────────────────────


class LeaseState(enum.Enum):
    GRANTED = "granted"            # active, you may issue commands
    QUEUED = "queued"              # waiting your turn
    PREEMPTED = "preempted"        # someone higher-priority took over
    EXPIRED = "expired"            # heartbeat lapsed
    REVOKED = "revoked"            # released by client or force-killed


@dataclass
class Lease:
    """An active or pending lease record."""
    token: str
    priority: Priority
    holder_label: str              # human-friendly: "teleop", "explorer", etc.
    holder_pid: int
    granted_at: float
    expires_at: float              # last heartbeat + TTL
    preempt_event: threading.Event = field(default_factory=threading.Event)
    state: LeaseState = LeaseState.GRANTED

    def is_alive(self, now: float) -> bool:
        return self.state == LeaseState.GRANTED and now < self.expires_at


@dataclass
class _QueueEntry:
    token: str
    priority: Priority
    holder_label: str
    holder_pid: int
    granted_event: threading.Event = field(default_factory=threading.Event)
    enqueued_at: float = field(default_factory=time.time)
    abandoned: bool = False


# ── Lease manager ────────────────────────────────────────────────────


class LeaseManager:
    """Single source of truth for which client owns the motors right now.

    Thread-safe. The transport layer (motor_daemon) calls into this from
    its request handlers. Multiple methods may be called concurrently
    from the daemon's worker pool.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        ttl_s: float = HEARTBEAT_TTL_S,
        grace_s: float = GRACE_PERIOD_S,
    ) -> None:
        self._clock = clock
        self._ttl_s = ttl_s
        self._grace_s = grace_s

        self._lock = threading.RLock()
        self._active: Optional[Lease] = None
        # Separate queue per priority so we can FIFO-within-priority while
        # still letting higher priorities jump ahead.
        self._queues: Dict[Priority, Deque[_QueueEntry]] = {
            p: deque() for p in Priority
        }
        # Pending preempt: the active lease has been told to wrap up,
        # the listed entry will be granted once the incumbent releases
        # (or the grace timer fires).
        self._pending_grant: Optional[_QueueEntry] = None

    # -- public API ----------------------------------------------------

    def acquire(
        self,
        priority: Priority,
        holder_label: str,
        holder_pid: int,
        wait_timeout_s: float = QUEUE_WAIT_TIMEOUT_S,
    ) -> Lease:
        """Block until a lease is granted, then return it.

        Behaviour by priority of incoming request vs. current holder:
          - SAFETY: bypasses queue; immediately becomes pending_grant and
            preempts incumbent. If no incumbent, grants instantly.
          - Strictly higher: marks incumbent for preempt, takes over after
            grace window (or earlier if incumbent releases gracefully).
          - Equal-or-lower: enqueues FIFO within its priority bucket.
        """
        entry = _QueueEntry(
            token=_make_token(),
            priority=priority,
            holder_label=holder_label,
            holder_pid=holder_pid,
        )

        with self._lock:
            # Try the immediate paths first.
            if self._active is None:
                lease = self._grant_locked(entry)
                return lease

            # Active lease present — figure out preempt vs queue.
            if priority > self._active.priority:
                # Higher priority — preempt the incumbent.
                self._active.preempt_event.set()
                self._active.state = LeaseState.PREEMPTED
                self._pending_grant = entry
                # The grace window timer will force-revoke if incumbent
                # doesn't call release in time.
                self._schedule_grace_expiry_locked()
            else:
                # Same or lower — queue FIFO.
                self._queues[priority].append(entry)

        # Wait outside the lock — granted_event is set when our turn comes.
        granted = entry.granted_event.wait(timeout=wait_timeout_s)
        if not granted:
            # Timed out waiting in queue. Mark abandoned so the dispatcher
            # skips us if our turn arrives later.
            with self._lock:
                entry.abandoned = True
                self._cleanup_abandoned_locked()
            raise TimeoutError(
                f"motor_lease: no grant after {wait_timeout_s}s "
                f"(priority={priority.name}, holder={holder_label})"
            )

        # We must have an active lease whose token matches ours now.
        with self._lock:
            if self._active is None or self._active.token != entry.token:
                raise RuntimeError(
                    "motor_lease: granted_event set but active lease mismatch — "
                    "internal state corruption"
                )
            return self._active

    def heartbeat(self, token: str) -> Lease:
        """Refresh the active lease. Raises if your token isn't the holder."""
        with self._lock:
            if self._active is None or self._active.token != token:
                raise PermissionError(
                    "motor_lease: heartbeat for non-active token"
                )
            self._active.expires_at = self._clock() + self._ttl_s
            return self._active

    def release(self, token: str) -> None:
        """Voluntarily end your session. Next queued request is granted."""
        with self._lock:
            if self._active is None or self._active.token != token:
                # Already gone — silently no-op (idempotent release).
                return
            self._active.state = LeaseState.REVOKED
            self._active = None
            self._dispatch_next_locked()

    def cancel(self, token: str) -> None:
        """Force-revoke a specific lease (admin / e-stop)."""
        with self._lock:
            if self._active is not None and self._active.token == token:
                self._active.state = LeaseState.REVOKED
                self._active.preempt_event.set()
                self._active = None
                self._dispatch_next_locked()
                return
            # Maybe it's queued — drop it.
            for q in self._queues.values():
                for e in q:
                    if e.token == token:
                        e.abandoned = True
                        return

    def reap_expired(self) -> Optional[Lease]:
        """Called periodically by the daemon's reaper thread.

        If the active lease's heartbeat lapsed, revoke it and dispatch the
        next request. Returns the revoked lease (for logging) or None.
        """
        with self._lock:
            now = self._clock()
            # Honour any pending preempt that's past its grace window.
            if (
                self._active is not None
                and self._active.state == LeaseState.PREEMPTED
                and now >= self._active.expires_at + self._grace_s
            ):
                revoked = self._active
                revoked.state = LeaseState.REVOKED
                self._active = None
                self._dispatch_next_locked()
                return revoked
            # Normal heartbeat expiry.
            if self._active is not None and now >= self._active.expires_at:
                revoked = self._active
                revoked.state = LeaseState.EXPIRED
                self._active = None
                self._dispatch_next_locked()
                return revoked
        return None

    def current(self) -> Optional[Lease]:
        with self._lock:
            return self._active

    def status(self) -> Dict[str, object]:
        """Snapshot for /status route. Safe to expose."""
        with self._lock:
            return {
                "active": _lease_to_dict(self._active) if self._active else None,
                "pending_grant": _entry_to_dict(self._pending_grant)
                if self._pending_grant else None,
                "queue_depth": {
                    p.name: len([e for e in q if not e.abandoned])
                    for p, q in self._queues.items()
                },
                "ttl_s": self._ttl_s,
                "grace_s": self._grace_s,
            }

    # -- internals -----------------------------------------------------

    def _grant_locked(self, entry: _QueueEntry) -> Lease:
        now = self._clock()
        lease = Lease(
            token=entry.token,
            priority=entry.priority,
            holder_label=entry.holder_label,
            holder_pid=entry.holder_pid,
            granted_at=now,
            expires_at=now + self._ttl_s,
        )
        self._active = lease
        entry.granted_event.set()
        return lease

    def _dispatch_next_locked(self) -> None:
        """Pick the next request to grant. Honours pending_grant first,
        else highest-priority non-abandoned queue head."""
        if self._pending_grant is not None and not self._pending_grant.abandoned:
            entry = self._pending_grant
            self._pending_grant = None
            self._grant_locked(entry)
            return
        self._pending_grant = None
        for prio in sorted(Priority, reverse=True):
            q = self._queues[prio]
            while q:
                entry = q.popleft()
                if not entry.abandoned:
                    self._grant_locked(entry)
                    return

    def _schedule_grace_expiry_locked(self) -> None:
        """Mark the incumbent's expires_at to enforce the grace window.

        We don't spawn a timer; reap_expired() (called by the daemon's
        reaper thread) compares against expires_at + grace_s. This keeps
        the lease manager fully synchronous and easy to test.
        """
        if self._active is None:
            return
        now = self._clock()
        # Keep the longer of (current expiry, now) so we don't shorten
        # an in-flight heartbeat — the +grace_s is added in reap_expired.
        self._active.expires_at = max(self._active.expires_at, now)

    def _cleanup_abandoned_locked(self) -> None:
        for q in self._queues.values():
            while q and q[0].abandoned:
                q.popleft()


# ── Helpers ──────────────────────────────────────────────────────────


def _make_token() -> str:
    """Random URL-safe token, hard to guess — clients only know their own."""
    return secrets.token_urlsafe(16)


def _lease_to_dict(lease: Lease) -> Dict[str, object]:
    return {
        "token_prefix": lease.token[:8] + "...",
        "priority": lease.priority.name,
        "holder_label": lease.holder_label,
        "holder_pid": lease.holder_pid,
        "granted_at": lease.granted_at,
        "expires_at": lease.expires_at,
        "state": lease.state.value,
    }


def _entry_to_dict(entry: _QueueEntry) -> Dict[str, object]:
    return {
        "priority": entry.priority.name,
        "holder_label": entry.holder_label,
        "holder_pid": entry.holder_pid,
        "enqueued_at": entry.enqueued_at,
    }
