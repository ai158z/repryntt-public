"""
Andrew's Hub — Git publish tools for pushing content to GitHub.

Local clone: ~/.repryntt/workspace/andrewshub/
Remote: git@github.com:ai158z/andrewshub.git
"""

import json
import logging
import subprocess
from pathlib import Path
from datetime import datetime

logger = logging.getLogger("repryntt.tools.git_publish")

REPO_DIR = Path.home() / ".repryntt" / "workspace" / "andrewshub"
REMOTE_URL = "git@github.com:ai158z/andrewshub.git"

# ── System content detection ──
# Fingerprints that indicate content was copied from system infrastructure
# rather than being Andrew's original creation.
_SYSTEM_FINGERPRINTS = [
    "class AgentDaemon",
    "class AutonomousAgentState",
    "persistent_agents.py",
    "repryntt.daemon",
    "repryntt.brain",
    "register_native_tools",
    "JARVIS_STARTER_TOOLS",
    "MCPClientManager",
    "def _scheduler_loop",
    "def _run_jarvis_autonomous",
    "ReprynttBrainSystem",
    "_jarvis_auto_cycle",
    "JARVIS_AUTO_CYCLE_INTERVAL",
    "def _call_api_with_tools",
    "nexus_app.py",
    "brain_impl.py",
    "filesystem_sandbox",
    "def register_brain_delegate_tools",
]

# Repo-relative paths that look like system file mirrors
_BLOCKED_PATH_PATTERNS = [
    "repryntt/",        # system module
    "brain/",           # brain infrastructure
    "config/",          # system configs
    "scripts/",         # operator scripts
    "docker/",          # deployment
    "bootstrap/",       # bootstrap files
    ".repryntt/",       # dotdir
]

# Maximum file size (200KB) — prevents dumping entire source files
_MAX_PUBLISH_SIZE = 200_000


def _check_system_content(filepath: str, content: str) -> str:
    """Check if content appears to be system infrastructure rather than original work.
    Returns error message if blocked, empty string if OK."""
    # Size check
    if len(content) > _MAX_PUBLISH_SIZE:
        return (f"Content too large ({len(content)} bytes, max {_MAX_PUBLISH_SIZE}). "
                "Hub is for original creations, not full source dumps.")

    # Path check — block paths that mirror system structure
    fp_lower = filepath.lower()
    for pattern in _BLOCKED_PATH_PATTERNS:
        if fp_lower.startswith(pattern):
            return (f"Path '{filepath}' looks like a system path. "
                    "Hub is for your original creations only — "
                    "research, articles, CodeForge projects, creative work. "
                    "Not system infrastructure files.")

    # Content fingerprint check — look for system code signatures
    fingerprint_hits = sum(1 for fp in _SYSTEM_FINGERPRINTS if fp in content)
    if fingerprint_hits >= 3:
        return ("Content appears to contain system infrastructure code "
                f"({fingerprint_hits} system signatures detected). "
                "Hub is for your original creations only. "
                "Use CodeForge to build projects, then publish those.")

    return ""


def _ensure_repo() -> Path:
    """Ensure the local repo clone exists and is ready."""
    if not (REPO_DIR / ".git").exists():
        REPO_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", REMOTE_URL, str(REPO_DIR)],
            capture_output=True, text=True, timeout=30
        )
        # If cloned empty, set up main branch
        if not (REPO_DIR / ".git" / "refs" / "heads" / "main").exists():
            subprocess.run(
                ["git", "checkout", "-b", "main"],
                cwd=str(REPO_DIR), capture_output=True, text=True, timeout=10
            )
    return REPO_DIR


