"""
CodeForge Tester — Runs generated tests locally via subprocess.

Supports Python (pytest/unittest), JavaScript/TypeScript (jest/vitest/mocha),
Go (go test), Rust (cargo test), and Java (junit). Tests run in isolated temp
directories with Docker services (databases, caches) spun up automatically.
Frontend projects get Playwright browser testing.
"""

import os
import json
import shutil
import subprocess
import tempfile
import logging
from typing import Tuple, Optional, Dict
from pathlib import Path

from .models import ForgeProject, ForgeModule, ProjectType

logger = logging.getLogger("codeforge.tester")

# Max time a test suite can run before being killed
TEST_TIMEOUT = 120  # seconds


def _write_project_to_disk(project: ForgeProject, work_dir: Path):
    """Write all project modules to a temporary directory for testing."""
    for module in project.modules:
        if not module.implementation:
            continue
        file_path = work_dir / module.filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(module.implementation, encoding="utf-8")

        # Write __init__.py for Python packages
        if module.language == "python":
            for parent in file_path.parents:
                if parent == work_dir:
                    break
                init = parent / "__init__.py"
                if not init.exists():
                    init.write_text("")

        # Write test file
        if module.test_code:
            test_filename = _test_filename(module)
            test_path = work_dir / test_filename
            test_path.parent.mkdir(parents=True, exist_ok=True)
            test_path.write_text(module.test_code, encoding="utf-8")


def _test_filename(module: ForgeModule) -> str:
    """Generate the test file path for a module."""
    p = Path(module.filename)
    if module.language == "python":
        return f"tests/test_{p.stem}.py"
    elif module.language in ("javascript", "typescript", "js", "ts"):
        ext = ".test.js" if module.language in ("javascript", "js") else ".test.ts"
        return f"tests/{p.stem}{ext}"
    elif module.language == "go":
        return f"{p.parent}/{p.stem}_test.go"
    elif module.language == "rust":
        # Rust tests live in the same file, but we write a separate test module
        return f"tests/{p.stem}_test.rs"
    elif module.language == "java":
        return f"tests/{p.stem}Test.java"
    return f"tests/test_{p.stem}{p.suffix}"


def _write_requirements(project: ForgeProject, work_dir: Path):
    """Write dependency files for the project's language ecosystem."""
    deps = project.spec.get("dependencies", [])
    test_fw = project.spec.get("test_framework", "pytest")

    if project.language == "python":
        reqs = list(deps)
        if test_fw == "pytest" and "pytest" not in reqs:
            reqs.append("pytest")
        req_file = work_dir / "requirements.txt"
        req_file.write_text("\n".join(reqs) + "\n")

    elif project.language in ("javascript", "typescript", "js", "ts"):
        pkg = {
            "name": project.name,
            "version": "1.0.0",
            "dependencies": {d: "*" for d in deps},
            "devDependencies": {},
        }
        if test_fw in ("jest", "vitest", "mocha"):
            pkg["devDependencies"][test_fw] = "*"
        if project.language in ("typescript", "ts"):
            pkg["devDependencies"]["typescript"] = "*"
            pkg["devDependencies"]["ts-jest"] = "*"
            pkg["devDependencies"]["@types/jest"] = "*"
        (work_dir / "package.json").write_text(json.dumps(pkg, indent=2))

    elif project.language == "go":
        # Write go.mod
        mod_name = project.name.replace("-", "_")
        go_mod = f"module {mod_name}\n\ngo 1.21\n"
        if deps:
            go_mod += "\nrequire (\n"
            for d in deps:
                go_mod += f"\t{d} latest\n"
            go_mod += ")\n"
        (work_dir / "go.mod").write_text(go_mod)

    elif project.language == "rust":
        # Write Cargo.toml
        cargo = (
            f'[package]\nname = "{project.name}"\nversion = "0.1.0"\n'
            f'edition = "2021"\n\n[dependencies]\n'
        )
        for d in deps:
            pkg_name = d.split(">=")[0].split("==")[0].strip()
            cargo += f'{pkg_name} = "*"\n'
        (work_dir / "Cargo.toml").write_text(cargo)


def _install_deps(project: ForgeProject, work_dir: Path) -> Tuple[bool, str]:
    """Install project dependencies in the work directory."""
    try:
        if project.language == "python":
            req_file = work_dir / "requirements.txt"
            if req_file.exists() and req_file.read_text().strip():
                result = subprocess.run(
                    ["pip", "install", "--quiet", "--target",
                     str(work_dir / ".forge_deps"), "-r", str(req_file)],
                    capture_output=True, text=True, timeout=120,
                    cwd=str(work_dir)
                )
                if result.returncode != 0:
                    return False, f"pip install failed: {result.stderr[:500]}"

        elif project.language in ("javascript", "typescript", "js", "ts"):
            pkg_file = work_dir / "package.json"
            if pkg_file.exists():
                result = subprocess.run(
                    ["npm", "install", "--silent"],
                    capture_output=True, text=True, timeout=120,
                    cwd=str(work_dir)
                )
                if result.returncode != 0:
                    return False, f"npm install failed: {result.stderr[:500]}"

        elif project.language == "go":
            go_mod = work_dir / "go.mod"
            if go_mod.exists():
                result = subprocess.run(
                    ["go", "mod", "tidy"],
                    capture_output=True, text=True, timeout=120,
                    cwd=str(work_dir)
                )
                if result.returncode != 0:
                    return False, f"go mod tidy failed: {result.stderr[:500]}"

        elif project.language == "rust":
            cargo_toml = work_dir / "Cargo.toml"
            if cargo_toml.exists():
                result = subprocess.run(
                    ["cargo", "fetch"],
                    capture_output=True, text=True, timeout=180,
                    cwd=str(work_dir)
                )
                if result.returncode != 0:
                    return False, f"cargo fetch failed: {result.stderr[:500]}"

        return True, ""
    except subprocess.TimeoutExpired:
        return False, "Dependency installation timed out"
    except FileNotFoundError as e:
        return False, f"Package manager not found: {e}"
    except Exception as e:
        return False, f"Dependency install error: {e}"


