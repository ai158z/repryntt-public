# Bootstrap File Protection

> A single-chokepoint guard for the agent's persistent identity files.

## Why this exists

The agent has multiple write paths (the dedicated `update_bootstrap_file`
tool, the generic filesystem editor, shell access, plugins, future MCP
servers...). Without a single chokepoint, protection rules drift and the
agent can clobber its own identity through whichever path is least guarded.

This module — `repryntt.core.bootstrap_guard.BootstrapFileGuard` — is the
chokepoint. Every code path that modifies a file under
`~/.repryntt/brain/bootstrap/` is expected to call it. The rules below are
enforced by code, not by prose in a markdown file.

## What it protects

| Category | Files | Replace? | Append? | Notes |
|---|---|---|---|---|
| `identity_config` | `IDENTITY.md` | ❌ | ❌ | Operator-only. Edit on disk. |
| `reference` | `CAPABILITIES.md`, `PROTOCOL.md`, `TOOLKIT.md`, `HEARTBEAT.md`, `OPERATOR.md`, `HOUSEHOLD.md`, `MY_FILES.md`, `VALUES.md`, `FRAMEWORKS.md`, `TRADING.md`, `LAUNCHING.md`, `INTERESTS.md` | ❌ | ✅ | Append-only. Operator may still edit on disk. |
| `living_journal` | `SPIRIT.md`, `PROFILE.md` | ✅ (60% shrinkage floor) | ✅ | Default mode is append; replace must include ≥60% of prior bytes. |
| `working_state` | `PULSE.md` | ✅ | ✅ (3/day) | Daily append rate limit; smaller size cap. |
| `log_curated` | `RECALL.md` | ✅ | ✅ (auto-consolidate) | Wrapper auto-consolidates with the LLM at the size cap. |
| `ephemeral` | `GENESIS.md` | ✅ | ✅ | No shrinkage floor — may be a one-line completion marker. |

All categories enforce **path-traversal protection**, **atomic writes**
(`tempfile` + `fsync` + `os.replace`), **flock-based concurrency control**,
and **structured JSONL audit logging**.

Successful `mode='replace'` writes archive the prior version to
`~/.repryntt/brain/bootstrap/replace_archive/<filename>.<UTC_timestamp>`.
The last `archive_keep` (default 10) snapshots are kept per file.

## Configuration

The default policy ships in `repryntt/config/bootstrap_policy.json`. Operators
override it with `~/.repryntt/brain/bootstrap_policy.json` — the override is
**deep-merged** on top of defaults, so you only specify what you change.

Example override (slow down PULSE.md churn, give SPIRIT.md a tighter floor):

```json
{
  "files": {
    "PULSE.md":  {"category": "working_state"},
    "SPIRIT.md": {"category": "living_journal"}
  },
  "categories": {
    "working_state":  {"append_per_day": 1},
    "living_journal": {"shrinkage_floor": 0.75}
  }
}
```

To register a new bootstrap file, add a `files` entry pointing at one of the
built-in categories (or define your own under `categories`).

## Audit log

Every attempted write — accepted or rejected — produces one JSONL line at
`~/.repryntt/brain/bootstrap_audit.jsonl`. Schema:

```json
{
  "ts": "2026-04-27T18:42:13.110492Z",
  "actor": "jarvis",
  "filename": "SPIRIT.md",
  "mode": "replace",
  "ok": false,
  "reason": "SHRINKAGE PROTECTION: ...",
  "bytes_before": 4821,
  "bytes_after": 0,
  "backup_path": null,
  "archive_path": null,
  "metadata": {"shrinkage_floor": 0.6}
}
```

Useful queries:

```bash
# How often does Andrew try (and fail) to clobber reference files?
jq -c 'select(.ok == false and .reason | startswith("PROTECTED REFERENCE"))' \
  ~/.repryntt/brain/bootstrap_audit.jsonl | wc -l

# Which files churned today?
jq -c 'select(.ok == true) | "\(.filename)\t\(.actor)\t\(.mode)"' \
  ~/.repryntt/brain/bootstrap_audit.jsonl | sort | uniq -c | sort -rn
```

## Operator escape hatch

`force=True` on `BootstrapFileGuard.write(...)` bypasses every policy gate
**except** path safety and atomicity. It is always recorded in the audit log
with `force=True`. Use this only from operator-initiated tools/scripts —
never expose it to the agent.

## Recovery

The agent has two tools:

- `list_bootstrap_archives(filename)` — show the snapshots available
- `restore_bootstrap_archive(filename, archive_name)` — roll back

The current file is itself archived first, so a restore is reversible.

## Adding a new write path

If you write a tool, plugin, or integration that touches a bootstrap file,
**use the guard**. Direct file I/O bypasses policy.

```python
from pathlib import Path
from repryntt.core.bootstrap_guard import get_bootstrap_guard

guard = get_bootstrap_guard()  # uses ~/.repryntt/brain/bootstrap by default
decision = guard.write(
    filename="SPIRIT.md",
    content="...",
    mode="append",
    actor="my_plugin",
)
if not decision.ok:
    raise RuntimeError(decision.reason)
```

For tests, construct a `BootstrapFileGuard` directly with a `tmp_path` and a
synthetic policy — see `tests/test_bootstrap_guard.py`.

## Why each protection exists

- **Path traversal** — agents may craft `../../etc/passwd` if a tool argument
  is taken at face value.
- **Atomic writes** — a crash mid-write must not leave a half-truncated
  identity file with no backup older than the rolling `.bak`.
- **Flock** — two concurrent agents (or the operator + agent) writing the
  same file would otherwise lose data.
- **Replace lockdown for reference files** — empirical observation: when the
  default mode for living journals was `replace`, the agent learned to
  rewrite *all* files via that pattern. Reference files like `CAPABILITIES.md`
  were collapsed from 412 lines to 8 over multiple bad heartbeats.
- **Shrinkage floor for living journals** — these files should grow over
  time. A short rewrite is almost always wrong (a hallucinated summary).
- **Daily rate limit on PULSE.md** — without it, framework-reset loops fill
  the file with duplicate noise within an hour.
- **Duplicate detection** — agents under context pressure re-paste content
  they've already submitted.
- **Timestamped archives + restore tool** — a single rolling `.bak` is
  destroyed by the next bad edit. Snapshots survive multiple bad edits in a
  row.
- **Structured audit log** — operators need to see *attempted* corruption
  (rejections), not just successful writes, to tell whether the agent is
  fighting the guard.

## Schema versioning

`$schema_version` in the policy file is bumped on backwards-incompatible
changes (currently `1`). The guard logs a warning if it loads a policy with
an unexpected schema version but does not refuse to load — that decision is
left to operators.
