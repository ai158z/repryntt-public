"""
CodeForge Packager — Bundle completed projects into production-ready deliverables.

Creates a clean directory with source code, tests, README, requirements,
quality report, Dockerfile, docker-compose.yml, CI/CD config, and .env.example.
Optionally registers in the REPRYNTT content-addressed artifact store for P2P
distribution.
"""

import json
import shutil
import time
import hashlib
import logging
from pathlib import Path
from typing import Optional

from .models import ForgeProject, ProjectType

logger = logging.getLogger("codeforge.packager")


def package_project(project: ForgeProject, output_base: Path) -> str:
    """
    Package a completed forge project into a production-ready deliverable directory.
    Includes source, tests, config, Docker, CI, and documentation.
    Returns the path to the packaged project.
    """
    pkg_dir = output_base / project.project_id / "package"

    # Clean previous package if exists
    if pkg_dir.exists():
        shutil.rmtree(pkg_dir)
    pkg_dir.mkdir(parents=True, exist_ok=True)

    # Write source files
    for module in project.modules:
        if not module.implementation:
            continue
        file_path = pkg_dir / module.filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(module.implementation, encoding="utf-8")

        # Write __init__.py for Python packages
        if module.language == "python":
            for parent in file_path.parents:
                if parent == pkg_dir:
                    break
                init = parent / "__init__.py"
                if not init.exists():
                    init.write_text("")

    # Write test files
    tests_dir = pkg_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    for module in project.modules:
        if not module.test_code:
            continue
        stem = Path(module.filename).stem
        if module.language == "python":
            test_file = tests_dir / f"test_{stem}.py"
        elif module.language in ("go",):
            test_file = pkg_dir / module.filename.replace(".go", "_test.go")
        elif module.language in ("rust",):
            test_file = tests_dir / f"{stem}_test.rs"
        elif module.language in ("java",):
            test_file = tests_dir / f"{stem}Test.java"
        else:
            test_file = tests_dir / f"{stem}.test{Path(module.filename).suffix}"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text(module.test_code, encoding="utf-8")

    # Write requirements / package manifest
    deps = project.spec.get("dependencies", [])
    if project.language == "python" and deps:
        (pkg_dir / "requirements.txt").write_text("\n".join(deps) + "\n")
    elif project.language in ("javascript", "typescript"):
        pkg_json = {
            "name": project.name,
            "version": "1.0.0",
            "description": project.description,
            "dependencies": {d.split(">=")[0].split("==")[0]: "*" for d in deps},
        }
        # Merge with any existing package.json from modules
        existing_pkg = pkg_dir / "package.json"
        if existing_pkg.exists():
            try:
                existing = json.loads(existing_pkg.read_text())
                existing.setdefault("dependencies", {}).update(pkg_json["dependencies"])
                pkg_json = existing
            except Exception:
                pass
        (pkg_dir / "package.json").write_text(json.dumps(pkg_json, indent=2))

    # Write .env.example from architecture env_vars
    arch_env = project.architecture.get("env_vars", {})
    if arch_env or project.database or project.services:
        env_lines = ["# Environment variables for " + project.name, ""]
        for k, v in arch_env.items():
            env_lines.append(f"{k}={v}")
        # Add service connection strings
        if project.database:
            if "DATABASE_URL" not in arch_env:
                env_lines.append(f"DATABASE_URL=your_{project.database}_connection_string")
        env_lines.append("")
        existing_env = pkg_dir / ".env.example"
        if not existing_env.exists():
            existing_env.write_text("\n".join(env_lines))

    # Write .gitignore if not already in modules
    gitignore_path = pkg_dir / ".gitignore"
    if not gitignore_path.exists():
        gitignore_content = _generate_gitignore(project.language)
        gitignore_path.write_text(gitignore_content)

    # Write quality report
    if project.quality:
        report = project.quality.to_dict()
        report["project_name"] = project.name
        report["project_id"] = project.project_id
        report["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        (pkg_dir / "QUALITY_REPORT.json").write_text(
            json.dumps(report, indent=2)
        )

    # Write forge metadata
    meta = {
        "project_id": project.project_id,
        "name": project.name,
        "description": project.description,
        "language": project.language,
        "framework": project.framework,
        "project_type": project.project_type,
        "frontend_framework": project.frontend_framework,
        "backend_framework": project.backend_framework,
        "database": project.database,
        "created_at": project.created_at,
        "completed_at": project.completed_at,
        "api_calls": project.api_calls,
        "quality_score": project.quality.overall_score if project.quality else 0,
        "modules": len(project.modules),
        "total_lines": project.quality.total_lines if project.quality else 0,
        "swarm_enabled": project.swarm_enabled,
        "coordinator_node": project.coordinator_node,
        "services": [s.name if hasattr(s, "name") else str(s) for s in project.services],
    }
    (pkg_dir / ".forge_meta.json").write_text(json.dumps(meta, indent=2))

    project.package_path = str(pkg_dir)
    logger.info(f"📦 Packaged {project.name} → {pkg_dir}")
    return str(pkg_dir)


