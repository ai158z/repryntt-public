"""
repryntt.codeforge.runtimes — Optional runtime probes for non-Python languages.

The agent loop uses real execution feedback per module: write file → ask the
interpreter if it's valid → on failure, feed the error back to the LLM. That
works trivially for Python (we ship CPython). For JS/TS we need Node.

This module wraps the Node calls behind a graceful-detection layer:
  - `detect_node()` runs once at import and caches the result
  - Every Node-using helper short-circuits to `(False, "node not available")`
    when Node isn't on PATH
  - Callers should check `node_available()` before scheduling JS/TS modules

This is what keeps the v1 release usable for Python-only installs.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


# ─── Detection (cached) ────────────────────────────────────────────────

_NODE_LOCK = threading.Lock()
_NODE_DETECTED: Optional[Tuple[bool, str]] = None


def detect_node(force: bool = False) -> Tuple[bool, str]:
    """Return (present, version-or-reason). Cached after first successful call.

    `force=True` re-runs detection (useful if the operator installed Node
    after the daemon started)."""
    global _NODE_DETECTED
    with _NODE_LOCK:
        if _NODE_DETECTED is not None and not force:
            return _NODE_DETECTED
        if not shutil.which("node"):
            _NODE_DETECTED = (False, "node binary not on PATH")
            return _NODE_DETECTED
        try:
            r = subprocess.run(
                ["node", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                ver = (r.stdout or r.stderr).strip()
                _NODE_DETECTED = (True, ver)
            else:
                _NODE_DETECTED = (False, f"node --version exit {r.returncode}")
        except Exception as e:
            _NODE_DETECTED = (False, f"node detection failed: {e}")
        return _NODE_DETECTED


def node_available() -> bool:
    return detect_node()[0]


def npm_available() -> bool:
    return bool(shutil.which("npm")) if node_available() else False


# ─── File extension classifiers ────────────────────────────────────────

JS_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs")
TS_EXTENSIONS = (".ts", ".tsx")
NODE_EXTENSIONS = JS_EXTENSIONS + TS_EXTENSIONS


def is_node_module(filename: str) -> bool:
    """True if this is a JS/TS source file that should go through Node probes."""
    return filename.endswith(NODE_EXTENSIONS)


def is_typescript(filename: str) -> bool:
    return filename.endswith(TS_EXTENSIONS)


# ─── Probes ────────────────────────────────────────────────────────────

def _run(cmd, cwd: Path, timeout: int) -> Tuple[int, str, str]:
    env = os.environ.copy()
    env.setdefault("CI", "true")  # keep tooling non-interactive
    try:
        r = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, env=env,
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        return 124, out, f"[timeout after {timeout}s] {err}"


def node_syntax_check(file_path: Path, cwd: Path) -> Tuple[bool, str]:
    """`node --check <file>` for .js. For .ts we use a transpile pass via
    `npx tsc --noEmit <file>` if available — falls back to (True, "") if
    no TS tooling is installed (we'll catch real errors at import time).
    """
    if not node_available():
        return False, "node not available"
    if is_typescript(str(file_path)):
        # Try tsc if installed; otherwise skip — `node -e require(...)` later
        # via ts-node, or just trust the runtime probe
        if shutil.which("npx"):
            code, _, err = _run(
                ["npx", "--yes", "--no-install", "tsc", "--noEmit", str(file_path)],
                cwd=cwd, timeout=30,
            )
            # If tsc not installed locally and --no-install prevented network:
            # don't fail the module on missing tooling; defer to runtime probe.
            if "could not determine" in err.lower() or code == 127:
                return True, ""
            return code == 0, err.strip()
        return True, ""
    code, _, err = _run(
        ["node", "--check", str(file_path)],
        cwd=cwd, timeout=15,
    )
    return code == 0, err.strip()


def node_require_probe(file_rel_path: str, cwd: Path) -> Tuple[bool, str]:
    """Spawn `node -e "require('./<file>')"` so we catch:
       - missing dependencies (`Error: Cannot find module 'x'`)
       - import-time exceptions
       - bad syntax that escaped --check
    """
    if not node_available():
        return False, "node not available"
    # For TypeScript: skip require-probe at this stage. tsc --noEmit covers
    # type-level correctness; runtime exercise happens via `npm test`.
    if is_typescript(file_rel_path):
        return True, ""
    rel = file_rel_path if file_rel_path.startswith("./") else f"./{file_rel_path}"
    snippet = f"try {{ require({rel!r}); }} catch (e) {{ console.error(e.stack || e.message); process.exit(1); }}"
    code, _, err = _run(
        ["node", "-e", snippet],
        cwd=cwd, timeout=20,
    )
    return code == 0, err.strip()


# ─── npm orchestration ─────────────────────────────────────────────────

_NPM_INSTALL_CACHE: dict = {}
_NPM_INSTALL_LOCK = threading.Lock()


def npm_install(cwd: Path, timeout: int = 180) -> Tuple[bool, str]:
    """Run `npm install` once per work_dir. Result cached.

    No-op if there's no `package.json` (we expect the agent to generate one
    early when JS modules are in the architecture)."""
    if not npm_available():
        return False, "npm not available"
    pkg = cwd / "package.json"
    if not pkg.exists():
        return True, "no package.json — skipping npm install"
    key = str(cwd.resolve())
    with _NPM_INSTALL_LOCK:
        if key in _NPM_INSTALL_CACHE:
            return _NPM_INSTALL_CACHE[key]
    code, _, err = _run(
        ["npm", "install", "--no-audit", "--no-fund", "--loglevel=error"],
        cwd=cwd, timeout=timeout,
    )
    ok = code == 0
    with _NPM_INSTALL_LOCK:
        _NPM_INSTALL_CACHE[key] = (ok, err.strip()[-2000:])
    return _NPM_INSTALL_CACHE[key]


def run_node_tests(test_path: Path, cwd: Path,
                   timeout: int = 120) -> Tuple[bool, str, str]:
    """Run tests for one JS/TS test file. Prefers `npm test` if scripted, else
    tries vitest / jest. Returns (passed, stdout, stderr).
    """
    if not node_available():
        return False, "", "node not available"
    # Prefer vitest then jest, both via npx
    if shutil.which("npx"):
        # vitest first — faster, supports both .ts and .js by default
        for tool, args in (
            ("vitest", ["vitest", "run", str(test_path), "--reporter=verbose"]),
            ("jest", ["jest", str(test_path), "--passWithNoTests"]),
        ):
            code, out, err = _run(
                ["npx", "--no-install", "--yes"] + args,
                cwd=cwd, timeout=timeout,
            )
            # If the tool isn't installed, npx --no-install returns 127 / E404
            err_low = err.lower()
            if code == 127 or "could not determine" in err_low or "404" in err_low[:200]:
                continue
            return code == 0, out, err
    # Fallback: npm test (if package.json has a test script)
    code, out, err = _run(
        ["npm", "test"],
        cwd=cwd, timeout=timeout,
    )
    return code == 0, out, err


# ─── Pretty status line for the daemon log ────────────────────────────

def runtime_status_line() -> str:
    """One-liner the daemon can print at startup so the operator knows what's
    supported."""
    node_ok, node_info = detect_node()
    if node_ok:
        npm_ok = npm_available()
        return f"runtimes: Python ✓ | Node {node_info} {'✓' if npm_ok else '(no npm)'}"
    return f"runtimes: Python ✓ | Node ✗ ({node_info}) — JS/TS modules will be skipped"
