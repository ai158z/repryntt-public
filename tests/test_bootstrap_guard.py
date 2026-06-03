"""Tests for the BootstrapFileGuard.

Covers every rejection path plus happy paths and concurrency behaviours.
Pure stdlib + pytest; no fixtures from the rest of repryntt are needed.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from repryntt.core.bootstrap_guard import (
    BootstrapFileGuard,
    GuardDecision,
    POLICY_SCHEMA_VERSION,
    atomic_write,
    load_policy,
    safe_resolve,
)


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def policy() -> dict:
    """Minimal policy covering the categories under test."""
    return {
        "$schema_version": POLICY_SCHEMA_VERSION,
        "defaults": {
            "max_bytes": 1000,
            "min_bytes_for_shrinkage_check": 50,
            "shrinkage_floor": 0.4,
            "default_mode": "append",
            "allow_replace": True,
            "archive_keep": 3,
        },
        "categories": {
            "identity_config": {"default_mode": "deny", "allow_replace": False, "allow_append": False},
            "reference":       {"default_mode": "append", "allow_replace": False, "allow_append": True},
            "living_journal":  {"default_mode": "append", "allow_replace": True,  "allow_append": True, "shrinkage_floor": 0.6},
            "working_state":   {"default_mode": "append", "allow_replace": True,  "allow_append": True, "max_bytes": 400, "append_per_day": 2},
            "ephemeral":       {"default_mode": "replace", "allow_replace": True, "allow_append": True, "shrinkage_floor": 0.0, "min_bytes_for_shrinkage_check": 0},
        },
        "files": {
            "IDENTITY.md":    {"category": "identity_config"},
            "PROTOCOL.md":    {"category": "reference"},
            "SPIRIT.md":      {"category": "living_journal"},
            "PULSE.md":       {"category": "working_state"},
            "GENESIS.md":     {"category": "ephemeral"},
        },
    }


@pytest.fixture
def guard(tmp_path: Path, policy: dict) -> BootstrapFileGuard:
    bootstrap_dir = tmp_path / "bootstrap"
    bootstrap_dir.mkdir()
    audit_path = tmp_path / "audit.jsonl"
    return BootstrapFileGuard(bootstrap_dir=bootstrap_dir, policy=policy, audit_path=audit_path)


# ── path safety ─────────────────────────────────────────────────────────────


class TestPathSafety:
    def test_traversal_dotdot(self, tmp_path: Path) -> None:
        assert safe_resolve(tmp_path, "../etc/passwd") is None

    def test_traversal_slash(self, tmp_path: Path) -> None:
        assert safe_resolve(tmp_path, "sub/file.md") is None

    def test_absolute_path(self, tmp_path: Path) -> None:
        assert safe_resolve(tmp_path, "/etc/passwd") is None

    def test_empty(self, tmp_path: Path) -> None:
        assert safe_resolve(tmp_path, "") is None

    def test_normal(self, tmp_path: Path) -> None:
        out = safe_resolve(tmp_path, "SPIRIT.md")
        assert out is not None
        assert out.name == "SPIRIT.md"

    def test_symlink_escape_rejected(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside.md"
        outside.write_text("secret")
        bdir = tmp_path / "bootstrap"
        bdir.mkdir()
        link = bdir / "EVIL.md"
        os.symlink(outside, link)
        assert safe_resolve(bdir, "EVIL.md") is None


# ── atomic write ────────────────────────────────────────────────────────────


def test_atomic_write_replaces_atomically(tmp_path: Path) -> None:
    p = tmp_path / "x.md"
    p.write_text("old")
    atomic_write(p, "new content")
    assert p.read_text() == "new content"
    # No leftover .tmp files
    leftovers = [f for f in tmp_path.iterdir() if f.name.startswith(".x.md.")]
    assert leftovers == []


# ── deny / unknown ──────────────────────────────────────────────────────────


class TestDenyAndUnknown:
    def test_identity_md_denied(self, guard: BootstrapFileGuard) -> None:
        d = guard.write("IDENTITY.md", "anything", actor="agent")
        assert not d.ok
        assert "PROTECTED" in d.reason or "operator-only" in d.reason

    def test_unknown_file_rejected(self, guard: BootstrapFileGuard) -> None:
        d = guard.write("HACKER.md", "x", actor="agent")
        assert not d.ok
        assert "unknown" in d.reason.lower()

    def test_force_bypasses_deny(self, guard: BootstrapFileGuard) -> None:
        d = guard.write("IDENTITY.md", "operator override\n", mode="replace", actor="operator", force=True)
        assert d.ok, d.reason


# ── reference (append-only) ─────────────────────────────────────────────────


class TestReferenceAppendOnly:
    def test_append_allowed(self, guard: BootstrapFileGuard) -> None:
        guard.write("PROTOCOL.md", "line one\n", actor="agent")
        d = guard.write("PROTOCOL.md", "line two\n", mode="append", actor="agent")
        assert d.ok, d.reason
        text = (guard.bootstrap_dir / "PROTOCOL.md").read_text()
        assert "line one" in text and "line two" in text

    def test_replace_blocked(self, guard: BootstrapFileGuard) -> None:
        guard.write("PROTOCOL.md", "x" * 500, mode="append", actor="agent")
        d = guard.write("PROTOCOL.md", "tiny rewrite", mode="replace", actor="agent")
        assert not d.ok
        assert "PROTECTED REFERENCE FILE" in d.reason

    def test_force_replace_works(self, guard: BootstrapFileGuard) -> None:
        guard.write("PROTOCOL.md", "x" * 500, mode="append", actor="agent")
        d = guard.write("PROTOCOL.md", "operator rewrite\n", mode="replace", actor="operator", force=True)
        assert d.ok, d.reason


# ── living journal shrinkage ────────────────────────────────────────────────


class TestLivingJournal:
    def test_shrinkage_blocked_at_60_percent(self, guard: BootstrapFileGuard) -> None:
        existing = "a" * 200
        (guard.bootstrap_dir / "SPIRIT.md").write_text(existing)
        # 119 chars = 59.5% of 200, below 60% floor
        d = guard.write("SPIRIT.md", "b" * 119, mode="replace", actor="agent")
        assert not d.ok
        assert "SHRINKAGE" in d.reason

    def test_shrinkage_allowed_at_61_percent(self, guard: BootstrapFileGuard) -> None:
        existing = "a" * 200
        (guard.bootstrap_dir / "SPIRIT.md").write_text(existing)
        d = guard.write("SPIRIT.md", "b" * 122, mode="replace", actor="agent")
        assert d.ok, d.reason

    def test_default_mode_is_append(self, guard: BootstrapFileGuard) -> None:
        # No mode → append (was 'replace' in v0.1, caused verification-art bug)
        (guard.bootstrap_dir / "SPIRIT.md").write_text("seed\n")
        d = guard.write("SPIRIT.md", "new section\n", actor="agent")
        assert d.ok and d.mode == "append"
        assert "seed" in (guard.bootstrap_dir / "SPIRIT.md").read_text()


# ── working state (PULSE.md) ────────────────────────────────────────────────


class TestWorkingState:
    def test_daily_rate_limit(self, guard: BootstrapFileGuard) -> None:
        # policy: 2 appends/day for PULSE.md
        d1 = guard.write("PULSE.md", "one\n", actor="agent")
        d2 = guard.write("PULSE.md", "two\n", actor="agent")
        d3 = guard.write("PULSE.md", "three\n", actor="agent")
        assert d1.ok and d2.ok
        assert not d3.ok
        assert "RATE LIMIT" in d3.reason

    def test_size_cap(self, guard: BootstrapFileGuard) -> None:
        # max_bytes=400 for working_state
        first = "x" * 350
        d = guard.write("PULSE.md", first, actor="agent")
        assert d.ok
        d2 = guard.write("PULSE.md", "y" * 100, actor="agent")
        assert not d2.ok
        assert "SIZE LIMIT" in d2.reason


# ── duplicate detection ─────────────────────────────────────────────────────


def test_append_duplicate_blocked(guard: BootstrapFileGuard) -> None:
    payload = "a paragraph that's clearly more than one hundred characters long " * 3
    d1 = guard.write("PROTOCOL.md", payload, actor="agent")
    assert d1.ok
    d2 = guard.write("PROTOCOL.md", payload, actor="agent")
    assert not d2.ok
    assert "DUPLICATE" in d2.reason


# ── archives + restore ──────────────────────────────────────────────────────


class TestArchives:
    def test_replace_creates_archive(self, guard: BootstrapFileGuard) -> None:
        (guard.bootstrap_dir / "SPIRIT.md").write_text("original" * 50)
        d = guard.write("SPIRIT.md", "replacement" * 50, mode="replace", actor="agent")
        assert d.ok, d.reason
        assert d.archive_path is not None
        assert Path(d.archive_path).exists()

    def test_archive_pruning(self, guard: BootstrapFileGuard) -> None:
        # archive_keep=3
        for i in range(6):
            (guard.bootstrap_dir / "SPIRIT.md").write_text(f"v{i} " + "x" * 200)
            guard.write("SPIRIT.md", f"replacement {i} " + "y" * 200, mode="replace", actor="agent", force=True)
        archives = guard.list_archives("SPIRIT.md")
        assert len(archives) <= 3

    def test_restore_round_trip(self, guard: BootstrapFileGuard) -> None:
        # Seed
        (guard.bootstrap_dir / "SPIRIT.md").write_text("ORIGINAL CONTENT " * 30)
        # Replace (creates archive of original)
        guard.write("SPIRIT.md", "NEW CONTENT " * 30, mode="replace", actor="agent")
        archives = guard.list_archives("SPIRIT.md")
        assert len(archives) >= 1
        # Restore
        d = guard.restore_archive("SPIRIT.md", archives[0]["archive_name"], actor="operator")
        assert d.ok, d.reason
        text = (guard.bootstrap_dir / "SPIRIT.md").read_text()
        assert "ORIGINAL CONTENT" in text

    def test_restore_rejects_traversal(self, guard: BootstrapFileGuard) -> None:
        d = guard.restore_archive("SPIRIT.md", "../../etc/passwd", actor="agent")
        assert not d.ok

    def test_restore_rejects_wrong_filename(self, guard: BootstrapFileGuard) -> None:
        # archive name doesn't start with target filename
        d = guard.restore_archive("SPIRIT.md", "PULSE.md.20260101_000000", actor="agent")
        assert not d.ok


# ── audit log ───────────────────────────────────────────────────────────────


def test_audit_log_records_every_attempt(guard: BootstrapFileGuard, tmp_path: Path) -> None:
    guard.write("PROTOCOL.md", "x", actor="agent")
    guard.write("IDENTITY.md", "x", actor="agent")  # rejected
    guard.write("SPIRIT.md", "y" * 200, mode="replace", actor="operator", force=True)

    audit_lines = guard.audit.path.read_text().strip().splitlines()
    assert len(audit_lines) == 3
    parsed = [json.loads(l) for l in audit_lines]
    assert parsed[0]["filename"] == "PROTOCOL.md" and parsed[0]["ok"] is True
    assert parsed[1]["filename"] == "IDENTITY.md" and parsed[1]["ok"] is False
    assert parsed[2]["filename"] == "SPIRIT.md"
    for ev in parsed:
        assert "ts" in ev
        assert "actor" in ev


# ── ephemeral (GENESIS.md) ──────────────────────────────────────────────────


def test_genesis_replace_with_short_content_allowed(guard: BootstrapFileGuard) -> None:
    (guard.bootstrap_dir / "GENESIS.md").write_text("LONG INITIAL CONTENT " * 50)
    d = guard.write("GENESIS.md", "Genesis Complete", mode="replace", actor="agent")
    assert d.ok, d.reason


# ── concurrency ─────────────────────────────────────────────────────────────


def test_concurrent_appends_preserve_all_lines(guard: BootstrapFileGuard) -> None:
    """Multiple threads appending to the same file must not lose writes."""
    # Use PROTOCOL.md (reference, append-only) and disable size cap by using a smaller payload
    threads = []
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            d = guard.write("PROTOCOL.md", f"line-{i}\n", mode="append", actor=f"t{i}")
            assert d.ok, d.reason
        except Exception as e:
            errors.append(e)

    for i in range(10):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    text = (guard.bootstrap_dir / "PROTOCOL.md").read_text()
    for i in range(10):
        assert f"line-{i}" in text


# ── policy loader ───────────────────────────────────────────────────────────


def test_load_policy_default_only(tmp_path: Path) -> None:
    default = tmp_path / "default.json"
    default.write_text(json.dumps({
        "$schema_version": POLICY_SCHEMA_VERSION,
        "defaults": {"x": 1},
        "files": {"A.md": {"category": "x"}},
    }))
    pol = load_policy(default_path=default, user_path=tmp_path / "nonexistent.json")
    assert pol["defaults"]["x"] == 1


def test_load_policy_user_override_deep_merges(tmp_path: Path) -> None:
    default = tmp_path / "default.json"
    default.write_text(json.dumps({
        "$schema_version": POLICY_SCHEMA_VERSION,
        "defaults": {"max_bytes": 1000, "shrinkage_floor": 0.4},
        "files": {"A.md": {"category": "ref"}},
    }))
    user = tmp_path / "user.json"
    user.write_text(json.dumps({
        "defaults": {"max_bytes": 5000},
        "files": {"B.md": {"category": "log"}},
    }))
    pol = load_policy(default_path=default, user_path=user)
    assert pol["defaults"]["max_bytes"] == 5000          # overridden
    assert pol["defaults"]["shrinkage_floor"] == 0.4     # preserved
    assert "A.md" in pol["files"] and "B.md" in pol["files"]