def _generate_gitignore(language: str) -> str:
    """Generate a sensible .gitignore for the project language."""
    common = (
        "# Environment\n.env\n.env.local\n.env.*.local\n\n"
        "# IDE\n.vscode/\n.idea/\n*.swp\n*.swo\n\n"
        "# OS\n.DS_Store\nThumbs.db\n\n"
        "# Forge metadata\n.forge_meta.json\nQUALITY_REPORT.json\n"
    )
    if language == "python":
        return common + (
            "# Python\n__pycache__/\n*.py[cod]\n*$py.class\n*.egg-info/\n"
            "dist/\nbuild/\n.eggs/\nvenv/\n.venv/\n*.egg\n.pytest_cache/\n"
        )
    if language in ("javascript", "typescript", "js", "ts"):
        return common + (
            "# Node\nnode_modules/\ndist/\nbuild/\n.next/\n"
            "coverage/\n*.tsbuildinfo\n"
        )
    if language == "go":
        return common + "# Go\nvendor/\n*.exe\n*.test\n*.out\n"
    if language == "rust":
        return common + "# Rust\ntarget/\nCargo.lock\n"
    return common


def register_artifact(project: ForgeProject, package_path: str) -> Optional[str]:
    """
    Register the packaged project in the REPRYNTT content-addressed store.
    Returns the content hash if successful.
    """
    try:
        from repryntt.comms.p2p import ContentStore, ArtifactMeta

        store_dir = Path.home() / ".repryntt" / "data" / "content_store"
        store = ContentStore(str(store_dir))

        # Create a tar.gz of the package
        import tarfile
        import io

        buf = io.BytesIO()
        pkg_path = Path(package_path)
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(str(pkg_path), arcname=project.name)
        content = buf.getvalue()

        meta = ArtifactMeta(
            content_hash="",  # computed by store
            filename=f"{project.name}.tar.gz",
            project=f"codeforge/{project.project_id}",
            size=len(content),
            created_at=time.time(),
            agent_name="CodeForge",
            content_type="binary",
            tags=["codeforge", project.language, project.framework or "standalone"],
            description=f"CodeForge project: {project.description[:200]}",
        )

        content_hash = store.store(content, meta)
        logger.info(f"📦 Registered artifact: {content_hash[:12]} ({len(content)} bytes)")
        return content_hash

    except ImportError:
        logger.warning("P2P content store not available — skipping artifact registration")
        return None
    except Exception as e:
        logger.error(f"Failed to register artifact: {e}")
        return None


def compute_package_hash(package_path: str) -> str:
    """Compute a SHA-256 hash of the entire package for integrity verification."""
    h = hashlib.sha256()
    pkg = Path(package_path)
    for f in sorted(pkg.rglob("*")):
        if f.is_file():
            h.update(f.read_bytes())
    return h.hexdigest()