def _run_git(*args, cwd=None) -> tuple:
    """Run a git command and return (stdout, stderr, returncode)."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=str(cwd or REPO_DIR),
        capture_output=True, text=True, timeout=30
    )
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def hub_publish(filepath: str = "", content: str = "",
                commit_message: str = "", **kw) -> str:
    """Create or update a file in Andrew's Hub and push to GitHub.

    Only for Andrew's original creations — research, articles, code he built
    via CodeForge, creative writing, etc. System files from repryntt/ are blocked.

    Args:
        filepath: Path relative to repo root (e.g. "research/quantum.md", "code/hello.py")
        content: The file content to write
        commit_message: Git commit message (auto-generated if empty)

    Returns:
        JSON with success status, commit hash, and GitHub URL
    """
    if not filepath or not content:
        return json.dumps({"success": False, "error": "filepath and content are required"})

    # Sanitize: no path traversal
    clean_path = Path(filepath)
    if ".." in clean_path.parts:
        return json.dumps({"success": False, "error": "Invalid path — no '..' allowed"})

    # ── Guard: block system file content ──
    # Andrew's Hub is for original creations only, not system infrastructure.
    _blocked = _check_system_content(str(clean_path), content)
    if _blocked:
        return json.dumps({"success": False, "error": _blocked})

    try:
        repo = _ensure_repo()

        # Pull latest first (in case of remote changes)
        _run_git("pull", "--rebase", "origin", "main")

        # Write file
        target = repo / clean_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

        # Stage
        _run_git("add", str(clean_path))

        # Check if there's anything to commit
        status_out, _, _ = _run_git("status", "--porcelain")
        if not status_out.strip():
            return json.dumps({"success": True, "message": "No changes to commit (file unchanged)"})

        # Commit
        msg = commit_message or f"Add {clean_path.name} — {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        _run_git("commit", "-m", msg)

        # Push
        _, push_err, push_rc = _run_git("push", "-u", "origin", "main")
        if push_rc != 0:
            return json.dumps({"success": False, "error": f"Push failed: {push_err}"})

        # Get commit hash
        sha, _, _ = _run_git("rev-parse", "--short", "HEAD")

        github_url = f"https://github.com/ai158z/andrewshub/blob/main/{clean_path}"
        logger.info(f"Published {clean_path} to andrewshub ({sha})")
        return json.dumps({
            "success": True,
            "file": str(clean_path),
            "commit": sha,
            "message": msg,
            "github_url": github_url,
        })

    except subprocess.TimeoutExpired:
        return json.dumps({"success": False, "error": "Git operation timed out"})
    except Exception as e:
        logger.error(f"hub_publish failed: {e}")
        return json.dumps({"success": False, "error": str(e)})


def hub_list(directory: str = "", **kw) -> str:
    """List files in Andrew's Hub repository.

    Args:
        directory: Subdirectory to list (empty = root)

    Returns:
        JSON with file listing
    """
    try:
        repo = _ensure_repo()
        _run_git("pull", "--rebase", "origin", "main")

        target = repo / directory if directory else repo
        if not target.exists():
            return json.dumps({"success": True, "files": [], "message": f"Directory '{directory}' not found"})

        files = []
        for item in sorted(target.rglob("*")):
            if ".git" in item.parts:
                continue
            rel = item.relative_to(repo)
            files.append({
                "path": str(rel),
                "type": "dir" if item.is_dir() else "file",
                "size": item.stat().st_size if item.is_file() else 0,
            })

        return json.dumps({"success": True, "files": files, "count": len(files)})

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def hub_read(filepath: str = "", **kw) -> str:
    """Read a file from Andrew's Hub.

    Args:
        filepath: Path relative to repo root

    Returns:
        JSON with file content
    """
    if not filepath:
        return json.dumps({"success": False, "error": "filepath is required"})

    clean_path = Path(filepath)
    if ".." in clean_path.parts:
        return json.dumps({"success": False, "error": "Invalid path"})

    try:
        repo = _ensure_repo()
        target = repo / clean_path
        if not target.exists():
            return json.dumps({"success": False, "error": f"File not found: {clean_path}"})

        content = target.read_text(encoding="utf-8")
        return json.dumps({"success": True, "path": str(clean_path), "content": content})

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def hub_delete(filepath: str = "", commit_message: str = "", **kw) -> str:
    """Delete a file from Andrew's Hub and push the change.

    Args:
        filepath: Path relative to repo root
        commit_message: Commit message (auto-generated if empty)

    Returns:
        JSON with success status
    """
    if not filepath:
        return json.dumps({"success": False, "error": "filepath is required"})

    clean_path = Path(filepath)
    if ".." in clean_path.parts:
        return json.dumps({"success": False, "error": "Invalid path"})

    try:
        repo = _ensure_repo()
        target = repo / clean_path
        if not target.exists():
            return json.dumps({"success": False, "error": f"File not found: {clean_path}"})

        _run_git("rm", str(clean_path))
        msg = commit_message or f"Remove {clean_path.name}"
        _run_git("commit", "-m", msg)
        _, push_err, push_rc = _run_git("push", "origin", "main")
        if push_rc != 0:
            return json.dumps({"success": False, "error": f"Push failed: {push_err}"})

        sha, _, _ = _run_git("rev-parse", "--short", "HEAD")
        logger.info(f"Deleted {clean_path} from andrewshub ({sha})")
        return json.dumps({"success": True, "deleted": str(clean_path), "commit": sha})

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})
