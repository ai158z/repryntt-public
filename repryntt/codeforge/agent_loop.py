"""
repryntt.codeforge.agent_loop — Per-module iterative agent loop with real
execution feedback.

This replaces the original one-shot module generation (LLM call → save) with
a real agent loop that writes to disk, runs CPython on the file, imports it
in a clean process, and feeds any errors back to the LLM until the module
actually works or a retry cap is hit.

The cycle per module:

    generate → write → py_compile → import → (fix on error) → repeat

Where each step uses the real interpreter, not an AST-only syntax check.

Dependency awareness: the LLM prompt for module N includes the *actual
generated source* of modules 1..N-1 (filtered to dependencies declared in
the architecture), not just their interface stubs. This is what stops the
"hallucinated import of dy10000" failure mode where modules generated in
isolation invent names for each other.

Public surface:
    generate_module_iteratively(module, project, provider_info, work_dir,
                                max_iters=5) -> bool

Returns True when the module imports cleanly in the work_dir, False if all
retries were exhausted. Either way, `module.implementation`, `module.status`,
and `module.test_output` are updated so the caller sees what happened.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .generator import (
    _call_llm,
    _postprocess_code,
)
from .models import ForgeModule, ForgeProject, ModuleStatus
from . import runtimes

logger = logging.getLogger(__name__)


# ─── Tunables ──────────────────────────────────────────────────────────

DEFAULT_MAX_ITERS = 8
IMPORT_TIMEOUT_SEC = 30
COMPILE_TIMEOUT_SEC = 15
TEST_COLLECT_TIMEOUT_SEC = 30
TEST_RUN_TIMEOUT_SEC = 90

# Max bytes of dependency source to include per dependency in the prompt
DEP_SOURCE_CHAR_BUDGET = 6000
# Max bytes of error trace to feed back to the LLM
ERROR_FEEDBACK_CHAR_BUDGET = 2000


# ─── Filesystem helpers ────────────────────────────────────────────────

def _module_path_in_workdir(module: ForgeModule, work_dir: Path) -> Path:
    """Resolve where this module's source file lands in the work_dir."""
    return work_dir / module.filename


