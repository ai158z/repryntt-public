"""Bootstrap file write guard — single enforcement point for *any* path that
modifies a file under ``~/.repryntt/brain/bootstrap/``.

Why it exists
-------------
The agent has multiple write paths (the dedicated ``update_bootstrap_file``
tool, the generic filesystem editor, shell access, plugins...). Without a
single chokepoint, protection rules drift and the agent can clobber its own
identity through a less-guarded path. ``BootstrapFileGuard`` centralises the
policy: every write — regardless of caller — runs the same checks, atomically
writes to disk, takes a flock, archives the prior version, and emits a
structured JSONL audit event.

Public API
~~~~~~~~~~
- :class:`BootstrapFileGuard` — the enforcer
- :class:`GuardDecision` — accept/reject result with reason
- :func:`get_bootstrap_guard` — module-level singleton accessor

Configuration
~~~~~~~~~~~~~
Default policy ships in ``repryntt/config/bootstrap_policy.json``. Users may
override or add files via ``~/.repryntt/brain/bootstrap_policy.json``
(deep-merged on top of defaults). See ``docs/bootstrap-protection.md``.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

POLICY_SCHEMA_VERSION = 1
ARCHIVE_DIRNAME = "replace_archive"
AUDIT_FILENAME = "bootstrap_audit.jsonl"
LOCK_TIMEOUT_SEC = 5.0


# ── Errors / results ────────────────────────────────────────────────────────


@dataclass
class GuardDecision:
    """Result of a guard write attempt."""

    ok: bool
    filename: str
    mode: str
    reason: str = ""
    bytes_before: int = 0
    bytes_after: int = 0
    backup_path: Optional[str] = None
    archive_path: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "filename": self.filename,
            "mode": self.mode,
            "reason": self.reason,
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "backup_path": self.backup_path,
            "archive_path": self.archive_path,
            "metadata": self.metadata,
        }


# ── Policy loader ───────────────────────────────────────────────────────────


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``over`` onto ``base`` (returns a new dict)."""
    out = dict(base)
    for k, v in over.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_policy(
    default_path: Optional[Path] = None,
    user_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Load the policy JSON, applying user override if present.

    Either argument may be ``None`` to skip that layer; ``load_policy()`` with
    no args resolves the package default and the standard user override path.
    """
    if default_path is None:
        # Package default ships at repryntt/config/bootstrap_policy.json.
        # __file__ = repryntt/core/bootstrap_guard/__init__.py
        # parents:  [bootstrap_guard, core, repryntt, <project_root>]
        default_path = Path(__file__).resolve().parents[2] / "config" / "bootstrap_policy.json"
    if user_path is None:
        user_path = Path.home() / ".repryntt" / "brain" / "bootstrap_policy.json"

    with open(default_path, "r", encoding="utf-8") as f:
        policy = json.load(f)

    if user_path.exists():
        try:
            with open(user_path, "r", encoding="utf-8") as f:
                user = json.load(f)
            policy = _deep_merge(policy, user)
            logger.info("BootstrapFileGuard: loaded user policy override from %s", user_path)
        except Exception as e:
            logger.warning("BootstrapFileGuard: failed to load user policy %s: %s", user_path, e)

    schema = policy.get("$schema_version", 1)
    if schema != POLICY_SCHEMA_VERSION:
        logger.warning(
            "BootstrapFileGuard: policy schema_version=%s, expected %s",
            schema, POLICY_SCHEMA_VERSION,
        )
    return policy


# ── Path safety ─────────────────────────────────────────────────────────────


def safe_resolve(bootstrap_dir: Path, filename: str) -> Optional[Path]:
    """Resolve ``filename`` to an absolute path under ``bootstrap_dir``.

    Rejects path traversal, absolute paths, and symlink escapes.
    """
    if not filename:
        return None
    if ".." in filename or "/" in filename or "\\" in filename:
        return None
    if os.path.isabs(filename):
        return None

    candidate = bootstrap_dir / filename
    real_dir = bootstrap_dir.resolve()
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return None

    try:
        resolved.relative_to(real_dir)
    except ValueError:
        return None

    if candidate.is_symlink():
        # Already covered by the relative_to check, but be explicit.
        link_target = Path(os.path.realpath(candidate))
        try:
            link_target.relative_to(real_dir)
        except ValueError:
            return None

    return resolved


# ── Atomic write + flock ────────────────────────────────────────────────────


@contextlib.contextmanager
def _file_lock(lock_path: Path, timeout: float = LOCK_TIMEOUT_SEC):
    """Acquire an exclusive flock on ``lock_path``. Creates the lockfile if
    missing. Times out after ``timeout`` seconds."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"flock timeout on {lock_path}") from e
                time.sleep(0.05)
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def atomic_write(path: Path, content: str) -> None:
    """Atomic write: tmp file → fsync → rename. Crash-safe."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


# ── Audit log ───────────────────────────────────────────────────────────────


class _AuditLog:
    """Append-only JSONL audit log. Thread-safe. One line per attempted write."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def write(self, event: Dict[str, Any]) -> None:
        event = dict(event)
        event.setdefault("ts", datetime.utcnow().isoformat() + "Z")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(event, ensure_ascii=False, default=str)
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception as e:  # never let audit failure break writes
            logger.warning("BootstrapFileGuard: audit write failed: %s", e)


# ── Main guard ──────────────────────────────────────────────────────────────


class BootstrapFileGuard:
    """Single chokepoint for any bootstrap-file modification.

    Usage::

        guard = get_bootstrap_guard()
        decision = guard.write(
            filename="SPIRIT.md",
            content="...",
            mode="append",
            actor="jarvis",
        )
        if not decision.ok:
            return {"success": False, "error": decision.reason}

    All writes go through one instance. The constructor takes overrides for
    testing; production code uses :func:`get_bootstrap_guard`.
    """

    def __init__(
        self,
        bootstrap_dir: Path,
        policy: Dict[str, Any],
        audit_path: Optional[Path] = None,
    ):
        self.bootstrap_dir = Path(bootstrap_dir).resolve()
        self.policy = policy
        self.audit = _AuditLog(audit_path or self.bootstrap_dir.parent / AUDIT_FILENAME)
        self._lock = threading.Lock()
        # Per-day append counters live in-memory only; persisted on disk in
        # ``<bootstrap_dir>/.append_count_<filename>`` files.
        self._counter_dir = self.bootstrap_dir
        self._lock_dir = self.bootstrap_dir / ".locks"

    # ── Policy lookup ────────────────────────────────────────────────────

    def policy_for(self, filename: str) -> Dict[str, Any]:
        """Return the merged policy for ``filename`` (file > category > defaults)."""
        defaults = self.policy.get("defaults", {})
        files = self.policy.get("files", {})
        categories = self.policy.get("categories", {})

        file_entry = files.get(filename, {})
        cat_name = file_entry.get("category")
        cat_entry = categories.get(cat_name, {}) if cat_name else {}

        merged: Dict[str, Any] = {}
        for layer in (defaults, cat_entry, file_entry):
            for k, v in layer.items():
                if k.startswith("_") or k == "category":
                    continue
                merged[k] = v
        merged["category"] = cat_name or "unknown"
        merged["filename"] = filename
        return merged

    def is_known(self, filename: str) -> bool:
        return filename in self.policy.get("files", {})

    def known_files(self) -> List[str]:
        return sorted(self.policy.get("files", {}).keys())

    # ── Counters (PULSE.md style daily rate limit) ───────────────────────

    def _counter_path(self, filename: str) -> Path:
        return self._counter_dir / f".append_count_{filename}"

    def _read_counter(self, filename: str) -> Tuple[str, int]:
        path = self._counter_path(filename)
        if not path.exists():
            return ("", 0)
        try:
            data = path.read_text(encoding="utf-8").strip()
            if ":" in data:
                date_str, count_str = data.split(":", 1)
                return (date_str, int(count_str))
        except Exception:
            pass
        return ("", 0)

    def _bump_counter(self, filename: str) -> int:
        today = datetime.utcnow().date().isoformat()
        date_str, count = self._read_counter(filename)
        if date_str != today:
            count = 0
        count += 1
        try:
            self._counter_path(filename).write_text(f"{today}:{count}", encoding="utf-8")
        except Exception:
            logger.debug("BootstrapFileGuard: counter write failed", exc_info=True)
        return count

    def _peek_counter(self, filename: str) -> int:
        today = datetime.utcnow().date().isoformat()
        date_str, count = self._read_counter(filename)
        return count if date_str == today else 0

    # ── Archives ─────────────────────────────────────────────────────────

    def _archive_dir(self) -> Path:
        return self.bootstrap_dir / ARCHIVE_DIRNAME

    def _make_archive(self, filepath: Path) -> Optional[Path]:
        if not filepath.exists():
            return None
        archive_dir = self._archive_dir()
        archive_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        archive_path = archive_dir / f"{filepath.name}.{ts}"
        try:
            shutil.copy2(filepath, archive_path)
        except Exception as e:
            logger.warning("BootstrapFileGuard: archive failed: %s", e)
            return None

        # Prune
        keep = int(self._merged_default("archive_keep", 10))
        prefix = f"{filepath.name}."
        archives = sorted(p for p in archive_dir.iterdir() if p.name.startswith(prefix))
        for old in archives[:-keep]:
            with contextlib.suppress(OSError):
                old.unlink()
        return archive_path

    def list_archives(self, filename: str) -> List[Dict[str, Any]]:
        archive_dir = self._archive_dir()
        if not archive_dir.exists():
            return []
        prefix = f"{filename}."
        out: List[Dict[str, Any]] = []
        for p in sorted(archive_dir.iterdir()):
            if not p.name.startswith(prefix):
                continue
            try:
                stat = p.stat()
                ts_part = p.name[len(prefix):]
                out.append({
                    "archive_name": p.name,
                    "timestamp": ts_part,
                    "bytes": stat.st_size,
                    "mtime": datetime.utcfromtimestamp(stat.st_mtime).isoformat() + "Z",
                })
            except OSError:
                continue
        return out

    def restore_archive(
        self,
        filename: str,
        archive_name: str,
        actor: str = "operator",
    ) -> GuardDecision:
        """Restore ``filename`` from a specific archive entry.

        ``archive_name`` is the archive filename (e.g. ``SPIRIT.md.20260427_120000``)
        as returned by :meth:`list_archives`.
        """
        target = safe_resolve(self.bootstrap_dir, filename)
        if target is None:
            return self._record(GuardDecision(
                ok=False, filename=filename, mode="restore",
                reason="path rejected",
            ), actor=actor)

        archive_path = self._archive_dir() / archive_name
        # Validate archive_name belongs to this filename and stays inside dir
        if (
            not archive_name.startswith(f"{filename}.")
            or "/" in archive_name
            or ".." in archive_name
        ):
            return self._record(GuardDecision(
                ok=False, filename=filename, mode="restore",
                reason=f"invalid archive name: {archive_name}",
            ), actor=actor)
        if not archive_path.exists():
            return self._record(GuardDecision(
                ok=False, filename=filename, mode="restore",
                reason=f"archive not found: {archive_name}",
            ), actor=actor)

        try:
            content = archive_path.read_text(encoding="utf-8")
        except Exception as e:
            return self._record(GuardDecision(
                ok=False, filename=filename, mode="restore",
                reason=f"archive read failed: {e}",
            ), actor=actor)

        bytes_before = target.stat().st_size if target.exists() else 0
        # Archive current first, then write
        with self._locked(filename):
            self._make_archive(target)
            atomic_write(target, content)

        return self._record(GuardDecision(
            ok=True, filename=filename, mode="restore",
            reason=f"restored from {archive_name}",
            bytes_before=bytes_before, bytes_after=len(content.encode("utf-8")),
            archive_path=str(archive_path),
        ), actor=actor)

    # ── Locking ──────────────────────────────────────────────────────────

    @contextlib.contextmanager
    def _locked(self, filename: str):
        """Hold both an in-process and a cross-process lock for ``filename``."""
        lock_path = self._lock_dir / f"{filename}.lock"
        with self._lock:
            with _file_lock(lock_path):
                yield

    # ── Audit helper ─────────────────────────────────────────────────────

    def _record(self, decision: GuardDecision, actor: str) -> GuardDecision:
        ev = decision.as_dict()
        ev["actor"] = actor
        self.audit.write(ev)
        return decision

    def _merged_default(self, key: str, fallback: Any) -> Any:
        return self.policy.get("defaults", {}).get(key, fallback)

    # ── The write entry point ───────────────────────────────────────────

    def write(
        self,
        filename: str,
        content: str,
        mode: Optional[str] = None,
        actor: str = "agent",
        force: bool = False,
    ) -> GuardDecision:
        """Apply policy and write ``content`` to ``filename``.

        Parameters
        ----------
        filename
            Bare filename (e.g. ``SPIRIT.md``). Path traversal is rejected.
        content
            Text to write or append.
        mode
            ``"append"`` or ``"replace"``. ``None`` falls back to the policy
            default for that file.
        actor
            Logical caller identity (``"jarvis"``, ``"operator"``, ...).
            Recorded in audit log.
        force
            Operator escape hatch — bypasses policy gates EXCEPT path safety
            and atomicity. Always logged with ``force=True``.
        """
        if not filename:
            return self._record(GuardDecision(
                ok=False, filename=filename or "", mode=mode or "",
                reason="filename is required",
            ), actor=actor)
        if content is None:
            return self._record(GuardDecision(
                ok=False, filename=filename, mode=mode or "",
                reason="content is required",
            ), actor=actor)

        target = safe_resolve(self.bootstrap_dir, filename)
        if target is None:
            return self._record(GuardDecision(
                ok=False, filename=filename, mode=mode or "",
                reason=f"path rejected for {filename}",
            ), actor=actor)

        pol = self.policy_for(filename)
        if not self.is_known(filename) and not force:
            return self._record(GuardDecision(
                ok=False, filename=filename, mode=mode or "",
                reason=(
                    f"unknown bootstrap file: {filename}. "
                    f"Known: {', '.join(self.known_files())}"
                ),
            ), actor=actor)

        mode = (mode or pol.get("default_mode") or "append").strip().lower()
        if mode == "deny" and not force:
            return self._record(GuardDecision(
                ok=False, filename=filename, mode=mode,
                reason=(
                    f"PROTECTED: {filename} is operator-only "
                    f"(category={pol.get('category')}). "
                    f"Operator must edit this file directly on disk."
                ),
            ), actor=actor)
        if mode not in ("append", "replace"):
            mode = "append"

        if mode == "append" and not pol.get("allow_append", True) and not force:
            return self._record(GuardDecision(
                ok=False, filename=filename, mode=mode,
                reason=f"append not allowed for {filename}",
            ), actor=actor)
        if mode == "replace" and not pol.get("allow_replace", True) and not force:
            return self._record(GuardDecision(
                ok=False, filename=filename, mode=mode,
                reason=(
                    f"PROTECTED REFERENCE FILE: {filename} is operator-curated "
                    f"and cannot be wholesale-replaced by the agent. "
                    f"Use mode='append' to add a dated section, or ask the "
                    f"operator to edit it directly. "
                    f"This protection exists because previous heartbeats have "
                    f"clobbered reference files. Do not work around this guard."
                ),
            ), actor=actor)

        with self._locked(filename):
            existing = ""
            if target.exists():
                try:
                    existing = target.read_text(encoding="utf-8")
                except Exception as e:
                    return self._record(GuardDecision(
                        ok=False, filename=filename, mode=mode,
                        reason=f"read failed: {e}",
                    ), actor=actor)

            if mode == "append":
                return self._do_append(target, filename, content, existing, pol, actor, force)
            return self._do_replace(target, filename, content, existing, pol, actor, force)

    # ── Append path ──────────────────────────────────────────────────────

    def _do_append(
        self,
        target: Path,
        filename: str,
        content: str,
        existing: str,
        pol: Dict[str, Any],
        actor: str,
        force: bool,
    ) -> GuardDecision:
        # Daily append rate limit (working_state files like PULSE.md)
        per_day = pol.get("append_per_day")
        if per_day is not None and not force:
            current = self._peek_counter(filename)
            if current >= int(per_day):
                return self._record(GuardDecision(
                    ok=False, filename=filename, mode="append",
                    reason=(
                        f"RATE LIMIT: {filename} has been appended to "
                        f"{current} times today (limit={per_day}). "
                        f"This file is for curated state, not a log."
                    ),
                    bytes_before=len(existing.encode("utf-8")),
                ), actor=actor)

        # Duplicate detection
        if existing and len(content) > 100 and not force:
            stripped = content.strip()
            if stripped and stripped in existing:
                return self._record(GuardDecision(
                    ok=False, filename=filename, mode="append",
                    reason=(
                        f"DUPLICATE: appended content ({len(content)} chars) "
                        f"is already present in {filename}."
                    ),
                    bytes_before=len(existing.encode("utf-8")),
                ), actor=actor)

        # Size cap
        max_bytes = int(pol.get("max_bytes", 20000))
        proposed_bytes = len(existing.encode("utf-8")) + len(content.encode("utf-8"))
        if proposed_bytes > max_bytes and not force:
            return self._record(GuardDecision(
                ok=False, filename=filename, mode="append",
                reason=(
                    f"SIZE LIMIT: {filename} is {len(existing)} chars; adding "
                    f"{len(content)} would exceed {max_bytes} bytes. Trim or "
                    f"use mode='replace' with curated content."
                ),
                bytes_before=len(existing.encode("utf-8")),
                metadata={"max_bytes": max_bytes},
            ), actor=actor)

        # Backup .bak (rolling) — leave timestamped archives for replace only
        backup_path: Optional[Path] = None
        if existing:
            backup_path = target.with_suffix(target.suffix + ".bak")
            try:
                shutil.copy2(target, backup_path)
            except Exception as e:
                logger.warning("BootstrapFileGuard: backup failed: %s", e)
                backup_path = None

        joiner = "" if (not existing or existing.endswith("\n")) else "\n"
        tail = "" if content.endswith("\n") else "\n"
        new_content = existing + joiner + content + tail
        atomic_write(target, new_content)

        if pol.get("append_per_day") is not None:
            self._bump_counter(filename)

        return self._record(GuardDecision(
            ok=True, filename=filename, mode="append",
            reason="appended",
            bytes_before=len(existing.encode("utf-8")),
            bytes_after=len(new_content.encode("utf-8")),
            backup_path=str(backup_path) if backup_path else None,
        ), actor=actor)

    # ── Replace path ─────────────────────────────────────────────────────

    def _do_replace(
        self,
        target: Path,
        filename: str,
        content: str,
        existing: str,
        pol: Dict[str, Any],
        actor: str,
        force: bool,
    ) -> GuardDecision:
        existing_len = len(existing)
        new_len = len(content)
        min_check = int(pol.get("min_bytes_for_shrinkage_check", 200))
        floor = float(pol.get("shrinkage_floor", 0.4))

        if (
            not force
            and existing_len >= min_check
            and floor > 0
            and new_len < int(existing_len * floor)
        ):
            preview = existing[:1500]
            if existing_len > 1500:
                preview += f"\n... ({existing_len - 1500} more chars)"
            return self._record(GuardDecision(
                ok=False, filename=filename, mode="replace",
                reason=(
                    f"SHRINKAGE PROTECTION: new content ({new_len} chars) is "
                    f"shorter than existing ({existing_len} chars; floor={int(floor*100)}%). "
                    f"Use mode='append' to add, or include the full prior content."
                ),
                bytes_before=len(existing.encode("utf-8")),
                metadata={"shrinkage_floor": floor, "preview": preview},
            ), actor=actor)

        backup_path: Optional[Path] = None
        archive_path: Optional[Path] = None
        if existing:
            backup_path = target.with_suffix(target.suffix + ".bak")
            try:
                shutil.copy2(target, backup_path)
            except Exception as e:
                logger.warning("BootstrapFileGuard: backup failed: %s", e)
                backup_path = None
            archive_path = self._make_archive(target)

        atomic_write(target, content)

        return self._record(GuardDecision(
            ok=True, filename=filename, mode="replace",
            reason="replaced",
            bytes_before=len(existing.encode("utf-8")),
            bytes_after=len(content.encode("utf-8")),
            backup_path=str(backup_path) if backup_path else None,
            archive_path=str(archive_path) if archive_path else None,
        ), actor=actor)


# ── Singleton accessor ──────────────────────────────────────────────────────


_GUARD_LOCK = threading.Lock()
_GUARD: Optional[BootstrapFileGuard] = None


def get_bootstrap_guard(
    bootstrap_dir: Optional[Path] = None,
    reload_policy: bool = False,
) -> BootstrapFileGuard:
    """Return the process-wide :class:`BootstrapFileGuard` singleton."""
    global _GUARD
    with _GUARD_LOCK:
        if _GUARD is None or reload_policy or (
            bootstrap_dir is not None
            and Path(bootstrap_dir).resolve() != _GUARD.bootstrap_dir
        ):
            if bootstrap_dir is None:
                bootstrap_dir = Path.home() / ".repryntt" / "brain" / "bootstrap"
            _GUARD = BootstrapFileGuard(
                bootstrap_dir=Path(bootstrap_dir),
                policy=load_policy(),
            )
        return _GUARD


def reset_bootstrap_guard() -> None:
    """Test-only — clears the singleton so the next call rebuilds it."""
    global _GUARD
    with _GUARD_LOCK:
        _GUARD = None
