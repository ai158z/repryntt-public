"""
repryntt.agents.task_types — Extension/keyword-based task type inference and
deterministic success-criterion verification.

The intake gate added typed-deliverable fields (expected_artifact_type,
expected_location, downstream_consumer, success_criterion) to every task,
but they're advisory: when the producer leaves them blank the self-eval has
nothing to bind to and tasks get marked COMPLETED without anyone running the
code. This module closes that loop:

  1. infer_type(title, description) — if the task obviously names a code
     artifact (.py / .js / .ts / explicit "function/module/script" wording),
     fill in a default {expected_artifact_type, expected_location,
     success_criterion}. Operator-supplied fields always win.

  2. verify_success_criterion(task, artifact_root) — for python_module tasks,
     actually run `python -c "import X"` and `pytest <tests>`, returning a
     structured verdict the task_queue uses to gate completion.

The goal is the operator never has to type these fields for the common case
(write foo.py, write tests/test_bar.py) — the system infers, the system
verifies, the system refuses to mark COMPLETED if pytest/import fail.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ─── Type inference ───────────────────────────────────────────────────


CODE_EXTENSIONS = {
    ".py": "python_module",
    ".js": "javascript_module",
    ".jsx": "javascript_module",
    ".mjs": "javascript_module",
    ".cjs": "javascript_module",
    ".ts": "typescript_module",
    ".tsx": "typescript_module",
}

DOC_EXTENSIONS = {
    ".md": "markdown_doc",
    ".rst": "rst_doc",
    ".txt": "text_doc",
}

DATA_EXTENSIONS = {
    ".json": "json_file",
    ".yaml": "yaml_file",
    ".yml": "yaml_file",
    ".toml": "toml_file",
    ".csv": "csv_file",
}


_FILENAME_RE = re.compile(
    r"(?:^|[\s`'\"(\[/])"            # boundary before
    r"([A-Za-z0-9_][A-Za-z0-9_./\-]*\.[A-Za-z]{1,5})"  # name + ext
    r"(?=$|[\s`'\")\]:,;])"          # boundary after
)


def extract_filename(text: str) -> Optional[str]:
    """Find the first plausible filename in the task title/description.

    Returns the raw match so callers see exactly what they got — the caller
    decides where to put it. We do not normalize paths here.
    """
    if not text:
        return None
    m = _FILENAME_RE.search(text)
    return m.group(1) if m else None


def _classify_extension(filename: str) -> Optional[str]:
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in CODE_EXTENSIONS:
        return CODE_EXTENSIONS[ext]
    if ext in DOC_EXTENSIONS:
        return DOC_EXTENSIONS[ext]
    if ext in DATA_EXTENSIONS:
        return DATA_EXTENSIONS[ext]
    return None


def _python_module_name(filename: str) -> str:
    """Convert a path-like filename into an import name.
       foo/bar/baz.py → foo.bar.baz   |   tests/test_x.py → tests.test_x
    """
    no_ext = filename[:-3] if filename.endswith(".py") else filename
    return no_ext.strip("/").replace("/", ".").replace("\\", ".")


def infer_type(title: str, description: str = "") -> Dict[str, str]:
    """Infer a default typed-deliverable spec from a free-text task.

    Returns a dict shaped like the intake-gate typed fields. Always returns
    something — caller decides which keys to merge into the task (i.e. only
    fill blanks; never overwrite operator-supplied values).
    """
    blob = f"{title}\n{description}".strip()
    out: Dict[str, str] = {
        "expected_artifact_type": "",
        "expected_location": "",
        "success_criterion": "",
    }

    fname = extract_filename(blob)
    if not fname:
        # Heuristic: any "write a python function/module/script" → assume .py
        low = blob.lower()
        if any(kw in low for kw in (
            "python function", "python module", "python script",
            "write a function", "write a module",
        )):
            out["expected_artifact_type"] = "python_module"
            out["success_criterion"] = (
                "Module imports cleanly AND any tests/ files that target it pass under pytest."
            )
        return out

    classified = _classify_extension(fname)
    if not classified:
        return out

    out["expected_artifact_type"] = classified
    out["expected_location"] = fname

    if classified == "python_module":
        mod = _python_module_name(fname)
        # Tests live in tests/test_<basename>.py by default.
        base = fname.rsplit("/", 1)[-1][:-3]  # strip .py
        test_path = f"tests/test_{base}.py" if not base.startswith("test_") else fname
        out["success_criterion"] = (
            f"`python -c \"import {mod}\"` exits 0 AND "
            f"`pytest {test_path}` exits 0 (or pytest exits 5 == no tests yet, allowed only for non-test source files)."
        )
    elif classified in ("javascript_module", "typescript_module"):
        out["success_criterion"] = (
            f"`node --check {fname}` exits 0 AND any matching __tests__/{fname} test file passes."
        )
    elif classified == "markdown_doc":
        out["success_criterion"] = (
            f"File at `{fname}` exists, is >= 200 bytes, and contains at least one H1/H2 heading."
        )
    elif classified == "json_file":
        out["success_criterion"] = (
            f"File at `{fname}` exists and parses as valid JSON."
        )

    return out


# Placeholder values that operators sometimes leave in task fields when they
# fill in a form from a template (`<NAME>.py`, `{path}`, `TBD`). We treat
# these as "operator did not actually fill this in" so the inferrer can
# auto-resolve them from title/description. Without this guard, a placeholder
# slips through intake and Andrew gets blocked at the critic gate later for
# "artifact not found at '<NAME>.py'".
_PLACEHOLDER_RE = re.compile(
    r"<[A-Za-z_][A-Za-z0-9_]*>|\{[A-Za-z_][A-Za-z0-9_]*\}|\bTBD\b|\bTODO\b|\bFIXME\b|\bXXX\b",
    re.IGNORECASE,
)


def _is_placeholder(value: Any) -> bool:
    if not value:
        return False
    return bool(_PLACEHOLDER_RE.search(str(value)))


def merge_with_existing(existing: Dict[str, str],
                        inferred: Dict[str, str]) -> Dict[str, str]:
    """Operator/caller fields always win EXCEPT when they contain an
    unfilled template placeholder (``<NAME>.py``, ``{path}``, ``TBD``…).
    Placeholder values are treated as blank so the inferrer can substitute
    a sensible value derived from the task title/description.
    """
    out: Dict[str, str] = {}
    for k, v in (existing or {}).items():
        if v and not _is_placeholder(v):
            out[k] = v
    for k, v in (inferred or {}).items():
        if not out.get(k) and v:
            out[k] = v
    return out


# ─── Deterministic verification ────────────────────────────────────────


@dataclass
class VerifyResult:
    passed: bool
    detail: str            # short human-readable summary
    stdout: str = ""
    stderr: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "detail": self.detail,
            "stdout": self.stdout[-2000:],
            "stderr": self.stderr[-2000:],
        }


def _run(cmd: List[str], cwd: Path, timeout: int = 60) -> Tuple[int, str, str]:
    env = os.environ.copy()
    env.setdefault("CI", "true")
    try:
        r = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, env=env,
        )
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or "") if isinstance(e.stdout, str) else "", \
               f"[timeout after {timeout}s] " + ((e.stderr or "") if isinstance(e.stderr, str) else "")
    except FileNotFoundError as e:
        return 127, "", f"[command not found: {e}]"


def _resolve_root(expected_location: str, artifact_root: Optional[Path]) -> Path:
    """Find the directory we should run from.

    Strategy: if the operator-provided artifact_root is a real dir, use it.
    Otherwise walk a few candidates (cwd, ~/.repryntt/workspace, /home/.../workspace).
    """
    if artifact_root and Path(artifact_root).is_dir():
        return Path(artifact_root)
    # candidate roots in priority order
    candidates: List[Path] = [
        Path.cwd(),
        Path.home() / ".repryntt" / "workspace",
        Path.home() / ".repryntt",
    ]
    for c in candidates:
        if (c / expected_location).exists():
            return c
    return Path.cwd()


def verify_python_module(expected_location: str,
                         artifact_root: Optional[Path] = None,
                         allow_no_tests: bool = True) -> VerifyResult:
    """Real verification: `python -c "import X"` then pytest if a test file
    exists. allow_no_tests=True means "pytest exits 5 (no tests collected)
    is OK for source files" — keeps single-file utilities passable.
    """
    if not expected_location or not expected_location.endswith(".py"):
        return VerifyResult(False, f"not a python module: {expected_location!r}")

    root = _resolve_root(expected_location, artifact_root)
    target = root / expected_location
    if not target.exists():
        return VerifyResult(False, f"file not found at {target}")

    mod = _python_module_name(expected_location)
    rc, out, err = _run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, '.'); import {mod}"],
        cwd=root, timeout=20,
    )
    if rc != 0:
        return VerifyResult(False, f"import failed: {mod}", out, err)

    # Look for a test file
    base = expected_location.rsplit("/", 1)[-1][:-3]
    is_test_file = base.startswith("test_")
    test_candidates: List[Path]
    if is_test_file:
        test_candidates = [target]
    else:
        test_candidates = [
            root / "tests" / f"test_{base}.py",
            root / f"test_{base}.py",
        ]
    test_path = next((p for p in test_candidates if p.exists()), None)
    if test_path is None:
        if allow_no_tests:
            return VerifyResult(True, f"import ok; no tests at {[str(p) for p in test_candidates]}", out, err)
        return VerifyResult(False, f"no tests found among {[str(p) for p in test_candidates]}", out, err)

    rel = test_path.relative_to(root) if test_path.is_absolute() else test_path
    rc, out, err = _run(
        [sys.executable, "-m", "pytest", str(rel), "-q", "--tb=short"],
        cwd=root, timeout=120,
    )
    if rc == 0:
        return VerifyResult(True, f"import ok; pytest ok ({rel})", out, err)
    if rc == 5:  # pytest "no tests collected"
        if allow_no_tests and not is_test_file:
            return VerifyResult(True, f"import ok; pytest collected no tests ({rel})", out, err)
        return VerifyResult(False, f"pytest collected no tests ({rel})", out, err)
    return VerifyResult(False, f"pytest failed (rc={rc}) on {rel}", out, err)


def verify_success_criterion(task_like: Any,
                             artifact_root: Optional[Path] = None) -> VerifyResult:
    """Generic verifier dispatch. Accepts a Task or a dict-like with
    expected_artifact_type/expected_location.

    Currently implements python_module verification. JS/TS support follows
    the same pattern via repryntt.codeforge.runtimes — left out here to keep
    Python-only installs zero-dep.
    """
    def _g(key: str) -> str:
        if isinstance(task_like, dict):
            return task_like.get(key, "") or ""
        return getattr(task_like, key, "") or ""

    art_type = _g("expected_artifact_type")
    loc = _g("expected_location")

    if not art_type and loc:
        # Try to classify from filename if caller skipped typing
        art_type = _classify_extension(loc) or ""

    if art_type == "python_module":
        return verify_python_module(loc, artifact_root=artifact_root)

    # No verifier — return permissive (don't block COMPLETED on types we
    # don't yet know how to verify). This is a deliberate v1 choice: we
    # tighten as we add verifiers, never gate on something we can't check.
    return VerifyResult(True, f"no verifier for type {art_type!r}; skipped")