def _write_module_source(module: ForgeModule, work_dir: Path) -> Path:
    p = _module_path_in_workdir(module, work_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(module.implementation or "", encoding="utf-8")
    return p


def _python_module_import_name(rel_path: str) -> str:
    """Turn `pkg/sub/foo.py` → `pkg.sub.foo`. Returns "" if not a .py file."""
    if not rel_path.endswith(".py"):
        return ""
    stem = rel_path[:-3]
    # Strip leading slash and trailing __init__
    parts = [p for p in stem.split("/") if p]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


# ─── CPython execution probes ──────────────────────────────────────────

def _run_subprocess(cmd: List[str], cwd: Path, timeout: int,
                    env: Optional[Dict[str, str]] = None
                    ) -> Tuple[int, str, str]:
    """Thin wrapper that returns (returncode, stdout, stderr)."""
    full_env = os.environ.copy()
    full_env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    if env:
        full_env.update(env)
    try:
        r = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, env=full_env,
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        return (
            124,
            (e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")),
            f"[timeout after {timeout}s] " + (
                e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
            ),
        )
    except FileNotFoundError as e:
        return 127, "", f"[command not found: {e}]"


def _real_py_compile(file_path: Path, work_dir: Path) -> Tuple[bool, str]:
    """Run real CPython on the file. Catches syntax errors AST-parse misses
    (e.g. unicode quirks, NULL bytes, encoding declarations).
    """
    code, _, err = _run_subprocess(
        [sys.executable, "-m", "py_compile", str(file_path)],
        cwd=work_dir, timeout=COMPILE_TIMEOUT_SEC,
    )
    return code == 0, err.strip()


def _try_import(import_name: str, work_dir: Path,
                env: Optional[Dict[str, str]] = None) -> Tuple[bool, str]:
    """Spawn `python -c "import <import_name>"` from work_dir.

    Catches:
      - ImportError (missing modules / circular imports)
      - ModuleNotFoundError
      - NameError raised at import-time
      - top-level syntax errors AST missed
      - side-effects that crash on import
    """
    if not import_name:
        return True, ""
    script = f"import sys; sys.path.insert(0, '.'); import {import_name}"
    code, _, err = _run_subprocess(
        [sys.executable, "-c", script],
        cwd=work_dir, timeout=IMPORT_TIMEOUT_SEC, env=env,
    )
    return code == 0, err.strip()


def _try_pytest_collect(test_path: Path, work_dir: Path) -> Tuple[bool, str]:
    """`pytest --collect-only` against the test file. Catches test-file
    import / discovery errors without actually running the tests."""
    code, out, err = _run_subprocess(
        [sys.executable, "-m", "pytest", str(test_path), "--collect-only", "-q"],
        cwd=work_dir, timeout=TEST_COLLECT_TIMEOUT_SEC,
    )
    return code == 0, (err.strip() or out.strip())


def _run_pytest(test_path: Path, work_dir: Path) -> Tuple[bool, str, str]:
    """Actually run the tests in this file. Returns (passed, stdout, stderr)."""
    code, out, err = _run_subprocess(
        [sys.executable, "-m", "pytest", str(test_path), "-q", "--tb=short"],
        cwd=work_dir, timeout=TEST_RUN_TIMEOUT_SEC,
    )
    return code == 0, out, err


# ─── Dependency-source context ─────────────────────────────────────────

def _dependency_sources(module: ForgeModule, project: ForgeProject) -> str:
    """Build a string with the actual generated source of this module's
    declared upstream dependencies, capped to a sensible budget.

    The LLM uses this to know exactly what names exist in dependency
    modules — eliminating "hallucinated import" failures."""
    deps_text: List[str] = []
    by_id = {m.module_id: m for m in project.modules}
    seen = set()
    for dep_id in (module.dependencies or []):
        dep = by_id.get(dep_id)
        if not dep or dep.module_id in seen:
            continue
        seen.add(dep.module_id)
        if not dep.implementation:
            continue
        snippet = dep.implementation
        if len(snippet) > DEP_SOURCE_CHAR_BUDGET:
            snippet = snippet[:DEP_SOURCE_CHAR_BUDGET] + "\n# ... [truncated]\n"
        deps_text.append(
            f"# === {dep.filename} ===\n{snippet}"
        )
    if not deps_text:
        return ""
    return (
        "\n\n# ─── Source of declared dependency modules "
        "(already generated; import by name) ───\n"
        + "\n\n".join(deps_text)
    )


# ─── LLM messages ──────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a senior {lang} engineer writing production-grade code that must "
    "compile, import cleanly, and pass tests. You will iterate based on real "
    "execution feedback.\n\n"
    "Rules:\n"
    "1. Output ONLY raw {lang} source code. No markdown fences. No prose.\n"
    "2. Implement every function and class completely. No TODOs, no `...`, no "
    "   `pass` placeholders unless explicitly a Protocol/ABC declaration.\n"
    "3. Import only from the standard library, the project's declared "
    "   requirements, and the project's own modules listed in dependency "
    "   context. Never invent module names.\n"
    "4. Use type hints. Use proper logging (not print). Validate inputs at "
    "   trust boundaries. No eval/exec/pickle of untrusted input.\n"
    "5. If a previous attempt is shown with an error, you MUST address that "
    "   specific error in your next attempt — do not regenerate from scratch "
    "   ignoring the feedback.\n"
)


from repryntt.agents.error_signatures import error_signature as _shared_error_signature


def _error_signature(error_text: str) -> str:
    """Back-compat shim — delegates to repryntt.agents.error_signatures.

    Originally lived here; moved to agents.error_signatures so the heartbeat
    tool-loop can use the same logic. Kept as a private re-export so existing
    callers in this module don't need to change.
    """
    return _shared_error_signature(error_text)


def _build_messages(module: ForgeModule, project: ForgeProject,
                    previous_attempt: Optional[str],
                    error_feedback: Optional[str],
                    iteration: int,
                    force_different_approach: bool = False) -> List[Dict[str, str]]:
    project_type = project.project_type or project.spec.get("project_type", "library") if project.spec else "library"

    sys_msg = _SYSTEM_PROMPT.format(lang=module.language or "python")

    dep_src = _dependency_sources(module, project)

    user = (
        f"# Project\n"
        f"- Name: {project.name}\n"
        f"- Type: {project_type}\n"
        f"- Language: {project.language}\n"
        f"- Framework: {project.framework or 'none'}\n\n"
        f"# Module to write: `{module.filename}`\n"
        f"Description: {module.description or '(none)'}\n"
        f"Interfaces: {module.interfaces or '(none)'}\n"
        f"{dep_src}\n\n"
    )

    if previous_attempt and error_feedback:
        snippet = previous_attempt
        if len(snippet) > 6000:
            snippet = snippet[:6000] + "\n# ... [truncated]"
        err = error_feedback
        if len(err) > ERROR_FEEDBACK_CHAR_BUDGET:
            err = err[-ERROR_FEEDBACK_CHAR_BUDGET:]
        user += (
            f"# Previous attempt (iteration {iteration - 1}) — FAILED\n"
            f"```\n{snippet}\n```\n\n"
            f"# Execution error from the previous attempt\n"
            f"```\n{err}\n```\n\n"
            f"Fix the specific error above. Rewrite the complete file. "
            f"Do NOT just describe the fix — output the full corrected source.\n"
        )
        if force_different_approach:
            user += (
                "\n# ⚠️ STRATEGY ESCALATION\n"
                "Your previous attempts have failed with the same error class "
                "repeatedly. Whatever mental model you've been operating on is "
                "wrong. Discard your previous approach. Re-read the module "
                "description and dependency context above and write a "
                "FUNDAMENTALLY DIFFERENT implementation. Do not iterate on the "
                "previous code — start over with a different structure.\n"
            )
    else:
        user += "Write the complete file. Output raw source only.\n"

    return [
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": user},
    ]


# ─── Per-module agent loop ─────────────────────────────────────────────

def generate_module_iteratively(
    module: ForgeModule,
    project: ForgeProject,
    provider_info: Dict[str, str],
    work_dir: Path,
    max_iters: int = DEFAULT_MAX_ITERS,
    api_call_counter: Optional[Dict[str, int]] = None,
) -> bool:
    """Generate this module with real execution feedback.

    Loop per iteration:
      1. LLM call → code
      2. Postprocess (strip fences, dedupe, AST sanity)
      3. Write to disk at work_dir/<module.filename>
      4. Real py_compile via subprocess
      5. If Python and not config: try `import <module>` from work_dir
      6. On any failure → feed error back, loop

    Returns True on success. Either way `module.implementation`,
    `module.status`, and `module.test_output` are updated.
    """
    if api_call_counter is None:
        api_call_counter = {"n": 0}

    previous_attempt = module.implementation or None
    error_feedback: Optional[str] = None
    last_error = ""
    # Strategy escalation: track signature of consecutive errors. When the
    # same signature appears twice in a row, the next iteration gets the
    # "try a fundamentally different approach" instruction.
    last_signature: str = ""
    repeat_count: int = 0
    force_different_approach: bool = False

    is_python = (module.language or "").lower() == "python" and module.filename.endswith(".py")
    is_node = runtimes.is_node_module(module.filename)
    is_config = not module.filename.endswith((".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"))
    import_name = _python_module_import_name(module.filename) if is_python else ""

    # Node modules in a Python-only install get marked SKIPPED.
    # The architect produced them, but we have no way to verify them — so
    # write the source for the operator and short-circuit.
    if is_node and not runtimes.node_available():
        if not module.implementation:
            # No previous attempt — generate once without execution probes,
            # so the operator gets something to look at.
            pass  # fall through to the generation loop, but probes will skip
        # Try one generation; mark SKIPPED regardless of outcome
        messages = _build_messages(module, project, None, None, 1)
        try:
            raw = _call_llm(messages, provider_info, max_tokens=4000, temperature=0.2)
            api_call_counter["n"] += 1
            if raw:
                cleaned, _, _ = _postprocess_code(raw, module.language or "javascript")
                module.implementation = cleaned
                try:
                    _write_module_source(module, work_dir)
                except Exception:
                    pass
        except Exception:
            pass
        module.status = ModuleStatus.SKIPPED.value
        module.test_output = (
            "Node not available on this host — module written but not verified. "
            "Install Node ≥18 and re-run to enable JS/TS verification."
        )
        logger.info(
            f"  agent-loop[{module.filename}] SKIPPED (Node not present; "
            f"source written for operator inspection)"
        )
        return True  # not a build failure; gracefully degraded

    for it in range(1, max_iters + 1):
        t0 = time.time()
        # Detect repeated-error pattern: if the last iteration's error has the
        # same signature as the one before it, this iteration gets the "try a
        # different approach" escalation.
        if error_feedback:
            sig = _error_signature(error_feedback)
            if sig and sig == last_signature:
                repeat_count += 1
            elif sig:
                last_signature = sig
                repeat_count = 1
        force_different_approach = repeat_count >= 2
        if force_different_approach:
            logger.info(
                f"  agent-loop[{module.filename}] iter {it}: error signature "
                f"{last_signature!r} repeated — escalating to different-approach hint"
            )
        messages = _build_messages(
            module, project, previous_attempt, error_feedback, it,
            force_different_approach=force_different_approach,
        )
        try:
            raw = _call_llm(messages, provider_info, max_tokens=4000, temperature=0.2)
        except Exception as e:
            last_error = f"LLM call raised: {e}"
            logger.warning(f"  agent-loop[{module.filename}] iter {it}: {last_error}")
            time.sleep(2 + it)
            continue
        api_call_counter["n"] += 1
        if not raw:
            last_error = "empty LLM response"
            logger.warning(f"  agent-loop[{module.filename}] iter {it}: empty response")
            time.sleep(2)
            continue

        cleaned, ast_ok, ast_err = _postprocess_code(raw, module.language or "python")
        module.implementation = cleaned
        module.status = ModuleStatus.GENERATING.value

        # Write to disk so execution probes see it
        try:
            file_path = _write_module_source(module, work_dir)
        except Exception as e:
            last_error = f"could not write to {work_dir}: {e}"
            logger.warning(f"  agent-loop[{module.filename}] iter {it}: {last_error}")
            previous_attempt = cleaned
            error_feedback = last_error
            continue

        # Real syntax check via py_compile (catches things ast.parse misses)
        if is_python:
            ok, py_err = _real_py_compile(file_path, work_dir)
            if not ok:
                last_error = f"py_compile failed:\n{py_err}"
                previous_attempt = cleaned
                error_feedback = last_error
                elapsed = time.time() - t0
                logger.info(f"  agent-loop[{module.filename}] iter {it}: py_compile FAIL ({elapsed:.1f}s)")
                continue

        # Import probe — catches NameError, ImportError, circular deps, etc.
        if is_python and import_name and not is_config:
            ok, imp_err = _try_import(import_name, work_dir)
            if not ok:
                last_error = f"import failed:\n{imp_err}"
                previous_attempt = cleaned
                error_feedback = last_error
                elapsed = time.time() - t0
                logger.info(f"  agent-loop[{module.filename}] iter {it}: import FAIL ({elapsed:.1f}s)")
                continue

        # Node syntax check + require probe for JS/TS modules
        if is_node:
            ok, node_err = runtimes.node_syntax_check(file_path, work_dir)
            if not ok:
                last_error = f"node syntax check failed:\n{node_err}"
                previous_attempt = cleaned
                error_feedback = last_error
                elapsed = time.time() - t0
                logger.info(f"  agent-loop[{module.filename}] iter {it}: node --check FAIL ({elapsed:.1f}s)")
                continue
            # Require probe — catches missing deps and import-time exceptions
            ok, req_err = runtimes.node_require_probe(module.filename, work_dir)
            if not ok:
                last_error = f"node require failed:\n{req_err}"
                previous_attempt = cleaned
                error_feedback = last_error
                elapsed = time.time() - t0
                logger.info(f"  agent-loop[{module.filename}] iter {it}: require FAIL ({elapsed:.1f}s)")
                continue

        # Config files: AST sanity + non-empty is the bar
        if is_config and not ast_ok:
            last_error = f"config-file sanity failed: {ast_err}"
            previous_attempt = cleaned
            error_feedback = last_error
            continue

        # ── success ──
        module.status = ModuleStatus.GENERATED.value
        module.test_output = "" if it == 1 else f"converged after {it} iterations"
        elapsed = time.time() - t0
        logger.info(f"  agent-loop[{module.filename}] iter {it}: OK ({elapsed:.1f}s)")
        return True

    # exhausted retries
    module.status = ModuleStatus.FAILED.value
    module.test_output = last_error
    logger.warning(
        f"  agent-loop[{module.filename}] exhausted {max_iters} iterations: {last_error[:120]}"
    )
    return False


# ─── Per-module test agent loop ────────────────────────────────────────

_TEST_SYSTEM_PROMPT = (
    "You are writing pytest tests for a Python module that must actually run "
    "and pass. The module is already on PYTHONPATH. You will iterate based "
    "on real pytest output.\n\n"
    "Rules:\n"
    "1. Output ONLY raw Python pytest source. No markdown fences.\n"
    "2. Import the module under test by name; do not stub it.\n"
    "3. Each test function name starts with `test_`.\n"
    "4. Include happy-path AND error-path tests where the public surface "
    "   raises on bad input.\n"
    "5. Do NOT use mocks for the module under test itself — exercise its real "
    "   behavior. Mocks are acceptable for external services (http, db) only.\n"
    "6. If a previous attempt failed, address the specific pytest error in "
    "   your next attempt — do not regenerate from scratch.\n"
)


def repair_cross_module_breakage(
    project: ForgeProject,
    provider_info: Dict[str, str],
    work_dir: Path,
    max_iters: int = 3,
    api_call_counter: Optional[Dict[str, int]] = None,
) -> int:
    """Cross-module repair pass.

    Called after the primary `_stage_generate` agent-loop run completes. Modules
    are generated in topological order — by the time module 5 is written, the
    LLM may have settled on a slightly different interface than what modules 1-4
    assumed. The standalone import probe in the primary loop checks each module
    in isolation; this pass re-runs `python -c "import <module>"` against every
    Python module from a clean process, and any module that now fails (because
    a later-generated peer broke a contract) gets routed through one focused
    agent-loop iteration with the new error feedback.

    Args:
        project: The forge project, with `.modules` already populated.
        provider_info: Same dict used everywhere else.
        work_dir: Where the agent loop has been writing files.
        max_iters: Per-module repair iterations (separate from generate cap).
        api_call_counter: Optional shared counter for total api_calls tracking.

    Returns:
        Number of modules that were successfully repaired. Modules that fail
        repair retain `status=FAILED` and a populated `test_output`.
    """
    if api_call_counter is None:
        api_call_counter = {"n": 0}

    py_modules = [
        m for m in project.modules
        if (m.language or "").lower() == "python"
        and m.filename.endswith(".py")
        and m.status == ModuleStatus.GENERATED.value
    ]
    if not py_modules:
        return 0

    # Topologically sort so we re-check in dependency order
    by_id = {m.module_id: m for m in project.modules}

    def _order(modules: List[ForgeModule]) -> List[ForgeModule]:
        # Simple topo: walk deps first
        out: List[ForgeModule] = []
        visited = set()
        def visit(mod: ForgeModule):
            if mod.module_id in visited:
                return
            visited.add(mod.module_id)
            for dep_id in (mod.dependencies or []):
                dep = by_id.get(dep_id)
                if dep and dep in modules:
                    visit(dep)
            out.append(mod)
        for m in modules:
            visit(m)
        return out

    repaired = 0
    for module in _order(py_modules):
        import_name = _python_module_import_name(module.filename)
        if not import_name:
            continue
        ok, err = _try_import(import_name, work_dir)
        if ok:
            continue
        # Broken — try one focused repair iteration
        logger.info(
            f"  repair[{module.filename}] now fails after cross-module build "
            f"completion: {err.splitlines()[-1] if err else '(no detail)'}"
        )
        module.test_output = err  # seed the feedback
        # generate_module_iteratively will pick this up as previous_attempt + error
        before_status = module.status
        success = generate_module_iteratively(
            module, project, provider_info,
            work_dir=work_dir,
            max_iters=max_iters,
            api_call_counter=api_call_counter,
        )
        if success:
            repaired += 1
            logger.info(f"  repair[{module.filename}] FIXED")
        else:
            logger.warning(
                f"  repair[{module.filename}] could not be repaired in {max_iters} iters"
            )
    if repaired:
        logger.info(f"  cross-module repair: fixed {repaired} module(s)")
    return repaired


def generate_tests_iteratively(
    module: ForgeModule,
    project: ForgeProject,
    provider_info: Dict[str, str],
    work_dir: Path,
    max_iters: int = DEFAULT_MAX_ITERS,
    api_call_counter: Optional[Dict[str, int]] = None,
) -> bool:
    """Generate tests with real pytest feedback. Same agent loop pattern,
    different probe (`pytest --collect-only` + `pytest`).
    """
    if api_call_counter is None:
        api_call_counter = {"n": 0}
    if (module.language or "").lower() != "python":
        # Test generation is Python-only for now
        return False
    if not module.implementation:
        return False

    test_filename = f"tests/test_{Path(module.filename).stem}.py"
    test_path = work_dir / test_filename
    test_path.parent.mkdir(parents=True, exist_ok=True)
    # Ensure tests/__init__.py exists so pytest discovers them
    (work_dir / "tests" / "__init__.py").touch(exist_ok=True)

    previous_attempt = None
    error_feedback = None

    for it in range(1, max_iters + 1):
        # Source of the module under test (so the LLM sees the real surface)
        src = module.implementation
        if len(src) > 8000:
            src = src[:8000] + "\n# ... [truncated]"

        user = (
            f"# Module under test: `{module.filename}`\n"
            f"Import name: `{_python_module_import_name(module.filename)}`\n\n"
            f"# Source\n```python\n{src}\n```\n\n"
        )
        if previous_attempt and error_feedback:
            err = error_feedback
            if len(err) > ERROR_FEEDBACK_CHAR_BUDGET:
                err = err[-ERROR_FEEDBACK_CHAR_BUDGET:]
            user += (
                f"# Previous test attempt — FAILED\n"
                f"```python\n{previous_attempt[:5000]}\n```\n\n"
                f"# pytest output\n```\n{err}\n```\n\n"
                f"Fix the specific failure. Rewrite the complete test file.\n"
            )
        else:
            user += "Write the complete pytest test file.\n"

        messages = [
            {"role": "system", "content": _TEST_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
        try:
            raw = _call_llm(messages, provider_info, max_tokens=3000, temperature=0.2)
        except Exception as e:
            logger.warning(f"  test-loop[{module.filename}] iter {it}: LLM raised {e}")
            time.sleep(2)
            continue
        api_call_counter["n"] += 1
        if not raw:
            continue

        cleaned, ast_ok, _ = _postprocess_code(raw, "python")
        module.test_code = cleaned
        test_path.write_text(cleaned, encoding="utf-8")

        if not ast_ok:
            previous_attempt = cleaned
            error_feedback = "AST parse failed; rewrite without syntax errors."
            continue

        # Collect-only first (catches import-time issues in the test file)
        ok, err = _try_pytest_collect(test_path, work_dir)
        if not ok:
            previous_attempt = cleaned
            error_feedback = f"pytest --collect-only failed:\n{err}"
            logger.info(f"  test-loop[{module.filename}] iter {it}: collect FAIL")
            continue

        # Real run
        ok, out, err = _run_pytest(test_path, work_dir)
        if ok:
            logger.info(f"  test-loop[{module.filename}] iter {it}: pytest PASS")
            return True
        previous_attempt = cleaned
        error_feedback = f"pytest run failed:\nstdout:\n{out}\nstderr:\n{err}"
        logger.info(f"  test-loop[{module.filename}] iter {it}: pytest FAIL")

    return False