def run_module_tests(module: ForgeModule, project: ForgeProject,
                     work_dir: Path,
                     extra_env: Optional[Dict[str, str]] = None) -> Tuple[bool, str]:
    """
    Run tests for a single module.
    Returns (passed, output_text).
    """
    if not module.test_code:
        return True, "No tests to run (skipped)"

    # Skip test execution for config/infra files
    skip_languages = ("dockerfile", "yaml", "env", "makefile", "json", "toml",
                      "gitignore", "markdown", "nginx")
    if module.language in skip_languages:
        return True, "Config file — no tests needed"

    test_file = _test_filename(module)
    test_path = work_dir / test_file

    if not test_path.exists():
        return False, f"Test file not found: {test_file}"

    try:
        env = os.environ.copy()
        # Add the work dir and deps to Python path
        deps_dir = work_dir / ".forge_deps"
        python_paths = [str(work_dir)]
        if deps_dir.exists():
            python_paths.append(str(deps_dir))
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = ":".join(python_paths + ([existing] if existing else []))

        # Inject service environment variables (DATABASE_URL, REDIS_URL, etc.)
        if extra_env:
            env.update(extra_env)

        test_fw = project.spec.get("test_framework", "pytest")

        # Route to the correct test runner based on language
        if module.language == "python" or test_fw in ("pytest", "unittest"):
            cmd = ["python3", "-m", "pytest", str(test_path), "-v",
                   "--tb=short", "--no-header", "-q"]

        elif module.language in ("javascript", "js"):
            if test_fw == "vitest":
                cmd = ["npx", "vitest", "run", str(test_path)]
            elif test_fw == "mocha":
                cmd = ["npx", "mocha", str(test_path)]
            else:  # jest (default for JS)
                cmd = ["npx", "jest", str(test_path), "--verbose"]

        elif module.language in ("typescript", "ts"):
            if test_fw == "vitest":
                cmd = ["npx", "vitest", "run", str(test_path)]
            else:
                cmd = ["npx", "jest", str(test_path), "--verbose"]

        elif module.language == "go":
            # Go tests run from the package directory
            test_dir = str(test_path.parent)
            cmd = ["go", "test", "-v", "-run", ".", test_dir]

        elif module.language == "rust":
            cmd = ["cargo", "test", "--", "--nocapture"]

        elif module.language == "java":
            # Basic javac + junit runner
            cmd = ["java", "-cp", f".:{str(work_dir)}", str(test_path)]

        else:
            # Fallback: try pytest for unknown languages
            cmd = ["python3", "-m", "pytest", str(test_path), "-v", "--tb=short"]

        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=TEST_TIMEOUT,
            cwd=str(work_dir),
            env=env,
        )

        output = result.stdout + "\n" + result.stderr
        # Cap output size
        output = output[:5000]
        passed = result.returncode == 0

        return passed, output

    except subprocess.TimeoutExpired:
        return False, f"Tests timed out after {TEST_TIMEOUT}s"
    except FileNotFoundError as e:
        return False, f"Test runner not found: {e} (language: {module.language})"
    except Exception as e:
        return False, f"Test execution error: {e}"


def run_all_tests(project: ForgeProject) -> Tuple[int, int, str]:
    """
    Run all module tests for a project in a temporary directory.
    Automatically spins up Docker services if the project needs databases/caches.
    Runs Playwright browser tests for frontend projects.
    Returns (passed_count, failed_count, combined_output).
    """
    from .environments import ServiceEnvironment, needs_browser_testing, run_browser_tests

    passed = 0
    failed = 0
    outputs = []

    # Create isolated temp directory
    work_dir = Path(tempfile.mkdtemp(prefix="codeforge_"))
    try:
        # Write all project files
        _write_project_to_disk(project, work_dir)
        _write_requirements(project, work_dir)

        # Install dependencies
        ok, err = _install_deps(project, work_dir)
        if not ok:
            outputs.append(f"⚠️ Dependency install: {err}")
            # Continue anyway — some tests might still work

        # Start Docker services if needed (db, cache, etc.)
        with ServiceEnvironment(project, work_dir) as svc_env:
            service_env_vars = svc_env.env_vars

            if svc_env.needs_docker and service_env_vars:
                outputs.append(f"🐳 Services: {', '.join(s.name for s in svc_env.services)}")

            # Run unit/integration tests for each module
            for module in project.modules:
                if not module.test_code:
                    continue

                ok, output = run_module_tests(module, project, work_dir,
                                              extra_env=service_env_vars)
                module.test_output = output

                if ok:
                    passed += 1
                    module.status = "passed"
                    outputs.append(f"✅ {module.filename}: PASSED")
                else:
                    failed += 1
                    module.status = "failed"
                    outputs.append(f"❌ {module.filename}: FAILED\n{output}")

            # Run browser tests for frontend/fullstack projects
            if needs_browser_testing(project):
                outputs.append("\n🌐 Running browser tests...")
                browser_ok, browser_output = run_browser_tests(
                    project, work_dir, service_env_vars
                )
                if browser_ok:
                    outputs.append(f"✅ Browser tests: PASSED\n{browser_output}")
                else:
                    failed += 1
                    outputs.append(f"❌ Browser tests: FAILED\n{browser_output}")

    finally:
        # Clean up temp directory
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass

    return passed, failed, "\n\n".join(outputs)
